"""工装侧 SDO 维护协议，单接收者。只在自动工位实际运行时打开硬件。"""
from contextlib import contextmanager
import time

from motor_platform import Bench, Canable, CanFrame, SdoError
from .diagnostics import current_log, log_event


class Maintenance:
    def __init__(self, config, cancelled=lambda: False, bench_factory=Bench, *, diagnostic_poll=False):
        self.config, self.cancelled, self.bench_factory = config, cancelled, bench_factory
        self.diagnostic_poll = diagnostic_poll

    def event(self, name, **details):
        target = current_log()
        if self.diagnostic_poll and target is not None:
            target.sample(name, interval=30, **details)
        else:
            log_event(name, **details)

    def check_stop(self):
        if self.cancelled():
            raise InterruptedError('操作已停止')

    def wait(self, duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self.check_stop()
            time.sleep(min(.05, max(0, deadline-time.monotonic())))

    @contextmanager
    def connect(self, node=None):
        self.check_stop()
        node = self.config.target_id if node is None else node
        started = time.perf_counter()
        self.event('transport.open', adapter_sn=self.config.adapter_sn, node_id=node)
        try:
            with self.bench_factory(transport=Canable(self.config.adapter_sn), motor_ids=[node]) as bench:
                self.event('transport.connected', adapter_sn=self.config.adapter_sn, node_id=node,
                          elapsed_s=round(time.perf_counter()-started, 4))
                try:
                    yield bench
                finally:
                    self.event('transport.releasing', node_id=node)
        except BaseException as exc:
            if not (self.diagnostic_poll and isinstance(exc, TimeoutError)):
                log_event('transport.failed', level='ERROR', error=exc, node_id=node,
                          adapter_sn=self.config.adapter_sn, elapsed_s=round(time.perf_counter()-started, 4))
            raise
        self.event('transport.closed', node_id=node, elapsed_s=round(time.perf_counter()-started, 4))

    @staticmethod
    def read(bench, node, index, size, timeout=.3, *, diagnostic_probe=False):
        bus = bench._bus
        started, reply = time.perf_counter(), None
        request = CanFrame(0x600+node, b'\x40'+index.to_bytes(2, 'little')+b'\0'*5)
        details = dict(node_id=node, index=f'0x{index:04X}', subindex=0, size=size,
                       can_id=f'0x{request.can_id:03X}', payload=request.data.hex(), timeout_s=timeout)
        if not diagnostic_probe:
            log_event('sdo.read.request', **details)
        try:
            with bus._lock:
                bus._check()
                if bus._sdo_uncertain:
                    raise SdoError('上一笔 SDO 结果未知，必须重连')
                bus._sdo_uncertain = True
                try:
                    bus.transport.send(request)
                    reply = bus._wait_sdo(node, index, time.perf_counter()+timeout)
                except SdoError:
                    bus._sdo_uncertain = False
                    raise
                if reply.data[0] != {1: 0x4F, 2: 0x4B, 4: 0x43}[size]:
                    raise SdoError(f'0x{index:04X} 对象长度不符：{reply.data.hex()}')
                bus._sdo_uncertain = False
                value = int.from_bytes(reply.data[4:4+size], 'little')
                if not diagnostic_probe:
                    log_event('sdo.read.ack', node_id=node,index=f'0x{index:04X}',size=size,value=value,
                              reply=reply.data.hex(),elapsed_s=round(time.perf_counter()-started,4))
                target = current_log()
                if target:
                    target.owner.snapshot(f'sdo.read.{node}.{index:04X}',
                        dict(node_id=node,index=f'0x{index:04X}',size=size,value=value,reply=reply.data.hex()),target=target)
                return value
        except BaseException as exc:
            if not diagnostic_probe or not isinstance(exc, TimeoutError):
                log_event('sdo.read.failed', level='ERROR', error=exc, **details,
                          reply=reply.data.hex() if reply else None,
                          elapsed_s=round(time.perf_counter()-started, 4), uncertain=bus._sdo_uncertain)
            raise

    @staticmethod
    def write(bench, node, index, size, value, timeout=.5):
        bus = bench._bus
        started, request, entered = time.perf_counter(), None, False
        try:
            with bus._lock:
                bus._check()
                if bus._sdo_uncertain:
                    raise SdoError('上一笔 SDO 结果未知，禁止重发写操作')
                payload = value.to_bytes(size, 'little').ljust(4, b'\0')
                request = CanFrame(0x600+node, bytes([{1: 0x2F, 2: 0x2B, 4: 0x23}[size]])+
                                   index.to_bytes(2, 'little')+b'\0'+payload)
                log_event('sdo.write.request', node_id=node,index=f'0x{index:04X}',size=size,value=value,
                          can_id=f'0x{request.can_id:03X}',payload=request.data.hex(),timeout_s=timeout)
                entered = True
                bus._write_sdo(node, index, request, time.perf_counter()+timeout)
        except BaseException as exc:
            log_event('sdo.write.failed', level='ERROR', error=exc, node_id=node,index=f'0x{index:04X}',
                      size=size,value=value,can_id=f'0x{0x600+node:03X}',payload=request.data.hex() if request else None,
                      timeout_s=timeout,elapsed_s=round(time.perf_counter()-started,4),
                      write_call_entered=entered,outcome='ACK_NOT_CONFIRMED' if entered else 'NOT_STARTED',
                      automatic_retry=False)
            raise
        log_event('sdo.write.ack',node_id=node,index=f'0x{index:04X}',
                  elapsed_s=round(time.perf_counter()-started,4))

    def probe(self):
        """只读固定工位 ID；空白 SN 也可被发现，不要求预先写过 SN。"""
        with self.connect() as bench:
            self.event('motor.sn.read.request',node_id=self.config.target_id,timeout_s=.15)
            raw = bench.motor(self.config.target_id).read_serial_bytes(timeout=.15)
            sn = raw.rstrip(b'\0').decode('ascii', errors='replace') or '未写入 SN'
            self.event('motor.sn.read.complete',node_id=self.config.target_id,sn=sn,raw=raw.hex())
            return sn

    def discover(self, *, exhaustive=False, fallback=True):
        """优先来料 ID=1 与工位 ID；无响应再只读扫描其余 1..127。"""
        preferred = list(dict.fromkeys((1, self.config.target_id)))
        started = time.perf_counter()
        self.event('discovery.start', preferred_ids=preferred, exhaustive=exhaustive, fallback=fallback)
        with self.connect() as bench:
            found = []
            scanned, timeouts = [], []
            def find(nodes):
                for node in nodes:
                    self.check_stop()
                    scanned.append(node)
                    try:
                        self.read(bench, node, 0x2040, 1, timeout=.012 if exhaustive else .025, diagnostic_probe=True)
                    except TimeoutError:
                        timeouts.append(node)
                        # 本会话仅探测，不写数据。不同 node 的迟到应答不会被下一节点接受。
                        # 任何后续维护会另开会话，绝不把不确定读事务接到写事务上。
                        bench._bus._sdo_uncertain = False
                        continue
                    found.append(node)
            find(preferred)
            if exhaustive or (not found and fallback):
                find(node for node in range(1, 128) if node not in preferred)
            self.event('discovery.scan', scanned_ids=scanned, responding_ids=found, timeout_ids=timeouts,
                      elapsed_s=round(time.perf_counter()-started,4))
            if len(found) != 1:
                if found:
                    raise RuntimeError('发现多个待测电机 ID：'+','.join(map(str, found))+'，请仅连接一台')
                raise TimeoutError('尚未发现来料电机（优先 ID 1，自动扫描 1～127）')
            node = found[0]
            raw = bench._bus.motor(node).read_serial_bytes(timeout=.2)
            sn = raw.rstrip(b'\0').decode('ascii', errors='replace') or '未写入 SN'
            self.event('discovery.identity',node_id=node,sn=sn,raw=raw.hex(),
                       elapsed_s=round(time.perf_counter()-started,4))
            return dict(node_id=node, sn=sn)

    def assign_id(self, source_id, expected_current):
        target = self.config.target_id
        if source_id == target:
            log_event('motor.id.unchanged', node_id=target)
            return
        log_event('motor.id.change', source_id=source_id,target_id=target,expected_sn=expected_current)
        adm = self.config.model[0] == '2'
        with self.connect(source_id) as bench:
            motor = bench.motor(source_id)
            raw = motor.read_serial_bytes()
            current = raw.rstrip(b'\0').decode('ascii', errors='replace') or '未写入 SN'
            log_event('motor.identity.check',node_id=source_id,expected=expected_current,received=current,
                      raw=raw.hex(),purpose='before_id_change')
            if current != expected_current:
                raise RuntimeError('改 ID 前来料身份发生变化，停止操作')
            log_event('motor.disable.request',node_id=source_id,reason='before_id_change')
            motor.disable()
            log_event('motor.disable.ack',node_id=source_id,reason='before_id_change')
            try:
                self.write(bench, source_id, 0x2040, 1, target)
            except TimeoutError:
                if not adm:
                    raise
                # ADM 的 0x2040 回调立即 ResetDSP，ACK 可能丢失；后续读回是唯一证明。
            if not adm:
                # GD 在启动时缓存 CAN ID。写回原 SN 末段触发已存在的重启回调，SN 不变。
                try:
                    self.write(bench, source_id, 0x200A, 4, int.from_bytes(raw[24:28], 'little'))
                except TimeoutError:
                    pass  # 仅末段复位可丢 ACK，仍必须从目标 ID 完整读回。
        self.wait_sn(expected_current)
        log_event('motor.id.verified',node_id=target,sn=expected_current)

    def wait_sn(self, expected):
        # 最后一段 SN 触发 GD/ADM 软复位，等待之后另开 USB 会话读回。
        self.wait(1.0)
        deadline = time.monotonic()+8
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            self.check_stop()
            try:
                received = self.probe()
                log_event('motor.sn.readback',attempt=attempt,expected=expected,received=received)
                if received == expected:
                    return
            except (OSError, TimeoutError, SdoError) as exc:
                log_event('motor.sn.reconnect_wait',level='WARNING',error=exc,attempt=attempt)
            self.wait(.25)
        raise RuntimeError('写 SN 后未能重连并完整读回，请检查电源和 ID；不会自动重写')

    def write_sn(self, sn, expected_current):
        if len(sn) != 26 or not sn.isascii() or not sn.isalnum():
            raise ValueError('SN 必须为 26 位 ASCII 字母数字')
        if sn[12:16] != self.config.hardware_version:
            raise ValueError('SN 硬件版本 ABCD 与工位产品配置不符')
        raw = sn.encode('ascii')+b'\0\0'
        node = self.config.target_id
        log_event('motor.sn.write',node_id=node,previous_sn=expected_current,sn=sn)
        with self.connect() as bench:
            current_raw = bench.motor(node).read_serial_bytes()
            current = current_raw.rstrip(b'\0').decode('ascii', errors='replace') or '未写入 SN'
            log_event('motor.identity.check',node_id=node,expected=expected_current,received=current,
                      raw=current_raw.hex(),purpose='before_sn_write')
            if current != expected_current:
                raise RuntimeError('电机身份发生变化，停止写号')
            log_event('motor.disable.request',node_id=node,reason='before_sn_write')
            bench.motor(node).disable()
            log_event('motor.disable.ack',node_id=node,reason='before_sn_write')
            for offset, index in enumerate(range(0x2004, 0x200B)):
                self.check_stop()
                try:
                    self.write(bench, node, index, 4, int.from_bytes(raw[offset*4:offset*4+4], 'little'))
                except TimeoutError:
                    if index != 0x200A:
                        raise
                    log_event('motor.sn.reset_ack_missing','末段写入触发复位，等待完整读回证明',level='WARNING')
                    # 仅最后一段允许因复位丢 ACK，必须另开会话完整读回证明。
        self.wait_sn(sn)
        log_event('motor.sn.verified',node_id=node,sn=sn)

    def calibrate(self):
        node = self.config.target_id
        with self.connect() as bench:
            motor = bench.motor(node)
            log_event('motor.disable.request',node_id=node,reason='before_calibration')
            motor.disable()
            log_event('motor.disable.ack',node_id=node,reason='before_calibration')
            initial = self.read(bench, node, 0x2000, 2)
            if initial & ~0x4000:
                raise RuntimeError(f'校准前驱动器错误：0x{initial:04X}')
            adm = self.config.model[0] == '2'
            log_event('calibration.preflight',node_id=node,initial_error=f'0x{initial:04X}',
                      platform='ADM' if adm else 'GD',timeout_s=self.config.calibration_timeout)
            if adm:
                # ADM 的旧 is_calibrated=1 不能冒充本次完成，先清零并读回。
                self.write(bench, node, 0x2072, 2, 0)
                if self.read(bench, node, 0x2072, 2) != 0:
                    raise RuntimeError('未能清除旧校准完成标志')
            elif not initial & 0x4000:
                raise RuntimeError('GD 固件未暴露本次校准进度；已有校准的电机需补完成协议，不能仅凭 ACK 放行')
            try:
                self.write(bench, node, 0x2002, 2, 0xF1)
                deadline = time.monotonic()+self.config.calibration_timeout
                stable = 0
                while time.monotonic() < deadline:
                    self.check_stop()
                    error = self.read(bench, node, 0x2000, 2)
                    if error & ~0x4000:
                        raise RuntimeError(f'校准驱动器错误：0x{error:04X}')
                    complete = self.read(bench, node, 0x2072, 2) == 1 if adm else True
                    stable = stable+1 if complete and error == 0 else 0
                    if current_log():
                        current_log().sample('calibration.progress',error_code=error,complete=complete,stable_samples=stable)
                    if stable >= 2:
                        log_event('motor.disable.request',node_id=node,reason='after_calibration')
                        motor.disable()
                        log_event('calibration.confirmed',node_id=node,stable_samples=stable,disable_ack=True)
                        return
                    self.wait(.2)
                raise TimeoutError('校准完成等待超时')
            except BaseException as exc:
                # ADM 校准态会重新置 RunSignal=1；SDO disable ACK 不足以证明校准被取消。
                try:
                    bench._bus.transport.send(CanFrame(0x600+node, b'\x2b\x02\x20\x00\x02\0\0\0'))
                except Exception as stop_exc:
                    log_event('calibration.stop_send_failed',level='ERROR',error=stop_exc)
                log_event('calibration.stop_unconfirmed','已尝试停止；固件未证明校准取消',
                          level='ERROR',error=exc,node_id=node)
                exc.add_note('校准停止未得到固件状态证明，请切断工装电源后处理；不会自动重新校准')
                raise
