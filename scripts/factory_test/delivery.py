"""Bridge v2 workpieces to the original OSS/MES queue; no motor/serial dependency."""
import json
from pathlib import Path
import threading
import time

from .storage import metadata
from .upload import (UploadError, UploadPlan, ReportUploader, report_lock, save_json, selected_config_path,
                     validate_endpoint, storage_target, legacy_storage_target, normalize_storage_target, credential_provider)
from .upload_queue import UploadQueue, production_pass, read_json


class ReportDelivery:
    def __init__(self, root, config, notify=lambda value: None, *, uploader=None, diagnostics=None):
        self.root = Path(root)/'data'
        self.config, self.notify = config, notify
        self.diagnostics = diagnostics
        self.stop_event = threading.Event()
        self.thread = None
        self.queue = None
        self.last_snapshot = None
        self.next_scan = 0
        self.error = None
        self.config_path = Path(config.upload_config).expanduser() if config.upload_config else selected_config_path()
        self.oss_target = storage_target(config.oss_endpoint,config.oss_bucket,config.oss_region,config.oss_prefix)
        try:
            endpoint = config.report_upload_url.strip()
            if not endpoint:
                try:
                    value = json.loads(self.config_path.read_text(encoding='utf-8'))
                    endpoint = value.get('mes_url', '') if isinstance(value, dict) else ''
                except (OSError, ValueError, UnicodeError):
                    endpoint = ''
            if not endpoint:
                raise UploadError('请配置报告上报地址或外置 upload-config.json 的 mes_url；报告已保存在本地')
            endpoint = validate_endpoint(endpoint)
            uploader = uploader or ReportUploader(config=self.config_path,endpoint=endpoint,max_attempts=1,
                credentials_provider=credential_provider(config,self.config_path),
                oss_endpoint=config.oss_endpoint,oss_bucket=config.oss_bucket,oss_region=config.oss_region,oss_prefix=config.oss_prefix,
                mes_api_key_header=config.report_api_key_header)
            self.queue = UploadQueue([self.root], endpoint=endpoint, config=self.config_path,
                                     notify=notify, uploader=uploader, diagnostics=diagnostics,oss_target=self.oss_target)
        except UploadError as exc:
            self.error = str(exc)
            if diagnostics:
                diagnostics.station.event('delivery.configuration', self.error, level='WARNING', error=exc)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name='v2-report-delivery', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.queue:
            self.queue.stop()

    def _seal(self, directory):
        directory = Path(directory).resolve()
        if directory.parent != self.root.resolve():
            raise UploadError('报告不属于当前工位的数据目录。')
        with report_lock(directory, '.delivery-intent.lock'):
            summary = read_json(metadata(directory, 'summary.json'))
            if not production_pass(summary):
                raise UploadError('只有真实、完成写号打印并测试通过的报告才能上报。')
            plan = UploadPlan.from_directory(directory, summary,object_prefix=self.config.oss_prefix)
            path = metadata(directory, 'upload-pending.json')
            if path.exists() and normalize_storage_target(read_json(path).get('oss_target',legacy_storage_target()))!=self.oss_target:
                raise UploadError('待传报告已绑定其他 OSS 目标，不自动迁移。',code='OSS_DESTINATION_MISMATCH')
            if not path.exists():
                save_json(path, dict(status='PENDING', request=plan.request,
                                    sha256={f.key:f.sha256 for f in plan.files},oss_target=self.oss_target))
            return path

    def enqueue(self, directory):
        directory = Path(directory).resolve()
        intent_path = self._seal(directory)
        if self.queue is not None:
            return self.queue.enqueue(directory)
        intent = read_json(intent_path)
        intent.update(status='BLOCKED_CONFIGURATION', last_error=self.error)
        save_json(intent_path, intent)
        if self.diagnostics:
            self.diagnostics.record_target(directory).event('delivery.blocked',self.error,level='WARNING',intent=intent)
        self._publish_blocked()
        return dict(status='BLOCKED_CONFIGURATION', last_error=self.error)

    def _publish_blocked(self):
        blocked = 0
        errors = []
        for directory in self.root.iterdir() if self.root.exists() else ():
            if not directory.is_dir():
                continue
            path = metadata(directory, 'summary.json')
            if not path.exists():
                continue
            try:
                summary = read_json(path)
                if not production_pass(summary):
                    continue
                state_path = metadata(directory, 'upload-state.json')
                if state_path.exists() and read_json(state_path).get('status') == 'SUCCEEDED':
                    continue
                self._seal(directory)
                blocked += 1
            except Exception as exc:
                if self.diagnostics:
                    self.diagnostics.record_target(directory).event('delivery.intent_error',level='ERROR',error=exc)
                blocked += 1
                errors.append(str(exc))
        payload = dict(pending=0, blocked=blocked, succeeded=0, active=None,
                       error=errors[0] if errors else self.error, reason=self.error, notice=None, accepted=None)
        if payload != self.last_snapshot:
            self.last_snapshot = dict(payload)
            self.notify(payload)
        self.next_scan = time.monotonic()+30

    def step(self):
        if self.queue:
            self.queue.step()
        elif time.monotonic() >= self.next_scan:
            self._publish_blocked()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.step()
            except Exception as exc:
                if self.diagnostics:
                    self.diagnostics.station.event('delivery.exception',level='ERROR',error=exc)
                self.notify(dict(pending=0, blocked=1, succeeded=0, active=None,
                                 error=f'报告后台异常：{type(exc).__name__}；本地记录保留',
                                 reason=None, notice=None, accepted=None))
            self.stop_event.wait(1)
