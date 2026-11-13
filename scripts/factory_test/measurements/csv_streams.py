"""将两路事件 CSV 排成独立、连续的列组；不插值、不去重、不重采样。

转换旧文件（另存 *_continuous.csv）：python -m scripts.factory_test.measurements.csv_streams <CSV路径>
两组有共同的时间原点，但每组使用自己的时间列，同一行不代表同步。
"""
import csv
from bisect import bisect_left
from itertools import zip_longest
import os
from pathlib import Path
import tempfile


def independent_rows(fields, rows, *, single_time=False, align_motor=False, max_skew_ms=20.0):
    if 'time_ms' not in fields or not {'motor_torque', 'dyno_torque_nm'} <= set(fields):
        raise ValueError('expected a two-device event CSV with time_ms and motor/dyno torque')
    motor = [name for name in fields if name.startswith(('motor_', 'driver_'))]
    dyno = [name for name in fields if name.startswith('dyno_')]
    shared = [name for name in fields if name not in ['time_ms', *motor, *dyno]]
    motor_fields = ['motor_time_ms', *motor, *['motor_'+name for name in shared]]
    dyno_fields = ['dyno_time_ms', *dyno, *['dyno_'+name for name in shared]]
    single_time = single_time or align_motor
    output_fields = (['time_ms', *motor_fields[1:], *dyno_fields[1:]]
                     if single_time else motor_fields + dyno_fields)
    if len(set(output_fields)) != len(output_fields):
        raise ValueError('ambiguous output column names')
    streams = {'motor': [], 'dyno': []}
    for row in rows:
        active = [source for source, key in (('motor', 'motor_torque'), ('dyno', 'dyno_torque_nm'))
                  if row[key] != '']
        if len(active) != 1:
            raise ValueError('each input row must contain exactly one real device sample')
        source = active[0]
        owned = motor if source == 'motor' else dyno
        streams[source].append([row['time_ms'], *[row[name] for name in owned],
                                *[row[name] for name in shared]])
    # 稳定排序保留相同时间戳的所有 USB 批次帧及原有顺序。
    for stream in streams.values():
        stream.sort(key=lambda row: float(row[0]))
    def combine(motor_row, dyno_row):
        if not single_time:
            return ((motor_row if motor_row is not None else ['']*len(motor_fields)) +
                    (dyno_row if dyno_row is not None else ['']*len(dyno_fields)))
        # 行号用于并排绘图；全局时间优先跟随电机，电机尾部结束后跟随测功机。
        timestamp = motor_row[0] if motor_row is not None else dyno_row[0]
        return ([timestamp] +
                (motor_row[1:] if motor_row is not None else ['']*(len(motor_fields)-1)) +
                (dyno_row[1:] if dyno_row is not None else ['']*(len(dyno_fields)-1)))
    if align_motor:
        if max_skew_ms <= 0:
            raise ValueError('max_skew_ms must be positive')
        dyno_times = [float(row[0]) for row in streams['dyno']]
        def aligned():
            next_index = 0
            for row_index, m in enumerate(streams['motor']):
                t = float(m[0])
                # 单调一对一匹配：同一USB批次的不同帧保序，同一帧绝不复制。
                next_index = bisect_left(dyno_times, t-max_skew_ms, lo=next_index)
                remaining = len(streams['motor'])-row_index
                last = max(next_index, len(dyno_times)-remaining)
                last = min(last, len(dyno_times)-1)
                right = bisect_left(dyno_times, t, lo=next_index)
                candidates = [min(i, last) for i in (right-1, right)
                              if next_index <= min(i, last) < len(dyno_times)]
                # 向左找到同时间戳的第一个未使用帧，避免跳过批次中间数据。
                candidates = [bisect_left(dyno_times, dyno_times[i], lo=next_index) for i in candidates]
                nearest = min(candidates, key=lambda i: abs(dyno_times[i]-t)) if candidates else None
                d = None
                if nearest is not None and abs(dyno_times[nearest]-t) <= max_skew_ms:
                    d = streams['dyno'][nearest]
                    next_index = nearest+1
                yield combine(m, d)
        packed = aligned()
    else:
        packed = (combine(m, d) for m, d in zip_longest(streams['motor'], streams['dyno']))
    return output_fields, packed


def pack_streams(path, destination=None, *, single_time=False, align_motor=False, max_skew_ms=20.0):
    """默认原子替换最终文件；指定 destination 时另存并拒绝覆盖。"""
    path = Path(path)
    with path.open(encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        fields, rows = reader.fieldnames, list(reader)
    if not fields:
        raise ValueError('CSV header missing')
    if 'time_ms' not in fields and {'motor_time_ms', 'dyno_time_ms'} <= set(fields):
        if align_motor:
            events = []
            source_fields = [name for name in fields if name not in ('motor_time_ms', 'dyno_time_ms')]
            for row in rows:
                for source in ('motor', 'dyno'):
                    if row[source+'_time_ms'] == '':
                        continue
                    event = {name: '' for name in source_fields}
                    event['time_ms'] = row[source+'_time_ms']
                    for name in source_fields:
                        if name.startswith(source+'_') or (source == 'motor' and name.startswith('driver_')):
                            event[name] = row[name]
                    events.append(event)
            output_fields, packed = independent_rows(['time_ms', *source_fields], events,
                                                     align_motor=True, max_skew_ms=max_skew_ms)
        else:
            output_fields, packed = fields, ([row[name] for name in fields] for row in rows)
    else:
        output_fields, packed = independent_rows(fields, rows, single_time=single_time,
                                                 align_motor=align_motor, max_skew_ms=max_skew_ms)
    if destination is not None:
        destination = Path(destination)
        with destination.open('x', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(output_fields)
            writer.writerows(packed)
        return destination
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='',
                                         dir=path.parent, prefix='.streams-', suffix='.tmp', delete=False) as f:
            temporary = Path(f.name)
            writer = csv.writer(f)
            writer.writerow(output_fields)
            writer.writerows(packed)
        os.replace(temporary, path)
        return path
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
