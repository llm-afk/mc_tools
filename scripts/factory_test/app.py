"""电机出厂总测：扭矩输出误差 → 电机特性曲线 → 白底PNG报告。

运行：python -m scripts.factory_test --id 1 --serial <CANable SN> --torque 30
预览：python -m scripts.factory_test --preview（模拟数据，不连接设备）
电机SN自动读取0x2004..0x200A；--serial是USB适配器SN，两者分别记录。
本目录data/<YYYYMMDD_HHMMSS_电机SN>/保存测试结果。
扭矩输出误差标准在下方LIMITS设置；电机特性曲线只记录特性参数，不作判定。
流程参数沿用子脚本；每项前核对SN，异常或中止停止后续测试并出未完成报告。
"""
import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from motor_platform import Bench, Canable, MitCommand
from scripts.kt_curve import app as kt
from scripts.tn_curve import app as tn
from scripts.factory_test import report

# KT按用户指定的目标与实际Nm作1:1理想线比较，5%是可调整的初版工程标准。
LIMITS = {
    'kt_linearity_max_pct': 5.0, # max|平滑实际扭矩-目标扭矩| / 最大目标 * 100%
}
BETWEEN_TESTS_S = 1.0           # 子脚本失能并关闭电源后，两项之间的等待
STAGES = (('kt', kt, 'kt_curve.csv'), ('tn', tn, 'curve.csv'))


def read_motor_sn(args):
    with Bench(transport=Canable(args.serial), motor_ids=[args.id]) as bench:
        return bench.motor(args.id).read_serial_number()


def create_output_directory(root, motor_sn, now=None):
    """按YYYYMMDD_HHMMSS_电机SN命名；同一秒重复创建时追加序号。"""
    now = datetime.now() if now is None else now
    safe_sn = ''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in str(motor_sn))
    safe_sn = safe_sn.strip(' ._') or 'UNKNOWN'
    base = f'{now:%Y%m%d_%H%M%S}_{safe_sn}'
    root = Path(root)
    for index in range(1000):
        directory = root / (base if index == 0 else f'{base}_{index+1}')
        try:
            directory.mkdir(parents=True)
            return directory
        except FileExistsError:
            continue
    raise RuntimeError('同一秒的测试输出目录过多')


def validate_limits(limits):
    for key, value in limits.items():
        if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
            raise ValueError(f'invalid report limit: {key}')


def print_results(summary, directory, output=None):
    output = sys.stdout if output is None else output
    labels = {'PASS': 'PASS', 'FAIL': 'FAIL', 'PENDING': '待定', 'INCOMPLETE': '未完成'}
    colors = {'PASS': '\033[92m', 'FAIL': '\033[91m', 'PENDING': '\033[93m', 'INCOMPLETE': '\033[91m'}
    print('\n测试简报' + ('（模拟样张）' if summary.get('demo') else ''), file=output)
    names = {'kt': '扭矩输出误差测试', 'tn': '电机特性曲线'}
    for index, sec in enumerate(summary.get('sections', []), 1):
        status = sec['status']
        label = '完成' if sec['name'] == 'tn' and status == 'PASS' else labels[status]
        if getattr(output, 'isatty', lambda: False)():
            label = colors[status]+label+'\033[0m'
        details = '；'.join(
            f'{m["label"]} {m["shown"]}{m["unit"]}' +
            (f'，标准 {m["standard"]}' if m.get('standard') else '')
            for m in sec['metrics'])
        print(f'{index}. {names[sec["name"]]}  {label}  {details or sec.get("note", "")}', file=output)
    print(f'总测结果: {labels[summary["status"]]}\n结果目录: {directory}', file=output, flush=True)


def execute(args, directory, *, stages=STAGES, identify=read_motor_sn, pause=time.sleep,
            motor_sn=None):
    """复用入口函数：子脚本负责设备与停机，总测负责顺序、身份核对及归档。"""
    argv = ['--id', str(args.id), '--serial', args.serial, '--torque', str(args.torque)]
    summary = dict(schema_version=1, demo=False, started_at=datetime.now().astimezone().isoformat(),
                   motor_sn=motor_sn, canable_sn=args.serial, motor_id=args.id,
                   target_torque=args.torque, limits=dict(LIMITS), stages={}, status='INCOMPLETE')
    for name, _, csv_name in stages:
        summary['stages'][name] = dict(status='SKIPPED', csv=f'{name}/{csv_name}', reason='尚未执行')
    try:
        if summary['motor_sn'] is None:
            summary['motor_sn'] = identify(args)
        print(f'电机 SN: {summary["motor_sn"]}', flush=True)
        for i, (name, module, _) in enumerate(stages):
            stage = summary['stages'][name]
            stage.update(status='RUNNING', started_at=datetime.now().astimezone().isoformat())
            if identify(args) != summary['motor_sn']:
                raise RuntimeError('电机SN变化，停止总测，避免混入其他电机数据')
            print(f'[{i+1}/{len(stages)}] {name.upper()}', flush=True)
            result = module.main(argv, output_dir=directory/name, quiet=True)
            stage['result'] = result
            reason = result.get('reason', result.get('status', 'unknown'))
            completed = (result.get('reason') == 'completed' if name == 'kt' else
                         result.get('reason') == 'stall_confirmed')
            stage.update(status='COMPLETED' if completed else 'ERROR', reason=reason,
                         finished_at=datetime.now().astimezone().isoformat())
            if not completed:
                raise RuntimeError(f'{name}: {reason}')
            if i+1 < len(stages):
                pause(BETWEEN_TESTS_S)
    except (Exception, KeyboardInterrupt) as exc:
        summary['error'] = str(exc) or '用户中止'
        summary['error_notes'] = getattr(exc, '__notes__', [])
        for stage in summary['stages'].values():
            if stage['status'] == 'RUNNING':
                stage.update(status='ERROR', reason=summary['error'])
    summary['finished_at'] = datetime.now().astimezone().isoformat()
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--id', type=int, default=1)
    p.add_argument('--serial', help='CANable适配器SN；电机SN自动读取')
    p.add_argument('--torque', type=float)
    p.add_argument('--preview', action='store_true', help='模拟预览，不连接设备')
    args = p.parse_args(argv)
    validate_limits(LIMITS)
    report.check_dependencies()  # 先检查报告依赖，再连接设备。
    if not args.preview:
        if not args.serial or not args.serial.strip() or not 1 <= args.id <= 127:
            p.error('请指定 --serial 和有效 --id')
        if args.torque is None or not math.isfinite(args.torque) or args.torque <= 0:
            p.error('--torque 必须为有限正数')
        MitCommand(torque=args.torque).pack()
        child_args = ['--id', str(args.id), '--serial', args.serial, '--torque', str(args.torque)]
        for _, module, _ in STAGES:
            module.parse_args(child_args)
    data_root = Path(__file__).resolve().parent/'data'
    if args.preview:
        directory = create_output_directory(data_root, 'DEMO')
        from .preview import create_demo
        summary = create_demo(directory)
    else:
        motor_sn = read_motor_sn(args)
        directory = create_output_directory(data_root, motor_sn)
        summary = execute(args, directory, motor_sn=motor_sn)
    try:
        summary['sections'] = report.analyze(directory, summary)
        summary['status'] = report.overall(summary['sections'], summary['stages'])
        report.render(directory/'report.png', summary)
    except Exception as exc:
        summary.update(status='INCOMPLETE', report_error=str(exc))
        raise
    finally:
        with (directory/'summary.json').open('x', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, allow_nan=False)
        print_results(summary, directory)
    return summary


if __name__ == '__main__':
    main()
