"""MIT 电机控制与状态看板。

在 mc_tools 目录运行：
    python -m scripts.mit_control --id 1 --rate 1000

运行中可输入：
    set p v tau kp kd   设置 MIT 指令
    stop                指令清零
    enable / disable    使能或失能
    rate hz             修改发送频率
    quit                失能并退出

终端上方实时显示反馈，下方编辑命令；↑↓ 历史，回车下发。
脚本默认持续运行，反馈数据保存在本目录的 data 文件夹。
"""
import argparse
from datetime import datetime
import math
from pathlib import Path
import shlex
import time
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from motor_platform import Bench, MitCommand
from motor_platform.core.recording import StateRecorder
from scripts.mit_control.console import ConsoleDashboard

# Change these per application; no global joint registry is loaded.
MOTOR_ID = 1
CANABLE_SERIAL = "20903498384550052"
COMMUNICATION_HZ = 1000

def parse_console_command(line):
    """Parse one operator command without touching hardware."""
    parts = shlex.split(line)
    if not parts:
        return "empty", None
    name = parts[0].lower()
    if name == "set":
        if len(parts) != 6:
            raise ValueError("usage: set <position> <velocity> <torque> <kp> <kd>")
        try:
            command = MitCommand(*(float(value) for value in parts[1:]))
            command.pack()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid MIT command: {exc}") from exc
        return "set", command
    if name == "rate":
        if len(parts) != 2:
            raise ValueError("usage: rate <hz>")
        try:
            rate = float(parts[1])
        except ValueError as exc:
            raise ValueError("rate must be a number") from exc
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("rate must be finite and positive")
        return "rate", rate
    if name in {"stop", "enable", "disable", "status", "help", "quit", "q", "exit"}:
        if len(parts) != 1:
            raise ValueError(f"{name} takes no arguments")
        return ("quit" if name in {"q", "exit"} else name), None
    raise ValueError(f"unknown command: {name}; type help")


