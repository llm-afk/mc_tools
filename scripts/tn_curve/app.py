"""磁粉阻尼器加载的电机 T-N 测试。

在平台根目录运行（--torque 覆盖本脚本默认最大前馈扭矩）：
    python -m scripts.tn_curve --id 1 --torque 30
查看适配器 SN：python -m motor_platform --list
换适配器：在运行命令后加 --serial <SN>，或修改下方 CANABLE_SERIAL。
电机由电池供电，电源只给磁粉阻尼器供电。quit / Ctrl+C 停止。
前馈扭矩用 1 秒线性升至目标，再保持 1 秒后加载和记录；数据写入 data/。
正常结束用 STOP_TORQUE_RAMP_S 将扭矩降零，确认失能后直接关闭电源。
只输出 curve.csv：两路真实帧按时间记录，未更新的一路留空，不用旧值补齐。
停机后追加电机、测功机速度/扭矩平滑四列；窗口在 *_SMOOTH_S 配置，原始值保留。
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import csv
import math
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from motor_platform import Bench, MitCommand, Dynamometer, PowerSupply, Canable
from motor_platform.components.dynamometer import detect_dynamometers
from motor_platform.components.power_supply.discovery import resolve_power_port
from utils.terminal import TerminalConsole
from scripts.tn_curve.recording import CurveRecorder, event_row
from scripts.tn_curve.smoothing import append_smoothed_columns

# 应用配置：设备组件不保存这些实验参数。
MOTOR_ID = 1                     # 选定 CAN 总线上的电机 ID，--id 可覆盖
CANABLE_SERIAL = '20663474384550052'  # USB 适配器 SN，--serial 可覆盖；不会自动换设备
POWER_PORT = 'auto'              # 电源串口自动探测；多台时明确填写 COM 端口
DYNO_PORT = 'auto'               # 测功机串口自动探测；多台时明确填写 COM 端口
TORQUE_FF = 30                  # 默认最大前馈扭矩，--torque 可覆盖，允许负值
MOTOR_HZ = 1000
LOAD_VOLTAGE_V = 24.0
CURRENT_STEP_A = 0.028125         # 平均每步 28.125 mA，15 Hz 时约 2.37 秒/A；累计目标量化到 1 mA
CURRENT_STEP_S = 1.0 / 15          # 指令起点间隔，包含串口设置及读回耗时；不补发积压步进
MAX_CURRENT_A = 5.0
MAX_CURRENT_HOLD_S = 3.0
TORQUE_SETTLE_S = 1.0            # 下发最大前馈扭矩后等待，再开始阻尼加载和记录
TORQUE_RAMP_S = 1.0              # 从 0 线性增加到 TORQUE_FF 的时间，支持负目标
STOP_TORQUE_RAMP_S = 1.0         # 正常停止：维持阻尼，电机扭矩用此时间线性降到 0
STALL_SPEED_RAD_S = 0.5
STALL_HOLD_S = 0.100
TEST_TIMEOUT_S = 120.0            # 从加载开始计时
MAX_MOTOR_TEMP_C = 80.0
MAX_DRIVER_TEMP_C = 80.0
FEEDBACK_TIMEOUT_S = 0.1
DYNO_TORQUE_SCALE = 100.0         # 沿用已验证接收组件；实测标定另行核对
DYNO_SPEED_SCALE = 100.0
MOTOR_SPEED_SMOOTH_S = 0.30       # 停机后速度平滑的完整时间窗口；越大越平滑、拐点越钝
MOTOR_TORQUE_SMOOTH_S = 0.30      # 扭矩平滑窗口；原始反馈保留，堵转判断仍使用原始速度
DYNO_SPEED_SMOOTH_S = 0.30        # 测功机速度平滑窗口；与电机采用相同的抗毛刺算法
DYNO_TORQUE_SMOOTH_S = 0.30       # 测功机扭矩平滑窗口；只在测功机行填写平滑值

COLUMNS = ['time_ms', 'dyno_torque_nm', 'dyno_speed_rad_s', 'dyno_power_w',
           'motor_speed_rad_s', 'motor_torque', 'motor_temperature_c', 'driver_temperature_c']


def validate(torque=None):
    torque = TORQUE_FF if torque is None else torque
    if torque is None or not math.isfinite(torque) or torque == 0:
        raise ValueError('用 --torque 或顶部 TORQUE_FF 指定有限非零的最大前馈扭矩')
    for name in ('MOTOR_HZ', 'LOAD_VOLTAGE_V', 'CURRENT_STEP_A', 'CURRENT_STEP_S',
                 'MAX_CURRENT_A', 'MAX_CURRENT_HOLD_S', 'TORQUE_SETTLE_S', 'TORQUE_RAMP_S',
                 'STOP_TORQUE_RAMP_S',
                 'STALL_SPEED_RAD_S', 'STALL_HOLD_S', 'TEST_TIMEOUT_S',
                 'MAX_MOTOR_TEMP_C', 'MAX_DRIVER_TEMP_C', 'FEEDBACK_TIMEOUT_S',
                 'DYNO_TORQUE_SCALE', 'DYNO_SPEED_SCALE',
                 'MOTOR_SPEED_SMOOTH_S', 'MOTOR_TORQUE_SMOOTH_S',
                 'DYNO_SPEED_SMOOTH_S', 'DYNO_TORQUE_SMOOTH_S'):
        value = globals()[name]
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f'{name} must be finite and positive')
    if CURRENT_STEP_A < 0.001:
        raise ValueError('电流步进至少 0.001 A')
    if round(MAX_CURRENT_A, 3) != MAX_CURRENT_A:
        raise ValueError('电源上限电流最多保留三位小数；步进按累计目标量化')
    MitCommand(torque=torque).pack()


class Progress:
    """Use fresh motor feedback timestamps, never count cached samples as new."""
    def __init__(self, started):
        self.started = started
        self.loading_at = None
        self.last_at = None
        self.low_since = None
        self.stability_text = '等待最大扭矩下发'

    def update(self, state):
        t, speed = state.received_at, abs(state.velocity)
        if self.last_at is not None and t <= self.last_at:
            return False
        self.last_at = t
        if self.loading_at is None:
            self.stability_text = f'最大扭矩已下发，剩余等待 {max(0, TORQUE_SETTLE_S-(t-self.started)):.1f}s'
            return t-self.started >= TORQUE_SETTLE_S
        if speed < STALL_SPEED_RAD_S:
            if self.low_since is None:
                self.low_since = t
            return t-self.low_since >= STALL_HOLD_S
        self.low_since = None
        return False


def ramp_torque(elapsed, target=None):
    """按单调时钟计算目标，调度延迟时不补发中间指令。"""
    target = TORQUE_FF if target is None else target
    return target * min(1.0, max(0.0, elapsed/TORQUE_RAMP_S))


def set_load_current(power, target):
    """在串口工作线程记录实际开始时间，避免读回后额外等待一个周期。"""
    started = time.perf_counter()
    confirmed = power.set_current(target)
    return confirmed, started + CURRENT_STEP_S


def load_target(step_number):
    """累计计算后量化到 1 mA，避免对单次小数步进取整造成长期速率偏差。"""
    return min(MAX_CURRENT_A, round(step_number*CURRENT_STEP_A, 3))


def graceful_stop(motor, power, pending, torque, ui, drain):
    """正常停机：1 秒降扭矩，再失能并直接关闭电源。

    CAN/反馈故障时上层走立即失能，不延长故障状态来执行斜坡。
    """
    began = time.perf_counter()
    try:
        while True:
            ratio = min(1.0, (time.perf_counter()-began)/STOP_TORQUE_RAMP_S)
            motor.set_mit(MitCommand(torque=torque*(1-ratio)))
            state = motor.state(max_age=FEEDBACK_TIMEOUT_S)
            if state.error_code or motor.stats()['fault']:
                raise RuntimeError('motor fault during controlled stop')
            drain('stopping_torque')
            ui._notice = f'停止中：电机扭矩 {torque*(1-ratio):.3f}，保持阻尼'
            ui.app.invalidate()
            if ratio >= 1:
                break
            time.sleep(.005)
    finally:
        # 即使斜坡期间异常，也尝试失能；保留原始异常。
        original = sys.exception()
        try:
            motor.disable()
        except Exception as exc:
            if original is None:
                raise
            original.add_note(f'Disable during stop failed: {exc}')
    # 串口操作顺序完成后关闭输出，不再阶梯降低电流。
    if pending is not None:
        pending.result()
    power.disable()
    ui._notice = '停止完成：电机已失能，电源输出已关闭'
    ui.app.invalidate()


def run(motor, dyno, power, pool, ui, writer, summary, *, torque=None):
    torque = TORQUE_FF if torque is None else torque
    progress = Progress(time.perf_counter())
    current = 0.0
    load_steps = 0
    pending = None
    next_step = math.inf
    limit_at = None
    next_display = 0
    reason = 'interrupted'
    recorder = CurveRecorder(writer, progress, FEEDBACK_TIMEOUT_S)
    applied_torque = 0.0
    motor_started = False
    def drain(phase):
        motor.drain_states()  # 停机过程不属于曲线时间范围。
        if motor.stats().get('recording_dropped', 0):
            raise RuntimeError('motor recording queue overflow')
        dyno.drain()
    try:
        motor.start_recording()
        motor.enable()
        motor_started = True
        motor.set_mit(MitCommand())
        ramp_started = time.perf_counter()
        target_reached = False
        progress.started = math.inf  # 到达最大扭矩后才开始保持计时
        while True:
            now = time.perf_counter()
            applied_torque = ramp_torque(now-ramp_started, torque)
            motor.set_mit(MitCommand(torque=applied_torque))
            if not target_reached and now-ramp_started >= TORQUE_RAMP_S:
                target_reached = True
                progress.started = time.perf_counter()
            if not target_reached:
                progress.stability_text = f'扭矩线性上升 {now-ramp_started:.2f}/{TORQUE_RAMP_S:g}s'
            state = motor.state(max_age=FEEDBACK_TIMEOUT_S)
            stats = motor.stats()
            if stats['fault'] or state.error_code:
                raise RuntimeError(f'motor fault: {stats["fault"]}, error={state.error_code}')
            if state.motor_temperature >= MAX_MOTOR_TEMP_C or state.board_temperature >= MAX_DRIVER_TEMP_C:
                raise RuntimeError('temperature limit reached')
            ds = dyno.state(max_age=FEEDBACK_TIMEOUT_S)
            if dyno.stats()['dropped_samples']:
                raise RuntimeError('dynamometer sample queue overflow')
            if progress.loading_at is None:
                if target_reached and progress.update(state):
                    progress.loading_at = time.perf_counter()
                    next_step = progress.loading_at
            # 逐条真实电机帧记录和判堵转，同批数据在确认帧处精确截断。
            motor_samples = motor.drain_states()
            recorder.add_dyno(dyno.drain())
            if motor.stats().get('recording_dropped', 0):
                raise RuntimeError('motor recording queue overflow')
            if recorder.record(motor_samples):
                reason = 'stall_confirmed'
                break
            line = ui.poll()
            if line is not None and line.strip().lower() in ('quit', 'q', 'exit', 'stop'):
                reason = 'operator_stop'
                break
            if pending is not None and pending.done():
                current, next_step = pending.result()
                pending = None
                if current >= MAX_CURRENT_A:
                    limit_at = time.perf_counter()
            if progress.loading_at is not None:
                if now-progress.loading_at >= TEST_TIMEOUT_S:
                    reason = 'test_timeout'
                    break
                if limit_at is not None and now-limit_at >= MAX_CURRENT_HOLD_S:
                    reason = 'current_limit_without_stall'
                    break
                if pending is None and now >= next_step and current < MAX_CURRENT_A:
                    load_steps += 1
                    target = load_target(load_steps)
                    # 只允许一个未完成事务；延迟时只发下一步，不追赶过去的周期。
                    pending = pool.submit(set_load_current, power, target)
            if now >= next_display:
                ui._panel = (f'ID={motor.node_id}  {"加载中" if progress.loading_at is not None else "最大扭矩等待中"}\n'
                    f'前馈扭矩 {applied_torque:.3f}/{torque}  加载电流限值 {current:.3f}/{MAX_CURRENT_A:.3f} A\n'
                    f'电机速度 {state.velocity:+.3f} rad/s  扭矩 {state.torque:+.4f}\n'
                    f'温度 {state.motor_temperature:.1f}/{state.board_temperature:.1f} C\n'
                    f'测功机 {ds.speed:.3f} rad/s  {ds.torque:+.4f} Nm  {ds.power:+.3f} W\n'
                    f'已记录 {recorder.count} 行   CAN {stats["actual_tx_hz"]:.1f} Hz  '
                    f'通信异常报告 {stats.get("can_protocol_reports", 0)} 次')
                ui._notice = (progress.stability_text if progress.loading_at is None
                              else 'quit / stop 停止测试；参数在脚本顶部设置')
                ui.app.invalidate()
                next_display = now+0.1
            time.sleep(0.001)
    finally:
        active_error = sys.exception()
        record_cutoff = time.perf_counter()
        try:
            if motor_started and (active_error is None or isinstance(active_error, KeyboardInterrupt)):
                graceful_stop(motor, power, pending, applied_torque, ui, drain)
                summary['controlled_stop_completed'] = True
            else:
                motor.disable()
        except Exception as cleanup:
            if active_error is None:
                raise
            active_error.add_note(f'Motor shutdown failed: {cleanup}')
        finally:
            if motor_started:
                motor.drain_states()
            if not recorder.finished:
                recorder.finish(record_cutoff)
            summary.update(reason=reason, rows=recorder.count, motor_rows=recorder.motor_count,
                           dyno_rows=recorder.dyno_count, loading_at=progress.loading_at,
                           motor_statistics=motor.stats(), dynamometer_statistics=dyno.stats())
    return reason


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id', type=int, default=MOTOR_ID)
    parser.add_argument('--serial', default=CANABLE_SERIAL, help='CANable USB serial number')
    parser.add_argument('--torque', type=float, default=TORQUE_FF,
                        help='maximum feedforward torque (firmware units), overrides TORQUE_FF')
    args = parser.parse_args(argv)
    try:
        validate(args.torque)  # 参数校验阶段不连接设备，不修改模块默认配置。
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))
    if not 1 <= args.id <= 127:
        parser.error('id must be in 1..127')
    return args


def main(argv=None):
    args = parse_args(argv)
    # Passive dyno discovery first; keep its port out of active SCPI discovery.
    matches = detect_dynamometers() if DYNO_PORT == 'auto' else [DYNO_PORT]
    if len(matches) != 1:
        raise RuntimeError(f'Expected one dynamometer, found {matches}; set DYNO_PORT')
    directory = Path(__file__).resolve().parent/'data'/f'{datetime.now():%Y%m%d-%H%M%S-%f}'
    directory.mkdir(parents=True)
    summary = {'motor_id': args.id, 'target_torque': args.torque, 'config': {k: v for k, v in globals().items()
               if k.isupper() and k != 'COLUMNS'}, 'timestamp': 'host perf_counter reception; no interpolation'}
    try:
        with Dynamometer(port=matches[0], torque_scale=DYNO_TORQUE_SCALE,
                         speed_scale=DYNO_SPEED_SCALE) as dyno:
            dyno.receive_state(timeout=1)
            port = resolve_power_port(POWER_PORT)
            summary.update(power_port=port, dynamometer_port=matches[0])
            with PowerSupply(port=port) as power:
                try:
                    power.disable()
                    power.set_current(0)
                    power.set_voltage(LOAD_VOLTAGE_V)
                    with TerminalConsole(title='电机 T-N 曲线测试', prompt='tn> ', commands='status | quit') as ui, \
                         (directory/'curve.csv').open('x', encoding='utf-8', newline='') as csvfile, \
                         ThreadPoolExecutor(max_workers=1, thread_name_prefix='load-power') as pool:
                        writer = csv.writer(csvfile)
                        writer.writerow(COLUMNS)
                        with Bench(transport=Canable(args.serial), motor_ids=[args.id], rate_hz=MOTOR_HZ) as bench:
                            power.enable()  # Zero current limit, independent motor battery.
                            with bench.start() as control:
                                run(control.motor(args.id), dyno, power, pool, ui, writer, summary,
                                    torque=args.torque)
                finally:
                    # Executor has joined any in-flight set_current before OFF.
                    active_error = sys.exception()
                    try:
                        power.disable()
                        summary['load_output_off_confirmed'] = True
                    except Exception as exc:
                        summary['load_output_off_confirmed'] = False
                        summary['load_shutdown_error'] = str(exc)
                        try:
                            power.disable(verify=False)
                        except Exception as fallback:
                            exc.add_note(f'Best-effort OFF failed: {fallback}')
                        if active_error is None:
                            raise
                        active_error.add_note(f'Load shutdown failed: {exc}')
    except BaseException as exc:
        summary['error'] = repr(exc)
        summary['reason'] = 'operator_stop' if isinstance(exc, KeyboardInterrupt) else 'error'
        raise
    finally:
        # 设备与文件上下文均已退出；离线平滑不会阻塞电机控制或延误停机。
        csv_path = directory/'curve.csv'
        if csv_path.exists():
            original = sys.exception()
            try:
                print('正在生成电机和测功机的速度、扭矩平滑列…', flush=True)
                append_smoothed_columns(csv_path, MOTOR_SPEED_SMOOTH_S,
                                        MOTOR_TORQUE_SMOOTH_S, FEEDBACK_TIMEOUT_S,
                                        DYNO_SPEED_SMOOTH_S, DYNO_TORQUE_SMOOTH_S)
            except Exception as exc:
                if original is None:
                    raise
                original.add_note(f'CSV smoothing failed; original data preserved: {exc}')
        print(f'Test result: {summary.get("reason", "incomplete")}; rows={summary.get("rows", 0)}\nCSV: {directory / "curve.csv"}')


if __name__ == '__main__':
    main()
