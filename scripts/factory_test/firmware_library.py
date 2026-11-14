"""Validate the numerically newest APP beside the workstation executable."""
import hashlib
from pathlib import Path
import re
import struct
import sys
import zlib


PLATFORMS = {'1':'GD', '2':'ADM'}
MAX_BYTES = {'GD':45*1024, 'ADM':48*1024}
VERSION_SUFFIX = re.compile(r'_([0-9]+)\.bin$', re.I)


class FirmwareError(RuntimeError):
    def __init__(self, code, message, **context):
        super().__init__(message)
        self.diagnostic_code = code
        self.diagnostic_context = context
        self.diagnostic_hint = '核对当前工件的固件目录、平台、版本、最后确认的偏移和重启回读。'


def platform_for(config):
    platform = PLATFORMS.get(config.hardware_version[1])
    if not platform:
        raise FirmwareError('FW_PLATFORM_UNKNOWN','当前工位的芯片平台尚无对应 OTA 协议')
    return platform


def latest_file(directory):
    candidates = []
    if directory.is_dir():
        for path in directory.iterdir():
            match = VERSION_SUFFIX.search(path.name)
            if match and path.is_file():
                candidates.append((int(match[1]),path))
    if not candidates:
        return None
    version = max(v for v,_ in candidates)
    found = [p for v,p in candidates if v==version]
    if len(found)!=1:
        raise FirmwareError('FW_VERSION_AMBIGUOUS',f'{directory.name} 目录内有多个版本 {version} 的固件',
                            directory=str(directory),files=[p.name for p in found])
    return version,found[0]


def read_image(path, version, platform):
    # Freeze one file in memory, so a field replacement cannot mix image chunks.
    with path.open('rb') as stream:
        image = stream.read(MAX_BYTES[platform]+1)
    size = len(image)
    reasonable_size = (size==MAX_BYTES['ADM'] if platform=='ADM' else 8<=size<=MAX_BYTES['GD'])
    if not reasonable_size or size%8 or not 0<=version<=65535 or re.search(r'(?:^|[_-])boot(?:loader)?(?:[_-]|$)',path.stem,re.I):
        raise FirmwareError('FW_IMAGE_LAYOUT',f'{path.name} 不符合 {platform} APP 镜像布局',size=size,platform=platform)
    sp,pc = struct.unpack_from('<II',image)
    if platform=='GD':
        if not (0x20000000<=sp<=0x20008000 and pc&1 and 0x08000000<=(pc&~1)<0x08000000+size):
            raise FirmwareError('FW_IMAGE_VECTOR','GD APP 的栈地址或复位向量与应用分区不符',sp=sp,pc=pc)
    elif 0x20000000<=sp<=0x20008000 and 0x08000000<=pc<0x08020000:
        raise FirmwareError('FW_IMAGE_VECTOR','ADM 目录内的镜像带有 ARM 复位向量',sp=sp,pc=pc)
    trailer = int.from_bytes(image[-4:],'little')
    calculated = zlib.crc32(image[:-4])
    if trailer!=calculated:
        raise FirmwareError('FW_IMAGE_CRC','APP 文件 CRC32 不匹配',expected=trailer,actual=calculated)
    embedded = None
    pack = {}
    if image[-40:-32]==b'BPKR128\0':
        embedded = int.from_bytes(image[-128:-126],'little')
        if embedded!=version:
            raise FirmwareError('FW_IMAGE_VERSION','APP 文件名与包内版本不一致',filename_version=version,embedded=embedded)
        pack = dict(hardware_tag=int.from_bytes(image[-126:-124],'little'),
                    packed_at=image[-124:-104].split(b'\0',1)[0].decode('ascii',errors='replace'),
                    git_hash=image[-104:-40].split(b'\0',1)[0].decode('ascii',errors='replace'))
    return image,dict(file=str(path),bytes=size,crc32=f'{trailer:08X}',sha256=hashlib.sha256(image).hexdigest(),
                      target_version=version,embedded_version=embedded,platform=platform,package=pack)


def package_firmware():
    parent = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2]
    return parent/'firmware'


def firmware_root(_config=None):
    return package_firmware()


def file_version(path):
    match = VERSION_SUFFIX.search(path.name)
    if not match:
        raise FirmwareError('FW_FILENAME', 'APP 文件名须以十进制版本结尾，例如 motor_app_245.bin', file=str(path))
    return int(match[1])


def app_target(_config=None):
    folder = firmware_root()
    found = latest_file(folder)
    if found is None:
        raise FirmwareError('FW_APP_MISSING', f'请将 APP 文件放入 {folder}，文件名以 _版本号.bin 结尾', directory=str(folder))
    return found


def load_target(config):
    version, path = app_target()
    return read_image(path, version, platform_for(config))
