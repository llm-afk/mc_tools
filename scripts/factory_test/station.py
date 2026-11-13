"""可恢复的工位流程；模拟硬件与真实业务共用状态机。"""
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
import uuid
import time

from . import app, report
from .integrations import MesClient, PrinterClient, PendingConfiguration, PrintUnknown, mes_payload
from .maintenance import Maintenance
from .station_config import StationConfig, atomic_json, state_root
from .diagnostics import current_log, log_event, log_scope, error_details


STEPS = (
    ('firmware', '检查 APP 版本 / 按需 OTA'),
    ('assign_id', '设置工位 ID / 重连核对'),
    ('base_sn', '写临时 SN / 重连核对'), ('calibrate', '自动校准'),
    ('kt', '扭矩输出误差测试'), ('tn', '电机特性曲线测试'),
    ('allocate', 'MES 申请正式 SN'), ('write_sn', '写正式 SN / 重连核对'),
    ('print', '标签打印'), ('report', '生成报告'), ('upload', '提交报告上传队列'),
)


class RecoveryRequired(RuntimeError):
    pass


class Services:
    def __init__(self, config, cancelled, emit, delivery=None, *, firmware_sha256=None):
        self.config, self.cancelled, self.emit = config, cancelled, emit
        self.motor = Maintenance(config, cancelled)
        self.delivery = delivery
        self.firmware_sha256 = firmware_sha256

    def firmware(self, node, expected_sn):
        from .firmware import check_firmware
        return check_firmware(self.config,self.motor,node,expected_sn,
                              lambda message:self.emit('firmware_progress',message), expected_sha256=self.firmware_sha256)

    def write_sn(self, sn, current):
        self.motor.write_sn(sn, current)

    def assign_id(self, source_id, current):
        confirmed = self.motor.discover(exhaustive=True)
        if confirmed != dict(node_id=source_id, sn=current):
            raise RuntimeError('维护前来料 ID 或 SN 改变，停止操作')
        self.motor.assign_id(source_id, current)

    def probe(self):
        return self.motor.probe()

    def calibrate(self):
        self.motor.calibrate()

    def measure(self, directory, base_sn):
        args = SimpleNamespace(id=self.config.target_id, serial=self.config.adapter_sn, torque=self.config.torque)
        stages = []
        for name, module, csv in app.STAGES:
            def run(*argv, _name=name, _module=module, **kwargs):
                self.check_stop()
                started = time.perf_counter()
                log_event('test.stage.start', stage=_name, output_directory=str(directory/_name),
                          motor_id=self.config.target_id, target_torque=self.config.torque,
                          power_port=self.config.power_port, dyno_port=self.config.dyno_port,
                          parameters={key:value for key,value in vars(_module).items() if key.isupper() and
                                      key not in ('COLUMNS','OUTPUT_COLUMNS')})
                self.emit('step', dict(name=_name, status='RUNNING'))
                previous_ports = (_module.POWER_PORT, _module.DYNO_PORT)
                try:
                    _module.POWER_PORT, _module.DYNO_PORT = self.config.power_port, self.config.dyno_port
                    result = _module.main(*argv, **kwargs)
                except BaseException as exc:
                    log_event('test.stage.failed', level='ERROR', error=exc, stage=_name,
                              output_directory=str(directory/_name),
                              elapsed_s=round(time.perf_counter()-started,4))
                    self.emit('step',dict(name=_name,status='FAILED'))
                    raise
                finally:
                    _module.POWER_PORT, _module.DYNO_PORT = previous_ports
                self.check_stop()
                complete = result.get('reason') == ('completed' if _name == 'kt' else 'stall_confirmed')
                log_event('test.stage.end', stage=_name, result=result, complete=complete,
                          elapsed_s=round(time.perf_counter()-started, 4))
                self.emit('step', dict(name=_name, status='DONE' if complete else 'FAILED'))
                return result
            stages.append((name, SimpleNamespace(main=run), csv))
        summary = app.execute(args, directory, stages=stages, motor_sn=base_sn)
        summary['sections'] = report.analyze(directory, summary)
        summary['status'] = report.overall(summary['sections'], summary['stages'])
        return summary

    def check_stop(self):
        if self.cancelled():
            raise InterruptedError('工位流程已停止')

    def allocate(self, payload):
        return MesClient(self.config).allocate(payload, self.cancelled)

    def print_sn(self, sn):
        printer = PrinterClient(self.config)
        printer.ready()
        self.check_stop()
        return printer.print_sn(sn)

    def render(self, directory, summary):
        path, started = directory/'report.png', time.perf_counter()
        log_event('report.render.start', path=str(path), status=summary.get('status'),
                  sn=summary.get('module_sn',summary.get('motor_sn')), sections=summary.get('sections'))
        try:
            report.render(path, summary)
        except BaseException as exc:
            log_event('report.render.failed', level='ERROR', error=exc, path=str(path),
                      elapsed_s=round(time.perf_counter()-started,4))
            raise
        log_event('report.render.done', path=str(path), bytes=path.stat().st_size,
                  elapsed_s=round(time.perf_counter()-started,4))

    def upload(self, directory, summary):
        from .delivery import ReportDelivery
        delivery = self.delivery or ReportDelivery(directory.parent.parent, self.config)
        return delivery.enqueue(directory)  # 这里只持久入队；网络上传不占用电机测试线程。


