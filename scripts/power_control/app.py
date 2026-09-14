"""电源实时反馈与控制测试。在平台根目录运行：
    python -m scripts.power_control
默认自动探测电源；也可 --port COM26。--scan 仅识别串口并退出。
命令：voltage 12 / current 1 / on / off / status / quit。
启动时保留当前输出；退出默认关闭输出，--keep-output 保持输出。
数据保存在本脚本 data/。--rate 15 设置电压电流轮询目标 Hz；完整状态约每秒刷新。
"""
import argparse
import csv
from dataclasses import asdict, replace
from datetime import datetime
import math
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from motor_platform import PowerSupply
from motor_platform.components.power_supply import detect_power_supplies, resolve_power_port
from motor_platform.components.power_supply.protocol import number
from utils.terminal import TerminalConsole


def parse_command(line):
    parts = line.lower().split()
    if not parts:
        return 'empty', None
    action = {'enable': 'on', 'disable': 'off'}.get(parts[0], parts[0])
    if action in ('voltage', 'current', 'rate') and len(parts) == 2:
        value = number(float(parts[1]))
        if action == 'rate' and not 0 < value <= 20:
            raise ValueError('rate must be in (0, 20] Hz')
        return action, value
    if action in ('on', 'off', 'status', 'quit', 'help') and len(parts) == 1:
        return action, None
    raise ValueError('voltage V | current A | on | off | rate Hz | status | quit')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='auto')
    parser.add_argument('--baudrate', type=int, default=9600)
    parser.add_argument('--rate', type=float, default=15)
    parser.add_argument('--scan', action='store_true')
    parser.add_argument('--keep-output', action='store_true')
    args = parser.parse_args(argv)
    if not math.isfinite(args.rate) or not 0 < args.rate <= 20 or args.baudrate <= 0:
        parser.error('rate must be in (0, 20] Hz; baudrate must be positive')
    if args.scan:
        matches = detect_power_supplies(ports=None if args.port == 'auto' else [args.port], baudrate=args.baudrate)
        for port, identity in matches.items():
            print(f'{port}: {identity}')
        if not matches:
            print('No UDP6721 found.')
        return
    port = resolve_power_port(args.port, baudrate=args.baudrate)
    path = Path(__file__).resolve().parent/'data'/f'{datetime.now():%Y%m%d-%H%M%S-%f}-power.csv'
    path.parent.mkdir(parents=True, exist_ok=True)
    help_text = 'voltage V | current A | on | off | rate Hz | status | quit'
    with path.open('x', encoding='utf-8', newline='') as log, TerminalConsole(
            title=f'UDP6721 电源控制 — {port}', prompt='power> ', commands=help_text) as ui:
        with PowerSupply(port=port, baudrate=args.baudrate) as power:
            try:
                rate = args.rate
                notice = '启动未改变输出。' + ('退出保持输出。' if args.keep_output else '退出关闭输出。')
                writer = None
                started = time.perf_counter()
                samples = 0
                deadline = started
                next_full = started
                state = None
                status_at = started
                while True:
                    line = ui.poll()
                    if line is not None:
                        try:
                            action, value = parse_command(line)
                            if action == 'quit':
                                break
                            if action == 'voltage':
                                notice = f'电压读回 {power.set_voltage(value):g} V'
                            elif action == 'current':
                                notice = f'限流读回 {power.set_current(value):g} A'
                            elif action == 'on':
                                power.enable()
                                notice = '输出 ON，已读回确认'
                            elif action == 'off':
                                power.disable()
                                notice = '输出 OFF，已读回确认'
                            elif action == 'rate':
                                rate = value
                                notice = f'轮询目标 {rate:g} Hz'
                            elif action == 'help':
                                notice = help_text
                        except ValueError as exc:
                            notice = f'命令错误：{exc}'
                        deadline = 0
                        next_full = 0  # Refresh output/mode/setpoints after commands.
                    now = time.perf_counter()
                    if now >= deadline:
                        if state is None or now >= next_full:
                            state = power.read_state()
                            status_at = time.perf_counter()
                            next_full = status_at + 1.0
                        else:
                            measured = power.measure()
                            state = replace(state, voltage=measured.voltage, current=measured.current,
                                            received_at=measured.received_at)
                        samples += 1
                        row = asdict(state)
                        row['power'] = state.power
                        row['status_received_at'] = status_at
                        if writer is None:
                            writer = csv.DictWriter(log, fieldnames=list(row))
                            writer.writeheader()
                        writer.writerow(row)
                        log.flush()
                        elapsed = time.perf_counter()-started
                        ui._panel = (
                            f'{power.identity}\n\n'
                            f'输出：{"ON" if state.output else "OFF"}    模式：{state.mode}\n\n'
                            f'开关/模式/设定约1秒刷新，龄期 {time.perf_counter()-status_at:.2f}s\n'
                            f'设定电压  {state.voltage_set:.2f} V    限流 {state.current_set:.3f} A\n'
                            f'实测电压  {state.voltage:.3f} V\n'
                            f'实测电流  {state.current:.4f} A\n'
                            f'计算功率  {state.power:.3f} W\n\n'
                            f'采样 {samples}    目标 {rate:g} Hz / 实际 {samples/elapsed:.1f} Hz\n'
                            f'退出策略：{"保持输出" if args.keep_output else "关闭输出"}'
                        )
                        ui._notice = notice
                        ui.app.invalidate()
                        ui.dirty = False
                        deadline = max(now + 1/rate, time.perf_counter())
                    time.sleep(max(0, min(0.01, deadline - time.perf_counter())))
            finally:
                if not args.keep_output:
                    original = sys.exc_info()[1]
                    try:
                        power.disable()
                    except Exception as exc:
                        try:
                            power.disable(verify=False)
                        except Exception as best_effort:
                            exc.add_note(f'Best-effort OFF also failed: {best_effort}')
                        if original is None:
                            raise
                        original.add_note(f'Output OFF unconfirmed: {exc}')
    print(f'Feedback CSV: {path}')


if __name__ == '__main__':
    main()
