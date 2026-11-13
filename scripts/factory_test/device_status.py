"""空闲时的设备健康查询。不使能电机，不改变电源输出。"""
import time
import threading
from .integrations import json_http, PrinterClient, PRINTER_STATUS_TIMEOUT
from .diagnostics import current_log, log_scope


def status(state, detail):
    return dict(state=state, detail=detail)


def adapter_status(devices, selected=''):
    if not selected:
        return status('OFFLINE', '请选择 CANable' if devices else '未检测到 CANable')
    matched = [item for item in devices if not selected or item['serial_number'] == selected]
    if len(matched) == 1:
        return status('READY', matched[0]['serial_number'])
    if selected and devices and not matched:
        return status('OFFLINE', f'绑定 SN {selected}；当前 ' + ', '.join(d['serial_number'] for d in devices))
    return status('OFFLINE', '未检测到 CANable' if not devices else '发现多只 CANable，请配置指定 SN')


class AdapterMonitor:
    """Independent, read-only USB enumeration; no serial ports or CAN traffic."""
    def __init__(self, emit, stop, *, detect=None, interval=.5):
        self.emit, self.stop, self.detect, self.interval = emit, stop, detect, interval

    def start(self):
        threading.Thread(target=self.run, name='adapter-discovery', daemon=True).start()

    def run(self):
        from motor_platform import Canable
        detect = self.detect or Canable.detect
        previous = None
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                devices, error = detect(), None
                signature = tuple(sorted((item['serial_number'], item['path']) for item in devices))
            except Exception as exc:
                devices, error = [], exc
                signature = (type(exc).__name__, str(exc))
            if signature != previous:
                self.emit(dict(devices=devices, error=error))
                previous = signature
            self.stop.wait(max(0, self.interval-(time.monotonic()-started)))


class _PrinterTrace:
    """Keep the GET exchange with the sampled probe, including headers, phase and HTTP ID."""
    def __init__(self, owner):
        self.owner = owner
        self.exchanges = []

    def event(self, name, message='', **details):
        if name.startswith('http.'):
            self.exchanges.append(dict(event=name, **{k:v for k,v in details.items() if k != 'error'}))


def record_probe(name, info, code, started, evidence, *, route=(), error=None):
    target = current_log()
    if target:
        target.owner.probe(target, name, info['state'], code, elapsed=time.monotonic()-started,
                           evidence=evidence, route=route, error=error)


def check_printer(config):
    started = time.monotonic()
    target = current_log()
    trace = _PrinterTrace(target.owner) if target else None
    evidence = dict(url=config.printer_url.rstrip('/')+config.printer_status_path,
                    printer_name=config.printer_name, timeout_s=PRINTER_STATUS_TIMEOUT)
    error = None
    try:
        with log_scope(trace):
            reply = PrinterClient(config, http=json_http).ready()
        data = reply['data']
        name = data.get('printerName') or config.printer_name
        info, code = status('READY', '服务报告就绪' + (' · '+name if isinstance(name,str) and name else '')), 'PRINTER_READY'
    except Exception as exc:
        error = exc
        code = getattr(exc, 'diagnostic_code', 'PRINTER_QUERY_ERROR')
        reply = getattr(exc, 'diagnostic_context', {}).get('response')
        data = reply.get('data') if isinstance(reply,dict) else {}
        data = data if isinstance(data, dict) else {}
        state = 'OFFLINE' if code == 'PRINTER_OFFLINE' else (
            'UNKNOWN' if code in ('PRINTER_STATUS_TIMEOUT','PRINTER_QUEUE_QUERY_FAILED') else 'ERROR')
        info = status(state, str(exc))
    names = data.get('allPrinters')
    if isinstance(names, list):
        info['printer_names'] = [name for name in names if isinstance(name,str)]
    # Keep UI state small. The measured Windows sample age lives separately from the HTTP round trip.
    age = data.get('sampleAgeMs', 0)
    age_s = age/1000 if type(age) in (int,float) and 0 <= age < float('inf') else 0
    evidence.update(response=data, exchanges=trace.exchanges if trace else [], print_submitted=False)
    record_probe('printer',info,code,started,evidence,
                 route=(evidence['url'],config.printer_name,data.get('portName'),data.get('reason')),error=error)
    return info, age_s