class DemoServices(Services):
    """不会创建 CANable、HTTP 请求或打印任务；报告也标明模拟。"""
    def firmware(self, node, expected_sn):
        self.check_stop()
        return dict(outcome='SIMULATED',node_id=node,sn=expected_sn)

    def probe(self):
        return 'DEMO-UNPROGRAMMED'

    def write_sn(self, sn, current):
        self.check_stop()

    def assign_id(self, source_id, current):
        self.check_stop()

    def calibrate(self):
        self.check_stop()

    def measure(self, directory, base_sn):
        from .preview import create_demo
        summary = create_demo(directory)
        summary.update(motor_sn=base_sn, motor_id=self.config.target_id, target_torque=self.config.torque)
        for name in ('kt', 'tn'):
            self.emit('step', dict(name=name, status='RUNNING'))
            self.check_stop()
            self.emit('step', dict(name=name, status='DONE'))
        summary['sections'] = report.analyze(directory, summary)
        summary['status'] = report.overall(summary['sections'], summary['stages'])
        return summary

    def allocate(self, payload):
        # 固定 requestId 在演示中也返回同一个模拟号；不会预生成真实 SN。
        serial = int(uuid.UUID(payload['requestId'])) % 9999 + 1
        return 'DEMO00000000'+self.config.hardware_version+payload['productionDate']+f'{serial:04d}'

    def print_sn(self, sn):
        return dict(sn=sn, success=True, message='模拟打印：未发送打印任务')

    def upload(self, directory, summary):
        return dict(status='SIMULATED', message='模拟上传：未访问网络')


