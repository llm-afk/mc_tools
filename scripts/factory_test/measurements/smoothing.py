"""停机后的鲁棒局部线性平滑；只生成派生列，不改原始测量。

按时间窗口加权拟合，再用残差重加权降低离群点影响（LOWESS 思路）。
双侧窗口没有单向滤波的固定延迟，但拐点附近仍会被平滑；不是物理模型重建。
"""
from bisect import bisect_left, bisect_right
import csv
import math
import os
from pathlib import Path
from statistics import median
import tempfile

SMOOTH_COLUMNS = ['motor_speed_smooth_rad_s', 'motor_torque_smooth',
                  'dyno_speed_smooth_rad_s', 'dyno_torque_smooth_nm']


def _local_smooth(times, values, window_s, gap_s=.1):
    """在原有时间点输出，跨失联间隔不混合；保留正负方向和线性趋势。"""
    if len(times) != len(values) or not math.isfinite(window_s) or window_s <= 0:
        raise ValueError('invalid smoothing input/window')
    if any(not math.isfinite(v) for v in times+values):
        raise ValueError('nonfinite smoothing input')
    if any(b < a for a, b in zip(times, times[1:])):
        raise ValueError('timestamps must be sorted')
    result = []
    segments = [0]+[i for i in range(1, len(times)) if times[i]-times[i-1] > gap_s]+[len(times)]
    radius = window_s/2
    for begin, end in zip(segments, segments[1:]):
        for i in range(begin, end):
            lo = bisect_left(times, times[i]-radius, begin, end)
            hi = bisect_right(times, times[i]+radius, lo, end)
            ys = values[lo:hi]
            if len(ys) < 3:
                result.append(values[i])
                continue
            xs = [(t-times[i])/radius for t in times[lo:hi]]
            base = [(1-min(1, abs(x))**3)**3 for x in xs]
            center = median(ys)
            scale = max(1e-12, max(abs(y) for y in ys)*1e-10)
            # 中位数初始化避免一个大尖峰主导第一次拟合。
            limit = max(scale, 6*median([abs(y-center) for y in ys]))
            robust = [(1-min(1, abs(y-center)/limit)**2)**2 for y in ys]
            intercept = center
            slope = 0.0
            for _ in range(4):
                weights = [a*b for a, b in zip(base, robust)]
                sw = sum(weights)
                if sw <= 1e-15:
                    break
                sx = sum(w*x for w, x in zip(weights, xs))
                sy = sum(w*y for w, y in zip(weights, ys))
                sxx = sum(w*x*x for w, x in zip(weights, xs))
                sxy = sum(w*x*y for w, x, y in zip(weights, xs, ys))
                determinant = sw*sxx-sx*sx
                slope = (sw*sxy-sx*sy)/determinant if determinant > 1e-15 else 0.0
                intercept = (sy-slope*sx)/sw
                residuals = [abs(y-intercept-slope*x) for x, y in zip(xs, ys)]
                limit = max(scale, 6*median(residuals))
                robust = [(1-min(1, r/limit)**2)**2 for r in residuals]
            # 限定在该窗口有效数据范围，避免拟合制造额外峰值或符号翻转。
            accepted = [y for y, w in zip(ys, weights) if w > 1e-12]
            result.append(min(max(intercept, min(accepted)), max(accepted)) if accepted else center)
    return result


def smooth(times, values, window_s, gap_s=.1):
    """5 ms 中位数分箱后拟合，仅将平滑估计映射回原时间点。

    分箱宽度不超过平滑窗口的 1/30；不合并跨失联段的数据。
    原始 CSV 的时间、采样点和数值完全不受分箱影响。
    """
    if len(times) != len(values) or not math.isfinite(window_s) or window_s <= 0:
        raise ValueError('invalid smoothing input/window')
    if any(not math.isfinite(v) for v in times+values):
        raise ValueError('nonfinite smoothing input')
    if any(b < a for a, b in zip(times, times[1:])):
        raise ValueError('timestamps must be sorted')
    width = min(.005, window_s/30)
    edges = [0]+[i for i in range(1, len(times)) if times[i]-times[i-1] > gap_s]+[len(times)]
    output = []
    for begin, end in zip(edges, edges[1:]):
        if end == begin:
            continue
        tx, vy = [], []
        first = begin
        while first < end:
            last = bisect_left(times, times[first]+width, first+1, end)
            tx.append(median(times[first:last]))
            vy.append(median(values[first:last]))
            first = last
        fitted = _local_smooth(tx, vy, window_s, gap_s)
        lower, upper = min(values[begin:end]), max(values[begin:end])
        for t in times[begin:end]:
            if len(tx) == 1:
                value = fitted[0]
            else:
                j = max(0, min(len(tx)-2, bisect_right(tx, t)-1))
                value = fitted[j]+(fitted[j+1]-fitted[j])*(t-tx[j])/(tx[j+1]-tx[j])
            output.append(min(upper, max(lower, value)))
    return output


def append_smoothed_columns(path, speed_window_s=.3, torque_window_s=.3, gap_s=.1,
                            dyno_speed_window_s=.3, dyno_torque_window_s=.3, *, output_columns=None):
    """原子替换同一 CSV；分别平滑两路各自的真实样本，不跨设备填充。"""
    path = Path(path)
    with path.open(encoding='utf-8', newline='') as source:
        reader = csv.DictReader(source)
        fields = reader.fieldnames
        rows = list(reader)
    if not fields:
        raise ValueError('CSV header missing')
    series = [('motor_speed_rad_s', SMOOTH_COLUMNS[0], speed_window_s),
              ('motor_torque', SMOOTH_COLUMNS[1], torque_window_s),
              ('dyno_speed_rad_s', SMOOTH_COLUMNS[2], dyno_speed_window_s),
              ('dyno_torque_nm', SMOOTH_COLUMNS[3], dyno_torque_window_s)]
    for source, destination, window in series:
        if output_columns is not None and destination not in output_columns:
            continue
        indices = [i for i, row in enumerate(rows) if row[source] != '']
        times = [float(rows[i]['time_ms'])/1000 for i in indices]
        values = [float(rows[i][source]) for i in indices]
        for row in rows:
            row[destination] = ''
        for index, value in zip(indices, smooth(times, values, window, gap_s)):
            rows[index][destination] = f'{value:.9g}'
    output_fields = fields+[name for name in SMOOTH_COLUMNS if name not in fields]
    # 应用可选择最终列；平滑仍使用原始速度样本，再原子写出所选字段。
    if output_columns is not None:
        if any(name not in output_fields for name in output_columns):
            raise ValueError('unknown output column')
        output_fields = list(output_columns)
        rows = [{name: row[name] for name in output_fields} for row in rows]
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='',
                                         dir=path.parent, prefix='.smoothing-', suffix='.tmp', delete=False) as target:
            temporary = Path(target.name)
            writer = csv.DictWriter(target, fieldnames=output_fields)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
