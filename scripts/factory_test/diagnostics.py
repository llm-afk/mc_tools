"""Connection-scoped diagnostics. Retention owns logs only, never production data."""
from contextlib import contextmanager
from contextvars import ContextVar
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
import json
import errno
import math
import os
from pathlib import Path
import platform
import re
import stat
import sys
import threading
import tempfile
import time
import traceback
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid
import zipfile
from urllib.error import HTTPError, URLError
from .version import VERSION
from .station_config import READINESS_LABELS


RETENTION_DAYS = 30
LOCAL_TIME = timezone(timedelta(hours=8))
OWNED_LOG = re.compile(r'(?:motor-\d{8}-\d{6}-[0-9a-f]{32}|station-\d{8})\.log')
_target = ContextVar('factory_diagnostic_target', default=None)
_secret_key = re.compile(r'(?i)^(?:x[-_]?api[-_]?key|(?:(?:mes|sn|report)[-_])?api[-_]?key|(?:oss[-_]?)?access[-_]?key(?:[-_]?(?:id|secret))?|protected_credentials|secret|password|authorization|proxy[-_]authorization|cookie|set[-_]cookie|token|signature|security[-_]?token|x[-_]oss[-_]security[-_]token)$')
BODY_PREVIEW_CHARS = 16384


def response_evidence(body, headers=None):
    """Keep non-JSON/HTTP-error evidence too; bound previews, never silently truncate."""
    raw = body if isinstance(body, bytes) else str(body).encode('utf-8', errors='replace')
    text = raw.decode('utf-8-sig', errors='replace')
    target = current_log()
    if target is not None:
        text = target.owner.redact(text)
    allowed = {'content-type', 'content-length', 'date', 'server', 'retry-after',
               'x-request-id', 'x-correlation-id', 'x-trace-id', 'traceparent', 'location',
               'x-oss-request-id'}
    selected = {str(k): str(v) for k, v in (headers or {}).items() if str(k).lower() in allowed}
    return dict(response_headers=selected, response_bytes=len(raw),
                response_preview=text[:BODY_PREVIEW_CHARS],
                response_truncated=len(text) > BODY_PREVIEW_CHARS)


def error_details(error):
    """Facts plus a check direction; unknown faults remain unknown, with their stack."""
    chain, seen, current = [], set(), error
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        item = dict(type=type(current).__name__, module=type(current).__module__, message=str(current))
        for key in ('errno', 'winerror', 'filename', 'filename2', 'code', 'status', 'url',
                    'reason', 'request_id', 'retryable', 'diagnostic_code', 'diagnostic_hint',
                    'diagnostic_context', 'length', 'dlc', 'channel', 'flags', 'can_id', 'payload_hex', '__notes__'):
            value = getattr(current, key, None)
            if value is not None:
                item[key.strip('_')] = str(value) if isinstance(value, BaseException) else value
        chain.append(item)
        reason = getattr(current, 'reason', None)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        if current is None and isinstance(reason, BaseException) and id(reason) not in seen:
            current = reason
    code = getattr(error, 'diagnostic_code', None)
    hint = getattr(error, 'diagnostic_hint', None)
    windows_codes = [item.get('winerror') for item in chain]
    errnos = [item.get('errno') for item in chain]
    types = {item['type'] for item in chain}
    if not code:
        if isinstance(error, HTTPError):
            code, hint = 'HTTP_STATUS_ERROR', '检查 HTTP 状态、响应正文、目标地址及服务端日志；HTTP 成功和业务成功需分别核对。'
        elif 'CanBusError' in types:
            code, hint = 'CAN_ERROR_FRAME', '核对错误帧 ID 和数据、最后一次电机反馈、使能请求及失能回执。'
        elif 'UsbFrameError' in types:
            code, hint = 'USB_FRAME_INVALID', '主机侧 USB 帧格式校验失败；检查长度、DLC、通道、flags 和原始数据，以及适配器固件/占用情况。'
        elif 'SdoError' in types:
            code, hint = 'SDO_RESPONSE_ERROR', '核对 SDO 索引、请求、应答及 abort code；事务结果不明时不接续写操作。'
        elif types & {'SSLError', 'SSLCertVerificationError'}:
            code, hint = 'TLS_ERROR', '检查电脑时间、证书链及 HTTPS 地址；保留证书验证，不关闭验证。'
        elif 'gaierror' in types:
            code, hint = 'DNS_ERROR', '检查服务域名、DNS 和网络配置。'
        elif types & {'ConnectionRefusedError'} or errno.ECONNREFUSED in errnos or 10061 in windows_codes:
            code, hint = 'CONNECTION_REFUSED', '目标端口拒绝连接；检查服务进程是否启动及地址、端口是否正确。'
        elif types & {'TimeoutError', 'SdoReplyTimeout', 'MotorAbsent', 'Timeout', 'ReadTimeout', 'ConnectTimeout'} or errno.ETIMEDOUT in errnos or 10060 in windows_codes:
            code, hint = 'TIMEOUT', '在期限内未收到确认；检查链路和设备/服务日志。超时本身不能证明命令未执行。'
        elif isinstance(error, PermissionError) or errno.EACCES in errnos or any(n in windows_codes for n in (5, 32)):
            code, hint = 'ACCESS_DENIED_OR_BUSY', '检查目录权限、文件是否被占用，以及设备是否被其他程序占用。'
        elif errno.ENOSPC in errnos or 112 in windows_codes:
            code, hint = 'DISK_FULL', '检查日志和报告目录所在磁盘的剩余空间。'
        elif isinstance(error, FileNotFoundError):
            code, hint = 'FILE_NOT_FOUND', '检查记录中的文件路径、配置文件和项目目录是否完整。'
        elif isinstance(error, (InterruptedError, KeyboardInterrupt)):
            code, hint = 'CANCELLED', '流程已请求停止；核对收尾日志中的失能和资源释放结果。'
        elif isinstance(error, URLError):
            code, hint = 'HTTP_TRANSPORT_ERROR', '检查 reason 中的底层网络错误、服务地址、代理和服务进程。'
        else:
            code, hint = 'UNEXPECTED_EXCEPTION', '按阶段、异常链和完整堆栈定位；本错误未被归为已知原因。'
    return dict(code=code, message=str(error), hint=hint or '查看该阶段的上下文和异常链。',
                exception_chain=chain)


