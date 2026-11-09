"""OSS file upload followed by the MES v2.2 report registration contract."""
from dataclasses import dataclass, field
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import argparse
import csv
import hashlib
import json
import logging
import os
from pathlib import Path
from .storage import metadata, atomic_replace
import re
import sys
import time
import tempfile
import uuid
from contextlib import contextmanager
from urllib.parse import urlsplit
from .station_config import (DEFAULT_REPORT_URL, DEFAULT_OSS_ENDPOINT,
                             DEFAULT_OSS_BUCKET, DEFAULT_OSS_REGION, DEFAULT_OSS_PREFIX)
from .service_credentials import values_for, CredentialStoreError


OSS_ENDPOINT = DEFAULT_OSS_ENDPOINT
OSS_BUCKET = DEFAULT_OSS_BUCKET
CLOUD_MES_URL = DEFAULT_REPORT_URL
LOCAL_MES_URL = 'http://192.168.1.203:8091/open-api/test-report'
MES_URL = CLOUD_MES_URL  # New work uses the documented production target; sealed reports keep their destination.
BACKOFF = (1, 5, 30, 300)


class UploadError(RuntimeError):
    def __init__(self, message, *, retryable=False, code='REPORT_UPLOAD_ERROR', hint='', context=None):
        super().__init__(message)
        self.retryable = retryable
        self.diagnostic_code = code
        self.diagnostic_hint = hint or '查看报告目录、原始错误和 reportKey；保留原文件和回执，不新建报告重传。'
        self.diagnostic_context = context or {}


def config_path():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'MotorFactoryTest' / 'upload-config.json'


def portable_config_path():
    base = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
    return base / 'upload-config.json'


def selected_config_path():
    # A release carries its own configuration. If it exists but is invalid, fail
    # explicitly instead of silently switching account/environment.
    local = config_path()
    portable = portable_config_path()
    legacy = portable.parent / 'MotorFactoryTest' / 'upload-config.json'
    return next((p for p in (portable, local, legacy) if p.exists()), local)


def configured_mes_url(path=None):
    """Read once per queue launch; credentials stay out of logs and URL errors."""
    path = Path(path) if path is not None else selected_config_path()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return MES_URL  # Missing credentials are reported by the durable uploader.
    except (OSError, ValueError, UnicodeError):
        raise UploadError(f'上传配置不可读，请检查：{path}；修改后重启软件。') from None
    if not isinstance(data, dict):
        raise UploadError(f'上传配置格式无效，请检查：{path}。')
    return validate_endpoint(data.get('mes_url', MES_URL))


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        atomic_replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class UploadBusy(UploadError):
    pass


@contextmanager
def report_lock(directory, name='.upload.lock'):
    """OS-owned lock: released on process death, never inferred from a stale PID."""
    path = metadata(directory, name)
    with path.open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise UploadBusy('报告正在由另一个任务处理。', retryable=True) from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def validate_endpoint(endpoint):
    if not isinstance(endpoint, str) or not endpoint or any(ch.isspace() for ch in endpoint):
        raise UploadError('MES 上报地址无效，请检查 upload-config.json 的 mes_url，修改后重启。')
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        raise UploadError('MES 上报地址无效。') from None
    if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or
            parts.password or parts.query or parts.fragment or parts.path in ('','/')):
        raise UploadError('MES 上报地址无效。')
    return endpoint


def storage_target(endpoint=None,bucket=None,region=None,prefix=None):
    return dict(endpoint=(OSS_ENDPOINT if endpoint is None else endpoint).rstrip('/'),
                bucket=OSS_BUCKET if bucket is None else bucket,
                region=DEFAULT_OSS_REGION if region is None else region,
                prefix=(DEFAULT_OSS_PREFIX if prefix is None else prefix).strip('/'))


def legacy_storage_target():
    return dict(endpoint=DEFAULT_OSS_ENDPOINT,bucket=DEFAULT_OSS_BUCKET,region=DEFAULT_OSS_REGION,prefix=DEFAULT_OSS_PREFIX)