class PrinterMonitor:
    """At most one read-only HTTP check; results belong to one configuration and page generation."""
    def __init__(self, check=check_printer, clock=time.monotonic):
        self.check, self.clock = check, clock
        self.lock = threading.Lock()
        self.thread = self.key = self.result = None
        self.next_check = self.sample_at = 0.0

    def poll(self, config, generation):
        key = (generation, config.printer_url, config.printer_status_path, config.printer_name)
        target = current_log()
        with self.lock:
            now = self.clock()
            if self.key != key:
                self.key, self.result, self.next_check = key, None, now
            if now >= self.next_check and not (self.thread and self.thread.is_alive()):
                self.thread = threading.Thread(target=self._run, args=(config,key,target),
                                               name='printer-service-status', daemon=True)
                if target: target.owner.track_probe(target,'printer')
                try:
                    self.thread.start()
                except RuntimeError:
                    if target: target.owner.track_probe(target,'printer',finished=True)
                    raise
            if self.result is None:
                return status('CHECKING','正在查询打印队列')
            if self.result['state'] == 'READY' and now-self.sample_at >= 10:
                return status('UNKNOWN','最近一次队列状态已过期，正在重新确认')
            return self.result

    def _run(self, config, key, target):
        try:
            with log_scope(target):
                info, age_s = self.check(config)
        except Exception as exc:
            if target:
                target.event('devices.printer.monitor_failed', level='ERROR', error=exc)
            info, age_s = status('UNKNOWN', '打印队列查询异常，详见日志'), 0
        finally:
            if target: target.owner.track_probe(target,'printer',finished=True)
        with self.lock:
            if self.key == key:
                now = self.clock()
                self.result, self.sample_at, self.next_check = info, now-age_s, now+3


