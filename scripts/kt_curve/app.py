"""关节输出扭矩一致性测试：固定阻尼，电机目标扭矩在 5 秒内从 0 线性升至指定值。

在平台根目录运行：
    python -m scripts.kt_curve --id 1 --serial <SN> --torque 30
阻尼器默认 24 V / 3 A，可用 --voltage / --current 覆盖。电机由独立电池供电。
电源开启后先保持 0.5 秒，再开始电机扭矩爬升；quit / Ctrl+C 提前结束。
采集后 1 秒降扭矩，再失能并关闭电源。
3 秒指采集斜坡，不包含连接和停机；Windows 调度并非硬实时。仅输出 data/ 下一个 CSV。
CSV以电机时间为准，一对一匹配测功机帧，多余帧舍弃；实际下发目标也逐帧记录。
"""
import argparse
import csv
from datetime import datetime
import math
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from motor_platform import Bench, Canable, Dynamometer, MitCommand, PowerSupply
from motor_platform.components.dynamometer import detect_dynamometers
from motor_platform.components.power_supply.discovery import resolve_power_port
from utils.terminal import TerminalConsole
from utils.smoothing import append_smoothed_columns
from utils.csv_streams import pack_streams
from scripts.kt_curve.recording import Recorder, OUTPUT_COLUMNS

# 实验配置只属于本脚本；设备组件不保存电机 ID、适配器 SN 或加载参数。
MOTOR_ID = 1
CANABLE_SERIAL = None           # 填写固定 SN 或用 --serial；查看：python -m motor_platform --list
TORQUE_FF = None                # 必须填写最大目标或传 --torque；单位沿用电机固件，支持负方向
LOAD_VOLTAGE_V = 24.0           # 阻尼器电源电压，--voltage 覆盖
LOAD_CURRENT_A = 3.0            # 阻尼器限流，--current 覆盖；整个采集期间保持不变
LOAD_SETTLE_S = 0.5             # 电源输出确认开启后保持，再使能电机；不计入 5 秒采集
POWER_PORT = 'auto'             # 多台电源时在此指定 COM 端口
DYNO_PORT = 'auto'              # 多台测功机时在此指定 COM 端口
RAMP_DURATION_S = 3.0           # 从开始爬升到结束采集的时间
MOTOR_HZ = 1000                 # 周期 CAN 发送目标频率；目标值按实际经过时间计算
TARGET_UPDATE_S = 0.001         # 应用更新目标的间隔；迟到不补发积压命令
STOP_RAMP_S = 1.0               # 正常结束、用户停止：保持阻尼，线性降扭矩到零
FEEDBACK_TIMEOUT_S = 0.1
MAX_MOTOR_TEMP_C = 80.0
MAX_DRIVER_TEMP_C = 80.0
DYNO_TORQUE_SCALE = 100.0
DYNO_SPEED_SCALE = 100.0
SMOOTH_WINDOW_S = 0.30          # 停机后平滑两路扭矩；原始样本不修改


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id', type=int, default=MOTOR_ID)
    parser.add_argument('--serial', default=CANABLE_SERIAL)
    parser.add_argument('--torque', type=float, default=TORQUE_FF)
    parser.add_argument('--voltage', type=float, default=LOAD_VOLTAGE_V)
    parser.add_argument('--current', type=float, default=LOAD_CURRENT_A)
    args = parser.parse_args(argv)
    if not 1 <= args.id <= 127:
        parser.error('id must be in 1..127')
    if not args.serial or not args.serial.strip():
        parser.error('指定 --serial <SN>，可用 python -m motor_platform --list 查看')
    if args.torque is None or not math.isfinite(args.torque) or args.torque == 0:
        parser.error('指定有限非零的 --torque 最大目标扭矩')
    for name in ('voltage', 'current'):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(f'{name} must be finite and positive')
    if round(args.voltage, 2) != args.voltage or round(args.current, 3) != args.current:
        parser.error('电压最多两位小数，电流最多三位小数')
    for name in ('LOAD_SETTLE_S', 'RAMP_DURATION_S', 'MOTOR_HZ', 'TARGET_UPDATE_S', 'STOP_RAMP_S',
                 'FEEDBACK_TIMEOUT_S', 'MAX_MOTOR_TEMP_C', 'MAX_DRIVER_TEMP_C',
                 'DYNO_TORQUE_SCALE', 'DYNO_SPEED_SCALE', 'SMOOTH_WINDOW_S'):
        if not math.isfinite(globals()[name]) or globals()[name] <= 0:
            parser.error(f'{name} must be finite and positive')
    try:
        MitCommand(torque=args.torque).pack()
    except (ValueError, OverflowError) as exc:
        parser.error(str(exc))
    return args


