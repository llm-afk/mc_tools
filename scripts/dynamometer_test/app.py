"""测功机接收验证。在平台根目录运行：
    python -m scripts.dynamometer_test
默认自动探测，115200、8N1；可 --port COM22 指定端口，--scan 只探测。
看板显示实时测量和实际收帧频率；status 查看状态，quit 或 Ctrl+C 退出。
所有取到的有效帧保存在本目录 data/；不发送设备指令。
--duration 10 定时运行；--no-console 输出文本；--display-hz 10 只改显示频率。
"""
import argparse
from contextlib import nullcontext
import csv
from dataclasses import asdict, fields
from datetime import datetime
import math
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from motor_platform import Dynamometer, DynamometerState
from motor_platform.components.dynamometer import detect_dynamometers
from utils.terminal import TerminalConsole


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='auto')
    parser.add_argument('--baudrate', type=int, default=115200)
    parser.add_argument('--scan', action='store_true')
    parser.add_argument('--duration', type=float, default=0)
    parser.add_argument('--display-hz', type=float, default=10)
    parser.add_argument('--torque-scale', type=float, default=100)
    parser.add_argument('--speed-scale', type=float, default=100)
    parser.add_argument('--no-console', action='store_true')
    args = parser.parse_args(argv)
    if args.baudrate <= 0 or not math.isfinite(args.duration) or args.duration < 0:
        parser.error('baudrate must be positive; duration must be finite and >= 0')
    if not math.isfinite(args.display_hz) or not 0 < args.display_hz <= 100:
        parser.error('display-hz must be in (0, 100]')
    if any(not math.isfinite(v) or v <= 0 for v in (args.torque_scale, args.speed_scale)):
        parser.error('scales must be finite and positive')
    return args


def resolve_port(port, baudrate):
    if port != 'auto':
        return port
    matches = detect_dynamometers(baudrate=baudrate)
    if len(matches) != 1:
        raise RuntimeError(f'Expected one DYN-200 candidate, found {matches}; specify --port COMxx')
    return matches[0]


def panel_text(port, state, stats, recorded, now):
    age = now-state.received_at if state is not None else None
    status = '等待有效数据' if age is None else ('反馈正常' if age <= 0.1 else '反馈过期')
    measurements = '尚未收到 CRC 有效帧'
    if state is not None:
        measurements = (
            f'扭矩  {state.torque:+.4f} Nm\n'
            f'转速  {state.rpm:.2f} rpm / {state.speed:.4f} rad/s\n'
            f'计算功率  {state.power:+.4f} W\n'
            f'原始值  torque={state.raw_torque}  speed={state.raw_speed}\n'
            f'数据龄期  {age*1000:.1f} ms'
        )
    return (
        f'{port}   {status}\n\n{measurements}\n\n'
        f'实际接收  {stats["actual_rx_hz"]:.1f} Hz（设备标称约 1000 Hz）\n'
        f'有效帧  {stats["rx_count"]}    已记录 {recorded}\n'
        f'接收字节  {stats["rx_bytes"]}    重同步丢弃字节 {stats["discarded_bytes"]}\n'
        f'缓存积压  {stats["buffered_samples"]}    缓存溢出丢帧 {stats["dropped_samples"]}\n'
        '无滤波、无随机抖动；同批数据可能共享接收时间。'
    )


def main(argv=None):
    args = parse_args(argv)
    if args.scan:
        matches = detect_dynamometers(ports=None if args.port == 'auto' else [args.port], baudrate=args.baudrate)
        print('DYN-200 candidates:', ', '.join(matches) if matches else '(none)')
        return
    port = resolve_port(args.port, args.baudrate)
    path = Path(__file__).resolve().parent/'data'/f'{datetime.now():%Y%m%d-%H%M%S-%f}-dynamometer.csv'
    path.parent.mkdir(parents=True, exist_ok=True)
    device = Dynamometer(port=port, baudrate=args.baudrate,
                         torque_scale=args.torque_scale, speed_scale=args.speed_scale)
    context = nullcontext(None) if args.no_console else TerminalConsole(
        title='测功机接收验证', prompt='dyno> ', commands='status | help | quit')
    recorded = 0
    latest = None
    with path.open('x', encoding='utf-8', newline='') as log, context as ui:
        writer = csv.DictWriter(log, fieldnames=['sample_index']+[f.name for f in fields(DynamometerState)]+['power'])
        writer.writeheader()
        def record_pending():
            nonlocal recorded, latest
            for state in device.drain():
                recorded += 1
                latest = state
                writer.writerow(dict(sample_index=recorded, **asdict(state), power=state.power))
        try:
            with device:
                started = time.perf_counter()
                next_display = started
                next_flush = started + 1
                notice = '只接收，不发送设备指令。缩放沿用旧实现，需实机核对。'
                while args.duration == 0 or time.perf_counter()-started < args.duration:
                    line = ui.poll() if ui is not None else None
                    if line is not None:
                        command = line.strip().lower()
                        if command in ('quit', 'q', 'exit'):
                            break
                        notice = 'status | help | quit' if command in ('help', '') else (
                            '状态已刷新' if command == 'status' else f'未知命令：{command}')
                        next_display = 0
                    record_pending()
                    stats = device.stats()
                    if stats['fault'] is not None:
                        raise RuntimeError(f'dynamometer receive failed: {stats["fault"]}')
                    now = time.perf_counter()
                    if now >= next_display:
                        text = panel_text(port, latest, stats, recorded, now)
                        if ui is None:
                            print(text, flush=True)
                        else:
                            ui._panel, ui._notice = text, notice
                            ui.app.invalidate()
                            ui.dirty = False
                        next_display = now + 1/args.display_hz
                    if now >= next_flush:
                        log.flush()
                        next_flush = now + 1
                    time.sleep(0.005)
        except KeyboardInterrupt:
            pass
        finally:
            # Context exit stops the producer before draining the final batch.
            record_pending()
            log.flush()
    print(f'Feedback CSV: {path}')
    print(f'Recorded {recorded} frames; final statistics: {device.stats()}')


if __name__ == '__main__':
    main()