class StationRunner:
    def __init__(self, config, *, root=None, emit=lambda *_: None, cancelled=lambda: False, services=None, delivery=None,
                 log_target=None, firmware_sha256=None):
        self.config, self.emit, self.cancelled = config, emit, cancelled
        self.root = Path(root) if root else state_root()
        self.pointer = self.root / ('demo-active.json' if config.simulation else 'active.json')
        self.services = services or (DemoServices if config.simulation else Services)(
            config, cancelled, emit, delivery=delivery, firmware_sha256=firmware_sha256)
        self.record = None
        self.log = log_target or current_log()

    def pending(self):
        if not self.pointer.exists():
            return None
        path = Path(json.loads(self.pointer.read_text(encoding='utf-8'))['record'])
        record = json.loads(path.read_text(encoding='utf-8'))
        return record if record.get('status') not in ('COMPLETE', 'CLOSED') else None

    def close_pending(self, reason):
        record = self.pending()
        if not record:
            raise RecoveryRequired('没有未完成工件')
        if not reason.strip():
            raise RecoveryRequired('必须填写异常工件处理说明')
        self.record = record
        if self.log:
            target = self.log.owner.record_target(record['directory'])
            target.event('flow.archived', reason, session=record['session'], previous_status=record['status'],
                           steps=record['steps'], request_id=record.get('request_id'), module_sn=record.get('module_sn'))
        record.update(previous_status=record['status'], status='CLOSED', operator_resolution=reason)
        self.save()  # 只归档，不删除原 SN、请求和异常证据。
        return record

    def already_completed(self, sn):
        path = self.root / 'completed.json'
        return not self.config.simulation and path.exists() and sn in json.loads(path.read_text(encoding='utf-8'))

    def save(self):
        self.record['updated_at'] = datetime.now().astimezone().isoformat()
        path = Path(self.record['directory'])/'station.json'
        try:
            atomic_json(path, self.record)
        except BaseException as exc:
            target = current_log() or self.log
            if target:
                target.event('flow.persistence.failed', '工件记录未能持久保存', level='ERROR',error=exc,
                             path=str(path), session=self.record['session'], status=self.record['status'],
                             step=self.record.get('current_step'), steps=self.record['steps'])
            raise

    def begin(self, detected_sn, detected_id=None):
        self.config.validate(live=not self.config.simulation)
        if self.pending():
            raise RecoveryRequired('有未完成工件，请先选择“继续未完成工件”，禁止重新取号')
        session = str(uuid.uuid4())
        production_date = self.config.production_date or datetime.now().strftime('%y%m%d')
        directory = self.root / ('demo-data' if self.config.simulation else 'data') / session
        directory.mkdir(parents=True)
        base = self.config.base_sn
        self.record = dict(session=session, directory=str(directory), status='RUNNING',
                           demo=self.config.simulation, config=asdict(self.config),
                           detected_sn=detected_sn, detected_id=detected_id or self.config.target_id,
                           base_sn=base, steps={}, firmware=None,
                           production_date=production_date,
                           request_id=str(uuid.uuid4()), created_at=datetime.now().astimezone().isoformat())
        if self.log:
            self.record['connection_log'] = self.log.reference
            self.record['connection_logs'] = [self.log.reference]
        self.save()
        atomic_json(self.pointer, dict(record=str(directory/'station.json')))
        return self.run()

    def resume(self):
        record = self.pending()
        if not record:
            raise RecoveryRequired('没有未完成工件')
        original_fields=record['config']
        restored=asdict(StationConfig())
        restored.update({key:value for key,value in original_fields.items() if key in restored})
        current=asdict(self.config)
        # A local encrypted-key location is not part of a workpiece's protocol identity.
        restored.pop('credentials_file',None)
        current.pop('credentials_file',None)
        if restored != current:
            raise RecoveryRequired('当前配置与未完成工件不一致，请恢复原工位配置后再继续')
        self.record = record
        record['config']=asdict(self.config)
        if self.log:
            self.log.event('flow.resume', '恢复已保存流程', session=record['session'],
                           previous_connection_log=record.get('connection_log'), steps=record['steps'])
        # 所有已进入副作用但未确认的步骤都阻止盲重试。
        for name in ('firmware','assign_id', 'base_sn', 'calibrate', 'measure', 'write_sn'):
            if record['steps'].get(name) in ('RUNNING', 'FAILED'):
                raise RecoveryRequired(f'{name} 执行结果未知，需要人工核对电机；禁止自动重写或重校准')
        if record['steps'].get('print') in ('RUNNING', 'UNKNOWN', 'FAILED'):
            raise RecoveryRequired('打印结果待人工核对；此处禁止自动重打')
        before_id = record['steps'].get('assign_id') != 'DONE'
        expected = (record.get('module_sn') if record['steps'].get('write_sn')=='DONE' else
                    record['base_sn'] if record['steps'].get('base_sn')=='DONE' else record['detected_sn'])
        if before_id and not self.config.simulation:
            incoming = self.services.motor.discover(exhaustive=True)
            if incoming['node_id']!=record['detected_id']:
                raise RecoveryRequired('恢复前来料电机 ID 不符')
            current = incoming['sn']
        else:
            current = self.services.probe()
        if current != expected and not self.config.simulation:
            raise RecoveryRequired('当前电机与未完成工件 SN 不符')
        if self.log:
            record['connection_log'] = self.log.reference
            references = record.setdefault('connection_logs', [])
            if self.log.reference not in references:
                references.append(self.log.reference)
            self.save()
        return self.run()

    def action(self, name, operation):
        if self.record['steps'].get(name) == 'DONE':
            log_event('flow.step.skip', '步骤已经完成，沿用原结果', step=name)
            return
        if self.cancelled():
            raise InterruptedError('已停止自动流程')
        self.record['current_step'] = name
        self.record['steps'][name] = 'RUNNING'
        if name=='print':
            self.record.setdefault('print_job_id',uuid.uuid4().hex)
        self.save()  # 先保存意图，再调用可能具有副作用的接口。
        started = time.perf_counter()
        log_event('flow.step.start', step=name)
        self.emit('step', dict(name=name, status='RUNNING'))
        if self.cancelled():
            raise InterruptedError('已停止自动流程')
        target = current_log()
        with log_scope(target.bind(step=name, motor_id=self.config.target_id,
                                  module_sn=self.record.get('module_sn'), base_sn=self.record['base_sn'],
                                  print_job_id=self.record.get('print_job_id')) if target else None):
            try:
                result = operation()
            except BaseException as exc:
                log_event('flow.step.failed', level='ERROR', error=exc, step=name,
                          elapsed_s=round(time.perf_counter()-started,4))
                raise
        log_event('flow.step.operation_returned', '步骤调用已返回，准备持久保存完成状态',step=name,
                  elapsed_s=round(time.perf_counter()-started,4),result=result,module_sn=self.record.get('module_sn'))
        self.record['steps'][name] = 'DONE'
        self.save()
        log_event('flow.step.done', step=name, elapsed_s=round(time.perf_counter()-started, 4), result=result)
        self.emit('step', dict(name=name, status='DONE'))
        return result

    def run(self):
        record = self.record
        target = self.log.bind(session=record['session'],request_id=record['request_id'],
                               report_directory=record['directory']) if self.log else None
        with log_scope(target):
            started = time.perf_counter()
            log_event('flow.start', '开始执行产测流程', config=record['config'],
                      detected_id=record['detected_id'], detected_sn=record['detected_sn'], base_sn=record['base_sn'])
            try:
                result = self._run()
            except BaseException as exc:
                log_event('flow.unhandled', '流程外围或记录保存异常', level='ERROR', error=exc,
                          step=record.get('current_step'), steps=record['steps'])
                raise
            log_event('flow.end', status=result['status'], steps=result['steps'],
                      elapsed_s=round(time.perf_counter()-started, 4), module_sn=result.get('module_sn'),
                      report_delivery=result.get('report_delivery'))
            return result

    def _run(self):
        record = self.record
        directory = Path(record['directory'])
        self.emit('resultdir', str(directory))
        if record['steps'].get('write_sn') == 'DONE':
            self.emit('sn', record['module_sn'])
            self.emit('sn_phase', '正式 SN · 已写入核对')
        elif record['steps'].get('base_sn') == 'DONE':
            self.emit('sn', record['base_sn'])
            self.emit('sn_phase', '临时 SN · 已写入核对')
        for name, state in record['steps'].items():
            if state == 'DONE':
                self.emit('step', dict(name=name, status=state))
                if name == 'measure':
                    for part in ('kt', 'tn'):
                        self.emit('step', dict(name=part, status=state))
        try:
            record['status'] = 'RUNNING'
            if 'firmware' in record or not record['steps']:
                def firmware():
                    record['firmware'] = self.services.firmware(record['detected_id'],record['detected_sn'])
                    return record['firmware']
                self.action('firmware',firmware)
            else:
                log_event('firmware.legacy_resume','旧版工件已开始后续工序',version_verified=False)
                self.emit('step',dict(name='firmware',status='LEGACY'))
            self.action('assign_id', lambda: self.services.assign_id(record['detected_id'], record['detected_sn']))
            self.action('base_sn', lambda: self.services.write_sn(record['base_sn'], record['detected_sn']))
            if record['steps'].get('write_sn') != 'DONE':
                self.emit('sn', record['base_sn'])
                self.emit('sn_phase', '临时 SN · 已写入核对')
            self.action('calibrate', self.services.calibrate)
            def measure():
                summary = self.services.measure(directory, record['base_sn'])
                if record.get('firmware'):
                    summary['firmware'] = record['firmware']
                record['summary'] = summary
                log_event('test.analysis', status=summary['status'], sections=summary.get('sections'),
                          limits=summary.get('limits'), stages=summary.get('stages'))
                self.emit('metrics', summary)
                atomic_json(directory/'summary.json', summary)
                if summary['status'] != 'PASS':
                    # 失败也保留测试报告，但不会进入取号、写号或打印。
                    self.services.render(directory, summary)
                    raise RuntimeError('测试未通过或未完成；不会申请正式 SN')
            self.action('measure', measure)
            def allocate():
                if 'mes_payload' not in record:
                    record['mes_payload'] = mes_payload(self.config, record['request_id'],
                                                       production_date=record.get('production_date'))
                    self.save()
                record['module_sn'] = self.services.allocate(record['mes_payload'])
                log_event('sn.allocated', module_sn=record['module_sn'], payload=record['mes_payload'])
                self.save()  # SN 一旦返回，立即持久化；补打/恢复不重新取号。
            self.action('allocate', allocate)
            self.action('write_sn', lambda: self.services.write_sn(record['module_sn'], record['base_sn']))
            self.emit('sn', record['module_sn'])
            self.emit('sn_phase', '正式 SN · 已写入核对')
            self.action('print', lambda: self.services.print_sn(record['module_sn']))
            def render():
                from .upload import station_report_key
                summary = record['summary']
                summary.update(motor_sn=record['module_sn'], module_sn=record['module_sn'],
                               base_sn=record['base_sn'], request_id=record['request_id'],
                               fixture_id=self.config.fixture_id,
                               calibration='COMPLETED', label_print='SIMULATED' if record['demo'] else 'CONFIRMED',
                               station_version=2, station_session=record['session'])
                summary['report_key'] = station_report_key(summary, record['session'])
                atomic_json(directory/'summary.json', summary)
                self.services.render(directory, summary)
                summary['station_ready'] = True
                atomic_json(directory/'summary.json', summary)
            self.action('report', render)
            submitted = self.action('upload', lambda: self.services.upload(directory, record['summary']))
            if submitted is not None:
                record['report_delivery'] = submitted
            record['status'] = 'COMPLETE'
            self.save()
            if not record['demo']:
                path = self.root/'completed.json'
                completed = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
                completed[record['module_sn']] = dict(directory=str(directory), completed_at=record['updated_at'])
                atomic_json(path, completed)
            self.emit('complete', dict(demo=record['demo'], sn=record['module_sn']))
        except BaseException as exc:
            step = record.get('current_step', '')
            status = 'UNKNOWN' if isinstance(exc, PrintUnknown) else 'PENDING' if isinstance(exc, PendingConfiguration) else 'FAILED'
            record['steps'][step] = status
            diagnostic = error_details(exc)
            notes = list(getattr(exc, '__notes__', []))
            redact = self.log.owner.redact if self.log else lambda value:value
            record.update(status=status, error=redact(str(exc)), error_notes=redact(notes), diagnostic=redact(diagnostic))
            log_event('flow.failed', '流程失败，保留完整异常和收尾提示', level='ERROR', error=exc,
                      step=step, status=status, steps=record['steps'], notes=record['error_notes'],
                      module_sn=record.get('module_sn'))
            try:
                self.save()
            except Exception as save_exc:
                # A disk/permission fault must not hide the original failure from the UI.
                log_event('flow.failure_record.failed', '原始失败已写入诊断日志，但工件状态保存失败',
                          level='ERROR', error=save_exc, original_diagnostic=record['diagnostic'])
                record['error_notes'].append(redact('本件状态保存失败：'+str(save_exc)))
            self.emit('step', dict(name=step, status=status))
            self.emit('failed', dict(message=record['error'], notes=record['error_notes'], step=step,
                step_label=dict(STEPS).get(step,'两项性能测试' if step=='measure' else step),
                diagnostic=record['diagnostic'], log_path=str(self.log.path) if self.log else None))
        return record


class PresenceLatch:
    """完成/失败后要求持续离线；USB 短暂抖动不会触发同一件重测。"""
    def __init__(self, absence_seconds=2.0):
        self.absence_seconds = absence_seconds
        self.wait_removal = False
        self.absent_since = None
        self.stable = 0

    def completed(self):
        self.wait_removal = True
        self.absent_since = None
        self.stable = 0

    def observe(self, present, now):
        if self.wait_removal:
            if present:
                self.absent_since = None
            elif self.absent_since is None:
                self.absent_since = now
            elif now-self.absent_since >= self.absence_seconds:
                self.wait_removal = False
                self.stable = 0
            return False
        self.stable = self.stable+1 if present else 0
        return self.stable == 2
