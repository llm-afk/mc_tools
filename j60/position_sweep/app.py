"""J60 位置扫频：回零、停稳、渐入、扫频、渐出、回零停稳、失能。
python -m j60.position_sweep
默认目标速度和前馈扭矩为 0；幅值单位 rad，输出端位置。
"""
import argparse
import csv
from datetime import datetime
import math
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from j60.canable import select_canable
from j60.protocol import CONTROL, MotionCommand, can_id
from j60.position_sweep.native import run_native
from scripts.position_sweep.trajectory import Chirp

# 用户配置：日常只需修改这 9 项，命令行参数可覆盖。
MOTOR_ID = 1                       # 1. 关节 ID
CANABLE_SERIAL = None              # 2. CANable SN；自动选择唯一设备；非空时核对 SN
COMMUNICATION_HZ = 1000.0          # 3. 通信目标频率，Hz
START_HZ = 0.1                     # 4. 扫频起始频率，Hz
STOP_HZ = 10.0                     # 5. 扫频截止频率，Hz
SWEEP_DURATION_S = 30.0            # 6. 完整摆幅扫频时间，s
AMPLITUDE_RAD = 1.0                # 7. 单边摆幅，rad；1 表示 -1..+1 rad
KP = 100                           # 8. 位置增益；填写数值或传 --kp
KD = 1                             # 9. 速度增益；填写数值或传 --kd


def chirp_from_args(args):
    return Chirp(args.start_hz, args.stop_hz, args.duration, args.amplitude,
                 args.ramp, args.kp, args.kd, args.velocity_mode)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", type=int, default=MOTOR_ID)
    parser.add_argument("--serial", default=CANABLE_SERIAL, help="check SN of the only detected CANable")
    parser.add_argument("--rate", type=float, default=COMMUNICATION_HZ, help="CAN target TX Hz")
    parser.add_argument("--start-hz", type=float, default=START_HZ)
    parser.add_argument("--stop-hz", type=float, default=STOP_HZ)
    parser.add_argument("--duration", type=float, default=SWEEP_DURATION_S,
                        help="full-amplitude linear sweep seconds, excluding ramps/homing")
    parser.add_argument("--amplitude", type=float, default=AMPLITUDE_RAD, help="one-sided amplitude, rad")
    parser.add_argument("--kp", type=float, default=KP, help="position gain, >0, J60 resolution 1")
    parser.add_argument("--kd", type=float, default=KD, help="velocity gain, >=0, J60 resolution 0.2")
    # Retain advanced CLI compatibility, without cluttering normal --help.
    parser.add_argument("--velocity-mode", choices=("zero", "derivative"), help=argparse.SUPPRESS)
    for option in ("home-speed", "settle", "home-timeout", "zero-tolerance", "zero-velocity",
                   "ramp", "max-motor-temp", "max-driver-temp"):
        parser.add_argument("--"+option, type=float, help=argparse.SUPPRESS)
    parser.set_defaults(**_INTERNAL_DEFAULTS)
    parser.add_argument("--output", type=Path, help="exclusive CSV path; never overwrite existing data")
    parser.add_argument("--preview", action="store_true", help="only export targets; never connect to hardware")
    parser.set_defaults(feedforward=False, max_travel=0, max_velocity=0)
    args = parser.parse_args(argv)
    try:
        can_id(CONTROL, args.id)
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
        if not 20 <= args.rate <= 1000:
            raise ValueError("rate must be in 20..1000 Hz")
        if args.stop_hz < args.start_hz:
            raise ValueError("stop-hz must be >= start-hz")
        if args.stop_hz >= args.rate/2:
            raise ValueError("stop-hz must be below rate/2 (sampling limit)")
        if args.velocity_mode == "derivative" and args.home_speed > 40:
            raise ValueError("home-speed exceeds J60 velocity range")
        if args.home_timeout <= args.settle:
            raise ValueError("home-timeout must exceed settle")
        MotionCommand(args.amplitude, (args.amplitude*(math.tau*args.stop_hz + 1.875/args.ramp) if args.velocity_mode == "derivative" else 0),
                   0.0, args.kp, args.kd).pack()
        if args.kp < 1:
            raise ValueError("kp must be at least 1 for J60 position control")
        if not math.isfinite((args.duration+2*args.ramp)*max(args.stop_hz, args.rate)):
            raise ValueError("sweep duration/frequency is too large")
    except (ValueError, TypeError, OverflowError) as exc:
        parser.error(str(exc))
    return args