def normalize_storage_target(value):
    return dict(value,prefix=value.get('prefix',DEFAULT_OSS_PREFIX))


def prepare_report(plan, endpoint, oss_target=None):
    """Caller owns the report lock. Seal hashes and destination before any network I/O."""
    state_path = metadata(plan.directory, 'upload-state.json')
    hashes = {f.key: f.sha256 for f in plan.files}
    oss_target = oss_target or storage_target()
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding='utf-8'))
        except (ValueError, UnicodeError):
            raise UploadError('上传记录损坏，已保留原文件，请维护人员检查；禁止覆盖已有 OSS 文件。') from None
        if state.get('request') != plan.request or state.get('sha256') != hashes:
            raise UploadError('此报告的文件或上报信息已改变，请使用新的报告目录，避免覆盖已上传资料。')
        previous_endpoint = state.get('endpoint', CLOUD_MES_URL)
        local_evidence = metadata(plan.directory, 'local-mes-validation.json')
        if 'endpoint' not in state and local_evidence.exists():
            previous_endpoint = json.loads(local_evidence.read_text(encoding='utf-8')).get('endpoint', previous_endpoint)
        if previous_endpoint != endpoint:
            raise UploadError('报告已绑定其他 MES 环境，禁止将本地回执当作云端成功或自动跨环境上传。')
        if normalize_storage_target(state.get('oss_target',legacy_storage_target())) != oss_target:
            raise UploadError('报告已绑定其他 OSS 目标；保留原文件和回执，不自动跨桶或跨地址补传。',
                              code='OSS_DESTINATION_MISMATCH',hint='恢复本报告原有 OSS 地址、Bucket、Region 后补传；新配置只用于新的报告。')
    else:
        state = dict(request=plan.request, sha256=hashes, uploaded={}, attempts=[], status='PENDING')
    state['endpoint'] = endpoint
    state['oss_target'] = oss_target
    receipt = state.get('receipt')
    if state.get('status') == 'SUCCEEDED':
        if (not isinstance(receipt, dict) or not receipt.get('reportId') or
                receipt.get('reportKey') != plan.request['reportKey'] or receipt.get('files') != plan.request['files']):
            raise UploadError('已上传记录缺少有效回执，请维护人员检查。')
        return state
    # Flush all three report snapshots to storage before declaring them queued.
    for file in plan.files:
        with file.path.open('r+b') as stream:
            os.fsync(stream.fileno())
    save_json(metadata(plan.directory, 'pending')/f'{plan.request["reportKey"]}.json', plan.request)
    save_json(state_path, state)
    return state


@dataclass(repr=False)
class Credentials:
    access_key_id: str
    access_key_secret: str
    mes_api_key: str

    @classmethod
    def load(cls, path=None):
        path = Path(path) if path is not None else selected_config_path()
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            values = [data[name] for name in ('access_key_id', 'access_key_secret', 'mes_api_key')]
            if not all(isinstance(v, str) and v.strip() for v in values):
                raise ValueError()
            from .diagnostics import current_log
            if current_log():
                for value in values:
                    current_log().owner.register_secret(value)
            from .diagnostics import log_event
            log_event('upload.credentials.loaded',path=str(path),
                      present_fields=['access_key_id','access_key_secret','mes_api_key'])
            return cls(*values)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            from .diagnostics import log_event
            log_event('upload.credentials.failed', '上传凭证配置读取失败', level='ERROR', error=exc,
                      path=str(path), required_fields=['access_key_id','access_key_secret','mes_api_key'])
            hint = '' if path.exists() or path != config_path() else f'；也可随程序携带：{portable_config_path()}'
            raise UploadError(f'上传配置缺失或无效，请配置：{path}{hint}', code='UPLOAD_CONFIGURATION_INVALID',
                hint='检查外置凭证 JSON 的路径、权限和三个必填密钥字段；不要把密钥粘贴到日志。') from exc

    def redact(self, message):
        for secret in (self.access_key_id, self.access_key_secret, self.mes_api_key):
            message = message.replace(secret, '[已隐藏]')
        return message


