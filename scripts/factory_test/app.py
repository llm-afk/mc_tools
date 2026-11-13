"""顺序执行两个测量内核，并核对每项开始前的电机身份。"""
from datetime import datetime
import time

from motor_platform import Bench, Canable
from scripts.factory_test.measurements.kt_curve import app as kt
from scripts.factory_test.measurements.tn_curve import app as tn

# KT按用户指定的目标与实际Nm作1:1理想线比较，5%是可调整的初版工程标准。
LIMITS = {
    'kt_linearity_max_pct': 5.0, # max|平滑实际扭矩-目标扭矩| / 最大目标 * 100%
}
BETWEEN_TESTS_S = 1.0           # 子脚本失能并关闭电源后，两项之间的等待
STAGES = (('kt', kt, 'kt_curve.csv'), ('tn', tn, 'curve.csv'))


def read_motor_sn(args):
    with Bench(transport=Canable(args.serial), motor_ids=[args.id]) as bench:
        return bench.motor(args.id).read_serial_number()


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
        from scripts.factory_test.diagnostics import log_event
        log_event('test.exception', '性能测试异常', level='ERROR', error=exc, stages=summary['stages'])
        summary['error'] = str(exc) or '用户中止'
        summary['error_notes'] = getattr(exc, '__notes__', [])
        for stage in summary['stages'].values():
            if stage['status'] == 'RUNNING':
                stage.update(status='ERROR', reason=summary['error'])
    summary['finished_at'] = datetime.now().astimezone().isoformat()
    return summary
