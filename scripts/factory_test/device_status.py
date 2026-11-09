"""空闲时的设备健康查询。不使能电机，不改变电源输出。"""
import time
import threading
from .integrations import json_http, PrinterClient
from .diagnostics import log_event


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


class DeviceHealth:
    def __init__(self):
        self.power_port = self.dyno_port = None

    def check(self, config, *, emit=None, cancelled=None):
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
        started = time.perf_counter()
        log_event('devices.check.start', adapter_sn=config.adapter_sn, power_port=config.power_port,
                  dyno_port=config.dyno_port, printer_url=config.printer_url, printer_name=config.printer_name)
        try:
            devices = Canable.detect()
            matched = [d for d in devices if not config.adapter_sn or d['serial_number'] == config.adapter_sn]
            log_event('devices.can.enumerated', configured_serial=config.adapter_sn, devices=devices, matches=matched)
            result['can'] = adapter_status(devices, config.adapter_sn)
        except Exception as exc:
            result['can'] = status('ERROR', str(exc))
            log_event('devices.can.failed', level='WARNING', error=exc, adapter_sn=config.adapter_sn)
        if emit: emit('can', result['can'])
        check_stop()
        try:
            available = list(comports())
            ports = [p.device for p in available]
            log_event('devices.serial.enumerated', ports=[{name:getattr(p,name,None) for name in (
                'device','description','hwid','vid','pid','serial_number','manufacturer')} for p in available])
        except Exception as exc:
            ports = []
            log_event('devices.serial.failed', level='WARNING', error=exc)
        candidates = []
        try:
            requested = config.dyno_port if config.dyno_port != 'auto' else self.dyno_port
            candidates = [requested] if requested in ports else ports if config.dyno_port == 'auto' else []
            matches = detect_dynamometers(ports=candidates, duration=.25)
            log_event('devices.dyno.detected', candidates=candidates, matches=matches, listen_s=.25)
            if len(matches) != 1:
                raise RuntimeError('未收到有效反馈' if not matches else '发现多台测功机，请在设置中指定串口')
            self.dyno_port = matches[0]
            with Dynamometer(port=self.dyno_port) as dyno:
                reading = dyno.receive_state(timeout=.4)
                log_event('devices.dyno.feedback', port=self.dyno_port, reading=reading)
                result['dyno'] = status('READY', f'{self.dyno_port} · {reading.torque:.2f} Nm / {reading.speed:.1f} rad/s')
        except Exception as exc:
            self.dyno_port = None
            result['dyno'] = status('OFFLINE', str(exc))
            log_event('devices.dyno.failed', level='WARNING', error=exc, candidates=candidates,
                      configured_port=config.dyno_port)
        if emit: emit('dyno', result['dyno'])
        check_stop()
        candidates = []
        try:
            requested = config.power_port if config.power_port != 'auto' else self.power_port
            if self.dyno_port is None and not requested:
                raise RuntimeError('等待测功机识别，避免向未知串口发送查询')
            # 被动识别出的测功机口不发送 SCPI；与实际测试保持一致。
            candidates = [p for p in ports if p != self.dyno_port]
            if requested:
                candidates = [requested] if requested in candidates else []
            matches = detect_power_supplies(ports=candidates, timeout=.2)
            log_event('devices.power.detected', candidates=candidates, matches=matches, query_timeout_s=.2)
            if len(matches) != 1:
                raise RuntimeError('未识别到 UDP6721 电源' if not matches else '发现多台电源，请在设置中指定串口')
            self.power_port = next(iter(matches))
            with PowerSupply(port=self.power_port) as power:
                reading = power.read_state()
                log_event('devices.power.feedback', port=self.power_port, reading=reading)
                result['power'] = status('READY', f'{self.power_port} · {reading.voltage:.1f} V · 输出'+('开启' if reading.output else '关闭'))
        except Exception as exc:
            self.power_port = None
            result['power'] = status('OFFLINE', str(exc))
            log_event('devices.power.failed', level='WARNING', error=exc, candidates=candidates,
                      configured_port=config.power_port, excluded_dyno_port=self.dyno_port)
        if emit: emit('power', result['power'])
        check_stop()
        try:
            PrinterClient(config, http=json_http).ready(timeout=2)
            result['printer'] = status('READY', '服务及打印机就绪')
        except Exception as exc:
            result['printer'] = status('OFFLINE', str(exc))
        if emit: emit('printer', result['printer'])
        check_stop()
        log_event('devices.check.end', results=result, elapsed_s=round(time.perf_counter()-started,4))
        return result