def credential_provider(config,legacy_path):
    """Freeze inline values for one delivery worker; a rotation cannot change an old worker's account."""
    try:
        inline = values_for(config)
    except CredentialStoreError as exc:
        raise UploadError(str(exc),code='UPLOAD_CONFIGURATION_INVALID') from exc
    def load():
        keys = ('access_key_id','access_key_secret','mes_api_key')
        values = {key:inline.get(key,'') for key in keys}
        if not all(values.values()):
            if Path(legacy_path).is_file():
                previous = Credentials.load(legacy_path)
                values = {key:values[key] or getattr(previous,key) for key in keys}
        if not all(values.values()):
            missing = ', '.join(key for key in keys if not values[key])
            raise UploadError('请在工位配置中填写报告/OSS 密钥：'+missing,code='UPLOAD_CONFIGURATION_INVALID',
                              hint='报告 API Key 和 OSS AccessKey 在服务接口中配置；也可以沿用原上传配置文件。')
        credentials = Credentials(**values)
        from .diagnostics import current_log
        if current_log():
            for value in values.values():
                current_log().owner.register_secret(value)
        return credentials
    return load


def import_configuration(credentials_csv, protocol_doc, output=None):
    with Path(credentials_csv).open(encoding='utf-8-sig', newline='') as stream:
        row = next(csv.DictReader(stream))
    document = Path(protocol_doc).read_text(encoding='utf-8-sig')
    key = re.search(r'X-Api-Key:\s*(xc-mes-testreport-[A-Za-z0-9]+)', document)
    if not key or not row.get('AccessKeyId') or not row.get('AccessKeySecret'):
        raise UploadError('资料中缺少 OSS 或 MES 上传凭证。')
    path = Path(output or config_path())
    save_json(path, dict(access_key_id=row['AccessKeyId'], access_key_secret=row['AccessKeySecret'],
                         mes_api_key=key.group(1)))
    return path


@dataclass
class ReportFile:
    path: Path
    key: str
    content: bytes = field(repr=False)

    @property
    def sha256(self):
        return hashlib.sha256(self.content).hexdigest()


@dataclass
class UploadPlan:
    directory: Path
    request: dict
    files: list[ReportFile]

    @classmethod
    def from_directory(cls, directory, summary=None, *, object_prefix=DEFAULT_OSS_PREFIX):
        directory = Path(directory).resolve()
        if summary is None and (metadata(directory, 'summary.json')).is_file():
            summary = json.loads((metadata(directory, 'summary.json')).read_text(encoding='utf-8'))
        report_key = directory.name
        if summary and summary.get('station_version') == 2:
            try:
                session = str(uuid.UUID(directory.name))
            except ValueError:
                raise UploadError('v2 报告目录不是有效会话 UUID。') from None
            if session != summary.get('station_session'):
                raise UploadError('v2 报告会话与目录不一致。')
            report_key = station_report_key(summary, session)
            if summary.get('report_key') != report_key or summary.get('module_sn') != summary.get('motor_sn'):
                raise UploadError('v2 正式 SN 或报告标识不匹配。')
        if not re.fullmatch(r'[A-Za-z0-9_]{10,128}', report_key):
            raise UploadError('报告标识须为 10～128 位字母、数字或下划线。')
        match = re.fullmatch(r'(\d{8}_\d{6})_([A-Za-z0-9]{26})(?:_\d+)?', report_key)
        if not match:
            raise UploadError('报告目录名须为“yyyyMMdd_HHmmss_26位SN”（允许末尾重复序号）。')
        stamp, sn = match.groups()
        try:
            test_time = datetime.strptime(stamp, '%Y%m%d_%H%M%S')
        except ValueError:
            raise UploadError('报告目录中的测试时间无效。') from None
        if summary and summary.get('motor_sn') != sn:
            raise UploadError('报告中的 SN 与目录名不一致，未上传。')
        files = []
        root=object_prefix.strip('/')
        prefix = (root+'/' if root else '')+f'{test_time:%Y%m}/{sn}/{report_key}'
        for choices in (('report.png',), ('kt/kt_curve.csv', 'kt_curve.csv'), ('tn/curve.csv', 'curve.csv')):
            path = next((directory/name for name in choices if (directory/name).is_file()), None)
            if path is None:
                raise UploadError(f'缺少报告文件：{choices[0]}。需要 1 个 PNG 和 2 个 CSV。')
            content = path.read_bytes()  # One immutable snapshot for hashing and upload retries.
            if not content:
                raise UploadError(f'报告文件为空：{path.name}')
            files.append(ReportFile(path, f'{prefix}/{path.name}', content))
        body = dict(sn=sn, reportKey=report_key, testTime=f'{test_time:%Y-%m-%d %H:%M:%S}',
                    files=[f.key for f in files], fileCount=len(files))
        intent_path = metadata(directory, 'upload-pending.json')
        if summary and summary.get('station_version') == 2 and intent_path.exists():
            intent = json.loads(intent_path.read_text(encoding='utf-8'))
            if intent.get('request') != body or intent.get('sha256') != {f.key:f.sha256 for f in files}:
                raise UploadError('待传报告的文件或上报信息已改变，保留原记录，禁止上传。')
        return cls(directory, body, files)


