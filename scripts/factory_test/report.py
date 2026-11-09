"""离线指标及白底矢量PDF；不改变原始CSV，不控制设备。"""
from bisect import bisect_left
import csv
import math
from pathlib import Path
import os
import shutil
import subprocess


def _find_pdftoppm():
    """返回 pdftoppm 命令；兼容未加入 PATH 的 Codex 内置 Poppler。"""
    command = shutil.which('pdftoppm')
    if command:
        return command

    executable = 'pdftoppm.exe' if os.name == 'nt' else 'pdftoppm'
    candidates = [
        Path.home() / '.cache' / 'codex-runtimes' / 'codex-primary-runtime'
        / 'dependencies' / 'native' / 'poppler' / 'Library' / 'bin' / executable,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def check_dependencies():
    try:
        import reportlab  # noqa: F401
    except ImportError as exc:
        raise RuntimeError('请先安装报告依赖：python -m pip install -e ".[report]"') from exc
    if _find_pdftoppm() is None:
        try:
            import fitz  # noqa: F401
        except ImportError as exc:
            raise RuntimeError('请先安装报告渲染依赖：python -m pip install PyMuPDF') from exc


def series(rows, x, y):
    points = []
    for row in rows:
        if row.get(x, '') == '' or row.get(y, '') == '':
            continue
        a, b = float(row[x]), float(row[y])
        if not (math.isfinite(a) and math.isfinite(b)):
            raise ValueError('CSV包含非有限数')
        points.append((a, b))
    return points


def regression(points):
    if len(points) < 10:
        raise ValueError('有效配对样本不足10点')
    xs, ys = zip(*points)
    mx, my = sum(xs)/len(xs), sum(ys)/len(ys)
    xx = sum((x-mx)**2 for x in xs)
    yy = sum((y-my)**2 for y in ys)
    if xx <= 1e-12 or yy <= 1e-12:
        raise ValueError('扭矩变化不足，无法计算斜率')
    slope = sum((x-mx)*(y-my) for x, y in points)/xx
    intercept = my-slope*mx
    r2 = 1-sum((y-slope*x-intercept)**2 for x, y in points)/yy
    return slope, intercept, r2


def pair_kt(rows, max_gap_ms=20):
    # 两路CSV按序平铺，同一行并非同步；按各自接收时间配对后才计算传递斜率。
    motors = sorted(series(rows, 'motor_time_ms', 'motor_torque'))
    dynos = series(rows, 'dyno_time_ms', 'dyno_torque_nm')
    times = [p[0] for p in motors]
    pairs = []
    for t, torque in dynos:
        i = bisect_left(times, t)
        if i < len(times) and times[i] == t:
            pairs.append((motors[i][1], torque))
        elif 0 < i < len(times) and 0 < times[i]-times[i-1] <= max_gap_ms:
            a, b = motors[i-1], motors[i]
            pairs.append((a[1]+(b[1]-a[1])*(t-a[0])/(b[0]-a[0]), torque))
    return pairs


def metric(label, value, unit='', lo=None, hi=None, precision=3):
    configured = lo is not None or hi is not None
    ok = (lo is None or value >= lo) and (hi is None or value <= hi)
    standard = (f'{lo:g} ~ {hi:g}' if lo is not None and hi is not None else
                f'>= {lo:g}' if lo is not None else f'<= {hi:g}' if hi is not None else '未配置')
    return dict(label=label, value=value, shown=f'{value:.{precision}f}', unit=unit,
                standard=standard, status=('PASS' if ok else 'FAIL') if configured else 'PENDING')


def kt_target_curve(rows, summary):
    """新CSV用实际下发目标；旧数据只能按已保存的斜坡参数重建理论目标。"""
    estimated = not any(r.get('motor_application_target_torque', '') != '' for r in rows)
    if 'time_ms' not in rows[0]:
        # 历史两路平铺文件先按真实时间一对一配对，不能用行号冒充同步。
        from utils.csv_streams import independent_rows
        fields = [k for k in rows[0] if k not in ('motor_time_ms', 'dyno_time_ms')]
        events = []
        for row in rows:
            for source in ('motor', 'dyno'):
                if row[source+'_time_ms'] == '': continue
                event = dict.fromkeys(fields, '')
                event['time_ms'] = row[source+'_time_ms']
                for key in fields:
                    if key.startswith(source+'_') or (source == 'motor' and key.startswith('driver_')):
                        event[key] = row[key]
                events.append(event)
        fields, packed = independent_rows(['time_ms', *fields], events, align_motor=True)
        rows = [dict(zip(fields, row)) for row in packed]
    duration = summary['stages']['kt'].get('result', {}).get('config', {}).get('RAMP_DURATION_S', 3.0)
    maximum = abs(summary['target_torque'])
    if maximum <= 0 or duration <= 0: raise ValueError('KT目标或斜坡参数无效')
    points = []
    for row in rows:
        if row.get('dyno_torque_smooth_nm', '') == '': continue
        x = (maximum*min(1.0, max(0.0, float(row['time_ms'])/(duration*1000))) if estimated else
             float(row['motor_application_target_torque']))
        y = float(row['dyno_torque_smooth_nm'])
        if not (math.isfinite(x) and math.isfinite(y)): raise ValueError('KT数据非有限数')
        points.append((x, y))
    if len(points) < 10: raise ValueError('KT有效目标/测功机平滑数据不足')
    return points, estimated


def analyze(directory, summary):
    sections = []
    titles = {'kt': '扭矩输出误差测试', 'tn': '电机特性曲线'}
    limits = summary['limits']
    for name in ('kt', 'tn'):
        stage = summary['stages'][name]
        section = dict(name=name, title=titles[name], status='INCOMPLETE', metrics=[], points=[],
                       note=stage.get('reason', ''), xlabel='', ylabel='')
        sections.append(section)
        if stage['status'] != 'COMPLETED':
            continue
        try:
            with (directory/stage['csv']).open(encoding='utf-8', newline='') as f:
                rows = list(csv.DictReader(f))
            if not rows:
                raise ValueError('CSV没有采样数据')
            if name == 'kt':
                points, estimated = kt_target_curve(rows, summary)
                try:
                    slope, intercept, r2 = regression(points)
                except ValueError:
                    # 稳定的零出力仍是有效FAIL数据，不能因无法拟合而掩盖输出误差。
                    slope = intercept = r2 = None
                maximum = abs(summary['target_torque'])
                deviation = max(abs(y-x) for x,y in points)
                section.update(points=points, xlabel='目标扭矩', ylabel='实际输出扭矩 / Nm',
                    note='', detail='旧数据目标按保存的斜坡时长重建' if estimated else '',
                    target_source='reconstructed_ramp' if estimated else 'recorded_command',
                    ideal_max=maximum, max_deviation_nm=deviation,
                    error_points=[(x, y-x) for x, y in points], r2=r2,
                    fit=[slope, intercept], metrics=[metric('线性度误差', deviation/maximum*100, '%FS',
                        hi=limits.get('kt_linearity_max_pct'), precision=2)])
            elif name == 'tn':
                required = ('dyno_torque_smooth_nm',
                            'motor_speed_smooth_rad_s', 'motor_torque_smooth')
                if any(key not in rows[0] for key in required):
                    raise ValueError('电机特性曲线缺少平滑后的速度或扭矩列')
                samples = []
                for row in rows:
                    if any(not row.get(key) for key in required):
                        continue
                    speed = abs(float(row['motor_speed_smooth_rad_s']))
                    torque = abs(float(row['dyno_torque_smooth_nm']))
                    motor_power = abs(float(row['motor_speed_smooth_rad_s']) *
                                      float(row['motor_torque_smooth']))
                    if not all(math.isfinite(value) for value in (speed, torque, motor_power)):
                        raise ValueError('电机特性曲线数据包含非有限数')
                    samples.append((speed, torque, motor_power))
                if len(samples) < 10:
                    raise ValueError('电机特性曲线有效配对样本不足')
                peak_sample = max(samples, key=lambda sample: sample[2])
                section.update(points=[(speed, torque) for speed, torque, _ in samples],
                    power_points=[(speed, power) for speed, _, power in samples],
                    xlabel='转速 rad/s', ylabel='测功机扭矩 / Nm',
                    note='', detail='',
                    metrics=[
                        dict(label='最大扭矩', shown=f'{max(s[1] for s in samples):.3f}', unit='Nm'),
                        dict(label='最大转速', shown=f'{max(s[0] for s in samples):.2f}', unit='rad/s'),
                        dict(label='最大功率', shown=f'{peak_sample[2]:.2f}', unit='W'),
                    ])
            section['status'] = ('PASS' if name == 'tn' else
                                 'FAIL' if any(m['status'] == 'FAIL' for m in section['metrics']) else
                                 'PENDING' if any(m['status'] == 'PENDING' for m in section['metrics']) else 'PASS')
            temperatures = [float(r[k]) for r in rows for k in ('motor_temperature_c', 'driver_temperature_c') if r.get(k)]
            section['max_temperature_c'] = max(temperatures) if temperatures else None
            section['csv_rows'] = len(rows)
        except (OSError, ValueError, KeyError, ZeroDivisionError) as exc:
            section.update(status='INCOMPLETE', note=str(exc), points=[], metrics=[])
    return sections


def overall(sections, stages):
    if any(s['status'] != 'COMPLETED' for s in stages.values()) or any(s['status'] == 'INCOMPLETE' for s in sections):
        return 'INCOMPLETE'
    if any(s['status'] == 'FAIL' for s in sections):
        return 'FAIL'
    if any(s['status'] == 'PENDING' for s in sections):
        return 'PENDING'
    return 'PASS'


def _render_pdf(path, summary):
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.colors import HexColor
    # Windows字体子集嵌入PDF，换电脑和打印时也能正确显示中文。
    font_path = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Fonts'/'simhei.ttf'
    if font_path.exists():
        pdfmetrics.registerFont(TTFont('ReportChinese', str(font_path)))
        chinese_font = 'ReportChinese'
    else:
        pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
        chinese_font = 'STSong-Light'
    c = Canvas(str(path), pagesize=(595.28, 841.89), pageCompression=1)
    c.setTitle('电机出厂测试报告 - '+str(summary.get('motor_sn') or '未读取'))
    ink, muted, blue, power_color, rule = '#142B3D', '#627381', '#1879A6', '#7656A7', '#DCE5EC'
    statuses = {'PASS': ('PASS', '#14815A'), 'FAIL': ('FAIL', '#BE3947'),
                'PENDING': ('待定', '#9A6B12'), 'INCOMPLETE': ('未完成', '#BE3947')}

    def text(x, y, value, size=9, color=ink, font=None):
        c.setFillColor(HexColor(color)); c.setFont(font or chinese_font, size); c.drawString(x, y, str(value))

    def line(x1, y1, x2, y2, color=rule, width=.6):
        c.setStrokeColor(HexColor(color)); c.setLineWidth(width); c.line(x1,y1,x2,y2)

    def badge(x, y, status):
        label, color = statuses[status]
        c.setFillColor(HexColor('#F0F5F7')); c.roundRect(x,y-5,63,22,5,fill=1,stroke=0)
        text(x+10,y+2,label,10,color)

    c.drawImage(str(Path(__file__).parent/'assets'/'jingshi.png'),32,788,24,24,mask='auto')
    text(64,794,'镜识科技',13,'#9CA9B2')
    title = '电机测试报告'
    text((595.28-pdfmetrics.stringWidth(title,chinese_font,24))/2,758,title,24)
    if summary.get('demo'):
        text(390,794,'模拟样张 · 仅供版式预览',9,'#BE3947')
    line(32,738,563,738)
    text(32,718,'电机 SN',8,muted); text(102,718,summary.get('motor_sn') or '未读取',11,font='Helvetica')
    text(350,718,'测试时间',8,muted)
    text(399,718,summary['started_at'][:19].replace('T',' '),8,font='Helvetica')
    if summary.get('calibration'):
        text(32,698,'工位：'+str(summary.get('fixture_id', ''))+
             '    校准：'+('模拟完成' if summary.get('demo') else '完成')+
             '    标签：'+('模拟打印' if summary.get('demo') else '打印已确认'),8,muted)
    for index, sec in enumerate(summary['sections']):
        top = 651-index*182
        line(32,top+10,563,top+10)
        text(32,top-7,sec['title'],12)
        # 左侧曲线，右侧验收数据；曲线轴以真实数值标尺显示。
        x,y,w,h = 64,top-130,226,82
        points = sec['points']
        if points:
            def draw_axes(chart_y, chart_h, xmin, xmax, ymin, ymax, ylabel, y_ticks=3,
                          symmetric=False):
                if xmax == xmin: xmax += 1
                if ymax == ymin: ymax += 1
                pad = (ymax-ymin)*.08
                if symmetric:
                    ymin, ymax = ymin-pad, ymax+pad
                else:
                    ymax += pad
                def tx(a): return x+(a-xmin)/(xmax-xmin)*w
                def ty(b): return chart_y+(b-ymin)/(ymax-ymin)*chart_h
                for j in range(y_ticks):
                    value = ymin+(ymax-ymin)*j/(y_ticks-1)
                    line(x,ty(value),x+w,ty(value))
                    text(x-30,ty(value)-2,f'{value:.1f}',7,blue,'Helvetica')
                for j in range(4):
                    value = xmin+(xmax-xmin)*j/3
                    text(tx(value)-7,chart_y-11,f'{value:.1f}',7,muted,'Helvetica')
                return tx,ty

            if sec['name'] == 'kt':
                maximum = sec['ideal_max']; step = max(1,len(points)//700)
                errors = sec['error_points']; bound=max(abs(b) for _,b in errors) or 1
                px2,py2 = draw_axes(y,h,0,maximum,-bound,bound,'输出误差 / Nm',4,
                                    symmetric=True)
                c.setDash(3,2)
                line(px2(0),py2(0),px2(maximum),py2(0),'#D44B4B',.65)
                c.setDash()
                c.setStrokeColor(HexColor(blue)); c.setLineWidth(.65)
                p=c.beginPath();p.moveTo(px2(errors[0][0]),py2(errors[0][1]))
                for a,b in errors[::step]: p.lineTo(px2(a),py2(b))
                p.lineTo(px2(errors[-1][0]),py2(errors[-1][1])); c.drawPath(p)
                text(x,y-25,'目标扭矩 / Nm',7,muted)
                line(174,top-28,185,top-28,blue,.65);text(188,top-30,'误差 / Nm',7,muted)
                c.setDash(3,2)
                line(246,top-28,257,top-28,'#D44B4B',.65)
                c.setDash()
                text(260,top-30,'理想',7,muted)
            else:
                xs,ys = zip(*points); xmin,xmax = 0,max(xs); ymin,ymax = min(0,min(ys)),max(ys)
                px,py = draw_axes(y,h,xmin,xmax,ymin,ymax,sec['ylabel'],4)
                power_points = sec['power_points']
                powers = [value for _, value in power_points]
                power_min, power_max = min(0, min(powers)), max(powers)
                if power_max == power_min: power_max += 1
                power_max += (power_max-power_min)*.08
                def power_y(value):
                    return y+(value-power_min)/(power_max-power_min)*h
                for j in range(4):
                    value = power_min+(power_max-power_min)*j/3
                    text(x+w+3,power_y(value)-2,f'{value:.0f}',7,power_color,'Helvetica')
                step = max(1,len(points)//700)
                c.setStrokeColor(HexColor(blue)); c.setLineWidth(.7)
                p=c.beginPath();p.moveTo(px(points[0][0]),py(points[0][1]))
                for a,b in points[::step]: p.lineTo(px(a),py(b))
                c.drawPath(p)
                c.setStrokeColor(HexColor(power_color)); c.setLineWidth(.7)
                p=c.beginPath();p.moveTo(px(power_points[0][0]),power_y(power_points[0][1]))
                for a,b in power_points[::step]: p.lineTo(px(a),power_y(b))
                c.drawPath(p)
                text(x,y-25,sec['xlabel'],7,muted)
                line(154,top-28,165,top-28,blue,.7);text(168,top-30,'扭矩 / Nm',7,muted)
                line(224,top-28,235,top-28,power_color,.7);text(238,top-30,'电机功率 / W',7,muted)
        else:
            text(x+25,y+40,'没有可用的完整测量数据',10,muted)
        text(336,top-46,'项目',8,muted);text(435,top-46,'实测',8,muted)
        if sec['name'] == 'kt': text(527,top-46,'标准',8,muted)
        for j,m in enumerate(sec['metrics']):
            yy=top-68-j*(25 if sec['name'] == 'tn' else 28)
            text(336,yy,m['label'],9)
            text(429,yy,m['shown']+' '+m['unit'],9)
            if sec['name'] == 'kt':
                text(479,yy,statuses.get(m['status'],('待定',muted))[0],7,statuses.get(m['status'],('',muted))[1])
                text(527,yy,m['standard'],8)
        if sec['name'] == 'kt': text(32,top-165,sec.get('detail','')[:90],7,muted)
    c.showPage(); c.save()


def render(path, summary):
    """生成单张PNG报告；PDF仅作为同目录临时矢量渲染文件。"""
    path = Path(path)
    if path.suffix.lower() != '.png':
        raise ValueError('报告输出路径必须以.png结尾')
    temporary = path.with_name('.report-rendering.pdf')
    prefix = path.with_suffix('')
    try:
        _render_pdf(temporary, summary)
        pdftoppm = _find_pdftoppm()
        if pdftoppm:
            completed = subprocess.run(
                [pdftoppm, '-png', '-singlefile', '-r', '180', str(temporary), str(prefix)],
                capture_output=True, text=True, encoding='utf-8', errors='replace', check=False)
            if completed.returncode != 0 or not path.exists():
                raise RuntimeError('PNG报告转换失败：'+(completed.stderr.strip() or f'退出码{completed.returncode}'))
        else:
            import fitz
            with fitz.open(temporary) as document:
                if document.page_count != 1:
                    raise RuntimeError('PNG报告转换失败：PDF页数异常')
                page = document.load_page(0)
                page.get_pixmap(dpi=180, alpha=False).save(path)
    finally:
        temporary.unlink(missing_ok=True)
