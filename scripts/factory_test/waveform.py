"""Retained Canvas plots: stable axes and bounded, peak-preserving line data."""
import math
import time


FEEDBACK_COLOR = '#2563EB'
TARGET_COLOR = '#D97706'
FRAME_INTERVAL = 1/60
SERIES = (('position','位置 / rad',.1),('velocity','速度 / rad/s',.2),('torque','扭矩 / 协议单位',.2))


def tick_text(value, step):
    digits=max(1,min(3,math.ceil(-math.log10(max(step,.001)))))
    while digits<3 and not math.isclose(step*10**digits,round(step*10**digits),abs_tol=1e-7):
        digits+=1
    if abs(value)<.5*10**-digits: value=0.0
    return f'{value:.{digits}f}'


def axis_bounds(low, high, minimum_span):
    span=max(high-low,minimum_span)
    center=(low+high)/2
    raw_step=span*1.2/4
    base=10**math.floor(math.log10(raw_step))
    step=next(mult*base for mult in (1,2,2.5,5,10) if mult*base>=raw_step)
    half=max(step*2,(high-low)/2+span*.1)
    # A rounded center prevents small feedback noise from moving every label.
    center=round(center/step)*step
    while center-half>low or center+half<high: half+=step
    return center-half,center+half


def envelope(points, key, target, start, span, pixels):
    """At most two extrema per pixel column; keep their original time order."""
    values=[(p['at'],p['target'][key] if target else p[key]) for p in points]
    if len(values)<=2: return values
    if all(value==values[0][1] for _,value in values): return [values[0],values[-1]]
    if len(values)<=2*pixels: return values
    selected={0,len(values)-1}
    bucket=None; lowest=highest=0
    for i,(at,value) in enumerate(values):
        column=int((at-start)/span*pixels)
        if column!=bucket:
            if bucket is not None: selected.update((lowest,highest))
            bucket=column; lowest=highest=i
        else:
            if value<values[lowest][1]: lowest=i
            if value>values[highest][1]: highest=i
    selected.update((lowest,highest))
    return [values[i] for i in sorted(selected)]


class WaveformPlot:
    def __init__(self, canvas):
        self.canvas=canvas
        self.layout=None
        self.bounds={}
        self.bound_times={}
        self.text={}
        self.visibility={}
        self.rows=[]
        for key,title,_ in SERIES:
            row=dict(title=canvas.create_text(0,0,text=title,anchor='w',fill='#334155',font=('Microsoft YaHei UI',10,'bold')),
                horizontal=[canvas.create_line(0,0,0,0,fill='#E8EDF4') for _ in range(5)],
                vertical=[canvas.create_line(0,0,0,0,fill='#F0F3F8') for _ in range(6)],
                ylabels=[canvas.create_text(0,0,anchor='e',fill='#64748B',font=('Segoe UI',9)) for _ in range(5)],
                xlabels=[canvas.create_text(0,0,fill='#64748B',font=('Segoe UI',9)) for _ in range(6)],
                time=canvas.create_text(0,0,text='时间 / s',fill='#64748B',font=('Microsoft YaHei UI',9)),
                empty=canvas.create_text(0,0,text='等待电机状态反馈',fill='#94A3B8',font=('Microsoft YaHei UI',11)),
                # A wider target under the feedback keeps both colors visible at overlap.
                target=canvas.create_line(0,0,0,0,fill=TARGET_COLOR,width=1.5),
                feedback=canvas.create_line(0,0,0,0,fill=FEEDBACK_COLOR,width=1))
            self.rows.append(row)
        self.simulation=canvas.create_text(0,0,text='离线模拟数据',anchor='e',fill='#9A6B12',font=('Microsoft YaHei UI',9))

    def reset(self):
        self.bounds.clear(); self.bound_times.clear()

    def set_text(self,item,text):
        if self.text.get(item)!=text:
            self.canvas.itemconfigure(item,text=text); self.text[item]=text

    def visible(self,item,show):
        if self.visibility.get(item)!=show:
            self.canvas.itemconfigure(item,state='normal' if show else 'hidden'); self.visibility[item]=show

    def render(self,samples,span,width,height,*,simulation=False):
        canvas=self.canvas
        width,height=max(width,320),max(height,300)
        layout=(width,height,span)
        left,right=70,width-20
        if layout!=self.layout:
            self.layout=layout
            for index,row in enumerate(self.rows):
                top=index*height/3+24; bottom=(index+1)*height/3-43
                row['rectangle']=(top,bottom)
                canvas.coords(row['title'],14,top-14)
                canvas.coords(row['empty'],(left+right)/2,(top+bottom)/2)
                canvas.coords(row['time'],(left+right)/2,bottom+32)
                for i,(line,label) in enumerate(zip(row['horizontal'],row['ylabels'])):
                    y=top+(bottom-top)*i/4
                    canvas.coords(line,left,y,right,y); canvas.coords(label,left-9,y)
                for i,(line,label) in enumerate(zip(row['vertical'],row['xlabels'])):
                    x=left+(right-left)*i/5
                    canvas.coords(line,x,top,x,bottom); canvas.coords(label,x,bottom+13)
                    self.set_text(label,tick_text(span*i/5,span/5))
            canvas.coords(self.simulation,width-22,10)
        self.visible(self.simulation,simulation)
        end=samples[-1]['at'] if samples else 0
        start=end-span
        points=[p for p in samples if p['at']>=start]
        now=time.monotonic()
        for (key,_,minimum),row in zip(SERIES,self.rows):
            top,bottom=row['rectangle']
            values=[p[key] for p in points]+[p['target'][key] for p in points]
            low,high=(min(values),max(values)) if values else (-1,1)
            proposed=axis_bounds(low,high,minimum)
            old=self.bounds.get(key)
            if (old is None or low<old[0] or high>old[1] or
                (now-self.bound_times[key]>=1.5 and proposed[1]-proposed[0]<(old[1]-old[0])*.65)):
                self.bounds[key]=proposed; self.bound_times[key]=now
            lo,hi=self.bounds[key]
            for i,label in enumerate(row['ylabels']):
                self.set_text(label,tick_text(hi-(hi-lo)*i/4,(hi-lo)/4))
            self.visible(row['empty'],not points)
            for target,item in ((True,row['target']),(False,row['feedback'])):
                curve=envelope(points,key,target,start,span,max(1,int(right-left)))
                coords=[]
                previous=None
                for at,value in curve:
                    x=left+(at-start)/span*(right-left)
                    y=bottom-(value-lo)/(hi-lo)*(bottom-top)
                    if target and previous is not None:
                        if value==previous[1]: continue
                        coords.extend((x,previous[2]))  # Held command, then a vertical change.
                    coords.extend((x,y)); previous=(at,value,y)
                if target and curve and coords:
                    coords.extend((left+(curve[-1][0]-start)/span*(right-left),previous[2]))
                show=len(coords)>=4
                self.visible(item,show)
                if show: canvas.coords(item,*coords)
