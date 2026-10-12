"""J60 基础 CAN 控制验证。在 mc_tools 运行 python -m j60.mit_control。

仅查询：python -m j60.mit_control --probe
持续控制：python -m j60.mit_control --id 1 --rate 1000
运行中：set p v tau kp kd | stop | rate hz | enable | disable | status | quit
"""
import argparse
from contextlib import nullcontext
import csv
from datetime import datetime
import math
from pathlib import Path
import shlex
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from j60.canable import J60Canable, select_canable
from j60.client import J60Client
from j60.mit_control.console import COMMAND_HELP, ConsoleDashboard
from j60.protocol import (
    CONTROL, GET_CONFIG, GET_STATUS, MotionCommand, MotionFeedback, can_id, describe_faults,
)

MOTOR_ID = 1
COMMUNICATION_HZ = 1000


def validate_rate(value):
    if not math.isfinite(value) or not 1 <= value <= 1000:
        raise ValueError("rate must be in [1, 1000] Hz")
    return value


def parse_console_command(line):
    parts = shlex.split(line)
    if not parts:
        return "empty", None
    action = parts[0].lower()
    if action == "set":
        if len(parts) != 6:
            raise ValueError("usage: set <p> <v> <torque> <kp> <kd>")
        command = MotionCommand(*(float(value) for value in parts[1:]))
        command.pack()
        return action, command
    if action == "rate":
        if len(parts) != 2:
            raise ValueError("usage: rate <hz>")
        return action, validate_rate(float(parts[1]))
    if action in {"stop", "enable", "disable", "status", "help", "quit", "q", "exit"}:
        if len(parts) != 1:
            raise ValueError(f"{action} takes no arguments")
        return ("quit" if action in {"q", "exit"} else action), None
    raise ValueError(f"unknown command: {action}; type help")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", type=int, default=MOTOR_ID, help="J60 ID: 0..15")
    parser.add_argument("--serial", help="optional SN check; exactly one connected CANable is required")
    parser.add_argument("--rate", type=float, default=COMMUNICATION_HZ, help="target control rate: 1..1000 Hz")
    for name in ("position", "velocity", "torque", "kp", "kd"):
        parser.add_argument(f"--{name}", type=float, default=0)
    parser.add_argument("--duration", type=float, default=0, help="seconds; 0 runs until quit/Ctrl+C")
    parser.add_argument("--display-hz", type=float, default=10)
    parser.add_argument("--reply-timeout", type=float, default=0.2, help="ACK/status deadline in seconds")
    parser.add_argument("--feedback-timeout", type=float, default=0.1,
                        help="feedback deadline in seconds, at least two control periods")
    parser.add_argument("--status-interval", type=float, default=1.0, help="seconds between fault queries")
    parser.add_argument("--status-endian", choices=("little", "big"), default="little",
                        help="manual: little; public SDK status parser: big")
    parser.add_argument("--output", type=Path, help="CSV path, created exclusively")
    parser.add_argument("--no-console", action="store_true", help="fixed-parameter run without terminal UI")
    parser.add_argument("--probe", action="store_true", help="only read configuration/faults; never enable")
    args = parser.parse_args(argv)
    try:
        can_id(CONTROL, args.id)
        validate_rate(args.rate)
        command = MotionCommand(args.position, args.velocity, args.torque, args.kp, args.kd)
        command.pack()
        for name in ("display_hz", "reply_timeout", "feedback_timeout", "status_interval"):
            value = getattr(args, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if args.display_hz > 100:
            raise ValueError("display-hz must be <=100")
        if not math.isfinite(args.duration) or args.duration < 0:
            raise ValueError("duration must be finite and nonnegative")
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    return args, command


class FrameRecorder:
    """Raw TX/RX plus decoded motion replies; buffered I/O, no per-frame flush."""
    FIELDS = ("host_time_s", "direction", "can_id", "dlc", "is_fd", "brs", "raw_hex",
              "position_rad", "velocity_rad_s", "torque_nm", "temperature_flag", "temperature_c")

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("x", encoding="utf-8", newline="")
        self.writer = csv.DictWriter(self.file, self.FIELDS)
        self.writer.writeheader()

    def write(self, direction, frame, received_at):
        row = dict(host_time_s=f"{received_at:.9f}", direction=direction,
                   can_id=f"0x{frame.can_id:03X}", dlc=len(frame.data),
                   is_fd=int(frame.is_fd), brs=int(frame.brs), raw_hex=frame.data.hex(" ").upper())
        if direction == "RX" and frame.can_id >> 5 == CONTROL and len(frame.data) == 8:
            state = MotionFeedback.unpack(frame.data)
            row.update(position_rad=state.position, velocity_rad_s=state.velocity,
                       torque_nm=state.torque, temperature_flag=state.temperature_flag,
                       temperature_c=state.temperature)
        self.writer.writerow(row)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.file.close()


def _print_feedback(client, enabled, rate, started):
    state = client.feedback
    motion = "waiting for motion reply"
    if state:
        motion = (f"p={state.position:+.5f} rad v={state.velocity:+.5f} rad/s "
                  f"tau={state.torque:+.5f} Nm temp[{state.temperature_flag}]={state.temperature:.1f} C")
    actual = client.control_tx_count / max(time.perf_counter()-started, 1e-6)
    print(f"id={client.motor_id} enabled={enabled} {motion} "
          f"err=0x{(client.status_code or 0):04X} control_tx/rx={client.control_tx_count}/{client.motion_rx_count} "
          f"mean={actual:.1f}/{rate:g} Hz", flush=True)


def run(args, desired):
    # Discovery happens before the UI, CSV creation or any CAN startup.
    args.serial = select_canable(args.serial)
    output = args.output or Path(__file__).resolve().parent / "data" / (
        datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-j60.csv")
    print(f"J60 ID={args.id}, CANable={args.serial}, CAN=1 Mbps standard/classic, "
          f"rate={args.rate:g} Hz, command={desired}", flush=True)
    print(f"Control TX=0x{can_id(CONTROL, args.id):03X}; RX=0x{can_id(CONTROL, args.id, reply=True):03X}")
    interactive = not args.no_console and not args.probe
    dashboard_context = ConsoleDashboard() if interactive else nullcontext(None)
    with FrameRecorder(output) as recorder, dashboard_context as dashboard:
        transport = J60Canable(args.serial)
        client = J60Client(transport, args.id, timeout=args.reply_timeout,
                           status_endian=args.status_endian, on_frame=recorder.write)
        enabled = needs_disable = False
        try:
            transport.open()
            print(f"CANable firmware={transport.info['firmware']}; classic CAN started at 1 Mbps; "
                  f"nominal timing={getattr(transport, 'nominal_timing', None)}", flush=True)
            client.drain()
            print(f"Read config: TX=0x{can_id(GET_CONFIG, args.id):03X} DLC=0; "
                  f"expected RX=0x{can_id(GET_CONFIG, args.id, reply=True):03X}", flush=True)
            config = client.get_config()
            client.get_status()
            print(f"J60 config={config}")
            print(f"Status=0x{client.status_code:04X} ({describe_faults(client.status_code)}), "
                  f"DLC={len(client.status_raw)}, endian={args.status_endian}, raw={client.status_raw.hex(' ')}")
            if len(client.status_raw) != 2:
                print("Status reply differs from manual DLC=2; additional bytes are logged without interpretation.")
            print(f"CSV: {output}", flush=True)
            if args.probe:
                print("Configuration/status replies received. Probe completed without enabling the motor.")
                return output
            if config["motor_id"] != args.id:
                raise RuntimeError(f"configuration motor ID {config['motor_id']} differs from requested ID {args.id}")
            client.check_status()
            # Establish a known starting state. No calibration, zeroing or configuration writes.
            client.disable()
            needs_disable = True  # Set BEFORE sending enable, including uncertain-write failure.
            client.enable()
            enabled = True
            started = enabled_at = time.perf_counter()
            next_tx = next_display = next_input = started
            next_status = started + args.status_interval
            next_flush = started + 1
            pending_status_at = None
            skipped = 0
            notice = "使能 ACK=00，正在发送 J60 控制帧"
            while args.duration == 0 or time.perf_counter()-started < args.duration:
                client.check_status()  # Faults outrank the next ordinary control/console command.
                now = time.perf_counter()
                if interactive and now >= next_input:
                    next_input = now + 0.01
                    line = dashboard.poll()
                    if line is not None:
                        try:
                            action, value = parse_console_command(line)
                        except ValueError as exc:
                            notice = f"命令错误: {exc}"
                        else:
                            if action == "quit":
                                break
                            if action == "set":
                                desired = value
                                notice = "五个控制参数已更新"
                            elif action == "stop":
                                desired = MotionCommand()
                                notice = "五个控制参数清零，保持使能"
                            elif action == "rate":
                                args.rate, next_tx = value, time.perf_counter()
                                notice = f"目标频率 {value:g} Hz"
                            elif action == "disable":
                                if enabled:
                                    enabled = needs_disable = False
                                    client.disable()  # Failure is fatal; do not automatically resend.
                                    notice = "失能 ACK=00，停止周期控制"
                                else:
                                    notice = "已经失能"
                            elif action == "enable":
                                if not enabled:
                                    client.get_status()
                                    client.check_status()
                                    needs_disable = True
                                    client.enable()
                                    enabled = True
                                    enabled_at = next_tx = time.perf_counter()
                                    notice = "使能 ACK=00，恢复当前目标"
                                else:
                                    notice = "已经使能"
                            elif action == "status":
                                if pending_status_at is None:
                                    client.send(GET_STATUS)
                                    pending_status_at = time.perf_counter()
                                notice = "正在查询故障码"
                            elif action == "help":
                                notice = COMMAND_HELP

                now = time.perf_counter()
                if enabled and now >= next_tx:
                    client.send(CONTROL, desired.pack())
                    period = 1 / args.rate
                    steps = max(1, int((time.perf_counter()-next_tx) / period)+1)
                    skipped += steps-1
                    next_tx += steps*period  # Skip missed deadlines, never burst to catch up.

                now = time.perf_counter()
                if pending_status_at is not None:
                    if client.status_at >= pending_status_at:
                        pending_status_at = None
                        notice = f"故障码 0x{client.status_code:04X}，原始 {client.status_raw.hex(' ')}"
                    elif now-pending_status_at > args.reply_timeout:
                        raise TimeoutError("J60 periodic status reply timed out")
                if now >= next_status and pending_status_at is None:
                    client.send(GET_STATUS)
                    pending_status_at = time.perf_counter()
                    next_status = now + args.status_interval
                client.check_status()
                feedback_timeout = max(args.feedback_timeout, 2/args.rate)
                if enabled and now-max(enabled_at, client.feedback_at) > feedback_timeout:
                    raise TimeoutError(f"J60 motion feedback missing for {feedback_timeout:g} s")
                if now >= next_display or (interactive and dashboard.dirty):
                    if interactive:
                        dashboard.render(client, enabled=enabled, desired=desired, rate=args.rate,
                                         started=started, skipped=skipped, notice=notice)
                    else:
                        _print_feedback(client, enabled, args.rate, started)
                    next_display = now + 1/args.display_hz
                if now >= next_flush:
                    recorder.file.flush()
                    next_flush = now + 1
                wait = min(0.01, max(0.000001, next_tx-time.perf_counter())) if enabled else 0.01
                client.receive(wait)
        except KeyboardInterrupt:
            print("Exit requested.")
        finally:
            original = sys.exc_info()[1]
            cleanup_errors = []
            if needs_disable:
                try:
                    client.disable()
                    print("Disable ACK=00 received.", flush=True)
                except Exception as exc:
                    cleanup_errors.append(RuntimeError(f"Disable UNCONFIRMED: {exc}"))
            try:
                transport.close()
            except Exception as exc:
                cleanup_errors.append(RuntimeError(f"CANable close failed: {exc}"))
            if original is not None:
                for error in cleanup_errors:
                    original.add_note(str(error))
            elif cleanup_errors:
                for error in cleanup_errors[1:]:
                    cleanup_errors[0].add_note(str(error))
                raise cleanup_errors[0]
    return output


def main(argv=None):
    args, command = parse_args(argv)
    try:
        output = run(args, command)
    except Exception as exc:
        print(f"J60 test failed: {exc}", file=sys.stderr, flush=True)
        for note in getattr(exc, "__notes__", ()):
            print(note, file=sys.stderr, flush=True)
        return 1
    print(f"Saved: {output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
