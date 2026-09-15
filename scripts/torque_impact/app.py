"""转子与转子架粘接可靠性冲击测试。

运行：python -m scripts.torque_impact --id 1 --serial <SN> --torque 30
目的：通过正反扭矩冲击，检查转子与转子架粘接后的出力是否保持。
流程：初始正反测量→休息1秒→5组正反冲击→停机判定。
初始正反方向各用50ms线性斜坡升至目标，再保持100ms取末段扭矩。
冲击脉冲保持100ms，每个脉冲后零扭矩休息900ms。

唯一合格判据：5组共10次冲击的扭矩幅值，除以第一次同方向
测得的初始扭矩幅值，全部≥90%为绿色PASS；任一低于90%为红色FAIL。
取末尾30/20/15ms原始测功机数据的中位数，优先较宽稳定窗口。
无稳定窗口时选波动最小的窗口；波动不另作合格门槛。
设备故障、记录失败、中止或无可计算数据仅报异常，不给合格结论。

CSV保留全部电机帧，按接收时间匹配测功机帧，每帧最多使用一次；
多余测功机帧舍弃，20ms内无可用帧则留空。判定使用原始数据。
quit / Ctrl+C 结束；配置在下方，结果保存在本脚本data文件夹。
"""
import argparse
from bisect import bisect_right
import csv
from datetime import datetime
import json
import math
from pathlib import Path
from statistics import median
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from motor_platform import Bench, Canable, Dynamometer, MitCommand, PowerSupply
from motor_platform.components.dynamometer import detect_dynamometers
from motor_platform.components.power_supply.discovery import resolve_power_port
from utils.terminal import TerminalConsole
from utils.csv_streams import pack_streams

MOTOR_ID = 1
CANABLE_SERIAL = None           # 可在此填写，或传 --serial；python -m motor_platform --list 查看
TORQUE_FF = None                # 必填正的最大幅值，--torque 覆盖；测试自动正反向
POWER_PORT = 'auto'
DYNO_PORT = 'auto'
LOAD_VOLTAGE_V = 24.0           # 给磁粉阻尼器供电，电机由独立电池供电
LOAD_CURRENT_A = 3.0
LOAD_SETTLE_S = 0.5
MOTOR_HZ = 1000
UPDATE_S = 0.001
BASELINE_RAMP_S = 0.05        # 初始正反测量各用50ms线性升扭矩，CLI目标幅值不变
MEASURE_HOLD_S = 0.1           # 初始测量：斜坡结束后保持目标扭矩的时间
MEASURE_WINDOW_S = 0.03        # 优先取最后30ms真实帧中位数
MIN_MEASURE_WINDOW_S = 0.015   # 末段尚有过渡时尝试20/15ms
PULSE_S = 0.1                  # 冲击阶跃目标持续时间
REST_S = 0.9                   # 每个脉冲后保持零扭矩
CYCLES = 5                     # 每轮一正一反，共10次冲击
RECOVER_S = 1.0                # 初始基准完成后休息1秒
STOP_RAMP_S = 1.0
FEEDBACK_TIMEOUT_S = 0.1
MAX_MOTOR_TEMP_C = 80.0
MAX_DRIVER_TEMP_C = 80.0
MAX_TORQUE_SPREAD_RATIO = 0.15 # 稳态扭矩P90-P10/中位数；工程初值
DROP_LIMIT = 0.10              # 出力下降超过10%不合格，需正常样机确认正式门槛
MIN_WINDOW_SAMPLES = 10
DYNO_TORQUE_SCALE = 100.0
DYNO_SPEED_SCALE = 100.0
MAX_EVENTS = 200000            # 有界内存，停机后写CSV，不在控制循环同步写盘


class InvalidTest(RuntimeError):
    """测量条件不满足，不能给出合格或不合格结论。"""