class DeviceHealth:
    def __init__(self):
        self.power_port = self.dyno_port = None
        self.printer = PrinterMonitor()

    def check(self, config, *, emit=None, cancelled=None, generation=None, inventory_emit=None):
        def check_stop():
            if cancelled is not None and cancelled():
                raise InterruptedError('设备检查已取消，正在释放连接')

        check_stop()
        if config.simulation:
            result = dict(can=status('SIMULATED', '模拟适配器'),
                        dyno=status('SIMULATED', '模拟测功机 · 反馈正常'),
                        power=status('SIMULATED', '模拟电源 · 输出关闭'),
                        printer=status('SIMULATED', '模拟打印机 · 未出纸'))
            if emit:
                for name, info in result.items(): emit(name, info)
            return result
        from motor_platform import Canable, PowerSupply, Dynamometer
        from motor_platform.components.dynamometer import detect_dynamometers
        from motor_platform.components.power_supply.discovery import detect_power_supplies
        from serial.tools.list_ports import comports

        result = {}
        def report(name, info):
            result[name] = info
            if emit:
                emit(name, info)
            check_stop()

        started = time.monotonic()
        devices, error = [], None
        try:
            devices = Canable.detect()
            info = adapter_status(devices, config.adapter_sn)
        except Exception as exc:
            info, error = status('ERROR', str(exc)), exc
        serials = tuple(item['serial_number'] for item in devices)
        record_probe('can', info, 'CAN_'+info['state'], started, dict(devices=devices,selected=config.adapter_sn),
                     route=(config.adapter_sn,serials),error=error)
        report('can', info)
        try:
            available = list(comports())
            ports = [p.device for p in available]
            inventory = [{name:getattr(p,name,None) for name in (
                'device','description','hwid','vid','pid','serial_number','manufacturer')} for p in available]
            inventory_error = None
        except Exception as exc:
            ports, inventory, inventory_error = [], [], exc
        if inventory_emit is not None:
            inventory_emit(dict(ports=inventory,error=str(inventory_error) if inventory_error else None))
        target = current_log()
        if target:
            target.owner.snapshot('devices.serial.enumerated',dict(ports=inventory,
                error=str(inventory_error) if inventory_error else None),target=target)

        started = time.monotonic()
        requested = config.dyno_port if config.dyno_port != 'auto' else self.dyno_port
        candidates = [requested] if requested in ports else ports if config.dyno_port == 'auto' else []
        observed = []
        evidence = dict(configured_port=config.dyno_port, candidates=candidates, listen_s=.25, observations=observed)
        code, error = 'DYNO_NO_FEEDBACK', inventory_error
        self.dyno_port = None
        try:
            matches = detect_dynamometers(ports=candidates, duration=.25, observations=observed)
            evidence['matches'] = matches
            if len(matches) == 1:
                with Dynamometer(port=matches[0]) as dyno:
                    reading = dyno.receive_state(timeout=.4)
                evidence['reading'] = reading
                self.dyno_port = matches[0]
                info, code = status('READY', f'{self.dyno_port} · {reading.torque:.2f} Nm / {reading.speed:.1f} rad/s'), 'DYNO_READY'
            elif matches:
                info, code = status('OFFLINE','发现多台测功机，请指定串口'), 'DYNO_MULTIPLE'
            elif inventory_error:
                info, code = status('ERROR','读取串口列表失败'), 'SERIAL_ENUM_ERROR'
            elif not candidates:
                info, code = status('OFFLINE', '未找到可用串口' if config.dyno_port=='auto' else config.dyno_port+' 未枚举'), 'DYNO_PORT_ABSENT'
            elif any(item.get('error') or item.get('fault') for item in observed):
                info, code = status('ERROR','测功机串口访问异常，详见日志'), 'DYNO_PORT_ERROR'
            elif any(item.get('rx_bytes',0) for item in observed):
                info, code = status('OFFLINE','收到串口数据，有效反馈帧不足'), 'DYNO_FRAMES_INSUFFICIENT'
            else:
                info = status('OFFLINE','未收到测功机反馈 · '+', '.join(candidates))
        except Exception as exc:
            info, code, error = status('ERROR',str(exc)), 'DYNO_READ_FAILED', exc
        record_probe('dyno',info,code,started,evidence,route=(tuple(candidates),self.dyno_port),error=error)
        report('dyno', info)

        started = time.monotonic()
        requested = config.power_port if config.power_port != 'auto' else self.power_port
        candidates = [p for p in ports if p != self.dyno_port and (not requested or p == requested)]
        observed = []
        evidence = dict(configured_port=config.power_port, excluded_dyno_port=self.dyno_port,
                        candidates=candidates, observations=observed, query_timeout_s=.2)
        self.power_port, error = None, None
        if not self.dyno_port and config.power_port == 'auto':
            info, code = status('WAITING', '等待测功机串口识别'), 'POWER_WAIT_DYNO'
            evidence.update(candidates=[],query_attempted=False)
        else:
            try:
                evidence['query_attempted'] = bool(candidates)
                matches = detect_power_supplies(ports=candidates, timeout=.2, observations=observed)
                evidence['matches'] = matches
                if len(matches) == 1:
                    port = next(iter(matches))
                    with PowerSupply(port=port) as power:
                        reading = power.read_state()
                    self.power_port = port
                    evidence['reading'] = reading
                    info, code = status('READY', f'{port} · {reading.voltage:.1f} V · 输出'+('开启' if reading.output else '关闭')), 'POWER_READY'
                elif matches:
                    info, code = status('OFFLINE','发现多台电源，请指定串口'), 'POWER_MULTIPLE'
                else:
                    info, code = status('OFFLINE','未识别到 UDP6721 电源'), 'POWER_NO_REPLY' if candidates else 'POWER_PORT_ABSENT'
            except Exception as exc:
                info, code, error = status('ERROR',str(exc)), 'POWER_PROBE_FAILED', exc
        record_probe('power',info,code,started,evidence,route=(tuple(evidence['candidates']),self.power_port),error=error)
        report('power', info)
        report('printer', self.printer.poll(config,generation) if generation is not None else check_printer(config)[0])
        return result