class _NullDashboard:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", type=int, default=MOTOR_ID)
    parser.add_argument("--serial", default=CANABLE_SERIAL)
    parser.add_argument("--rate", type=float, default=COMMUNICATION_HZ, help="target CAN TX frequency in Hz")
    parser.add_argument("--position", type=float, default=0, help="position in rad")
    parser.add_argument("--velocity", type=float, default=0, help="velocity in rad/s")
    parser.add_argument("--torque", type=float, default=0, help="feedforward torque, firmware units")
    parser.add_argument("--kp", type=float, default=0, help="position gain")
    parser.add_argument("--kd", type=float, default=0, help="velocity gain")
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="run duration in seconds; default 0 runs continuously until Ctrl+C",
    )
    parser.add_argument("--display-hz", type=float, default=10, help="console refresh rate; independent of CAN rate")
    parser.add_argument("--output", type=Path, help="feedback CSV path, created exclusively")
    parser.add_argument("--no-console", action="store_true", help="disable runtime keyboard commands")
    args = parser.parse_args(argv)
    try:
        command = MitCommand(args.position, args.velocity, args.torque, args.kp, args.kd)
        command.pack()
        for name in ("rate", "display_hz"):
            value = getattr(args, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not math.isfinite(args.duration) or args.duration < 0:
            raise ValueError("duration must be finite and non-negative; 0 means continuous")
        if args.display_hz > 100:
            raise ValueError("display-hz must be <=100")
        # Validate all script configuration before touching hardware.
        Bench(motor_ids=[args.id], serial=args.serial, rate_hz=args.rate)
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    return args, command


def main(argv=None):
    args, command = parse_args(argv)
    duration_text = "continuous until Ctrl+C" if args.duration == 0 else f"{args.duration:g} s"
    print(
        f"ID={args.id}, target rate={args.rate:g} Hz, duration={duration_text}, command={command}",
        flush=True,
    )
    output = args.output or Path(__file__).resolve().parent / "data" / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-mit.csv")
    motor = None
    desired = command
    enabled = False
    interactive = not args.no_console
    dashboard_context = ConsoleDashboard() if interactive else _NullDashboard()
    shutdown_notice = ''
    # Initialize the terminal before enabling: UI startup cannot consume a lease.
    with StateRecorder(output) as recorder, dashboard_context as dashboard:
        with Bench(motor_ids=[args.id], serial=args.serial, rate_hz=args.rate) as bench:
            raw = bench.motor(args.id)
            raw.disable()
            with bench.start(command_timeout=0.25) as control:
                motor = control.motor(args.id)
                try:
                    motor.enable()
                    enabled = True
                    initial = motor.state()
                    if initial.error_code or abs(initial.velocity) > 0.1:
                        raise RuntimeError(f"initial motor feedback not ready: {initial}")
                    recorder.write(initial)
                    motor.set_mit(desired)
                    started = time.perf_counter()
                    next_display = started
                    previous_rx = initial.received_at
                    notice = "0x400 控制帧已收到 TPDO5 回传，电机已自动使能"
                    while args.duration == 0 or time.perf_counter() - started < args.duration:
                        line = dashboard.poll() if interactive else None
                        if line is not None:
                            try:
                                action, value = parse_console_command(line)
                                if action == "set":
                                    desired = value
                                    if enabled:
                                        motor.set_mit(desired)
                                    notice = "MIT 目标已更新"
                                elif action == "stop":
                                    desired = MitCommand()
                                    if enabled:
                                        motor.set_mit(desired)
                                    notice = "MIT 五项已清零，电机保持使能"
                                elif action == "rate":
                                    motor.set_rate(value)
                                    notice = f"目标发送频率已改为 {value:g} Hz"
                                elif action == "enable":
                                    # Re-send the neutral enabling RPDO, then restore the target.
                                    motor.enable()
                                    enabled = True
                                    motor.set_mit(desired)
                                    notice = "0x400 控制帧已收到回传，电机已自动使能并恢复 MIT 目标"
                                elif action == "disable":
                                    if enabled:
                                        motor.disable()
                                        enabled = False
                                        notice = "已收到失能控制字 ACK，周期 MIT 已停止"
                                    else:
                                        notice = "电机已经由本脚本失能"
                                elif action == "status":
                                    notice = "状态已显示在看板中"
                                elif action == "help":
                                    notice = "set p v tau kp kd | stop | rate hz | enable | disable | quit"
                                elif action == "quit":
                                    raise KeyboardInterrupt
                            except ValueError as exc:
                                notice = f"命令错误: {exc}"

                        if enabled:
                            motor.set_mit(desired)  # Refresh application lease at about 100 Hz.
                        try:
                            state = motor.state(max_age=0.1)
                        except TimeoutError:
                            if enabled:
                                raise
                        stats = motor.stats()
                        if state.error_code or stats["fault"] or (enabled and not stats["periodic_active"]):
                            raise RuntimeError(f"motor fault: {state}, {stats}")
                        if state.received_at != previous_rx:
                            recorder.write(state)
                            previous_rx = state.received_at
                        now = time.perf_counter()
                        status_due = now >= next_display
                        if status_due or (interactive and getattr(dashboard, "dirty", False)):
                            if interactive:
                                dashboard.render(node_id=args.id, enabled=enabled, desired=desired,
                                                 state=state, stats=stats, notice=notice, started=started)
                            else:
                                print(f"id={state.node_id} p={state.position:+.5f} rad "
                                      f"v={state.velocity:+.5f} rad/s tau={state.torque:+.5f} "
                                      f"temp={state.motor_temperature:.1f}/{state.board_temperature:.1f} C "
                                      f"err=0x{state.error_code:04X} tx/rx={stats['tx_count']}/{stats['rx_count']} "
                                      f"actual={stats['actual_tx_hz']:.1f} Hz", flush=True)
                            if status_due:
                                next_display = now + 1/args.display_hz
                        time.sleep(0.01)
                except KeyboardInterrupt:
                    shutdown_notice = "Exit requested."
                finally:
                    # Preserve the original fault if shutdown cannot confirm disable.
                    if enabled:
                        original = sys.exc_info()[1]
                        try:
                            if motor.stats().get('fault') == 'application command lease expired':
                                shutdown_notice = 'Lease expired: best-effort disable already sent; ACK unconfirmed.'
                            else:
                                motor.disable()
                                shutdown_notice = 'Disable ACK received.'
                        except Exception as cleanup:
                            if original is None:
                                raise
                            original.add_note(f'Disable unconfirmed: {cleanup}')
                        enabled = False
    if shutdown_notice:
        print(shutdown_notice, flush=True)
    print(f"Feedback snapshots: {output.resolve()}", flush=True)
    if motor is not None:
        print("Final statistics:", motor.stats(), flush=True)


if __name__ == "__main__":
    main()
