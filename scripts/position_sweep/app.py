"""位置环连续线性扫频：缓慢回零、停稳、增加摆幅、扫频、收回零位、失能。

先检查下方“用户配置”的 9 项，确认 KP、KD 为本次测试需要的实际增益。
配置完成后，在 mc_tools 目录直接运行，无需在命令行重复填写参数：
    python -m scripts.position_sweep

只导出目标预览，不连接电机（同样使用下方配置）：
    python -m scripts.position_sweep --preview

命令行参数可临时覆盖对应配置，未传的参数仍使用下方配置，例如：
    python -m scripts.position_sweep --stop-hz 5 --duration 20
若将 KP 或 KD 设为 None，则必须在命令行指定对应的 --kp 或 --kd。
CANABLE_SERIAL 留空或未匹配时自动探测：只有一台则使用，没有或有多台则报错。

start-hz/stop-hz 是位置正弦频率；rate 是 CAN 目标发送频率。
默认位置摆幅 ±1 rad，目标速度和前馈扭矩为 0。Ctrl+C 立即尝试失能。
实机使用原生 C 收发线程；首次运行自动用 MinGW gcc 编译，Python 仅负责配置和保存 CSV。
"""
import argparse
from contextlib import contextmanager
from datetime import datetime
import gc
import math
from pathlib import Path
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from motor_platform import Bench, Canable, MitCommand
from motor_platform.components.motor.protocol import validate_node
from scripts.position_sweep.recording import Recorder
from scripts.position_sweep.trajectory import Chirp, Home, Source


# 用户配置：日常只需修改这 9 项，命令行参数可覆盖。
MOTOR_ID = 1                       # 1. 关节 ID
CANABLE_SERIAL = None              # 2. CANable SN；留空或未匹配时，仅自动选择唯一设备
COMMUNICATION_HZ = 1000.0          # 3. 通信目标频率，Hz
START_HZ = 0.1                     # 4. 扫频起始频率，Hz
STOP_HZ = 10.0                     # 5. 扫频截止频率，Hz
SWEEP_DURATION_S = 30.0            # 6. 完整摆幅扫频时间，s
AMPLITUDE_RAD = 1.0                # 7. 单边摆幅，rad；1 表示 -1..+1 rad
KP = 100                           # 8. 位置增益；填写数值或传 --kp
KD = 2                             # 9. 速度增益；填写数值或传 --kd


