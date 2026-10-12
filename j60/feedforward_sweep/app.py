"""J60-6 前馈扭矩扫频：position/velocity/kp/kd 固定为 0，只扫前馈字段。

先修改下方“用户配置”，然后在 mc_tools 目录直接运行：

python -m j60.feedforward_sweep

命令行参数仍可临时覆盖源码配置，例如 --amplitude 0.2。

幅值为关节输出端 N·m，不换算电流。先用 HOME_KP/HOME_KD 回零停稳，再切换零增益前馈扫频。
扫频含渐入渐出，结束/异常/Ctrl+C 后失能，退出后保存每包反馈。
"""
import argparse
import csv
from datetime import datetime
import math
from pathlib import Path
import struct
import sys

from j60.canable import select_canable
from j60.protocol import CONTROL, MotionCommand, can_id
from j60.feedforward_sweep.native import run_native
from scripts.position_sweep.trajectory import Chirp


# 用户配置：日常只需修改这 9 项。
MOTOR_ID = 1                       # 1. 关节 ID
CANABLE_SERIAL = None              # 2. SN 自动选择唯一设备；非空时仅核对 SN
COMMUNICATION_HZ = 1000.0          # 3. 通信频率，Hz
START_HZ = 0.1                     # 4. 扫频起始频率，Hz
STOP_HZ = 20.0                     # 5. 扫频截止频率，Hz
SWEEP_DURATION_S = 30.0            # 6. 完整幅值扫频时间，s
AMPLITUDE = 30                     # 7. 单边前馈扭矩幅值，输出端 N·m
HOME_KP = 100.0                    # 8. 仅回零阶段的位置增益
HOME_KD = 2.0                      # 9. 仅回零阶段的速度增益
# 扫频阶段位置、速度、KP、KD 固定为 0。

COLUMNS = ('target_feedforward_torque', 'kp', 'kd', 'position_rad', 'velocity_rad_s',
           'torque', 'motor_temperature_c', 'driver_temperature_c', 'frequency_hz')


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--id', type=int, default=MOTOR_ID)
    p.add_argument('--serial', default=CANABLE_SERIAL)
    for flag, default in (('rate', COMMUNICATION_HZ), ('start-hz', START_HZ),
                          ('stop-hz', STOP_HZ), ('duration', SWEEP_DURATION_S),
                          ('amplitude', AMPLITUDE), ('max-travel', MAX_TRAVEL_RAD),
                          ('max-velocity', MAX_VELOCITY_RAD_S)):
        p.add_argument('--'+flag, type=float, default=default)
    p.add_argument('--output', type=Path)
    p.add_argument('--preview', action='store_true')
    p.set_defaults(**_INTERNAL_DEFAULTS, kp=0, kd=0, feedforward=True,
                   home_kp=HOME_KP, home_kd=HOME_KD)
    a = p.parse_args(argv)
    try:
        can_id(CONTROL, a.id)
        for name in ('rate','start_hz','stop_hz','duration','amplitude','max_velocity'):
            value = getattr(a,name)
            if value is None or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be a finite positive value')
        if not math.isfinite(a.max_travel) or a.max_travel < 0:
            raise ValueError('max_travel must be finite and >=0; 0 disables the travel limit')
        if not 20 <= a.rate <= 1000 or not a.start_hz <= a.stop_hz < a.rate/2:
            raise ValueError('require 20<=rate<=1000 and start-hz<=stop-hz<rate/2')
        if not math.isfinite((a.duration+2*a.ramp)*max(a.rate,a.stop_hz)):
            raise ValueError('duration/frequency is too large')
        MotionCommand(torque=a.amplitude).pack()
        MotionCommand(kp=a.home_kp,kd=a.home_kd).pack()
        if a.home_kp < 1:
            raise ValueError('HOME_KP must be at least 1 for J60 gain quantization')
        if a.serial is not None:
            a.serial = a.serial.strip() or None
    except (TypeError,ValueError,OverflowError) as exc:
        p.error(str(exc))
    return a


class Recorder:
    def __init__(self, path):
        path.parent.mkdir(parents=True,exist_ok=True)
        self.file = path.open('x',encoding='utf-8-sig',newline='')
        self.writer = csv.writer(self.file)
        self.writer.writerow(COLUMNS)
        self.file.flush()
        self.rows = 0

    def write_native_rows(self, rows, count):
        for i in range(count):
            r=rows[i]
            self.writer.writerow((r.target,0,0,r.position,r.velocity,r.torque,
                                  r.mt if math.isfinite(r.mt) else '',
                                  r.bt if math.isfinite(r.bt) else '',r.frequency))
            self.rows += 1

    def preview(self, args):
        plan=Chirp(args.start_hz,args.stop_hz,args.duration,args.amplitude,args.ramp,0,0)
        for i in range(math.ceil(plan.total_duration*args.rate)+1):
            reference=plan.sample(min(i/args.rate,plan.total_duration))
            target=struct.unpack('<f',struct.pack('<f',reference.command.position))[0]
            self.writer.writerow((target,0,0,'','','','','',reference.frequency_hz))
            self.rows += 1

    def __enter__(self):
        return self

    def __exit__(self,*_):
        self.file.close()


def main(argv=None):
    args=parse_args(argv)
    suffix='-preview.csv' if args.preview else '.csv'
    path=args.output or Path(__file__).resolve().parent/'data'/(
        datetime.now().strftime('%Y%m%d-%H%M%S-%f')+
        f'_{args.stop_hz:g}_{args.amplitude:g}_0_0'+suffix)
    print(f'Feedforward: {args.start_hz:g}->{args.stop_hz:g} Hz, {args.duration:g} s, '
          f'+/-{args.amplitude:g} Nm (J60-6 output side); kp=kd=0',flush=True)
    print(f'CSV: {path.resolve()}',flush=True)
    result=0
    recorder=None
    try:
        with Recorder(path) as recorder:
            if args.preview:
                recorder.preview(args)
            else:
                serial=select_canable(args.serial)
                print(f'CANable SN: {serial}',flush=True)
                run_native(serial,recorder,args)
                print('Sweep complete; disable ACK received.',flush=True)
    except KeyboardInterrupt as exc:
        print('Stopped; disable attempted.',flush=True)
        for note in getattr(exc,'__notes__',()):
            print(note,file=sys.stderr)
        result=130
    except Exception as exc:
        print(f'Sweep failed: {exc}',file=sys.stderr,flush=True)
        for note in getattr(exc,'__notes__',()):
            print(note,file=sys.stderr)
        result=1
    if recorder is not None:
        print(f'Saved {recorder.rows} rows'+(' (preview only)' if args.preview else ''),flush=True)
    return result


# 内部维护区：渐入渐出与保护参数，日常配置无需修改。
MAX_TRAVEL_RAD = 0.0               # 0 关闭位移保护；正数为相对回零停稳位置的最大位移，rad
MAX_VELOCITY_RAD_S = 40.0           # 最大速度绝对值，rad/s
HOME_SPEED_RAD_S = 5.0             # PD 回零目标峰值速度，rad/s
_INTERNAL_DEFAULTS = dict(
    velocity_mode='zero', ramp=2.0, max_motor_temp=80.0, max_driver_temp=80.0,
    zero_velocity=0.1,             # 启动前静止检查阈值
    home_speed=HOME_SPEED_RAD_S, settle=0.5, home_timeout=10.0, zero_tolerance=0.05,
)


if __name__ == '__main__':
    raise SystemExit(main())