@contextmanager
def log_scope(target):
    token = _target.set(target)
    try:
        yield target
    finally:
        _target.reset(token)


def current_log():
    return _target.get()


def log_event(event, message='', *, level='INFO', error=None, **details):
    target = current_log()
    if target is not None:
        target.event(event, message, level=level, error=error, **details)


class LogTarget:
    def __init__(self, owner, path, context=None, *, create=True):
        self.owner, self.path = owner, Path(path)
        self.context, self.create = context or {}, create

    @property
    def reference(self):
        return self.path.relative_to(self.owner.root).as_posix()

    def bind(self, **context):
        return LogTarget(self.owner, self.path, {**self.context, **context}, create=self.create)

    def event(self, event, message='', *, level='INFO', error=None, **details):
        self.owner.append(self, event, message, level=level, error=error,
                          details={**self.context, **details})

    def sample(self, event, message='', *, interval=1.0, **details):
        key = (self.reference, event)
        with self.owner.lock:
            now = self.owner.monotonic()
            if now-self.owner.samples.get(key, float('-inf')) < interval:
                return
            self.owner.samples[key] = now
        self.event(event, message, **details)


class DiagnosticLogs:
    def __init__(self, storage_root, *, simulation=False, clock=time.time,
                 monotonic=time.monotonic, failure=lambda message: None):
        self.root = Path(storage_root)/'logs'
        self.simulation, self.clock, self.monotonic = simulation, clock, monotonic
        self.failure = failure
        self.lock = threading.RLock()
        self.boot_id = uuid.uuid4().hex
        self.started = monotonic()
        self.active = self.latest = None
        self.identity = None
        self.absent_since = None
        self.connected_at = None
        self.next_cleanup = 0
        self.last_error = None
        self.samples, self.snapshots = {}, {}
        self.probes = {}
        self.pending_probes = set()
        self.station_probe_file = None
        self.secrets = set()
        self.sequence = 0
        self.boot_files = {}
        self.closed = False
        self.station.event('application.start', '上位机启动', runtime=self.runtime_info(),
                           retention_days=RETENTION_DAYS, log_directory=str(self.directory()))
        self.cleanup(force=True)

    def directory(self, simulation=None):
        simulation = self.simulation if simulation is None else simulation
        return self.root/('preview' if simulation else 'production')

    @property
    def station(self):
        date = datetime.fromtimestamp(self.clock(), LOCAL_TIME).strftime('%Y%m%d')
        return LogTarget(self, self.directory()/f'station-{date}.log')

    @property
    def current(self):
        with self.lock:
            return self.active or self.station

    def runtime_info(self):
        result = dict(version=VERSION, channel='production', python=sys.version, platform=platform.platform(),
                      executable=sys.executable, frozen=bool(getattr(sys, 'frozen', False)),
                      pid=os.getpid(), working_directory=str(Path.cwd()), arguments=sys.argv)
        if result['frozen']:
            try:
                manifest = json.loads((Path(sys.executable).parent/'release-manifest.json').read_text(encoding='utf-8'))
                if isinstance(manifest, dict):
                    result.update({key: manifest.get(key) for key in ('built_at', 'sha256')})
            except (OSError, ValueError):
                pass
        return result

    def register_secret(self, value):
        if isinstance(value, str) and len(value) >= 4:
            with self.lock:
                self.secrets.add(value)

    def redact(self, value):
        if is_dataclass(value):
            value = asdict(value)
        if isinstance(value, dict):
            return {str(k): '[REDACTED]' if _secret_key.fullmatch(str(k)) else self.redact(v)
                    for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        if not isinstance(value, str):
            return value
        for secret in tuple(self.secrets):
            value = value.replace(secret, '[REDACTED]')
        def clean_url(match):
            try:
                parts = urlsplit(match.group())
                host = parts.netloc.rpartition('@')[-1]
                query = urlencode([(k, '[REDACTED]' if _secret_key.fullmatch(k) or
                    'signature' in k.lower() or 'credential' in k.lower() else v)
                    for k, v in parse_qsl(parts.query, keep_blank_values=True)])
                return urlunsplit((parts.scheme, host, parts.path, query, ''))
            except ValueError:
                return '[REDACTED_URL]'
        value = re.sub(r'https?://[^\s<>"\']+', clean_url, value)
        value = re.sub(r'(?i)(Bearer\s+)\S+', r'\1[REDACTED]', value)
        value = re.sub(r'(?i)((?:X-Api-Key|api_key|access_key_secret|password|token)\s*["\']?\s*[:=]\s*["\']?)[^\s,"\'}]+',
                       r'\1[REDACTED]', value)
        return value

    def _owned(self, path):
        """No recursive removal, no symlinks/junctions, no arbitrary record path."""
        path = Path(path)
        if path.parent not in (self.root/'production', self.root/'preview') or not OWNED_LOG.fullmatch(path.name):
            return False
        for item in (self.root, path.parent, path):
            if item.exists() or item.is_symlink():
                info = item.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
                    return False
        return True

    def append(self, target, event, message, *, level, error, details):
        with self.lock:
            try:
                if not self._owned(target.path):
                    raise OSError('日志路径不属于工位日志目录，或路径包含链接')
                if not target.create and not target.path.exists():
                    # A delayed upload must not resurrect a log already expired by retention.
                    target = self.station
                    details['expired_connection_log'] = details.get('connection_log', '')
                    if not self._owned(target.path):
                        raise OSError('当天工位日志路径包含链接')
                target.path.parent.mkdir(parents=True, exist_ok=True)
                stamp = datetime.fromtimestamp(self.clock(), LOCAL_TIME).isoformat(timespec='milliseconds')
                self.sequence += 1
                if error is not None:
                    details.setdefault('diagnostic', error_details(error))
                context = self.redact(dict(schema_version=2, sequence=self.sequence, boot_id=self.boot_id,
                    log_file=target.reference, thread=threading.current_thread().name,
                    uptime_s=round(self.monotonic()-self.started, 3), **details))
                display = self.redact(str(message)).replace('\r',r'\r').replace('\n',r'\n')
                line = f'{stamp} {level:7} [{event}] {display}'
                line += ' '+json.dumps(context, ensure_ascii=False, default=str, allow_nan=False)+'\n'
                if error is not None:
                    line += self.redact(''.join(traceback.format_exception(type(error), error, error.__traceback__)))
                    if not line.endswith('\n'):
                        line += '\n'
                with target.path.open('a', encoding='utf-8') as stream:
                    if target.path not in self.boot_files:
                        self.boot_files[target.path] = stream.tell()
                    stream.write(line)
                    if error is not None or level == 'ERROR' or event in ('connection.open', 'connection.close', 'flow.step.done', 'flow.start'):
                        stream.flush()
                        os.fsync(stream.fileno())
                self.last_error = None
            except Exception as exc:
                message = '诊断日志保存失败：'+str(exc)
                if message != self.last_error:
                    self.last_error = message
                    try:
                        self.failure(message)
                    except Exception:
                        pass  # Diagnostics must not prevent motor cleanup.

    def observe(self, present, *, identity=None, config=None, absence_seconds=2.0):
        with self.lock:
            if self.closed:
                return None
            if config is not None and self.simulation != config.simulation:
                self.finish_connection('mode_changed')
                self.simulation = config.simulation
            now = self.monotonic()
            if present is None:
                self.absent_since = None  # An inconclusive probe interrupts the evidence of continuous absence.
                return self.active
            if present:
                if self.active is None:
                    self.flush_probes(self.station)
                    stamp = datetime.fromtimestamp(self.clock(), LOCAL_TIME).strftime('%Y%m%d-%H%M%S')
                    connection_id = uuid.uuid4().hex
                    self.active = LogTarget(self, self.directory()/f'motor-{stamp}-{connection_id}.log',
                                            dict(connection_id=connection_id, mode='preview' if self.simulation else 'production'))
                    self.latest = self.active
                    self.connected_at = now
                    self.identity = identity
                    self.active.event('connection.open', '识别到电机，开始记录连接周期', identity=identity,
                                      config=config, runtime=self.runtime_info())
                    self.station.event('connection.index', '电机连接日志', connection_log=self.active.reference, identity=identity)
                elif self.absent_since is not None:
                    self.active.event('connection.restored', '短暂通信中断后恢复', gap_s=round(now-self.absent_since, 3), identity=identity)
                if self.identity != identity:
                    self.active.event('connection.identity', '连接身份更新', previous=self.identity, identity=identity)
                    self.identity = identity
                self.absent_since = None
            elif self.active is not None:
                if self.absent_since is None:
                    self.absent_since = now
                    self.active.event('connection.lost', '暂未收到电机响应，等待确认断开', level='WARNING')
                elif now-self.absent_since >= absence_seconds:
                    self.finish_connection('disconnect_confirmed')
            return self.active

    def finish_connection(self, reason):
        with self.lock:
            if self.active is not None:
                target = self.active
                self.flush_probes(target)
                target.event('connection.close', '结束连接周期记录', reason=reason,
                             duration_s=round(self.monotonic()-self.connected_at, 3), identity=self.identity)
                self.station.event('connection.closed', '电机连接周期结束', connection_log=target.reference, reason=reason)
                self.latest = target
                self.samples = {key:value for key,value in self.samples.items() if key[0] != target.reference}
                self.snapshots = {key:value for key,value in self.snapshots.items() if key[0] != target.reference}
            self.active = None
            self.identity = None
            self.absent_since = self.connected_at = None

    def snapshot(self, event, value, *, target=None):
        target = target or self.current
        key = (target.reference, event)
        clean = self.redact(value)
        with self.lock:
            if self.snapshots.get(key) == clean:
                return
            self.snapshots[key] = clean
        target.event(event, details=clean)

    def probe(self, target, device, state, code, *, elapsed, evidence, route=(), error=None):
        """Count every completed idle check. Keep full evidence at transitions and heartbeats."""
        with self.lock:
            key = (target.reference, device)
            if target.path.name.startswith('station-') and target.reference == self.station.reference:
                if self.station_probe_file and self.station_probe_file != target.path:
                    self.flush_probes(LogTarget(self,self.station_probe_file,create=False))
                self.station_probe_file = target.path
            now = self.monotonic()
            stats = self.probes.setdefault(key, dict(counts=Counter(), consecutive_unready=0,
                max_consecutive_unready=0, max_elapsed_ms=0, last_ready_at=None))
            previous = stats.get('code')
            changed = (state, code, route) != stats.get('signature')
            ms = round(elapsed*1000, 1)
            stats['counts'][code] += 1
            stats['consecutive_unready'] = 0 if state == 'READY' else stats['consecutive_unready']+1
            stats['max_consecutive_unready'] = max(stats['max_consecutive_unready'],stats['consecutive_unready'])
            stats['max_elapsed_ms'] = max(stats['max_elapsed_ms'], ms)
            if state == 'READY':
                stats['last_ready_at'] = datetime.fromtimestamp(self.clock(), LOCAL_TIME).isoformat(timespec='milliseconds')
            stats.update(signature=(state,code,route), code=code, state=state)
            late = target.reference != self.current.reference
            if changed or late or now-stats.get('reported_at', now) >= 30:
                stats['reported_at'] = now
                target.event('devices.probe.changed' if changed else 'devices.probe.summary',
                    level='WARNING' if changed and state in ('OFFLINE','ERROR','UNKNOWN') else 'INFO',
                    error=error if changed else None, device=device, previous_code=previous, elapsed_ms=ms,
                    evidence=evidence, **self.probe_totals(stats))
            if late and key not in self.pending_probes:
                self.probes.pop(key, None)

    @staticmethod
    def probe_totals(stats):
        return {key:value for key,value in stats.items() if key not in ('signature','reported_at')}

    def flush_probes(self, target):
        with self.lock:
            for key in tuple(self.probes):
                if key[0] == target.reference:
                    target.event('devices.probe.summary', device=key[1], final=True,
                                 check_in_flight=key in self.pending_probes, **self.probe_totals(self.probes[key]))
                    if key not in self.pending_probes:
                        del self.probes[key]

    def track_probe(self, target, device, *, finished=False):
        key = (target.reference,device)
        with self.lock:
            if finished:
                self.pending_probes.discard(key)
                if target.reference != self.current.reference and key in self.probes:
                    target.event('devices.probe.summary',device=device,final=True,
                                 **self.probe_totals(self.probes.pop(key)))
            else:
                self.pending_probes.add(key)

    def export_boot(self, destination):
        """Export a consistent cut of this boot; compress outside the writer lock."""
        destination = Path(destination)
        if destination.suffix.lower() != '.zip':
            raise ValueError('请选择 ZIP 文件名')
        with self.lock:
            for (ref,device),stats in self.probes.items():
                LogTarget(self,self.root/ref,create=False).event('devices.probe.summary', device=device,
                    export_snapshot=True, **self.probe_totals(stats))
            files = [(path,offset,path.stat().st_size if self._owned(path) and path.is_file() else None)
                     for path,offset in self.boot_files.items()]
            cutoff = datetime.fromtimestamp(self.clock(),LOCAL_TIME).isoformat(timespec='seconds')
        summary = [f'上位机 v{VERSION}',f'启动编号 {self.boot_id}',f'导出截止 {cutoff}']
        fd, name = tempfile.mkstemp(prefix='.station-logs-',suffix='.tmp',dir=destination.parent)
        os.close(fd)
        try:
            with zipfile.ZipFile(name,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as archive:
                for path,offset,end in files:
                    reference = path.relative_to(self.root).as_posix()
                    summary.extend(('',reference))
                    if end is None or end <= offset:
                        summary.append('原日志已清理或截断')
                        continue
                    try:
                        with path.open('rb') as source, archive.open(reference,'w') as out:
                            source.seek(offset)
                            self._copy_boot_records(source,out,end-offset,summary)
                    except FileNotFoundError:
                        summary.append('原日志已清理')
                archive.writestr('诊断摘要.txt', ('\n'.join(summary)+'\n').encode('utf-8-sig'))
            os.replace(name,destination)
        finally:
            Path(name).unlink(missing_ok=True)

    def _copy_boot_records(self, source, out, remaining, summary):
        header = re.compile(rb'^\d{4}-\d\d-\d\dT\S+\s+(\w+)\s+\[([^\]]+)\]')
        marker = re.compile(rb' \{"schema_version": \d+, "sequence": \d+, "boot_id": "([0-9a-f]{32})", "log_file":')
        selected = False
        errors = Counter()
        malformed = 0
        states = {}
        blockers = product = None
        firmware = []
        while remaining:
            line = source.readline(remaining)
            if not line: raise OSError('导出时日志被截断')
            remaining -= len(line)
            head = header.match(line)
            context = marker.search(line,head.end()) if head else None
            if context:
                selected = context[1].decode()==self.boot_id
                if selected:
                    try:
                        data = json.loads(line[context.start():])
                    except (ValueError,UnicodeError):
                        malformed += 1
                        out.write(line)
                        continue
                    event = head[2].decode()
                    if head[1] == b'ERROR':
                        errors[data.get('diagnostic',{}).get('code') or event] += 1
                    if event.startswith('devices.probe.'):
                        states[data['device']] = data
                    elif event == 'devices.readiness':
                        blockers = data['details'].get('blockers',[])
                    elif event == 'connection.product_profile':
                        product = data['details']
                    elif event=='firmware.library.selected':
                        new = data['current']
                        old = (data.get('previous') or {}).get('version', '未选择')
                        firmware.append(f"工位换版 {data['platform']}  {old} → {new['version']}  {new['name']}  SHA256 {new['sha256']}")
                    elif event=='firmware.version.before':
                        firmware.append(f"APP 读回版本 {data['app_version']}，工位平台 {data['platform']}")
                    elif event=='firmware.selected':
                        firmware.append(f"目标 APP {data['target_version']}，{data['file']}，SHA256 {data['sha256']}")
                    elif event in ('firmware.version.verified','firmware.version.accepted'):
                        firmware.append(f"APP 结果 {data['outcome']}，确认版本 {data['after']}")
                    elif event=='firmware.exchange.failed':
                        firmware.append(f"OTA {data['phase']} 阶段应答异常，已确认 {data['confirmed_bytes']} 字节")
            if selected:
                out.write(line)
        for device,data in states.items():
            summary.append(f"{device}  {data['code']}  各结果次数 {dict(data['counts'])}  最慢 {data['max_elapsed_ms']} ms")
        if blockers is not None:
            summary.append('最近一次开测等待项  '+('、'.join(READINESS_LABELS.get(k,k) for k in blockers) or '无'))
        if product and product.get('differences'):
            summary.append('来料 SN 与工位差异  '+', '.join(
                f"{key} {product['observed'][key]} → {product['expected'][key]}" for key in product['differences']))
        if errors:
            summary.append('错误日志条数  '+str(dict(errors)))
        if malformed:
            summary.append(f'有 {malformed} 条记录的结构化字段不完整，原文已保留')
        summary.extend(firmware)

    def record_target(self, directory):
        try:
            record = json.loads((Path(directory)/'station.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            record = {}
        if not isinstance(record, dict):
            record = {}
        reference = record.get('connection_log', '')
        if not isinstance(reference, str):
            reference = ''
        path = self.root/reference
        try:
            owned = bool(reference) and self._owned(path)
        except OSError:
            owned = False
        target = LogTarget(self, path, create=False) if owned else self.station
        return target.bind(connection_log=reference, session=record.get('session'),
                           request_id=record.get('request_id'), module_sn=record.get('module_sn'),
                           report_directory=str(directory))

    def cleanup(self, *, force=False):
        with self.lock:
            now = self.clock()
            if not force and now < self.next_cleanup:
                return []
            self.next_cleanup = now+3600
        # Do not hold the writer lock while scanning a month's worth of files.
        # Recheck each expired candidate under the lock so a late receipt cannot
        # update the file between the age check and deletion.
        cutoff = now-RETENTION_DAYS*86400
        deleted = []
        for directory in (self.root/'production', self.root/'preview'):
            try:
                if not directory.exists() or not self._owned(directory/'station-00000000.log'):
                    continue
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if not OWNED_LOG.fullmatch(entry.name):
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if (not stat.S_ISREG(info.st_mode) or
                                getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400) or
                                info.st_mtime >= cutoff):
                            continue
                        path = directory/entry.name
                        with self.lock:
                            if self.active and path == self.active.path:
                                continue
                            if self._owned(path) and path.stat().st_mtime < cutoff:
                                path.unlink()
                                deleted.append(path.relative_to(self.root).as_posix())
            except FileNotFoundError:
                continue  # Another process may have removed an expired log.
            except OSError as exc:
                self.station.event('retention.error', '清理到期日志失败', level='ERROR', error=exc)
        if deleted:
            self.station.event('retention.deleted', '已清理超过 30 天未更新的日志',count=len(deleted),deleted=deleted[:100])
        return deleted

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.finish_connection('application_closed_before_disconnect')
            self.flush_probes(self.station)
            self.station.event('application.stop', '上位机退出')
            self.closed = True