def ramp_torque(elapsed, maximum):
    return maximum * max(0.0, min(1.0, elapsed / RAMP_DURATION_S))


def check_motor(motor):
    state = motor.state(max_age=FEEDBACK_TIMEOUT_S)
    stats = motor.stats()
    if state.error_code or stats['fault']:
        raise RuntimeError(f'motor fault: {state.error_code}, {stats["fault"]}')
    if state.motor_temperature >= MAX_MOTOR_TEMP_C or state.board_temperature >= MAX_DRIVER_TEMP_C:
        raise RuntimeError('motor/driver temperature limit reached')
    if stats.get('recording_dropped', 0):
        raise RuntimeError('motor recording queue overflow')
    return state, stats


def stop_motor(motor, torque, controlled, collect):
    try:
        if controlled:
            began = time.perf_counter()
            while True:
                ratio = min(1.0, (time.perf_counter()-began)/STOP_RAMP_S)
                motor.set_mit(MitCommand(torque=torque*(1-ratio)))
                check_motor(motor)
                collect()  # 接收线程可能刚入队的截止前样本也保留；停机样本被时间边界过滤。
                if ratio >= 1:
                    break
                time.sleep(.005)
    finally:
        original = sys.exception()
        try:
            motor.disable()
        except Exception as exc:
            if original is None:
                raise
            original.add_note(f'Motor disable failed: {exc}')


def run(motor, dyno, ui, args, result):
    recorder = None
    enabled = False
    applied = 0.0
    def collect():
        motors, dynos = motor.drain_states(), dyno.drain()
        if recorder is not None:
            recorder.add('motor', motors)
            recorder.add('dyno', dynos)
    try:
        motor.start_recording()
        dyno.drain()
        dropped_before = dyno.stats()['dropped_samples']
        motor.enable()
        enabled = True
        check_motor(motor)
        dyno.state(max_age=FEEDBACK_TIMEOUT_S)
        collect()
        recorder = Recorder(time.perf_counter(), RAMP_DURATION_S)
        result['recorder'] = recorder
        next_display = 0.0
        while True:
            now = time.perf_counter()
            state, stats = check_motor(motor)
            ds = dyno.state(max_age=FEEDBACK_TIMEOUT_S)
            if dyno.stats()['dropped_samples'] != dropped_before:
                raise RuntimeError('dynamometer sample queue overflow')
            line = ui.poll()
            if line is not None and line.strip().lower() in ('quit', 'stop', 'q', 'exit'):
                recorder.cutoff = min(recorder.cutoff, time.perf_counter())
                result['reason'] = 'operator_stop'
                break
            applied = ramp_torque(now-recorder.origin, args.torque)
            motor.set_mit(MitCommand(torque=applied))
            recorder.command(time.perf_counter(), applied)
            collect()
            if now >= recorder.cutoff:
                result['reason'] = 'completed'
                break
            if now >= next_display:
                ui._panel = (f'ID={args.id}  扭矩爬升 {now-recorder.origin:.2f}/{RAMP_DURATION_S:g} s\n'
                    f'下发目标 {applied:+.4f}/{args.torque:+g}  阻尼电源 {args.voltage:g} V / {args.current:g} A\n'
                    f'电机 {state.torque:+.4f}  {state.velocity:+.3f} rad/s\n'
                    f'测功机 {ds.torque:+.4f} Nm  {ds.speed:.3f} rad/s\n'
                    f'温度 电机/驱动器 {state.motor_temperature:.1f}/{state.board_temperature:.1f} C\n'
                    f'采集 {len(recorder.events)} 帧  CAN {stats["actual_tx_hz"]:.1f} Hz')
                ui._notice = 'quit / Ctrl+C 提前结束；采集后自动降扭矩'
                ui.app.invalidate()
                next_display = now+.1
            time.sleep(TARGET_UPDATE_S)
    except BaseException as exc:
        result['reason'] = 'operator_stop' if isinstance(exc, KeyboardInterrupt) else 'error'
        if recorder is not None:
            recorder.cutoff = min(recorder.cutoff, time.perf_counter())
        raise
    finally:
        original = sys.exception()
        try:
            ui._notice = '正在降扭矩、失能；随后关闭阻尼器电源'
            ui.app.invalidate()
            stop_motor(motor, applied, enabled and (original is None or isinstance(original, KeyboardInterrupt)), collect)
        except Exception as exc:
            result['reason'] = 'error'
            if original is None:
                raise
            original.add_note(f'Motor shutdown failed: {exc}')
        finally:
            collect()


