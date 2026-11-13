"""Present CANable interfaces via Windows PnP metadata; never open a USB channel.

SetupAPI/CM signatures: https://learn.microsoft.com/windows/win32/api/setupapi/
No PowerShell process, WinUSB initialization, reset or device control transfer.
"""
import ctypes as C
from ctypes import wintypes as W
import os


class Guid(C.Structure):
    _fields_ = [('a', W.DWORD), ('b', W.WORD), ('c', W.WORD), ('d', C.c_ubyte*8)]


class DeviceInfo(C.Structure):
    _fields_ = [('size', W.DWORD), ('guid', Guid), ('node', W.DWORD), ('reserved', C.c_size_t)]


def _bind(dll, name, args, result=W.BOOL):
    method = getattr(dll, name)
    method.argtypes, method.restype = args, result
    return method


class _Api:
    def __init__(self):
        setup = C.WinDLL('setupapi', use_last_error=True)
        cfg = C.WinDLL('cfgmgr32', use_last_error=True)
        handle, number = W.HANDLE, W.DWORD
        self.create = _bind(setup, 'SetupDiGetClassDevsW', [C.POINTER(Guid), W.LPCWSTR, W.HWND, number], handle)
        self.enumerate = _bind(setup, 'SetupDiEnumDeviceInfo', [handle, number, C.POINTER(DeviceInfo)])
        self.instance_id = _bind(setup, 'SetupDiGetDeviceInstanceIdW',
                                 [handle, C.POINTER(DeviceInfo), W.LPWSTR, number, C.POINTER(number)])
        self.destroy = _bind(setup, 'SetupDiDestroyDeviceInfoList', [handle])
        self.parent = _bind(cfg, 'CM_Get_Parent', [C.POINTER(number), number, number], number)
        self.id_size = _bind(cfg, 'CM_Get_Device_ID_Size', [C.POINTER(number), number, number], number)
        self.node_id = _bind(cfg, 'CM_Get_Device_IDW', [number, W.LPWSTR, number, number], number)


def _interface_guid(instance):
    import winreg
    key = 'SYSTEM\\CurrentControlSet\\Enum\\'+instance+'\\Device Parameters'
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as entry:
        values = winreg.QueryValueEx(entry, 'DeviceInterfaceGUIDs')[0]
    if isinstance(values, str):
        values = [values]
    if not values or not values[0]:
        raise OSError('CANable DeviceInterfaceGUIDs missing; check the WinUSB driver')
    return values[0]


def detect_canables():
    """Return the same serial_number/path contract as the former CIM query."""
    if os.name != 'nt':
        raise OSError('CANable WinUSB discovery requires Windows')
    api = _Api()
    devices = api.create(None, 'USB', None, 0x06)  # PRESENT | ALLCLASSES
    if devices == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    found = []
    try:
        index = 0
        while True:
            info = DeviceInfo(size=C.sizeof(DeviceInfo))
            if not api.enumerate(devices, index, C.byref(info)):
                error = C.get_last_error()
                if error == 259:  # ERROR_NO_MORE_ITEMS
                    break
                raise C.WinError(error)
            index += 1
            text, required = C.create_unicode_buffer(256), W.DWORD()
            if not api.instance_id(devices, C.byref(info), text, len(text), C.byref(required)):
                if C.get_last_error() != 122:  # ERROR_INSUFFICIENT_BUFFER
                    raise C.WinError(C.get_last_error())
                text = C.create_unicode_buffer(required.value)
                if not api.instance_id(devices, C.byref(info), text, len(text), None):
                    raise C.WinError(C.get_last_error())
            instance = text.value
            if not instance.upper().startswith('USB\\VID_1D50&PID_606F&MI_00\\'):
                continue
            parent, size = W.DWORD(), W.DWORD()
            status = api.parent(C.byref(parent), info.node, 0)
            if status:
                raise OSError(f'CM_Get_Parent failed for CANable: CONFIGRET=0x{status:X}')
            status = api.id_size(C.byref(size), parent.value, 0)
            if status:
                raise OSError(f'CM_Get_Device_ID_Size failed for CANable: CONFIGRET=0x{status:X}')
            identity = C.create_unicode_buffer(size.value+1)
            status = api.node_id(parent.value, identity, len(identity), 0)
            if status:
                raise OSError(f'CM_Get_Device_IDW failed for CANable: CONFIGRET=0x{status:X}')
            if not identity.value.upper().startswith('USB\\VID_1D50&PID_606F\\'):
                raise OSError('CANable parent identity is not the expected composite USB device')
            serial = identity.value.rsplit('\\', 1)[-1]
            path = '\\\\?\\'+instance.replace('\\', '#')+'#'+_interface_guid(instance)
            found.append(dict(serial_number=serial, path=path))
    finally:
        api.destroy(devices)
    return sorted(found, key=lambda item:(item['serial_number'],item['path']))
