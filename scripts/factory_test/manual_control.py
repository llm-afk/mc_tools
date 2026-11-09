"""调试页自动连接；使能和目标仍由工程人员明确操作。"""
from collections import deque
from contextlib import ExitStack
from dataclasses import asdict
import math
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from motor_platform import Bench, Canable, MitCommand
from motor_platform.runtime import RuntimeShutdownError
from .maintenance import Maintenance
from .station_config import hardware_parts, hardware_torque_limit
from .diagnostics import current_log, log_event, log_scope
from .waveform import WaveformPlot, FRAME_INTERVAL, FEEDBACK_COLOR, TARGET_COLOR


class ManualSession:
    def __init__(self, config, node, emit, diagnostics=None):
        self.config, self.node, self.emit = config, node, emit
        self.commands = queue.Queue(maxsize=16)
        self.stop = threading.Event()
        self.thread = None
        self.connected = self.enabled = False
        self.target = MitCommand()
        self._pending_target = MitCommand()
        self._target_lock = threading.Lock()
        self.maintenance_busy = False
        self.firmware = '模拟' if config.simulation else '—'
        self.profile_known = config.simulation and config.hardware_version[1] in ('1','2')
        self.diagnostics = diagnostics
        self.motor_sn = None
        self.failure = None
        self.cleanup_confirmed = True
        self.state_change_started = False

    @property
    def active(self):
        return self.thread is not None and self.thread.is_alive()

    @property
    def reconnect_allowed(self):
        return not self.failure or (not self.state_change_started and self.cleanup_confirmed)

    def start(self):
        if self.active:
            raise RuntimeError('调试连接已存在')
        if self.node is not None and (type(self.node) is not int or not 1 <= self.node <= 127):
            raise ValueError('电机 ID 范围为 1～127')
        self.thread = threading.Thread(target=self._run, name='manual-motor', daemon=False)
        self.thread.start()

    def command(self, kind, target=None):
        if kind not in ('enable','disable','target','clear_error','zero','calibrate'):
            raise ValueError('未知控制指令')
        if kind == 'target':
            self.set_target(target)
            return
        if not self.connected:
            raise RuntimeError('请先连接电机')
        if self.stop.is_set():
            raise RuntimeError('调试连接正在退出')
        if self.maintenance_busy:
            raise RuntimeError('维护操作进行中')
        if kind in ('clear_error','zero','calibrate') and self.enabled:
            raise RuntimeError('请先停止电机，再执行维护操作')
        if kind in ('zero','calibrate') and not self.profile_known:
            raise RuntimeError('未识别芯片平台，无法确定标零/校准协议；不影响通用使能和控制')
        if kind in ('clear_error','zero','calibrate'):
            self.maintenance_busy=True
        try:
            self.commands.put_nowait((kind, target))
        except queue.Full:
            self.maintenance_busy=False
            raise

    @property
    def pending_target(self):
        with self._target_lock:
            return self._pending_target

    def set_target(self,command):
        """Replace the latest requested value; no I/O, no queued motion history."""
        command.pack()
        if self.stop.is_set():
            raise RuntimeError('调试连接正在退出')
        with self._target_lock:
            self._pending_target=command

    def disconnect(self):
        self.stop.set()

    def _state(self, message, *, error=False):
        log_event('debug.state',message,level='ERROR' if error else 'INFO',connected=self.connected,
                  enabled=self.enabled,node_id=self.node,maintenance_busy=self.maintenance_busy,firmware=self.firmware,
                  profile_known=self.profile_known,hardware_version=self.config.hardware_version,sn=self.motor_sn)
        self.emit('control_state', dict(message=message, connected=self.connected,
                  enabled=self.enabled, error=error, busy=self.maintenance_busy, firmware=self.firmware,
                  node_id=self.node,profile_known=self.profile_known,
                  torque_limit=hardware_torque_limit(self.config.hardware_version) or 0.0))

    def _open_hardware(self):
        serial = self.config.adapter_sn
        if not serial:
            devices = Canable.detect()
            if len(devices) != 1:
                raise RuntimeError('请在工位设置中选择唯一 CANable')
            serial = devices[0]['serial_number']
        if self.node is None:
            from dataclasses import replace
            incoming=Maintenance(replace(self.config,adapter_sn=serial),self.stop.is_set).discover(exhaustive=True)
            self.node=incoming['node_id']
        bench=Bench(transport=Canable(serial),motor_ids=[self.node])
        try:
            bench.open()
            raw=bench.motor(self.node).read_serial_bytes(timeout=.2)
            from dataclasses import replace
            sn=raw.rstrip(b'\0').decode('ascii',errors='replace')
            self.motor_sn = sn
            self.profile_known=False
            if len(sn)==26:
                try:
                    hardware_parts(sn[12:16])
                except ValueError:
                    pass
                else:
                    self.config=replace(self.config,model=sn[13:16],hardware_version=sn[12:16])
                    self.profile_known=sn[13] in ('1','2')  # Only the chip platform selects maintenance protocol.
            self.firmware=str(Maintenance.read(bench,self.node,0x2100,2))
            channel=bench.start(rate_hz=1000,command_timeout=.25).motor(self.node)
            channel.monitor()
            deadline=time.perf_counter()+.25
            while True:
                try:
                    channel.state(max_age=.2)
                    break
                except TimeoutError:
                    if time.perf_counter() >= deadline:
                        raise
                    if self.stop.wait(.005):
                        raise InterruptedError('调试连接已取消')
            log_event('debug.monitor.started',node_id=self.node,target_hz=1000,
                      request_can_id=hex(0x100+self.node),feedback_can_id=hex(0x190+self.node),
                      neutral_target=asdict(MitCommand()),statistics=channel.stats())
            return bench,channel
        except BaseException as exc:
            log_event('debug.connect.failed',level='ERROR',error=exc,node_id=self.node,adapter_sn=serial)
            try:
                bench.close()
            except Exception as close_exc:
                confirmed=isinstance(close_exc,RuntimeShutdownError) and close_exc.cleanup_confirmed
                self.cleanup_confirmed = self.cleanup_confirmed and confirmed
                log_event('debug.connect.runtime_fault' if confirmed else 'debug.connect.cleanup.failed',
                          level='INFO' if confirmed else 'ERROR',error=close_exc,node_id=self.node,
                          cleanup_confirmed=confirmed)
                if not confirmed:
                    exc.add_note('连接失败后的资源释放也发生异常，详见 debug.connect.cleanup.failed')
            raise

    def _maintain(self, kind):
        if kind in ('zero','calibrate') and not self.profile_known:
            raise RuntimeError('未识别芯片平台，无法确定标零/校准协议')
        from dataclasses import replace
        maintenance=Maintenance(replace(self.config,target_id=self.node),self.stop.is_set)
        if kind=='calibrate':
            maintenance.calibrate()
        else:
            with maintenance.connect() as bench:
                if kind=='zero' and self.config.model.startswith('2'):
                    # ADM 0x2002=3 是空实现；0x2070 的 enc_set_zero 回调才是真正标零。
                    maintenance.write(bench,self.node,0x2070,2,0)
                else:
                    maintenance.write(bench,self.node,0x2002,2,0xFF if kind=='clear_error' else 3)
                error=maintenance.read(bench,self.node,0x2000,2)
                if error:
                    raise RuntimeError(f'操作后驱动器仍报告错误 0x{error:04X}')

    def _run(self):
        scopes = ExitStack()
        bench = channel = None
        failed = None
        p = v = 0.0
        previous = time.perf_counter()
        last_rx = 0.0
        last_error = None
        last_pending = None
        try:
            if self.diagnostics:
                scopes.enter_context(log_scope(self.diagnostics.current.bind(debug=True,phase='debug.connect')))
            log_event('debug.connect.start',node_id=self.node,config=self.config)
            if not self.config.simulation:
                bench,channel=self._open_hardware()
            elif self.node is None:
                self.node=1
            if self.diagnostics:
                self.diagnostics.observe(True,identity=dict(node_id=self.node,sn=self.motor_sn or self.config.base_sn),
                                         config=self.config)
                scopes.enter_context(log_scope(self.diagnostics.current.bind(debug=True)))
                log_event('debug.connected',node_id=self.node,sn=self.motor_sn,firmware=self.firmware,
                          config=self.config,enabled=False)
            self.connected = True
            self._state('已连接 · 电机未使能' if self.profile_known else '已连接 · 可使能；芯片平台未识别，标零/校准不可用')
            while not self.stop.wait(.01):
                while not self.commands.empty():
                    if self.stop.is_set():
                        break
                    kind, target = self.commands.get_nowait()
                    log_event('debug.command',command=kind,enabled=self.enabled)
                    if kind == 'enable':
                        self.state_change_started = True
                        if channel is not None:
                            channel.enable()
                        self.enabled = True
                        self._state('电机已使能 · 实时参数运行')
                    elif kind == 'disable':
                        if channel is not None:
                            channel.disable()
                        self.enabled = False
                        self.target = MitCommand()
                        self.state_change_started=False
                        self._state('电机已失能 · 继续采集状态')
                    elif kind in ('clear_error','zero','calibrate'):
                        self.state_change_started = True
                        if self.enabled:
                            raise RuntimeError('维护操作前电机必须失能')
                        self._state('正在'+{'clear_error':'清除错误','zero':'标零','calibrate':'校准'}[kind])
                        if self.config.simulation:
                            if kind=='calibrate' and self.stop.wait(1):
                                break
                            if kind=='zero': p=v=0.0
                        else:
                            bench.close()  # 先停止周期 I/O，再打开独立 SDO 维护会话。
                            bench=channel=None
                            self._maintain(kind)
                            if not self.stop.is_set(): bench,channel=self._open_hardware()
                        self.maintenance_busy=False
                        self.state_change_started=False
                        self._state('维护操作完成 · 电机未使能')
                now = time.perf_counter()
                dt, previous = min(.05, now-previous), now
                if self.stop.is_set():
                    break
                requested=self.pending_target
                if requested!=last_pending:
                    log_event('debug.target.pending',target=asdict(requested),enabled=self.enabled)
                    last_pending=requested
                self.target=requested if self.enabled else MitCommand()
                if channel is not None:
                    if self.enabled:
                        channel.set_mit(self.target)
                    state = channel.state(max_age=.2)
                    if state.error_code and self.enabled:
                        raise RuntimeError(f'驱动器错误 0x{state.error_code:04X}')
                    statistics=channel.stats()
                    if statistics['fault']:
                        raise RuntimeError(statistics['fault'])
                    if state.error_code != last_error:
                        if state.error_code or last_error:
                            log_event('debug.feedback.error',level='ERROR' if state.error_code else 'INFO',
                                      node_id=self.node,error_code=state.error_code,previous_error=last_error,
                                      statistics=statistics)
                        last_error=state.error_code
                    if state.received_at == last_rx:
                        continue
                    last_rx = state.received_at
                    sample = dict(at=state.received_at, position=state.position,
                                  velocity=state.velocity, torque=state.torque,
                                  motor_temperature=state.motor_temperature,board_temperature=state.board_temperature,
                                  error_code=state.error_code)
                else:
                    force = ((self.target.torque + self.target.kp*(self.target.position-p)
                              + self.target.kd*(self.target.velocity-v)) if self.enabled else 0.0)
                    v += (force*4-.8*v)*dt
                    v = max(-100, min(100, v))
                    p += v*dt
                    sample = dict(at=now, position=p, velocity=v, torque=force,motor_temperature=25.0,board_temperature=26.0)
                sample['target'] = asdict(requested)
                sample['sent_target'] = asdict(self.target)
                sample['enabled'] = self.enabled
                if current_log():
                    current_log().sample('debug.telemetry',sample=sample,
                                         statistics=channel.stats() if channel is not None else dict(simulated=True))
                self.emit('control_sample', sample)
        except BaseException as exc:
            target = current_log() or (self.diagnostics.current if self.diagnostics else None)
            if target:
                target.event('debug.exception',level='ERROR',error=exc,node_id=self.node,enabled=self.enabled)
            notes=list(getattr(exc,'__notes__',[]))
            if notes:
                self.cleanup_confirmed=False
            failed = '；'.join([str(exc),*notes])
        finally:
            if channel is not None and self.enabled:
                try:
                    channel.disable()
                    log_event('debug.cleanup.disable_ack',node_id=self.node)
                except Exception as exc:
                    self.cleanup_confirmed = False
                    log_event('debug.cleanup.disable_failed',level='ERROR',error=exc,node_id=self.node)
                    failed = (failed+'；' if failed else '')+'失能未确认：'+str(exc)
            if bench is not None:
                try:
                    bench.close()
                except Exception as exc:
                    confirmed=isinstance(exc,RuntimeShutdownError) and exc.cleanup_confirmed
                    self.cleanup_confirmed = self.cleanup_confirmed and confirmed
                    log_event('debug.cleanup.runtime_fault' if confirmed else 'debug.cleanup.close_failed',
                              level='INFO' if confirmed else 'ERROR',error=exc,cleanup_confirmed=confirmed)
                    if not confirmed or not failed:
                        failed = (failed+'；' if failed else '')+str(exc)
            if channel is not None:
                log_event('debug.monitor.stopped',node_id=self.node,statistics=channel.stats(),
                          cleanup_confirmed=self.cleanup_confirmed)
            self.connected = self.enabled = False
            self.target=MitCommand()
            self.maintenance_busy=False
            self.failure = failed
            self._state(failed or '已断开调试连接', error=bool(failed))
            log_event('debug.disconnected',message=failed or '调试连接已释放',level='ERROR' if failed else 'INFO')
            if self.diagnostics:
                self.diagnostics.finish_connection('debug_connection_failed' if failed else 'debug_disconnected')
            scopes.close()