def print_result(result, directory, output=None):
    output = sys.stdout if output is None else output
    status = result['status']
    label = {'PASS': 'PASS', 'FAIL': 'FAIL', 'INVALID': '异常'}.get(status, '异常')
    color = '\033[92m' if status == 'PASS' else '\033[91m'
    reset = '\033[0m'
    shown = f'{color}{label}{reset}' if getattr(output, 'isatty', lambda: False)() else label
    print(f'{shown}: {result["reason"]}\n数据: {directory}', file=output)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--id', type=int, default=MOTOR_ID)
    p.add_argument('--serial', default=CANABLE_SERIAL)
    p.add_argument('--torque', type=float, default=TORQUE_FF)
    a = p.parse_args(argv)
    if not 1 <= a.id <= 127 or not a.serial or not a.serial.strip():
        p.error('指定有效的 --id (1..127) 和 --serial')
    if a.torque is None or not math.isfinite(a.torque) or a.torque <= 0:
        p.error('--torque 必须是有限正数，表示正反冲击的最大幅值')
    try:
        MitCommand(torque=a.torque).pack()
    except ValueError as exc:
        p.error(str(exc))
    for name in ('LOAD_VOLTAGE_V', 'LOAD_CURRENT_A', 'LOAD_SETTLE_S', 'MOTOR_HZ', 'UPDATE_S',
                 'MEASURE_HOLD_S', 'MEASURE_WINDOW_S', 'MIN_MEASURE_WINDOW_S', 'PULSE_S', 'REST_S',
                 'RECOVER_S', 'STOP_RAMP_S', 'FEEDBACK_TIMEOUT_S', 'BASELINE_RAMP_S',
                 'MAX_MOTOR_TEMP_C', 'MAX_DRIVER_TEMP_C', 'MAX_TORQUE_SPREAD_RATIO',
                 'DYNO_TORQUE_SCALE', 'DYNO_SPEED_SCALE'):
        if not math.isfinite(globals()[name]) or globals()[name] <= 0:
            p.error(f'invalid {name}')
    if not 0 < DROP_LIMIT < 1:
        p.error('invalid retention/pulse thresholds')
    if MEASURE_WINDOW_S > min(MEASURE_HOLD_S, PULSE_S):
        p.error('measurement window exceeds hold/pulse duration')
    if MIN_MEASURE_WINDOW_S > MEASURE_WINDOW_S:
        p.error('minimum measurement window exceeds preferred window')
    if any(type(v) is not int or v <= 0 for v in (CYCLES, MIN_WINDOW_SAMPLES, MAX_EVENTS)):
        p.error('counts must be positive integers')
    return a


def summarize(dynos, motors):
    if min(len(dynos), len(motors)) < MIN_WINDOW_SAMPLES:
        raise InvalidTest('测量窗口样本不足')
    values = sorted(s.torque for s in dynos)
    signed = median(values)
    magnitude = abs(signed)
    if not math.isfinite(signed):
        raise InvalidTest('测量数据非有限数')
    spread = values[int(.9*(len(values)-1))]-values[int(.1*(len(values)-1))]
    return dict(torque_nm=signed, magnitude_nm=magnitude, spread_nm=spread,
                motor_temperature_c=median(s.motor_temperature for s in motors),
                driver_temperature_c=median(s.board_temperature for s in motors),
                motor_samples=len(motors), dyno_samples=len(dynos))


def retention(reading, baseline):
    denominator = baseline['magnitude_nm']
    if not math.isfinite(denominator) or denominator <= 0:
        raise InvalidTest('初始扭矩为零，无法计算保持率')
    return reading['magnitude_nm']/denominator


def judge(before, pulses):
    if not pulses or {p['direction'] for p in pulses} != {'positive', 'negative'}:
        raise InvalidTest('缺少正反冲击数据')
    pulse_ratios = [retention(p, before[p['direction']]) for p in pulses]
    ratios = {direction: min(r for p, r in zip(pulses, pulse_ratios) if p['direction'] == direction)
              for direction in ('positive', 'negative')}
    worst = min(pulse_ratios)
    return dict(status='PASS' if worst >= 1-DROP_LIMIT else 'FAIL',
                retention=ratios, pulse_retention=pulse_ratios,
                worst_retention=worst, drop_limit=DROP_LIMIT)


