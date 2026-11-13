"""Factory CAN DFU for the GD and ADM application download slots."""
import time

from motor_platform import CanFrame
from motor_platform.components.motor.client import CanBusError
from .diagnostics import log_event
from .firmware_library import (FirmwareError, app_target, firmware_root, latest_file,
                               load_target, platform_for, read_image)


START = bytes.fromhex('dd dd dd dd')
COMMIT = bytes.fromhex('ff ff ff ff')


def exchange(transport, node, payload, expected, timeout, phase, offset, check_stop):
    check_stop()
    can_id = 0x680+node
    started = time.monotonic()
    context = dict(phase=phase,node_id=node,can_id=hex(can_id),confirmed_bytes=offset,
                   tx=payload.hex(),expected_ack=expected.hex(),timeout_s=timeout,automatic_retry=False)
    try:
        transport.send(CanFrame(can_id,payload))
        deadline = started+timeout
        while True:
            check_stop()
            remaining = deadline-time.monotonic()
            if remaining<=0:
                raise FirmwareError('FW_ACK_TIMEOUT',f'{phase} 应答超时，已确认 {offset} 字节',**context)
            frame = transport.recv(min(.1,remaining))
            if frame is None:
                continue
            if frame.is_error:
                raise CanBusError(frame)
            if frame.can_id!=can_id or frame.is_extended or frame.is_remote:
                continue
            if frame.data!=expected:
                raise FirmwareError('FW_ACK_REJECTED',f'{phase} 应答不匹配，已确认 {offset} 字节',
                                    received=frame.data.hex(),**context)
            return time.monotonic()-started
    except Exception as exc:
        uncertain_commit = phase=='commit' and (isinstance(exc,OSError) or getattr(exc,'diagnostic_code','')=='FW_ACK_TIMEOUT')
        log_event('firmware.exchange.failed',level='WARNING' if uncertain_commit else 'ERROR',error=exc,
                  elapsed_s=round(time.monotonic()-started,4),**context)
        raise


def transfer(transport,node,image,check_stop,progress):
    progress('擦除 APP 下载区')
    exchange(transport,node,START,START,8,'erase',0,check_stop)
    log_event('firmware.erase.ack',node_id=node)
    started = time.monotonic()
    next_percent = 10
    slowest_ack = 0
    for offset in range(0,len(image),8):
        delay = exchange(transport,node,image[offset:offset+8],b'',1,'data',offset,check_stop)
        slowest_ack = max(slowest_ack,delay)
        confirmed = offset+8
        percent = confirmed*100//len(image)
        if percent>=next_percent:
            next_percent=(percent//10+1)*10
            progress(f'写入 APP {percent}%')
            log_event('firmware.progress',confirmed_bytes=confirmed,total_bytes=len(image),percent=percent,
                      frames_acknowledged=confirmed//8,max_data_ack_ms=round(slowest_ack*1000,2),
                      elapsed_s=round(time.monotonic()-started,3))
    progress('校验 CRC，等待驱动器重启')
    try:
        exchange(transport,node,COMMIT,COMMIT,6,'commit',len(image),check_stop)
    except (FirmwareError,OSError) as exc:
        if isinstance(exc,InterruptedError) or isinstance(exc,FirmwareError) and exc.diagnostic_code!='FW_ACK_TIMEOUT':
            raise
        # The reboot may precede the final ACK. Resolve only by a fresh identity and version read.
        log_event('firmware.commit.unconfirmed',level='WARNING',confirmed_bytes=len(image),readback_required=True)
        return False
    log_event('firmware.commit.ack',confirmed_bytes=len(image))
    return True


def read_identity(motor,bench,node):
    serial = bench.motor(node).read_serial_bytes(timeout=.3).rstrip(b'\0').decode('ascii',errors='replace')
    version = motor.read(bench,node,0x2100,2)
    return serial or '未写入 SN',version


def check_firmware(config,motor,node,expected_sn,progress, *, expected_sha256=None):
    platform = platform_for(config)
    root = firmware_root(config)
    image,info = load_target(config)
    target = info['target_version']
    if expected_sha256 and info['sha256'] != expected_sha256:
        raise FirmwareError('FW_TARGET_CHANGED','点击开始后工位 APP 已变化，请按最新目标重新开测',
                            expected=expected_sha256, actual=info['sha256'], target=target)
    confirmed = motor.discover(exhaustive=True)
    if confirmed != dict(node_id=node,sn=expected_sn):
        raise FirmwareError('FW_IDENTITY_CHANGED','检查固件前来料身份发生变化',observed=confirmed)
    with motor.connect(node) as bench:
        sn,before = read_identity(motor,bench,node)
        log_event('firmware.version.before',node_id=node,sn=sn,app_version=before,platform=platform,
                  version_object='0x2100',directory=str(root))
        if sn!=expected_sn:
            raise FirmwareError('FW_IDENTITY_CHANGED','版本查询时读回的电机 SN 已变化',observed=sn)
        info.update(node_id=node,sn=sn,before=before,adapter_sn=config.adapter_sn)
        try:
            boot = latest_file(root/platform/'BOOT')
            info['local_boot'] = dict(file=boot[1].name,version=boot[0]) if boot else None
        except (OSError,FirmwareError,ValueError) as exc:
            log_event('firmware.boot.inventory_failed',level='WARNING',error=exc)
        info['device_boot_version'] = None  # The object dictionary exposes the application version only.
        log_event('firmware.selected',**info)
        # These readable objects are present in their respective drive OD tables.
        # Positive evidence works with blank incoming SNs and prevents cross-platform flashing.
        register = 0x2072 if platform=='ADM' else 0x2001
        try:
            marker = motor.read(bench,node,register,2)
        except Exception as exc:
            raise FirmwareError('FW_PLATFORM_UNCONFIRMED',f'无法确认来料为 {platform} 平台，请核对工位配置',
                                node_id=node,register=hex(register),incoming_sn=sn) from exc
        log_event('firmware.platform.confirmed',platform=platform,register=hex(register),value=marker)
        if before>=target:
            info.update(outcome='CURRENT' if before==target else 'NEWER',after=before,transferred_bytes=0)
            progress(f'当前版本 {before}，工位目标 {target}，继续产测')
            log_event('firmware.version.accepted',**info)
            return info
        progress(f'APP {before} → {target}，正在升级')
        motor.check_stop()
        bench.motor(node).disable(timeout=1)
        log_event('firmware.disable.ack',node_id=node,before=before,target_version=target)
        info['commit_ack'] = transfer(bench._bus.transport,node,image,motor.check_stop,progress)
    progress(f'重连核对 APP {target}')
    deadline = time.monotonic()+15
    last = None
    attempts = 0
    while time.monotonic()<deadline:
        motor.wait(.35)
        attempts += 1
        try:
            with motor.connect(node) as bench:
                actual_sn,after = read_identity(motor,bench,node)
        except InterruptedError:
            raise
        except (OSError,TimeoutError,RuntimeError) as exc:
            last = str(exc)
            continue
        if actual_sn!=expected_sn:
            raise FirmwareError('FW_IDENTITY_CHANGED','重启后电机 SN 不符',expected=expected_sn,observed=actual_sn)
        if after==target:
            info.update(outcome='UPDATED',after=after,transferred_bytes=len(image),readback_attempts=attempts)
            progress(f'升级完成，已读回版本 {after}')
            log_event('firmware.version.verified',**info)
            return info
        last = f'读回版本 {after}'
    raise FirmwareError('FW_READBACK_UNCONFIRMED',f'升级后未确认目标版本 {target}，停止产测',
                        last_readback=last,attempts=attempts,**info)
