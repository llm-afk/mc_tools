"""Classic-CAN startup using the existing mc_tools WinUSB implementation."""
import struct

from motor_platform.transports.canable import Canable, SUPPORTED_FIRMWARE, bit_timing


def select_canable(serial=None):
    """Use exactly one attached adapter; stop on zero/multiple devices."""
    adapters = J60Canable.detect()
    if not adapters:
        raise OSError("未检测到 CANable，请连接一只适配器后重试。")
    if len(adapters) != 1:
        serials = ", ".join(adapter["serial_number"] for adapter in adapters)
        raise OSError(f"检测到 {len(adapters)} 只 CANable，SN: {serials}；请只保留一只适配器后重试。")
    found = adapters[0]["serial_number"]
    if serial is not None and serial != found:
        raise OSError(f"唯一 CANable 的 SN 为 {found}，与 --serial {serial} 不一致。")
    return found


class J60Canable(Canable):
    """Reuse discovery, USB I/O, frame codec, echo filtering and cleanup."""

    def open(self):
        if self._usb:
            raise RuntimeError("CANable already open")
        self._usb = self._connect()
        try:
            self.info = self._info(self._usb)
            if (self.info["channels"] != 1
                    or self.info["firmware"] not in SUPPORTED_FIRMWARE
                    or int(self.info["features"], 16) & 0x4500 != 0x4500):
                raise OSError(f"unsupported CANable capabilities/firmware: {self.info}")
            nominal = bit_timing(self.info["clock_hz"], 1_000_000, 75.0,
                                 self.info["nominal_limits"])
            self.nominal_timing = nominal
            self._configured = True
            self._set(2, struct.pack("<II", 0, 0))  # CAN stop/reset
            self._set(0, struct.pack("<I", 0xBEEF))  # legacy gs_usb little endian
            self._set(1, struct.pack("<5I", *nominal))
            self._set(2, struct.pack("<II", 1, 0))  # start, FD mode flag cleared
            self._started = True
            return self
        except BaseException:
            self.close()
            raise
