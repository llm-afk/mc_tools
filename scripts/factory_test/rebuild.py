"""从已有总测结果另存新版报告与对齐CSV，不连接硬件，不覆盖源数据。

python -m scripts.factory_test.rebuild <旧总测目录>
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
from . import app, report
from utils.csv_streams import pack_streams


def rebuild(source):
    source=Path(source).resolve()
    with (source/'summary.json').open(encoding='utf-8') as f: summary=json.load(f)
    target=Path(__file__).parent/'data'/('revised-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    target.mkdir(parents=True)
    summary['source_directory']=str(source)
    summary['previous_limits']=summary['limits']
    summary['limits']=dict(app.LIMITS)
    summary['stages'] = {name: summary['stages'][name] for name, _, _ in app.STAGES}
    for name,stage in summary['stages'].items():
        # 使用固定预期文件名，不执行历史summary中的路径或指令。
        filename={'kt':'kt_curve.csv','tn':'curve.csv'}[name]
        (target/name).mkdir()
        original=source/name/filename
        stage['csv']=f'{name}/{filename}'
        if original.exists():
            dest=target/name/filename
            shutil.copy2(original,dest)
            with dest.open(encoding='utf-8') as f: header=f.readline()
            if name in ('kt','tn') and 'motor_time_ms' in header:
                pack_streams(dest,align_motor=True)
    summary['sections']=report.analyze(target,summary)
    summary['status']=report.overall(summary['sections'],summary['stages'])
    report.render(target/'report.png',summary)
    with (target/'summary.json').open('x',encoding='utf-8') as f:
        json.dump(summary,f,ensure_ascii=False,indent=2,allow_nan=False)
    app.print_results(summary,target)
    return target


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',type=Path)
    rebuild(p.parse_args().source)