def power_off(power):
    original = sys.exception()
    try:
        power.disable()
    except Exception as exc:
        try:
            power.disable(verify=False)
        except Exception as fallback:
            exc.add_note(f'Best-effort OFF failed: {fallback}')
        if original is None:
            raise
        original.add_note(f'Power shutdown failed: {exc}')


def main(argv=None, *, output_dir=None, quiet=False):
    args = parse_args(argv)  # 所有参数校验先于连接和输出操作。
    matches = detect_dynamometers() if DYNO_PORT == 'auto' else [DYNO_PORT]
    if len(matches) != 1:
        raise RuntimeError(f'Expected one dynamometer, found {matches}; set DYNO_PORT')
    result = {'reason': 'error'}
    directory = (Path(output_dir) if output_dir is not None else
                 Path(__file__).resolve().parent/'data'/f'{datetime.now():%Y%m%d-%H%M%S-%f}')
    directory.mkdir(parents=True)
    csv_path = directory/'kt_curve.csv'
    try:
        with csv_path.open('x', encoding='utf-8', newline='') as target:
            try:
                with Dynamometer(port=matches[0], torque_scale=DYNO_TORQUE_SCALE,
                                 speed_scale=DYNO_SPEED_SCALE) as dyno:
                    dyno.receive_state(timeout=1)
                    # 先占用被动测功机串口，再探测 SCPI 电源。
                    with PowerSupply(port=resolve_power_port(POWER_PORT)) as power:
                        try:
                            power.disable()
                            power.set_current(0)
                            power.set_voltage(args.voltage)
                            power.set_current(args.current)
                            with TerminalConsole(title='关节输出扭矩一致性测试', prompt='torque> ',
                                                 commands='status | quit') as ui:
                                with Bench(transport=Canable(args.serial), motor_ids=[args.id], rate_hz=MOTOR_HZ) as bench:
                                    power.enable()  # 电源读回已完成，之后采集循环不做阻塞 SCPI 查询。
                                    time.sleep(LOAD_SETTLE_S)  # 先建立阻尼，此时尚未使能电机。
                                    with bench.start() as control:
                                        try:
                                            run(control.motor(args.id), dyno, ui, args, result)
                                        finally:
                                            power_off(power)  # 降扭矩、失能之后立即关闭阻尼输出。
                        finally:
                            power_off(power)
            except BaseException as exc:
                result['reason'] = 'operator_stop' if isinstance(exc, KeyboardInterrupt) else 'error'
                raise
            finally:
                recorder = result.get('recorder', Recorder(0, RAMP_DURATION_S))
                recorder.write(csv.writer(target), args, result['reason'])
    finally:
        # 所有设备已经停止；平滑和磁盘输出不占用控制循环。
        original = sys.exception()
        try:
            if csv_path.exists():
                append_smoothed_columns(csv_path, SMOOTH_WINDOW_S, SMOOTH_WINDOW_S,
                                        FEEDBACK_TIMEOUT_S, SMOOTH_WINDOW_S, SMOOTH_WINDOW_S,
                                        output_columns=OUTPUT_COLUMNS)
                pack_streams(csv_path, align_motor=True)
        except Exception as exc:
            if original is None:
                raise
            original.add_note(f'CSV smoothing failed: {exc}')
        if not quiet:
            print(f'Test result: {result["reason"]}\nCSV: {csv_path}')
    return {'reason': result['reason'], 'csv': str(csv_path),
            'config': {k: v for k, v in globals().items() if k.isupper()}, 'arguments': vars(args)}


if __name__ == '__main__':
    main()