COLUMNS = ('target_position_rad', 'kp', 'kd', 'position_rad', 'velocity_rad_s',
           'torque', 'motor_temperature_c', 'driver_temperature_c', 'frequency_hz')

def encoded_targets(p, kp, kd):
    raw = int.from_bytes(MotionCommand(position=p, kp=kp, kd=kd).pack(), 'little')
    return ((raw & 65535)*80/65535-40, (raw >> 30) & 1023,
            ((raw >> 40) & 255)*51/255)

class Recorder:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open('x', encoding='utf-8-sig', newline='')
        self.writer = csv.writer(self.file)
        self.writer.writerow(COLUMNS)
        self.file.flush()
        self.rows = 0

    def write_native_rows(self, rows, count):
        for i in range(count):
            r = rows[i]
            self.writer.writerow((r.target,r.kp,r.kd,r.position,r.velocity,r.torque,
                                  r.mt if math.isfinite(r.mt) else '',
                                  r.bt if math.isfinite(r.bt) else '',r.frequency))
            self.rows += 1

    def preview(self, args):
        plan = chirp_from_args(args)
        total = plan.total_duration + args.settle
        for i in range(math.ceil(total*args.rate)+1):
            ref = plan.sample(min(i/args.rate,total))
            self.writer.writerow((*encoded_targets(ref.command.position,args.kp,args.kd),
                                  '', '', '', '', '', ref.frequency_hz))
            self.rows += 1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.file.close()

def main(argv=None):
    args = parse_args(argv)
    output = args.output or Path(__file__).resolve().parent/'data'/(
        datetime.now().strftime('%Y%m%d-%H%M%S-%f')+
        f'_{args.stop_hz:g}_{args.amplitude:g}_{args.kp:g}_{args.kd:g}'+
        ('-preview.csv' if args.preview else '.csv'))
    print(f'ID={args.id} | {args.start_hz:g}->{args.stop_hz:g} Hz / {args.duration:g} s | '
          f'+/-{args.amplitude:g} rad | kp={args.kp:g} kd={args.kd:g} | '
          f'CAN={args.rate:g} Hz | velocity={args.velocity_mode}', flush=True)
    print(f'CSV: {output.resolve()}', flush=True)
    recorder = None
    result = 0
    try:
        serial = None if args.preview else select_canable(args.serial)
        with Recorder(output) as recorder:
            if args.preview:
                recorder.preview(args)
            else:
                print(f'CANable SN: {serial}', flush=True)
                run_native(serial, recorder, args)
                print('扫频完成，已回零停稳并收到失能 ACK。', flush=True)
    except KeyboardInterrupt as exc:
        print('已中止，已尝试失能。', flush=True)
        for note in getattr(exc, '__notes__', ()):
            print(note, file=sys.stderr)
        result = 130
    except Exception as exc:
        print(f'测试失败：{exc}', file=sys.stderr, flush=True)
        for note in getattr(exc, '__notes__', ()):
            print(note, file=sys.stderr)
        result = 1
    if recorder is not None:
        print(f'已保存 {recorder.rows} 行'+('；仅目标预览，未连接硬件。' if args.preview else '反馈。'), flush=True)
    return result

_INTERNAL_DEFAULTS = dict(velocity_mode='zero', home_speed=1.0, settle=0.5,
    home_timeout=10.0, zero_tolerance=0.05, zero_velocity=0.1, ramp=2.0,
    max_motor_temp=80.0, max_driver_temp=80.0)

if __name__ == '__main__':
    raise SystemExit(main())
