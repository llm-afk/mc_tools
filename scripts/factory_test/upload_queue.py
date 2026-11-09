"""Durable, recoverable report delivery. Never opens a CAN or serial connection."""
from datetime import datetime
import json
import os
from pathlib import Path
from .storage import metadata
from .storage import publish_csvs
import re
import sys
import threading
import time

from .upload import (MES_URL, ReportUploader, UploadBusy, UploadError, UploadPlan,
                     prepare_report, report_lock, save_json, validate_endpoint)
from .upload import CLOUD_MES_URL, LOCAL_MES_URL, configured_mes_url, storage_target
from .diagnostics import current_log, log_event, log_scope

RETRY_DELAYS = (5, 15, 30, 60, 120, 300)
BLOCKED_RECHECK_S = 600
SCAN_SECONDS = 30
NAME_PATTERN = re.compile(r'\d{8}_\d{6}_[A-Za-z0-9]{26}(?:_\d+)?')


def read_json(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise UploadError('本地记录格式错误。')
    return value


def production_pass(summary):
    return (summary.get('demo') is False and not summary.get('integration_test') and
            (summary.get('station_version') != 2 or summary.get('station_ready') is True) and
            summary.get('status') == 'PASS' and summary.get('test_status', 'PASS') == 'PASS')


class UploadQueue:
    def __init__(self, roots, *, endpoint=None, config=None, notify=lambda value: None,
                 uploader=None, clock=time.time, diagnostics=None,oss_target=None):
        self.roots = tuple(dict.fromkeys(Path(p).resolve() for p in roots))
        self.endpoint_error = None
        try:
            self.endpoint = configured_mes_url(config) if endpoint is None else validate_endpoint(endpoint)
        except UploadError as exc:
            self.endpoint = MES_URL
            self.endpoint_error = str(exc)
        self.notify, self.clock = notify, clock
        self.diagnostics = diagnostics
        self.uploader = uploader or ReportUploader(config=config, endpoint=self.endpoint, max_attempts=1)
        self.oss_target = oss_target or (self.uploader.oss_target if isinstance(self.uploader,ReportUploader) else storage_target())
        self.stop_event, self.wake = threading.Event(), threading.Event()
        self.lock = threading.RLock()
        self.entries = {}
        self.active = None
        self.scan_error = None
        self.next_scan = 0
        self.last_snapshot = None
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name='report-upload-queue', daemon=True)
        self.thread.start()

    def stop(self):
        # No forced cancellation during a file commit; a killed process is recovered on startup.
        self.stop_event.set()
        self.wake.set()

    def _run(self):
        while not self.stop_event.is_set():
            self.wake.clear()
            try:
                self.step()
            except Exception as exc:
                if self.diagnostics:
                    self.diagnostics.station.event('delivery.worker.failed',level='ERROR',error=exc,
                        endpoint=self.endpoint,active=self.active,roots=[str(root) for root in self.roots])
                self.scan_error = f'上传后台暂不可用：{type(exc).__name__}；本地记录保留，自动重试。'
                self._publish()
            self.wake.wait(1)

    def _remember(self, directory, item):
        with self.lock:
            self.entries[Path(directory)] = item

    def _publish(self, notice=None, accepted=None):
        with self.lock:
            items = list(self.entries.values())
        payload = dict(pending=sum(i['status'] in ('PENDING', 'RETRY') for i in items),
                       blocked=sum(i['status'] == 'BLOCKED' for i in items),
                       succeeded=sum(i['status'] == 'SUCCEEDED' for i in items),
                       active=self.active, error=self.scan_error,
                       reason=next((i.get('last_error') for status in ('BLOCKED', 'RETRY', 'UPLOADING')
                                    for i in items if i['status'] == status and i.get('last_error')), None))
        if notice or accepted or payload != self.last_snapshot:
            self.last_snapshot = dict(payload)
            payload['notice'] = notice
            payload['accepted'] = str(accepted) if accepted else None
            self.notify(payload)  # GUI adapter only puts this into a Queue; no Tk access here.

    def enqueue(self, directory):
        if self.endpoint_error:
            raise UploadError(self.endpoint_error)
        directory = Path(directory).resolve()
        with report_lock(directory, '.upload-queue.lock'):
            summary = read_json(metadata(directory, 'summary.json'))
            if not production_pass(summary):
                raise UploadError('只有真实、完整且测试通过的报告才能进入生产上传队列。')
            with report_lock(directory):
                plan = UploadPlan.from_directory(directory, summary,object_prefix=self.oss_target['prefix'])
                state = prepare_report(plan, self.endpoint,self.oss_target)
            path = metadata(directory, 'upload-queue.json')
            item = read_json(path) if path.exists() else dict(attempts=0, next_attempt=0)
            if item.get('endpoint', self.endpoint) != self.endpoint:
                raise UploadError('待传任务属于其他 MES 环境，未自动迁移。')
            item.update(endpoint=self.endpoint, reportKey=plan.request['reportKey'],
                        status='SUCCEEDED' if state['status'] == 'SUCCEEDED' else item.get('status', 'PENDING'))
            save_json(path, item)
        self._remember(directory, item)
        self.wake.set()
        self._publish(accepted=directory)
        if self.diagnostics:
            self.diagnostics.record_target(directory).event('delivery.queued',report_key=plan.request['reportKey'],
                                                            endpoint=self.endpoint,status=item['status'])
        return dict(status=item['status'], receipt=state.get('receipt'))

    def _recover_render(self, directory):
        """Recover the crash window after CSV/test outcome commit but before PNG/manifest."""
        source = metadata(directory, 'report-input.json')
        outcome_only = not source.exists()
        if outcome_only:
            source = metadata(directory, 'test-outcome.json')
        if not source.exists() or (metadata(directory, 'upload-state.json')).exists():
            return
        summary_path = metadata(directory, 'summary.json')
        if summary_path.exists():
            saved = read_json(summary_path)
            if not saved.get('report_error'):
                return
        summary = read_json(source)
        if not outcome_only and not production_pass(summary):
            return
        if outcome_only and (summary.get('demo') is not False or summary.get('integration_test')):
            return
        with report_lock(directory):
            # Another live test may still be finishing the original render.
            if (metadata(directory, 'upload-state.json')).exists():
                return
            from . import report
            publish_csvs(directory)
            summary['sections'] = report.analyze(directory, summary)
            if report.overall(summary['sections'], summary['stages']) != 'PASS':
                raise UploadError('恢复报告时测试数据不完整或判定不通过，未上报。')
            summary.update(status='PASS', test_status='PASS', output_directory=str(directory))
            report.render(directory/'report.png', summary)
            summary.pop('report_error', None)
            save_json(summary_path, summary)

    def discover(self):
        errors = []
        for root in self.roots:
            if not root.exists():
                continue
            try:
                directories = [p for p in root.iterdir() if p.is_dir() and (
                    NAME_PATTERN.fullmatch(p.name) or re.fullmatch(
                        r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', p.name))]
            except OSError:
                errors.append(f'无法读取报告目录：{root}')
                continue
            # Deliberately non-recursive: local-MES experiments and demos are never swept into production.
            for directory in directories:
                try:
                    queue_path = metadata(directory, 'upload-queue.json')
                    if queue_path.exists():
                        item = read_json(queue_path)
                        if item.get('endpoint') != self.endpoint:
                            if not self._correct_rejected_route(directory):
                                continue
                            item = read_json(queue_path)
                        # Clamp persisted deadlines after wall-clock jumps; never strand a task for days.
                        if item.get('next_attempt', 0) > self.clock()+BLOCKED_RECHECK_S:
                            with report_lock(directory, '.upload-queue.lock'):
                                item = read_json(queue_path)
                                item['next_attempt'] = min(item.get('next_attempt', 0), self.clock()+BLOCKED_RECHECK_S)
                                save_json(queue_path, item)
                        self._remember(directory, item)
                        if item['status'] == 'SUCCEEDED':
                            self._sync_receipt(directory)
                        continue
                    self._recover_render(directory)
                    if not (metadata(directory, 'summary.json')).exists():
                        continue  # A setup-only directory is not a completed test.
                    summary = read_json(metadata(directory, 'summary.json'))
                    if production_pass(summary):
                        self.enqueue(directory)
                except UploadBusy:
                    continue
                except Exception as exc:
                    message = str(exc) if isinstance(exc, UploadError) else f'本地报告读取/保存异常：{type(exc).__name__}'
                    if self.diagnostics:
                        self.diagnostics.record_target(directory).event('delivery.scan_error',level='ERROR',error=exc)
                    # Never replace corrupt queue/manifest evidence to make a report uploadable.
                    self._remember(directory, dict(status='BLOCKED', attempts=0, next_attempt=self.clock()+BLOCKED_RECHECK_S,
                                                   last_error=message, discovery_error=True))
                    errors.append(f'{directory.name}：{message}')
        self.scan_error = errors[0] if errors else None
        self.next_scan = self.clock()+SCAN_SECONDS
        self._publish()

    def _correct_rejected_route(self, directory):
        """One-way repair of the previous release's wrong cloud default.

        Only known route rejections may move to the specifically requested LAN
        service. A success, lost acknowledgement or arbitrary environment change
        remains bound to its original destination. State is committed first so
        a crash between the two atomic writes can be resumed from the audit.
        """
        if self.endpoint != LOCAL_MES_URL:
            return False
        with report_lock(directory, '.upload-queue.lock'), report_lock(directory):
            path = metadata(directory, 'upload-queue.json')
            item = read_json(path)
            if item.get('endpoint') != CLOUD_MES_URL or item.get('status') == 'SUCCEEDED':
                return False
            summary = read_json(metadata(directory, 'summary.json'))
            if not production_pass(summary):
                return False
            state_path = metadata(directory, 'upload-state.json')
            state = read_json(state_path)
            destination = state.get('endpoint', CLOUD_MES_URL)
            audit = state.get('endpoint_corrections', [])
            resuming = destination == self.endpoint and any(
                record.get('from') == CLOUD_MES_URL and record.get('to') == self.endpoint
                and record.get('reason') == 'rejected-missing-route' for record in audit)
            if not resuming:
                attempts = state.get('attempts', [])
                def rejected(attempt):
                    error = attempt.get('error', '')
                    return (attempt.get('status') == 'FAILED' and
                            'No static resource' in error and 'open-api/test-report' in error)
                if (destination != CLOUD_MES_URL or state.get('status') != 'FAILED'
                        or state.get('receipt') or not attempts or not all(map(rejected, attempts))):
                    return False
            # Validate the exact original request, bytes, environment and any
            # success receipt before editing either destination record.
            plan = UploadPlan.from_directory(directory, summary,object_prefix=self.oss_target['prefix'])
            state = prepare_report(plan, destination,self.oss_target)
            if not resuming:
                state.setdefault('endpoint_corrections', []).append(dict(
                    **{'from': destination, 'to': self.endpoint}, reason='rejected-missing-route',
                    previous_error=state.get('last_error'), at=datetime.now().astimezone().isoformat()))
                for attempt in state['attempts']:
                    attempt.setdefault('endpoint', destination)
                state.update(endpoint=self.endpoint, status='PENDING', last_error=None)
                save_json(state_path, state)
            item.update(endpoint=self.endpoint, next_attempt=0, last_error=None,
                        status='SUCCEEDED' if state['status'] == 'SUCCEEDED' else 'PENDING')
            save_json(path, item)
        if self.diagnostics:
            self.diagnostics.record_target(directory).event('delivery.endpoint.corrected',
                report_key=item.get('reportKey'),previous_endpoint=CLOUD_MES_URL,endpoint=self.endpoint,
                reason='rejected-missing-route',resuming=resuming)
        self._publish(f'报告 {directory.name} 已切换到局域网 MES 补报，保留原报告标识和 OSS 回执。')
        return True

    def _sync_receipt(self, directory):
        """Repair legacy summary/workflow mirrors from the authoritative, persisted receipt."""
        with report_lock(directory):
            state = read_json(metadata(directory, 'upload-state.json'))
            receipt = state.get('receipt', {})
            request = state.get('request', {})
            if (state.get('endpoint', CLOUD_MES_URL) != self.endpoint or state.get('status') != 'SUCCEEDED' or
                    not isinstance(request, dict) or not request.get('reportKey') or not request.get('files') or
                    not receipt.get('reportId') or receipt.get('reportKey') != request.get('reportKey') or
                    receipt.get('files') != request.get('files')):
                raise UploadError('上传成功状态缺少匹配的云端回执。')
            for name in ('summary.json', 'commissioning.json'):
                path = metadata(directory, name)
                if not path.exists():
                    continue
                data = read_json(path)
                if data.get('upload', {}).get('receipt') == receipt:
                    continue
                data['upload'] = dict(status='SUCCEEDED', receipt=receipt, last_error=None)
                if name == 'commissioning.json':
                    data['phase'] = '产线流程完成'
                elif isinstance(data.get('commissioning'), dict):
                    data['commissioning']['phase'] = '产线流程完成'
                    data['commissioning']['upload'] = data['upload']
                save_json(path, data)
            intent_path = metadata(directory, 'upload-pending.json')
            if intent_path.exists():
                intent = read_json(intent_path)
                intent.update(status='SUCCEEDED', receipt=receipt, last_error=None)
                save_json(intent_path, intent)
            pending = metadata(directory, 'pending')/f'{request["reportKey"]}.json'
            pending.unlink(missing_ok=True)

    def step(self):
        if self.stop_event.is_set():
            return
        if self.endpoint_error:
            self.scan_error = self.endpoint_error
            self._publish()
            return
        if self.clock() >= self.next_scan:
            self.discover()
        with self.lock:
            candidates = sorted(self.entries.items(), key=lambda pair: (pair[1].get('next_attempt', 0), pair[0].name))
        for directory, cached in candidates:
            if (cached['status'] == 'SUCCEEDED' or cached.get('discovery_error') or
                    cached.get('next_attempt', 0) > self.clock() or self.stop_event.is_set()):
                continue
            try:
                with report_lock(directory, '.upload-queue.lock'):
                    self._process(directory)
            except UploadBusy:
                continue
            except Exception as exc:
                message = str(exc) if isinstance(exc, UploadError) else f'本地待传记录不可读：{type(exc).__name__}'
                if self.diagnostics:
                    self.diagnostics.record_target(directory).event('delivery.queue_error',level='ERROR',error=exc)
                self._remember(directory, dict(status='BLOCKED', discovery_error=True, last_error=message))
                self.active = None
                self._publish(f'报告上报受阻：{directory.name}；{message}。')
                continue
            break  # One report per iteration; a broken report cannot starve later reports.
        self._publish()

    def _process(self, directory):
        target = self.diagnostics.record_target(directory) if self.diagnostics else current_log()
        with log_scope(target):
            return self._process_record(directory)

    def _process_record(self, directory):
        path = metadata(directory, 'upload-queue.json')
        item = read_json(path)  # Another instance may have completed it since discovery.
        if item['status'] == 'SUCCEEDED' or item.get('next_attempt', 0) > self.clock():
            self._remember(directory, item)
            return
        if item.get('endpoint') != self.endpoint:
            raise UploadError('待传任务环境不匹配。')
        self.active = directory.name
        started = time.perf_counter()
        log_event('delivery.attempt',endpoint=self.endpoint,report_key=item.get('reportKey'),
                  attempt=item.get('attempts',0)+1,directory=str(directory))
        self._publish()
        previous_error = item.get('last_error')
        try:
            summary = read_json(metadata(directory, 'summary.json'))
            if not production_pass(summary):
                raise UploadError('报告不再满足真实测试 PASS 条件，已停止自动上报。')
            receipt = self.uploader(directory)
            item.update(status='SUCCEEDED', receipt=receipt, last_error=None, next_attempt=0)
            save_json(path, item)
            self._sync_receipt(directory)
            log_event('delivery.succeeded',receipt=receipt,elapsed_s=round(time.perf_counter()-started,4))
            notice = f'报告上传成功：{directory.name}，MES 编号 {receipt["reportId"]}。'
        except UploadBusy:
            self.active = None
            return
        except Exception as exc:
            retryable = isinstance(exc, UploadError) and exc.retryable
            error = str(exc) if isinstance(exc, UploadError) else f'本地记录/上传处理异常：{type(exc).__name__}'
            attempts = item.get('attempts', 0)+1
            delay = RETRY_DELAYS[min(attempts-1, len(RETRY_DELAYS)-1)] if retryable else BLOCKED_RECHECK_S
            item.update(status='RETRY' if retryable else 'BLOCKED', attempts=attempts,
                        last_error=error, next_attempt=self.clock()+delay)
            log_event('delivery.failed',level='ERROR',error=exc,status=item['status'],attempt=attempts,
                      retry_delay_s=delay,next_attempt=item['next_attempt'],endpoint=self.endpoint,
                      elapsed_s=round(time.perf_counter()-started,4))
            try:
                save_json(path, item)
            except OSError:
                self.active = None
                self._remember(directory, dict(item, status='BLOCKED', discovery_error=True))
                raise
            notice = None if error == previous_error else (
                f'报告已保存，等待网络恢复自动补传：{directory.name}。' if retryable else
                f'报告上报受阻：{directory.name}；{error} 后台将定期复查。')
        self.active = None
        self._remember(directory, item)
        self._publish(notice)


def default_roots():
    from .station_config import state_root
    roots = [state_root()/'data', Path(__file__).parent/'data',
             Path(os.environ.get('LOCALAPPDATA', str(Path.home())))/'MotorFactoryTest'/'data']
    if getattr(sys, 'frozen', False):
        roots.append(Path(sys.executable).resolve().parent/'data')
        roots.append(Path(sys.executable).resolve().parent/'MotorFactoryTest'/'data')
        roots.append(Path(sys.executable).resolve().parent/'data')  # Legacy reports beside a copied EXE.
    return tuple(dict.fromkeys(p.resolve() for p in roots))