class Experiment:
    def __init__(self, motor, dyno, ui, args, result):
        self.motor, self.dyno, self.ui, self.args, self.result = motor, dyno, ui, args, result
        self.origin = time.perf_counter()
        self.events, self.commands = [], [(self.origin, 'initial', 0, 0.0)]
        self.applied = 0.0
        self.next_display = 0.0
        self.dropped = dyno.stats()['dropped_samples']

    def collect(self):
        for source, samples in (('motor', self.motor.drain_states()), ('dyno', self.dyno.drain())):
            for s in samples:
                if s.received_at >= self.origin:
                    if len(self.events) >= MAX_EVENTS:
                        raise InvalidTest('记录容量不足')
                    self.events.append((s.received_at, source, s))

    def check(self):
        m = self.motor.state(max_age=FEEDBACK_TIMEOUT_S)
        d = self.dyno.state(max_age=FEEDBACK_TIMEOUT_S)
        stats = self.motor.stats()
        if m.error_code or stats['fault']:
            raise RuntimeError(f'motor fault: {m.error_code}, {stats["fault"]}')
        if m.motor_temperature >= MAX_MOTOR_TEMP_C or m.board_temperature >= MAX_DRIVER_TEMP_C:
            raise RuntimeError('温度越限')
        if stats.get('recording_dropped', 0) or self.dyno.stats()['dropped_samples'] != self.dropped:
            raise RuntimeError('反馈记录队列溢出')
        return m, d

    def segment(self, phase, duration, target, cycle=0, ramp=False, stopping=False):
        began = previous = time.perf_counter()
        max_gap = 0.0
        initial = self.applied
        while True:
            now = time.perf_counter()
            max_gap = max(max_gap, now-previous)
            previous = now
            m, d = self.check()
            if not stopping:
                line = self.ui.poll()
                if line is not None and line.strip().lower() in ('quit', 'stop', 'q', 'exit'):
                    raise InvalidTest('用户中止')
            self.applied = initial+(target-initial)*min(1., (now-began)/duration) if ramp else target
            self.motor.set_mit(MitCommand(torque=self.applied))
            self.commands.append((time.perf_counter(), phase, cycle, self.applied))
            self.collect()
            if now >= self.next_display:
                self.ui._panel = (f'ID={self.args.id}  {phase}  轮次 {cycle}/{CYCLES}\n'
                    f'目标 {self.applied:+.3f}  测功机 {d.torque:+.3f} Nm / {d.speed:.3f} rad/s\n'
                    f'电机 {m.torque:+.3f} / {m.velocity:+.3f} rad/s\n'
                    f'温度 {m.motor_temperature:.1f}/{m.board_temperature:.1f} C\n'
                    f'已采集 {len(self.events)} 帧')
                self.ui._notice = 'quit / Ctrl+C 中止；最终结果为出力保持率，不直接定位粘接失效'
                self.ui.app.invalidate()
                self.next_display = now+.1
            if now-began >= duration:
                self.last_segment_max_gap = max_gap
                return began, now
            time.sleep(UPDATE_S)

    def window(self, end, width):
        # 原始接收时间窗口，不按应用轮询次数采样，不平滑脉冲。
        selected = [(source, s) for at, source, s in self.events if end-width <= at <= end]
        return ([s for source, s in selected if source == 'dyno'],
                [s for source, s in selected if source == 'motor'])

    def rest(self, duration, cycle=0):
        self.segment('zero_rest', duration, 0, cycle)
        self.check()

    def measure_tail(self, end):
        widths = sorted({MEASURE_WINDOW_S, MIN_MEASURE_WINDOW_S,
                         min(MEASURE_WINDOW_S, max(MIN_MEASURE_WINDOW_S, .020))}, reverse=True)
        candidates = []
        for width in widths:
            try:
                reading = summarize(*self.window(end, width))
            except InvalidTest:
                continue
            reading['window_s'] = width
            candidates.append(reading)
            if reading['spread_nm'] <= MAX_TORQUE_SPREAD_RATIO*max(reading['magnitude_nm'], 1e-12):
                return reading
        if not candidates:
            raise InvalidTest('末段没有足够的有效测量数据')
        return min(candidates, key=lambda r: r['spread_nm']/max(r['magnitude_nm'], 1e-12))

    def measure(self, label):
        readings = {}
        for direction, sign in (('positive', 1), ('negative', -1)):
            if label == 'before':
                self.segment(f'{label}_{direction}_ramp', BASELINE_RAMP_S, sign*self.args.torque, ramp=True)
            _, end = self.segment(f'{label}_{direction}_hold', MEASURE_HOLD_S, sign*self.args.torque)
            # 到期先归零，再统计末段；不让统计耗时延长满扭矩施加时间。
            self.motor.set_mit(MitCommand())
            self.applied = 0.0
            self.commands.append((time.perf_counter(), f'{label}_zero', 0, 0.0))
            readings[direction] = self.measure_tail(end)
            self.result.setdefault(label, {})[direction] = readings[direction]
            self.rest(REST_S)
        return readings

    def execute(self):
        before = self.measure('before')
        self.rest(RECOVER_S)
        self.result['completed_pulses'] = 0
        for cycle in range(1, CYCLES+1):
            for direction, sign in (('positive', 1), ('negative', -1)):
                start, end = self.segment(f'impact_{direction}', PULSE_S, sign*self.args.torque, cycle)
                # 先结束脉冲，再分析，避免统计耗时延长施力。
                self.motor.set_mit(MitCommand())
                self.applied = 0.0
                self.commands.append((time.perf_counter(), 'zero_rest', cycle, 0.0))
                duration = end-start
                overrun = max(0.0, duration-PULSE_S)
                reading = self.measure_tail(end)
                ratio = retention(reading, before[direction])
                self.result.setdefault('pulses', []).append(dict(reading,
                    cycle=cycle, direction=direction, duration_s=duration,
                    overrun_s=overrun, max_loop_gap_s=self.last_segment_max_gap,
                    retention=ratio, valid=ratio >= 1-DROP_LIMIT))
                self.result['completed_pulses'] += 1
                self.rest(REST_S, cycle)
        self.result.update(judge(before, self.result['pulses']))

    def save_csv(self, path):
        times = [c[0] for c in self.commands]
        with path.open('x', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['time_ms', 'motor_torque', 'motor_temperature_c',
                        'driver_temperature_c', 'motor_application_target_torque',
                        'dyno_torque_nm', 'dyno_speed_rad_s'])
            for at, source, s in sorted(self.events, key=lambda e: e[0]):
                _, _, _, command = self.commands[bisect_right(times, at)-1]
                values = ['', '', '', '', s.torque, s.speed] if source == 'dyno' else [
                    s.torque, s.motor_temperature, s.board_temperature, command, '', '']
                w.writerow([f'{(at-self.origin)*1000:.6f}', *values])