def station_report_key(summary, session):
    """Keep the proven MES identifier format without renaming a v2 workpiece folder."""
    sn = summary.get('motor_sn', '')
    if not isinstance(sn, str) or not re.fullmatch(r'[A-Z0-9]{26}', sn):
        raise UploadError('v2 报告必须关联完整的正式 SN。')
    try:
        started = datetime.fromisoformat(summary['started_at'])
        if started.tzinfo is None:
            raise ValueError('missing timezone')
        stamp = started.astimezone(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S')
        suffix = uuid.UUID(session).int
    except (KeyError, ValueError, TypeError):
        raise UploadError('v2 报告的测试时间或会话标识无效。') from None
    return f'{stamp}_{sn}_{suffix}'


class OssStore:
    def __init__(self, credentials, *, endpoint=None,bucket=None,region=None,prefix=None):
        import oss2
        # SDK debug output must never expose signing material in the startup log.
        logging.getLogger('oss2').setLevel(logging.WARNING)
        self.target = storage_target(endpoint,bucket,region,prefix)
        self.bucket = oss2.Bucket(oss2.AuthV4(credentials.access_key_id, credentials.access_key_secret),
            self.target['endpoint'], self.target['bucket'], region=self.target['region'], connect_timeout=10)

    def put(self, file):
        from .diagnostics import log_event, response_evidence
        started = time.perf_counter()
        log_event('oss.put',key=file.key,bytes=len(file.content),sha256=file.sha256,
                  endpoint=self.target['endpoint'],bucket=self.target['bucket'],region=self.target['region'],source_path=str(file.path))
        try:
            response = self.bucket.put_object(file.key, file.content, headers={
                'Content-Type': 'image/png' if file.path.suffix.lower() == '.png' else 'text/csv'})
        except Exception as exc:
            log_event('oss.exception',level='ERROR',error=exc,key=file.key,
                      elapsed_s=round(time.perf_counter()-started,4),status=getattr(exc,'status',None),
                      oss_code=getattr(exc,'code',None),request_id=getattr(exc,'request_id',None),
                      server_details=getattr(exc,'details',None),
                      **response_evidence(getattr(exc,'body','') or ''))
            status = getattr(exc, 'status', None)
            code = getattr(exc, 'code', None) or type(exc).__name__
            # Retrying a lost ACK puts the same immutable bytes at the same key.
            raise UploadError(f'OSS 上传失败：{code}（HTTP {status or "网络异常"}）。',
                retryable=status is None or status in (408, 429) or status >= 500,
                code='OSS_PUT_FAILED',hint='按 OSS request_id 核对鉴权、桶权限及网络，重试继续使用原 key 和原始字节。') from exc
        if response.status != 200:
            log_event('oss.http_error',level='ERROR',http_status=response.status,key=file.key,
                      request_id=getattr(response,'request_id',None),elapsed_s=round(time.perf_counter()-started,4))
            raise UploadError(f'OSS 返回异常状态：{response.status}', retryable=response.status >= 500,
                              code='OSS_HTTP_ERROR',hint='核对 OSS 状态及 request_id 对应的服务端记录。')
        receipt = dict(etag=response.etag, request_id=response.request_id)
        log_event('oss.confirmed',key=file.key,receipt=receipt,elapsed_s=round(time.perf_counter()-started,4))
        return receipt


class MesClient:
    def __init__(self, credentials, endpoint=None, *, api_key_header='X-Api-Key'):
        self.credentials = credentials
        self.endpoint = validate_endpoint(MES_URL if endpoint is None else endpoint)
        self.api_key_header = api_key_header

    def register(self, body):
        import requests
        from .diagnostics import current_log, log_event, response_evidence
        if current_log():
            current_log().owner.register_secret(self.credentials.mes_api_key)
        started = time.perf_counter()
        log_event('report_mes.submit',endpoint=self.endpoint,request=body)
        try:
            response = requests.post(self.endpoint, json=body, headers={
                'Content-Type': 'application/json;charset=UTF-8',
                self.api_key_header: self.credentials.mes_api_key}, timeout=(5, 10), allow_redirects=False)
        except requests.RequestException as exc:
            log_event('report_mes.network_error',level='ERROR',error=exc,endpoint=self.endpoint,
                      elapsed_s=round(time.perf_counter()-started,4),report_key=body.get('reportKey'))
            raise UploadError(f'MES 网络请求失败：{type(exc).__name__}', retryable=True,
                code='REPORT_MES_NETWORK',hint='按 reportKey 核对网络和服务日志；响应丢失时复用原 reportKey 查询/重试。') from exc
        raw = getattr(response,'content',b'')
        headers = getattr(response,'headers',{})
        log_event('report_mes.http',endpoint=self.endpoint,http_status=response.status_code,
                  elapsed_s=round(time.perf_counter()-started,4),
                  **response_evidence(raw if isinstance(raw,(str,bytes)) else '',
                                      headers if isinstance(headers,Mapping) else {}))
        if response.status_code != 200:
            raise UploadError(f'MES HTTP 状态异常：{response.status_code}',
                retryable=response.status_code in (408, 429) or response.status_code >= 500,
                code='REPORT_MES_HTTP',hint='检查 HTTP 状态、响应正文、服务地址和响应中的 request-id。')
        try:
            result = response.json()
        except ValueError as exc:
            log_event('report_mes.invalid_json',level='ERROR',error=exc,endpoint=self.endpoint)
            raise UploadError('MES 响应不是有效 JSON。', retryable=True,code='REPORT_MES_INVALID_JSON',
                hint='检查响应正文是否为代理、登录页或服务错误页，确认报告接口地址。') from exc
        if not isinstance(result, dict):
            raise UploadError('MES 响应格式错误。', retryable=True)
        code = result.get('code')
        log_event('report_mes.reply',response=result)
        if code != 200:
            message = self.credentials.redact(str(result.get('msg', '未提供错误说明')))[:1000]
            parameter_error = any(word in message for word in (
                'SN', 'sn', '参数', '唯一键', '不能为空', '时间格式', '文件路径', '文件类型',
                'fileCount', '文件数', '文件数量', '不合法', '不一致', '格式', '必填',
                'No static resource', 'NoResourceFoundException', '接口不存在', '路径不存在'))
            retryable = code == 500 and not parameter_error
            raise UploadError(f'MES 拒绝上报（code={code}）：{message}', retryable=retryable)
        data = result.get('data')
        if not isinstance(data, dict) or data.get('reportKey') != body['reportKey'] or not data.get('reportId'):
            raise UploadError('MES 成功响应缺少匹配的报告凭据。', retryable=True,code='REPORT_RECEIPT_MISMATCH',
                hint='核对响应 data.reportKey 与本次请求一致，且存在 reportId；不可仅凭 HTTP 200 判为上传成功。',
                context=dict(expected_report_key=body['reportKey'],response=result))
        return data


class ReportUploader:
    def __init__(self, *, config=None, progress=lambda message: None, max_attempts=3,
                 store=None, mes=None, sleep=time.sleep, endpoint=None,
                 credentials_provider=None,oss_endpoint=None,oss_bucket=None,oss_region=None,oss_prefix=None,
                 mes_api_key_header='X-Api-Key'):
        self.config, self.progress = config, progress
        self.max_attempts = max(1, min(10, max_attempts))
        self.store, self.mes, self.sleep = store, mes, sleep
        self.credentials_provider = credentials_provider
        self.mes_api_key_header = mes_api_key_header
        self.oss_target = storage_target(oss_endpoint,oss_bucket,oss_region,oss_prefix)
        if isinstance(store,OssStore):
            if all(value is None for value in (oss_endpoint,oss_bucket,oss_region,oss_prefix)):
                self.oss_target = dict(store.target)
            elif self.oss_target != store.target:
                raise UploadError('上传任务与 OSS 客户端目标不一致。')
        self.endpoint = configured_mes_url(config) if endpoint is None else validate_endpoint(endpoint)
        if isinstance(mes, MesClient) and mes.endpoint != self.endpoint:
            raise UploadError('上传任务与 MES 客户端地址不一致。')

    def __call__(self, directory, summary=None):
        with report_lock(directory):
            return self._upload(directory, summary)

    def _upload(self, directory, summary=None):
        from .diagnostics import log_event
        started = time.perf_counter()
        log_event('upload.prepare',directory=str(directory),endpoint=self.endpoint)
        plan = UploadPlan.from_directory(directory, summary,object_prefix=self.oss_target['prefix'])
        state_path = metadata(plan.directory, 'upload-state.json')
        pending = metadata(plan.directory, 'pending')/f'{plan.request["reportKey"]}.json'
        state = prepare_report(plan, self.endpoint,self.oss_target)
        log_event('upload.plan',directory=str(plan.directory),endpoint=self.endpoint,
                  report_key=plan.request['reportKey'],sn=plan.request['sn'],status=state['status'],
                  files=[dict(path=str(f.path),key=f.key,bytes=len(f.content),sha256=f.sha256) for f in plan.files])
        if state['status'] == 'SUCCEEDED':
            try:
                pending.unlink(missing_ok=True)
            except OSError as exc:
                log_event('upload.pending_cleanup.failed',level='WARNING',error=exc,path=str(pending))
            log_event('upload.receipt.reused',receipt=state['receipt'],report_key=plan.request['reportKey'])
            return state['receipt']
        try:
            credentials = ((self.credentials_provider() if self.credentials_provider else Credentials.load(self.config))
                           if self.store is None or self.mes is None else None)
            store = self.store or OssStore(credentials,**self.oss_target)
            mes = self.mes or MesClient(credentials, self.endpoint,api_key_header=self.mes_api_key_header)
            for attempt_number in range(self.max_attempts):
                attempt = dict(started_at=datetime.now().astimezone().isoformat(), status='UPLOADING',
                               endpoint=self.endpoint)
                state['attempts'].append(attempt)
                state['attempts'] = state['attempts'][-100:]
                state.update(status='UPLOADING', last_error=None)
                save_json(state_path, state)
                attempt_started = time.perf_counter()
                log_event('upload.attempt',attempt=attempt_number+1,max_attempts=self.max_attempts,
                          endpoint=self.endpoint,report_key=plan.request['reportKey'])
                try:
                    for index, file in enumerate(plan.files, 1):
                        if file.key not in state['uploaded']:
                            self.progress(f'上传文件 {index}/3：{file.path.name}')
                            state['uploaded'][file.key] = store.put(file)
                            save_json(state_path, state)
                        else:
                            log_event('upload.file.reused',key=file.key,sha256=file.sha256,
                                      receipt=state['uploaded'][file.key])
                    self.progress('文件已上传，正在登记 MES…')
                    data = mes.register(plan.request)
                    receipt = dict(destination='OSS+MES', endpoint=self.endpoint, bucket=self.oss_target['bucket'],
                        reportId=data['reportId'], reportKey=data['reportKey'], duplicated=bool(data.get('duplicated')),
                        sn=plan.request['sn'], files=plan.request['files'])
                    attempt.update(status='SUCCEEDED', finished_at=datetime.now().astimezone().isoformat())
                    state.update(status='SUCCEEDED', receipt=receipt, last_error=None)
                    save_json(state_path, state)
                    try:
                        pending.unlink(missing_ok=True)  # Never delete report files.
                    except OSError as exc:
                        log_event('upload.pending_cleanup.failed',level='WARNING',error=exc,path=str(pending))
                    log_event('upload.confirmed',receipt=receipt,
                              elapsed_s=round(time.perf_counter()-started,4))
                    self.progress(f'上传成功，MES 报告编号：{data["reportId"]}')
                    return receipt
                except UploadError as exc:
                    log_event('upload.attempt.failed',level='ERROR',error=exc,attempt=attempt_number+1,
                              report_key=plan.request['reportKey'],retryable=exc.retryable,
                              elapsed_s=round(time.perf_counter()-attempt_started,4))
                    attempt.update(status='FAILED', error=str(exc))
                    state.update(status='FAILED', last_error=str(exc))
                    save_json(state_path, state)
                    if not exc.retryable or attempt_number + 1 >= self.max_attempts:
                        raise
                    delay = BACKOFF[min(attempt_number, len(BACKOFF)-1)]
                    log_event('upload.retry',delay_s=delay,attempt=attempt_number+1,
                              report_key=plan.request['reportKey'],endpoint=self.endpoint)
                    self.progress(f'上传暂未完成，{delay} 秒后重试…')
                    self.sleep(delay)
        except Exception as exc:
            log_event('upload.failed', '报告上传未完成，本地文件与恢复状态保留',level='ERROR',error=exc,
                      endpoint=self.endpoint,report_key=plan.request['reportKey'],directory=str(directory),
                      uploaded_keys=list(state['uploaded']),elapsed_s=round(time.perf_counter()-started,4))
            message = str(exc) if isinstance(exc, UploadError) else f'上传处理中断：{type(exc).__name__}'
            state.update(status='FAILED', last_error=message)
            save_json(state_path, state)
            raise UploadError(message + '；本地 PNG、CSV 和待传记录已保留。',
                retryable=getattr(exc, 'retryable', False),code=getattr(exc,'diagnostic_code','REPORT_UPLOAD_ERROR'),
                hint=getattr(exc,'diagnostic_hint','检查原始异常、报告目录和持久化状态。')) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description='直接上传报告目录到 OSS 和 MES，无需连接电机或测试 PASS。')
    parser.add_argument('directory', nargs='?')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--credentials-csv', type=Path)
    parser.add_argument('--protocol-doc', type=Path)
    parser.add_argument('--attempts', type=int, default=3)
    parser.add_argument('--mes-url', help='显式指定联调 MES 地址；报告绑定该环境，不自动迁移。')
    args = parser.parse_args(argv)
    try:
        if args.credentials_csv or args.protocol_doc:
            if not (args.credentials_csv and args.protocol_doc):
                parser.error('导入配置需要同时提供 --credentials-csv 和 --protocol-doc')
            path = import_configuration(args.credentials_csv, args.protocol_doc, args.config)
            print(f'上传配置已保存：{path}（凭证不写入日志或发布包）')
        if args.directory:
            ReportUploader(config=args.config, progress=print, max_attempts=args.attempts,
                           endpoint=args.mes_url)(args.directory)
        elif not args.credentials_csv:
            parser.error('请指定报告目录')
        return 0
    except UploadError as exc:
        print(f'上传失败：{exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