class ControlPage:
    def __init__(self, host, parent):
        self.host,self.session=host,None
        self.samples=deque(maxlen=4096)
        self.frozen=False
        self.last_draw=0.0
        self.record_file=self.record_writer=None
        self.send_after=None
        self._syncing_targets=False
        self.want_connection=False
        self.next_connect=0.0
        self.session_generation=0
        self.plot_dirty=True
        self._status_text={}
        self.frame=ttk.Frame(parent,style='Page.TFrame')
        style=ttk.Style(self.frame)
        style.configure('Panel.TLabelframe',background='#FFFFFF',bordercolor='#E2E8F0')
        style.configure('Panel.TLabelframe.Label',background='#FFFFFF',foreground='#334155')
        style.configure('Panel.TLabel',background='#FFFFFF',foreground='#334155')
        style.configure('Panel.TCheckbutton',background='#FFFFFF',foreground='#334155')
        style.map('Panel.TCheckbutton',background=[('active','#FFFFFF')])
        style.configure('Panel.TButton',background='#FFFFFF',foreground='#334155',bordercolor='#D8E2EE',lightcolor='#FFFFFF',darkcolor='#FFFFFF')
        style.map('Panel.TButton',background=[('disabled','#FFFFFF'),('active','#F8FAFC')],foreground=[('disabled','#A4AFBD')])
        style.configure('Panel.TCombobox',background='#FFFFFF',fieldbackground='#FFFFFF',bordercolor='#D8E2EE')
        style.map('Panel.TCombobox',fieldbackground=[('readonly','#FFFFFF')],selectbackground=[('readonly','#FFFFFF')],selectforeground=[('readonly','#334155')])
        style.configure('Panel.Horizontal.TScale',background='#FFFFFF',troughcolor='#EDF2F7',bordercolor='#E2E8F0')
        body=ttk.Frame(self.frame)
        body.pack(fill='both',expand=True)
        left=ttk.Frame(body,style='Card.TFrame',padding=12)
        left.pack(side='left',fill='both',expand=True,padx=(0,10))
        bar=ttk.Frame(left,style='Card.TFrame')
        bar.pack(fill='x',pady=(0,8))
        ttk.Label(bar,text='实时波形',style='Card.TLabel').pack(side='left')
        ttk.Label(bar,text='━━ 反馈',style='Small.TLabel',foreground=FEEDBACK_COLOR).pack(side='left',padx=(12,8))
        ttk.Label(bar,text='━━ 目标',style='Small.TLabel',foreground=TARGET_COLOR).pack(side='left')
        self.window_s=tk.StringVar(value='5')
        window=ttk.Combobox(bar,textvariable=self.window_s,values=['5','10','30'],width=4,state='readonly')
        window.pack(side='right')
        window.bind('<<ComboboxSelected>>',lambda _:self.invalidate_plot())
        ttk.Label(bar,text='秒',style='Small.TLabel').pack(side='right',padx=6)
        self.freeze_button=ttk.Button(bar,text='暂停显示',command=self.freeze)
        self.freeze_button.pack(side='right',padx=8)
        self.canvas=tk.Canvas(left,bg='#FFFFFF',highlightthickness=0,height=640)
        self.canvas.pack(fill='both',expand=True)
        self.plot=WaveformPlot(self.canvas)
        self.canvas.bind('<Configure>',lambda _:self.invalidate_plot())
        right=ttk.Frame(body,style='Card.TFrame',padding=14,width=325)
        right.pack(side='right',fill='y')
        right.pack_propagate(False)
        self.node=tk.StringVar(value='—')
        connect=ttk.Frame(right,style='Card.TFrame')
        connect.pack(fill='x')
        ttk.Label(connect,text='电机 ID',style='Card.TLabel').pack(side='left')
        self.node_entry=ttk.Entry(connect,textvariable=self.node,width=5,state='readonly')
        self.node_entry.pack(side='left',padx=8)
        self.connect_button=ttk.Button(connect,text='自动连接',command=self.connect,style='Panel.TButton')
        self.connect_button.pack(side='left')
        self.disconnect_button=ttk.Button(connect,text='断开',command=self.disconnect,state='disabled',style='Panel.TButton')
        self.disconnect_button.pack(side='right')
        self.message=tk.StringVar(value='未连接')
        ttk.Label(right,textvariable=self.message,style='Small.TLabel',wraplength=285).pack(anchor='w',pady=(8,12))
        parameters=ttk.LabelFrame(right,text='参数设置',padding=8,style='Panel.TLabelframe')
        parameters.pack(fill='x')
        self.targets={}
        self.target_entries={}
        self.target_sliders={}
        self.range_labels={}
        torque=hardware_torque_limit(host.config.hardware_version) or 0.0
        limits={'torque':(-torque,torque),'velocity':(-50,50),
                'position':(-500,500),'kp':(0,200),'kd':(0,10)}
        for row,(name,title) in enumerate((('torque','转矩'),('velocity','速度'),('position','位置'),('kp','KP'),('kd','KD'))):
            ttk.Label(parameters,text=title,style='Panel.TLabel').grid(row=row,column=0,sticky='w',pady=5)
            value=tk.StringVar(value='0')
            self.targets[name]=value
            entry=ttk.Entry(parameters,textvariable=value,width=7)
            self.target_entries[name]=entry
            value.trace_add('write',lambda *_args,n=name:self.target_changed(n))
            entry.grid(row=row,column=1,padx=5)
            lo,hi=limits[name]
            lower=ttk.Label(parameters,text=f'{lo:g}',font=('Segoe UI',8),style='Panel.TLabel')
            lower.grid(row=row,column=2)
            slider=ttk.Scale(parameters,from_=lo,to=hi,length=102,style='Panel.Horizontal.TScale',
                command=lambda v,n=name:self.slider(n,v))
            slider.grid(row=row,column=3,padx=4)
            self.target_sliders[name]=slider
            slider.bind('<MouseWheel>',lambda e,n=name:self.wheel(n,e))
            entry.bind('<MouseWheel>',lambda e,n=name:self.wheel(n,e))
            slider.set(0)
            upper=ttk.Label(parameters,text=f'{hi:g}',font=('Segoe UI',8),style='Panel.TLabel')
            upper.grid(row=row,column=4)
            self.range_labels[name]=(lower,upper)
            entry.bind('<Return>',lambda _:self.apply(report_errors=True))
            entry.bind('<FocusOut>',lambda _:self.apply(report_errors=True))
        buttons=ttk.LabelFrame(right,text='控制',padding=8,style='Panel.TLabelframe')
        buttons.pack(fill='x',pady=12)
        self.store=tk.BooleanVar(value=False)
        ttk.Checkbutton(buttons,text='保存显示数据',variable=self.store,command=self.toggle_storage,style='Panel.TCheckbutton').grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,7))
        self.extra_buttons=[]
        for row,left_spec,right_spec in (
            (1,('错误清除','clear_error'),('标零','zero')),
            (2,('使能电机','enable'),('停止电机','disable')),
            (3,('编码器校准','calibrate'),('清除波形','clear'))):
            for col,(text,kind) in enumerate((left_spec,right_spec)):
                button=ttk.Button(buttons,text=text,command=lambda k=kind:self.clear() if k=='clear' else self.send(k),style='Panel.TButton',
                                  state='normal' if kind=='clear' else 'disabled')
                button.grid(row=row,column=col,sticky='ew',padx=3,pady=3)
                if kind=='enable': self.enable_button=button
                elif kind=='disable': self.disable_button=button
                elif kind!='clear':
                    button.operation=kind
                    self.extra_buttons.append(button)
        buttons.columnconfigure(0,weight=1);buttons.columnconfigure(1,weight=1)
        status=ttk.LabelFrame(right,text='状态信息',padding=10,style='Panel.TLabelframe')
        status.pack(fill='x')
        self.status_values={}
        for row,(name,label) in enumerate((('motor','电机状态'),('motor_temperature','电机温度'),
                ('board_temperature','驱动器温度'),('firmware','固件版本'))):
            ttk.Label(status,text=label,style='Panel.TLabel').grid(row=row,column=0,sticky='w',pady=5)
            value=tk.StringVar(value='未连接' if name=='motor' else '—')
            self.status_values[name]=value
            ttk.Label(status,textvariable=value,style='Panel.TLabel').grid(row=row,column=1,sticky='e',padx=12)
        status.columnconfigure(1,weight=1)
        self.plot_after=self.frame.after(round(FRAME_INTERVAL*1000),self.refresh_plot)

    @property
    def active(self):
        return self.session is not None and self.session.active

    def enter(self):
        if self.session is not None and not self.active and self.session.cleanup_confirmed:
            self.session=None
        self.want_connection=True
        self.next_connect=0.0
        self.invalidate_plot()

    def leave(self):
        self.want_connection=False
        self.disconnect()

    def observe_adapters(self, serials):
        if (not self.host.config.simulation and self.active and self.session.config.adapter_sn not in serials):
            # Keep the automatic-connect intent, unlike an explicit Disconnect.
            self.session.disconnect()
            self.message.set('CANable 已断开，正在释放调试连接…')

    def tick(self):
        if (not self.want_connection or self.active or self.host.page != 'control'
                or getattr(self.host,'settings_save_pending',False)
                or self.host.pending_page is not None or not self.host.dependencies_ready
                or self.host.shutdown.is_set() or self.host.busy or self.host.scan_in_progress or self.host.handoff_error):
            return
        if not self.host.config.simulation and not self.host.config.adapter_sn:
            text='等待 CANable；多只适配器时请在工位配置中选择'
            if self.message.get()!=text: self.message.set(text)
            return
        if self.session is not None and not self.session.reconnect_allowed:
            self.want_connection=False
            self.message.set('连接已停止，请查看错误日志；确认设备状态后可重新连接。')
            return
        if time.monotonic() >= self.next_connect:
            self.connect(automatic=True)

    def connect(self, *, automatic=False):
        if self.active or self.host.busy or getattr(self.host,'settings_save_pending',False):
            self.message.set('设备正在使用中，请先停止并断开现有任务')
            return
        if not self.host.config.simulation and not self.host.config.adapter_sn:
            self.message.set('请先在工位配置中选择已探测到的 CANable')
            return
        if (self.host.pending_page is not None or self.host.scan_in_progress or self.host.handoff_error):
            self.message.set(self.host.handoff_error or '正在等待前一模式释放连接…')
            return
        try:
            from dataclasses import replace
            self.want_connection=True
            self.next_connect=time.monotonic()+1.0
            self.session_generation+=1
            generation=self.session_generation
            self.session=ManualSession(replace(self.host.config),None,
                lambda k,v:self.host.events.put((k,dict(v,session_generation=generation))), diagnostics=self.host.diagnostics)
            self.apply()
            self.samples.clear()
            self.plot.reset()
            self._status_text.clear()
            self.session.start()
            self.connect_button.config(state='disabled')
            self.node_entry.config(state='disabled')
            self.disconnect_button.config(state='normal')
            self.node.set('探测中')
            self.message.set('正在自动探测设备和电机 ID…')
        except Exception as exc:
            self.host.diagnostics.current.event('debug.ui.connect.failed',level='ERROR',error=exc)
            self.message.set(str(exc))

    def send(self, kind, target=None):
        try:
            if self.session is None: raise RuntimeError('请先连接电机')
            if kind=='enable' and not self.apply(report_errors=True): return
            self.session.command(kind,target)
        except Exception as exc:
            self.host.diagnostics.current.event('debug.ui.command.failed',level='ERROR',error=exc,command=kind)
            self.message.set(str(exc))

    def cancel_target_update(self):
        if self.send_after is not None:
            self.frame.after_cancel(self.send_after)
            self.send_after=None

    def target_changed(self,name):
        if not self._syncing_targets and self.send_after is None:
            # Throttle, rather than waiting for dragging/typing to stop.
            self.send_after=self.frame.after(20,self.apply)

    def apply(self,*,report_errors=False):
        self.cancel_target_update()
        try:
            command=MitCommand(**{name:float(value.get()) for name,value in self.targets.items()})
            command.pack()
            self._syncing_targets=True
            try:
                for name,scale in self.target_sliders.items():
                    scale.set(getattr(command,name))
            finally:
                self._syncing_targets=False
            if self.session is not None and not self.session.stop.is_set():
                self.session.set_target(command)
            return True
        except Exception as exc:
            if report_errors:
                self.host.diagnostics.current.event('debug.ui.target.failed',level='ERROR',error=exc,
                    target_fields={name:value.get() for name,value in self.targets.items()})
                self.message.set('目标未更新：'+str(exc))
            return False

    def disconnect(self):
        self.want_connection=False
        self.cancel_target_update()
        if self.session:
            self.session.disconnect()
            self.message.set('正在失能并断开…')

    def slider(self, name, value):
        if self._syncing_targets: return
        number=float(value)
        if name=='position':number=round(number,1)
        if number==0:number=0.0
        self.targets[name].set(f'{number:.3f}')

    def wheel(self,name,event):
        scale=self.target_sliders[name]
        lower,upper=float(scale.cget('from')),float(scale.cget('to'))
        step={'torque':.1,'velocity':.1,'position':.1,'kp':.1,'kd':.01}[name]
        try: value=float(self.targets[name].get())
        except ValueError: value=float(scale.get())
        if not math.isfinite(value): value=float(scale.get())
        value=max(lower,min(upper,value+event.delta/120*step))
        scale.set(value)  # The same callback as dragging updates the entry/send debounce.
        return 'break'

    def clear(self):
        self.samples.clear()
        self.plot.reset()
        self.draw()

    def toggle_storage(self):
        if self.store.get():
            try:
                import csv
                from pathlib import Path
                from datetime import datetime
                from .station_config import state_root
                folder=Path(self.host.storage_root or state_root())/'debug'
                folder.mkdir(parents=True,exist_ok=True)
                path=folder/(datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'-display-samples.csv')
                self.record_file=path.open('w',newline='',encoding='utf-8-sig')
                self.record_writer=csv.writer(self.record_file)
                self.record_writer.writerow(('host_time_s','position_rad','velocity_rad_s','feedback_torque',
                    'target_position_rad','target_velocity_rad_s','target_feedforward','kp','kd',
                    'enabled','sent_position_rad','sent_velocity_rad_s','sent_feedforward','sent_kp','sent_kd'))
                self.message.set('正在保存显示数据：'+path.name)
            except Exception as exc:
                self.host.diagnostics.current.event('debug.storage.failed',level='ERROR',error=exc)
                self.store.set(False)
                self.message.set(str(exc))
        else:
            self.finish_storage()

    def finish_storage(self):
        if self.record_file:
            self.record_file.close()
            self.record_file=self.record_writer=None
        self.store.set(False)

    def freeze(self):
        self.frozen=not self.frozen
        self.freeze_button.config(text='继续显示' if self.frozen else '暂停显示')
        if not self.frozen: self.draw()

    def event(self, kind, value):
        if value.get('session_generation',self.session_generation) != self.session_generation:
            return
        if kind=='control_state':
            self.message.set(value['message'])
            connected,enabled=value['connected'],value['enabled']
            busy=value.get('busy',False)
            self.enable_button.config(state='normal' if connected and not enabled and not busy else 'disabled')
            limit=value.get('torque_limit',0.0)
            self.target_sliders['torque'].configure(from_=-limit,to=limit)
            lower,upper=self.range_labels['torque']
            lower.configure(text=f'{-limit:g}')
            upper.configure(text=f'{limit:g}')
            self.disable_button.config(state='normal' if connected and enabled else 'disabled')
            self.disconnect_button.config(state='normal' if connected else 'disabled')
            self.connect_button.config(state='disabled' if connected else 'normal')
            self.node_entry.config(state='readonly')
            if value.get('node_id') is not None: self.node.set(str(value['node_id']))
            elif not connected: self.node.set('—')
            for button in self.extra_buttons:
                available=button.operation=='clear_error' or value.get('profile_known',False)
                button.config(state='normal' if connected and not enabled and not busy and available else 'disabled')
            self.status_values['motor'].set('维护中' if busy else '已使能' if enabled else '已连接 / 未使能' if connected else '未连接')
            self.status_values['firmware'].set(value.get('firmware','—'))
            if not connected:
                self.cancel_target_update()
                self.finish_storage()
            self.host.write_log('电机调试：'+value['message'],persist=False)
        else:
            self.samples.append(value)
            status={}
            for name in ('motor_temperature','board_temperature'):
                if name in value: status[name]=f'{value[name]:.1f} °C'
            if 'enabled' in value:
                error=value.get('error_code',0)
                text=f'故障 0x{error:04X}' if error else '已使能' if value['enabled'] else '已连接 / 未使能'
                status['motor']=text
            for name,text in status.items():
                if self._status_text.get(name)!=text:
                    self.status_values[name].set(text); self._status_text[name]=text
            if self.record_writer:
                target=value['target']
                sent=value.get('sent_target',target)
                self.record_writer.writerow((value['at'],value['position'],value['velocity'],value['torque'],
                    target['position'],target['velocity'],target['torque'],target['kp'],target['kd'],
                    value.get('enabled',False),sent['position'],sent['velocity'],sent['torque'],sent['kp'],sent['kd']))
            self.plot_dirty=True

    def invalidate_plot(self):
        self.plot_dirty=True

    def refresh_plot(self):
        self.plot_after=None
        started=time.perf_counter()
        try:
            if self.plot_dirty and not self.frozen and self.host.page=='control':
                self.draw()
        except Exception as exc:
            self.host.callback_exception(type(exc),exc,exc.__traceback__)
        finally:
            delay=max(1,round((FRAME_INTERVAL-(time.perf_counter()-started))*1000))
            self.plot_after=self.frame.after(delay,self.refresh_plot)

    def draw(self):
        self.plot_dirty=False
        self.last_draw=time.monotonic()
        self.plot.render(self.samples,float(self.window_s.get()),self.canvas.winfo_width(),self.canvas.winfo_height(),
                         simulation=self.host.config.simulation)