def main(argv=None, *, output_dir=None, quiet=False):
    args = parse_args(argv)
    directory = (Path(output_dir) if output_dir is not None else
                 Path(__file__).resolve().parent/'data'/f'{datetime.now():%Y%m%d-%H%M%S-%f}')
    directory.mkdir(parents=True)
    result = dict(status='INVALID', reason='未完成', arguments=vars(args),
                  config={k: v for k, v in globals().items() if k.isupper()}, completed_pulses=0)
    experiment = None
    try:
        ports = detect_dynamometers() if DYNO_PORT == 'auto' else [DYNO_PORT]
        if len(ports) != 1:
            raise InvalidTest(f'测功机需唯一，实际为 {ports}；配置 DYNO_PORT')
        with Dynamometer(port=ports[0], torque_scale=DYNO_TORQUE_SCALE, speed_scale=DYNO_SPEED_SCALE) as dyno:
            dyno.receive_state(timeout=1)
            with PowerSupply(port=resolve_power_port(POWER_PORT)) as power:
                try:
                    power.disable()
                    power.set_current(0)
                    power.set_voltage(LOAD_VOLTAGE_V)
                    power.set_current(LOAD_CURRENT_A)
                    with TerminalConsole(title='正反扭矩冲击测试', prompt='impact> ', commands='quit') as ui:
                        with Bench(transport=Canable(args.serial), motor_ids=[args.id], rate_hz=MOTOR_HZ) as bench:
                            power.enable()
                            time.sleep(LOAD_SETTLE_S)
                            with bench.start() as control:
                                motor = control.motor(args.id)
                                motor.start_recording()
                                dyno.drain()
                                experiment = Experiment(motor, dyno, ui, args, result)
                                enabled = False
                                try:
                                    motor.enable()
                                    enabled = True
                                    experiment.execute()
                                    result['reason'] = '出力保持率满足门槛' if result['status'] == 'PASS' else '出力下降超限'
                                finally:
                                    original = sys.exception()
                                    try:
                                        if enabled and experiment.applied != 0 and (original is None or isinstance(original, (InvalidTest, KeyboardInterrupt))):
                                            experiment.segment('shutdown', STOP_RAMP_S, 0, ramp=True, stopping=True)
                                    finally:
                                        try:
                                            motor.disable()
                                        except Exception as exc:
                                            if original is None:
                                                raise
                                            original.add_note(f'Disable failed: {exc}')
                                        finally:
                                            experiment.collect()
                finally:
                    original = sys.exception()
                    try:
                        power.disable()
                        result['power_off_confirmed'] = True
                    except Exception as exc:
                        result['power_off_confirmed'] = False
                        try:
                            power.disable(verify=False)
                        except Exception as fallback:
                            exc.add_note(f'Best-effort OFF failed: {fallback}')
                        if original is None:
                            raise
                        original.add_note(f'Power OFF failed: {exc}')
    except (Exception, KeyboardInterrupt) as exc:
        result.update(status='INVALID', reason=str(exc) or '用户中止',
                      errors=getattr(exc, '__notes__', []))
    finally:
        try:
            if experiment is not None:
                experiment.save_csv(directory/'process.csv')
                pack_streams(directory/'process.csv', align_motor=True)
        except Exception as exc:
            result.update(status='INVALID', reason=f'CSV写入失败: {exc}')
        with (directory/'result.json').open('x', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, allow_nan=False)
        if not quiet:
            print_result(result, directory)
    return result


if __name__ == '__main__':
    main()
