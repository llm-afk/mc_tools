"""Drive APP image validation and the station's persistent firmware selection."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import uuid
import zlib

from .diagnostics import log_event
from .lifecycle import AlreadyRunning, StationInstance
from .station_config import atomic_json, state_root
from .storage import atomic_replace


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


def firmware_root(config):
    if config.firmware_directory:
        # v1.0.6 allowed relative paths alongside the EXE.
        return package_firmware().parent/Path(config.firmware_directory).expanduser()
    station = Path(config.credentials_file).parent if config.credentials_file else state_root()
    return station/'firmware'


def file_version(path):
    match = VERSION_SUFFIX.search(path.name)
    if not match:
        raise FirmwareError('FW_FILENAME', 'APP 文件名须以十进制版本结尾，例如 motor_app_245.bin', file=str(path))
    return int(match[1])


@dataclass(frozen=True)
class AppFile:
    path: Path
    version: int
    sha256: str = ''  # Empty only for a directly maintained v1.0.6 folder.

    def load(self, platform):
        data, info = read_image(self.path, self.version, platform)
        if self.sha256 and self.sha256 != info['sha256']:
            raise FirmwareError('FW_SELECTION_HASH', '工位已选 APP 的文件内容发生变化，请重新导入',
                                file=str(self.path), expected=self.sha256, actual=info['sha256'])
        return data, info

    def reference(self):
        return dict(name=self.path.name, version=self.version, sha256=self.sha256)


def _stored_file(folder, ref):
    name, digest, version = ref['name'], ref['sha256'], ref['version']
    if (Path(name).name != name or ':' in name or not re.fullmatch('[0-9a-f]{64}', digest)
            or type(version) is not int or version != file_version(Path(name))):
        raise ValueError('固件索引中的名称、版本或指纹无效')
    return AppFile(folder/'已导入'/digest/name, version, digest)


def _saved_pair(folder):
    index = folder/'当前版本.json'
    try:
        raw = index.read_bytes()
    except FileNotFoundError:
        return None
    try:
        doc = json.loads(raw)
        return (_stored_file(folder, doc['current']),
                _stored_file(folder, doc['previous']) if doc['previous'] is not None else None)
    except (ValueError, KeyError, TypeError, FirmwareError) as exc:
        raise FirmwareError('FW_SELECTION_INVALID', '工位固件索引损坏，请重新选择 APP', file=str(index)) from exc


def selections(config):
    platform = platform_for(config)
    folder = firmware_root(config)/platform/'APP'
    saved = _saved_pair(folder)
    if saved is not None:
        return saved
    # A site which has only ever copied files keeps its numeric-latest behaviour.
    found = latest_file(folder)
    if not found and not config.firmware_directory:
        found = latest_file(package_firmware()/platform/'APP')
    if found:
        version, path = found
        return AppFile(path, version), None
    return None, None


def _required_target(config):
    current, _ = selections(config)
    if current is None:
        raise FirmwareError('FW_APP_MISSING', f'请为 {platform_for(config)} 选择工位 APP',
                            directory=str(firmware_root(config)))
    return current


def app_target(config):
    current = _required_target(config)
    return current.version, current.path


def load_target(config):
    return _required_target(config).load(platform_for(config))


def _archive(folder, source, platform):
    data, info = source.load(platform)
    selected = _stored_file(folder, dict(name=source.path.name, version=source.version, sha256=info['sha256']))
    try:
        with selected.path.open('rb') as existing:
            if existing.read(len(data)+1) == data:
                return selected
    except FileNotFoundError:
        pass
    selected.path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix='.tmp', dir=selected.path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if Path(tmp).read_bytes() != data:
            raise FirmwareError('FW_COPY_FAILED', '固件写入后读回不一致', file=str(selected.path))
        atomic_replace(tmp, selected.path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return selected


def change_app(config, source=None, *, restore=None):
    """Commit a verified image, then atomically switch the pointer. Keep the last selection."""
    platform = platform_for(config)
    folder = firmware_root(config)/platform/'APP'
    if (source is None) == (restore is None):
        raise ValueError('请选择 APP 文件或指定需要恢复的上一版')
    try:
        with StationInstance(folder, lock_name='.换版.lock'):
            try:
                current, previous = selections(config)
            except FirmwareError as exc:
                if source is None or exc.diagnostic_code not in ('FW_SELECTION_INVALID', 'FW_VERSION_AMBIGUOUS'):
                    raise
                if exc.diagnostic_code == 'FW_SELECTION_INVALID':
                    index = folder/'当前版本.json'
                    backup = index.with_name(f'索引备份-{uuid.uuid4().hex}.json')
                    backup.write_bytes(index.read_bytes())
                log_event('firmware.library.repair', level='WARNING', error=exc, directory=str(folder))
                current = previous = None
            if restore is not None:
                if previous is None or previous != restore:
                    raise FirmwareError('FW_PREVIOUS_CHANGED', '上一版已变化，请查看当前版本后重新选择')
                previous.load(platform)
                chosen = previous
            else:
                path = Path(source)
                chosen = _archive(folder, AppFile(path, file_version(path)), platform)
            if chosen == current:
                log_event('firmware.library.unchanged', platform=platform, current=chosen.reference(),
                          source=str(source) if source else str(chosen.path))
                return current, previous
            if current and not current.sha256:
                # Preserve the old hand-copied image even if its USB/folder disappears later.
                try:
                    current = _archive(folder, current, platform)
                except (FileNotFoundError, FirmwareError) as exc:
                    if isinstance(exc, FirmwareError) and exc.diagnostic_code == 'FW_COPY_FAILED':
                        raise
                    log_event('firmware.library.previous_unavailable', level='WARNING', error=exc, file=str(current.path))
                    current = None
            previous = current if current != chosen else previous
            atomic_json(folder/'当前版本.json', dict(current=chosen.reference(),
                        previous=previous.reference() if previous else None))
            log_event('firmware.library.selected', platform=platform, action='restore' if restore else 'import',
                      source=str(source) if source else str(chosen.path), current=chosen.reference(),
                      previous=previous.reference() if previous else None, directory=str(folder))
            return chosen, previous
    except AlreadyRunning as exc:
        raise FirmwareError('FW_LIBRARY_BUSY', '另一工位正在更换此目录的固件，请稍后重试', directory=str(folder)) from exc