def chirp_from_args(args):
    return Chirp(args.start_hz, args.stop_hz, args.duration, args.amplitude,
                 args.ramp, args.kp, args.kd, args.velocity_mode)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", type=int, default=MOTOR_ID)
    parser.add_argument("--serial", default=CANABLE_SERIAL, help="preferred CANable SN; unmatched/empty selects the only detected adapter")
    parser.add_argument("--rate", type=float, default=COMMUNICATION_HZ, help="CAN target TX Hz")
    parser.add_argument("--start-hz", type=float, default=START_HZ)
    parser.add_argument("--stop-hz", type=float, default=STOP_HZ)
    parser.add_argument("--duration", type=float, default=SWEEP_DURATION_S,
                        help="full-amplitude linear sweep seconds, excluding ramps/homing")
    parser.add_argument("--amplitude", type=float, default=AMPLITUDE_RAD, help="one-sided amplitude, rad")
    parser.add_argument("--kp", type=float, default=KP, help="position gain, >0, resolution 0.01")
    parser.add_argument("--kd", type=float, default=KD, help="velocity gain, >=0, resolution 0.01")
    # Retain advanced CLI compatibility, without cluttering normal --help.
    parser.add_argument("--velocity-mode", choices=("zero", "derivative"), help=argparse.SUPPRESS)
    for option in ("home-speed", "settle", "home-timeout", "zero-tolerance", "zero-velocity",
                   "ramp", "max-motor-temp", "max-driver-temp"):
        parser.add_argument("--"+option, type=float, help=argparse.SUPPRESS)
    parser.set_defaults(**_INTERNAL_DEFAULTS)
    parser.add_argument("--output", type=Path, help="exclusive CSV path; never overwrite existing data")
    parser.add_argument("--preview", action="store_true", help="only export targets; never connect to hardware")
    args = parser.parse_args(argv)
    try:
        validate_node(args.id)
        if args.serial is not None:
            if not isinstance(args.serial, str):
                raise ValueError("CANable SN must be a string or None")
            args.serial = args.serial.strip() or None
        if args.kp is None or args.kd is None:
            raise ValueError("请指定 --kp 和 --kd，或修改脚本中的 KP/KD")
        for name in ("rate", "start_hz", "stop_hz", "duration", "amplitude", "home_speed",
                     "settle", "home_timeout", "zero_tolerance", "zero_velocity", "ramp",
                     "max_motor_temp", "max_driver_temp"):
            value = getattr(args, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if args.rate < 20:
            raise ValueError("rate must be >=20 Hz for the 100 ms feedback watchdog")
        if args.stop_hz < args.start_hz:
            raise ValueError("stop-hz must be >= start-hz")
        if args.stop_hz >= args.rate/2:
            raise ValueError("stop-hz must be below rate/2 (sampling limit)")
        if args.home_timeout <= args.settle:
            raise ValueError("home-timeout must exceed settle")
        MitCommand(args.amplitude, args.amplitude*(math.tau*args.stop_hz + 1.875/args.ramp),
                   0.0, args.kp, args.kd).pack()
        if args.kp < 0.01:
            raise ValueError("kp must be at least 0.01 for position control")
        if not math.isfinite((args.duration+2*args.ramp)*max(args.stop_hz, args.rate)):
            raise ValueError("sweep duration/frequency is too large")
    except (ValueError, TypeError, OverflowError) as exc:
        parser.error(str(exc))
    return args


def resolve_serial(preferred):
    """Select before opening CAN; never choose arbitrarily among adapters."""
    devices = Canable.detect()
    matches = [device for device in devices if preferred and device["serial_number"] == preferred]
    if len(matches) == 1:
        return matches[0]["serial_number"]
    if len(matches) > 1:
        raise OSError(f"CANable SN {preferred} 匹配到多台设备，无法唯一选择")
    if not devices:
        raise OSError("未检测到 CANable 适配器")
    if len(devices) != 1:
        serials = ", ".join(device["serial_number"] for device in devices)
        raise OSError(f"CANable SN {preferred!r} 未匹配，检测到多台设备：{serials}；请填写正确 SN")
    actual = devices[0]["serial_number"]
    if preferred:
        print(f"配置 SN {preferred} 未匹配，自动使用唯一 CANable：{actual}", flush=True)
    return actual


def check_state(state, args):
    if state.error_code:
        raise RuntimeError(f"motor fault 0x{state.error_code:04X}")
    if state.motor_temperature >= args.max_motor_temp or state.board_temperature >= args.max_driver_temp:
        raise RuntimeError(f"temperature limit: motor={state.motor_temperature:g}, "
                           f"driver={state.board_temperature:g} C")


def collect(motor, recorder, args, *, check=True, require_active=False):
    samples = motor.drain_states()
    # Save the entire batch, including a fault frame, before interpreting it.
    for sample in samples:
        recorder.write(sample)
    if check:
        for sample in samples:
            check_state(sample, args)
        stats = motor.stats()
        if stats.get("recording_dropped", 0):
            raise RuntimeError("motor feedback recording queue overflow; CSV is incomplete")
        if stats.get("fault"):
            raise RuntimeError(f"motor runtime fault: {stats['fault']}")
        if require_active and not stats["periodic_active"]:
            raise RuntimeError("periodic motor control stopped")
    return samples


class ZeroWait:
    """Require a continuous interval of actual feedback within zero tolerances."""
    def __init__(self, after, args):
        self.after, self.args = after, args
        self.since = None
        self.ready = False
        self.previous = None

    def update(self, samples):
        for state in samples:
            if state.received_at < self.after:
                continue
            if self.previous is not None and state.received_at-self.previous > FEEDBACK_TIMEOUT_S:
                self.since, self.ready = None, False
            self.previous = state.received_at
            if abs(state.position) <= self.args.zero_tolerance and abs(state.velocity) <= self.args.zero_velocity:
                if self.since is None:
                    self.since = state.received_at
                self.ready = state.received_at-self.since >= self.args.settle
            else:
                self.since, self.ready = None, False


def run(motor, recorder, args):
    motor.start_recording()
    try:
        motor.enable()
        initial = motor.state(max_age=FEEDBACK_TIMEOUT_S)
        collect(motor, recorder, args)
        check_state(initial, args)
        if abs(initial.velocity) > args.zero_velocity:
            raise RuntimeError(f"initial motor is moving: {initial.velocity:g} rad/s")
        home = Home(initial.position, args.home_speed, args.kp, args.kd, args.velocity_mode)
        if not math.isfinite(home.duration):
            raise ValueError("homing duration is too large; check initial position/home-speed")
        source = Source(home, time.perf_counter(), recorder)
        end_at = source.started+home.duration
        zero = ZeroWait(end_at, args)
        sweeping = False
        motor.set_mit_source(source, on_sent=source.on_sent)
        next_display = 0.0
        while True:
            samples = collect(motor, recorder, args, require_active=True)
            state = motor.state(max_age=FEEDBACK_TIMEOUT_S)
            check_state(state, args)
            now = time.perf_counter()
            zero.update(samples)
            if zero.ready:
                if sweeping:
                    return
                plan = chirp_from_args(args)
                recorder.begin_capture(now)
                source = Source(plan, now, recorder)
                end_at = now+plan.total_duration
                zero = ZeroWait(end_at, args)
                sweeping = True
            elif now > end_at+args.home_timeout:
                raise TimeoutError("motor did not settle at zero within home-timeout")
            motor.set_mit_source(source, on_sent=source.on_sent)
            if now >= next_display:
                stats = motor.stats()
                reference = source.plan.sample(now-source.started)
                print(f"{reference.phase:10s} f={reference.frequency_hz:7.3f} Hz "
                      f"p={state.position:+.4f} rad v={state.velocity:+.4f} rad/s "
                      f"temp={state.motor_temperature:.1f}/{state.board_temperature:.1f} C "
                      f"TX={stats['actual_tx_hz']:.1f} Hz RX={recorder.rows}", flush=True)
                next_display = now+0.5
            time.sleep(0.005)
    finally:
        original = sys.exception()
        try:
            motor.disable()
        except Exception as cleanup:
            if original is None:
                raise
            original.add_note(f"Disable unconfirmed: {cleanup}")


def preview(args, recorder):
    chirp = chirp_from_args(args)
    total = chirp.total_duration+args.settle
    if not math.isfinite(total*args.rate):
        raise ValueError("preview duration/rate is too large")
    for index in range(math.ceil(total*args.rate)+1):
        t = min(index/args.rate, total)
        recorder.preview(chirp.sample(t))


@contextmanager
def control_scheduling():
    """Limit GIL contention and cyclic-GC pauses; restore after worker shutdown."""
    previous = sys.getswitchinterval()
    gc_enabled = gc.isenabled()
    sys.setswitchinterval(min(previous, 0.0002))
    gc.disable()  # Packet records are acyclic; reference counting remains active.
    try:
        yield
    finally:
        sys.setswitchinterval(previous)
        if gc_enabled:
            gc.enable()


def main(argv=None):
    args = parse_args(argv)
    output = args.output or Path(__file__).resolve().parent / "data" / (
        datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        + f"_{args.stop_hz:g}_{args.amplitude:g}_{args.kp:g}_{args.kd:g}"
        + ("-preview.csv" if args.preview else ".csv"))
    print(f"ID={args.id} | {args.start_hz:g}->{args.stop_hz:g} Hz / {args.duration:g} s | "
          f"+/-{args.amplitude:g} rad | kp={args.kp:g} kd={args.kd:g} | "
          f"CAN={args.rate:g} Hz | velocity={args.velocity_mode}", flush=True)
    print(f"CSV: {output.resolve()}", flush=True)
    motor = recorder = None
    result = 0
    try:
        with Recorder(output, time.perf_counter(), capture_enabled=False,
                      defer_csv=not args.preview) as recorder:
            if args.preview:
                preview(args, recorder)
            else:
                try:
                    serial = resolve_serial(args.serial)
                    print(f"CANable SN: {serial}", flush=True)
                    with Bench(transport=Canable(serial), motor_ids=[args.id], rate_hz=args.rate) as bench:
                        bench.motor(args.id).disable()
                        from motor_platform.transports.canable import _Usb
                        if isinstance(getattr(bench._transport, '_usb', None), _Usb):
                            from scripts.position_sweep.native import run_native
                            run_native(bench, recorder, args)
                        else:
                            # Non-USB simulation retains the portable runtime path.
                            with control_scheduling(), bench.start(command_timeout=COMMAND_TIMEOUT_S) as control:
                                motor = control.motor(args.id)
                                run(motor, recorder, args)
                finally:
                    # The worker has stopped: also retain TPDOs received during
                    # normal disable or the runtime's own exception cleanup.
                    if motor is not None:
                        original = sys.exception()
                        try:
                            collect(motor, recorder, args)
                        except Exception as cleanup:
                            if original is None:
                                raise
                            original.add_note(f"Final recording/status: {cleanup}")
    except KeyboardInterrupt as exc:
        print("已中止，已尝试失能。", flush=True)
        for note in getattr(exc, "__notes__", ()):
            print(note, file=sys.stderr, flush=True)
        result = 130
    except Exception as exc:
        print(f"测试失败：{exc}", file=sys.stderr, flush=True)
        for note in getattr(exc, "__notes__", ()):
            print(note, file=sys.stderr, flush=True)
        result = 1
    if recorder is not None:
        if args.preview:
            print(f"仅目标预览：{recorder.rows} 行；反馈列留空，未连接硬件。", flush=True)
        else:
            print(f"已保存 {recorder.rows} 包反馈，主机平均接收 {recorder.actual_rx_hz:.1f} Hz。", flush=True)
            if result == 0:
                print("扫频完成，已回零并收到失能 ACK。", flush=True)
    if motor is not None:
        print("Final statistics:", motor.stats(), flush=True)
    return result


# 内部维护区：回零、收尾和保护参数，日常配置无需修改。
FEEDBACK_TIMEOUT_S = 0.1
COMMAND_TIMEOUT_S = 0.25
_INTERNAL_DEFAULTS = dict(
    velocity_mode="zero",
    home_speed=1.0,
    settle=0.5,
    home_timeout=10.0,
    zero_tolerance=0.05,
    zero_velocity=0.1,
    ramp=2.0,
    max_motor_temp=80.0,
    max_driver_temp=80.0,
)


if __name__ == "__main__":
    raise SystemExit(main())
