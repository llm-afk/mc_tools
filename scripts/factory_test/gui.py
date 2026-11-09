"""产线桌面入口：python -m scripts.factory_test.gui。"""
import importlib.util
import os
from pathlib import Path
import queue
import sys
import threading
import json
import time
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from dataclasses import asdict, replace
from contextlib import redirect_stdout, redirect_stderr

from .station_config import (StationConfig, MATERIALS,
                             hardware_input_value, material_options, load_config, save_config, state_root)
from .station import StationRunner, PresenceLatch, STEPS
from .maintenance import Maintenance
from .device_status import DeviceHealth, AdapterMonitor, adapter_status
from .diagnostics import DiagnosticLogs, log_scope, error_details
from .service_credentials import CredentialStore, CredentialStoreError, FIELDS as SECRET_FIELDS
from tkinter.scrolledtext import ScrolledText

from motor_platform import Canable

ROOT = Path(__file__).resolve().parents[2]
PYTHON_DEPENDENCIES = {
    'serial': 'pyserial>=3.5,<4',
    'reportlab': 'reportlab>=4,<5',
    'fitz': 'PyMuPDF>=1.24,<2',
    'oss2': 'oss2>=2.19,<3',
    'requests': 'requests>=2.32,<3',
}
GUI_EVENT_LIMIT = 128
GUI_EVENT_BUDGET_S = .010


class WindowConsole:
    """通过标准输出把控制面板传到父窗口，停止仍走原脚本的 quit 路径。"""
    cancelled = threading.Event()

    def __init__(self, **kwargs):
        self._panel = '正在连接设备…'
        self._notice = ''
        self.app = self
        self.last_update = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.invalidate(force=True)

    def invalidate(self, force=False):
        now = time.monotonic()
        if force or now - self.last_update >= .2:
            print('@panel ' + json.dumps(self._panel + '\n' + self._notice, ensure_ascii=False), flush=True)
            self.last_update = now

    def poll(self):
        return 'quit' if self.cancelled.is_set() else None


class EventWriter:
    def __init__(self, events, target=None, level='INFO'):
        self.events, self.target, self.level = events, target, level
        self.buffers = {}
        self.lock = threading.Lock()

    def line(self, value):
        if not value.strip():
            return
        if self.target is not None:
            if value.startswith('@panel '):
                self.target.sample('test.telemetry', value[7:], interval=1.0)
            else:
                self.target.event('test.console', value, level=self.level)
        self.events.put(('logged_line', value))

    def write(self, value):
        if value:
            with self.lock:
                thread = threading.get_ident()
                buffer = self.buffers.get(thread, '')+value
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    self.line(line.rstrip('\r'))
                self.buffers[thread] = buffer
        return len(value)

    def flush(self):
        with self.lock:
            buffer = self.buffers.pop(threading.get_ident(), '')
            if buffer:
                self.line(buffer)

    def finish(self):
        with self.lock:
            for buffer in self.buffers.values():
                if buffer:
                    self.line(buffer)
            self.buffers.clear()

    def isatty(self):
        return False


class FactoryWindow:
    def __init__(self, root, *, config_path=None, storage_root=None, preview_only=False):
        self.root = root
        self.config_path = Path(config_path).resolve() if config_path else Path(storage_root or state_root())/'station.json'
        self.storage_root = storage_root
        self.preview_only = preview_only
        self.events = queue.Queue()
        self.shutdown = threading.Event()
        self.lock = threading.Lock()
        self.busy = False
        self.dependencies_ready = False
        self.resume_requested = False
        self.demo_present = True
        self.last_probe = ''
        self.latest_result = None
        self.worker = None
        self.delivery = None
        self.last_delivery_message = None
        self.started_sessions = 0
        self.latch = PresenceLatch()
        try:
            self.config = load_config(self.config_path)
            load_error = ''
            load_exception = None
        except Exception as exc:
            self.config = StationConfig(automatic=False)
            load_error = '配置读取失败，自动流程已暂停：' + str(exc)
            load_exception = exc
        # Simulation is reserved for the guarded release self-check. A legacy
        # saved preview flag must never turn the production entry into a demo.
        self.config = replace(self.config, simulation=bool(preview_only))
        settings_path = self.config_path
        self.config = replace(self.config,credentials_file=str(settings_path))
        self.credential_store = CredentialStore(settings_path)
        credential_exception = None
        try:
            self.saved_credentials = self.credential_store.load()
            self.credential_error = ''
        except CredentialStoreError as exc:
            self.saved_credentials = dict.fromkeys(SECRET_FIELDS,'')
            self.credential_error = str(exc)
            credential_exception = exc
        self.diagnostics = DiagnosticLogs(storage_root or state_root(), simulation=self.config.simulation,
            failure=lambda message:self.events.put(('log_failure',message)))
        self.diagnostics.register_secret(os.environ.get(self.config.api_key_env, ''))
        for value in self.saved_credentials.values():
            self.diagnostics.register_secret(value)
        self.diagnostics.station.event('configuration.loaded', config=self.config, load_error=load_error,
                                       error=load_exception,level='ERROR' if load_exception else 'INFO')
        if credential_exception:
            self.diagnostics.station.event('configuration.credentials.load_failed',level='ERROR',
                error=credential_exception,path=str(settings_path))
        self.config_error = load_error
        self.auto = tk.BooleanVar(value=self.config.automatic)
        self.page = 'production'
        self.start_requested = False
        self.ready_to_start = False
        self.scan_in_progress = False
        self.pending_page = None
        self.settings_save_pending = False
        self.production_pause = threading.Event()
        self.handoff_error = ''
        self.mode_generation = 0
        self.health_states = {}
        self.device_health = DeviceHealth()
        self.last_health = 0.0
        self.adapter_monitor = None
        self.adapter_devices = None
        self.adapter_error = None
        from .operator_view import build_view
        build_view(self)
        if load_error:
            self.write_log(load_error)
        if self.credential_error:
            self.write_log(self.credential_error)
        self.write_log('临时 SN 只保留工位硬件 ABCD，其余位置填字符 0。')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.report_callback_exception = self.callback_exception
        root.after(100, self.poll)
        self.bootstrap_dependencies()

    def configure_delivery(self):
        if self.delivery is not None:
            self.delivery.stop()
            self.delivery = None
        if self.config.simulation:
            self.upload_status.set('报告上传：离线预览')
            return
        from .delivery import ReportDelivery
        self.delivery = ReportDelivery(self.storage_root or state_root(), replace(self.config),
            notify=lambda value:self.events.put(('report_delivery',value)), diagnostics=self.diagnostics)
        self.delivery.start()

    def show_settings(self):
        if self.busy:
            self.hint.set('任务运行中，结束后可修改工位设置。')
            return
        if self.control_page.active and not self.settings_save_pending:
            self.settings_error.set('调试连接中；保存后将自动停止电机、释放连接，再按新配置自动连接。')
        self.show_service_keys.set(False)
        for widget in self.secret_widgets.values():
            widget.configure(show='*')
        self.settings_window.deiconify()
        self.settings_window.transient(self.root)
        self.settings_window.lift()
        self.settings_window.focus_set()

    def show_adapter_status(self):
        if self.adapter_devices is not None:
            info = (dict(state='ERROR', detail='USB 枚举失败：'+str(self.adapter_error)) if self.adapter_error
                    else adapter_status(self.adapter_devices, self.config.adapter_sn))
            self.device_state('can', info['state'], info['detail'])

    def update_adapters(self, value):
        self.adapter_devices = value['devices']
        self.adapter_error = value.get('error')
        serials = sorted({item['serial_number'] for item in self.adapter_devices})
        self.devices.configure(values=serials)
        error = value.get('error')
        if error is None:
            self.control_page.observe_adapters(serials)
            if self.config.adapter_sn and self.config.adapter_sn not in serials:
                self.clear_adapter_selection()
            self.select_single_adapter()
            if self.vars['adapter_sn'].get() not in serials:
                self.vars['adapter_sn'].set(self.config.adapter_sn if self.config.adapter_sn in serials else '')
        else:
            self.vars['adapter_sn'].set('')
        self.show_adapter_status()
        try:
            # ttk copies -values into its popup listbox only when posting.
            # Keep that list current even if the dropdown is already open.
            popup = self.devices.tk.call('ttk::combobox::PopdownWindow', str(self.devices))
            self.devices.tk.call('ttk::combobox::ConfigureListbox', str(self.devices))
            if self.devices.tk.getboolean(self.devices.tk.call('winfo', 'ismapped', popup)):
                if serials:
                    self.devices.tk.call('ttk::combobox::PlacePopdown', str(self.devices), popup)
                else:
                    self.devices.tk.call('ttk::combobox::Unpost', str(self.devices))
        except tk.TclError as exc:
            self.diagnostics.current.event('ui.adapter_popup.refresh_failed',level='WARNING',error=exc)
        self.diagnostics.current.event('devices.can.watch.changed', devices=self.adapter_devices,
                                      selected_serial=self.config.adapter_sn, error=error,
                                      level='WARNING' if error else 'INFO')

    def clear_adapter_selection(self):
        self.save_adapter_selection('', reason='adapter_removed')

    def select_single_adapter(self):
        if (self.adapter_error is None and self.adapter_devices is not None and len(self.adapter_devices)==1
                and not self.busy and not self.control_page.active and self.pending_page is None):
            serial = self.adapter_devices[0]['serial_number']
            if self.config.adapter_sn != serial:
                self.save_adapter_selection(serial, reason='only_adapter')

    def save_adapter_selection(self, serial, *, reason):
        with self.lock:
            previous = self.config.adapter_sn
            self.config = replace(self.config, adapter_sn=serial)
            config = replace(self.config)
        self.vars['adapter_sn'].set(serial)
        self.ready_to_start = self.start_requested = False
        self.last_health = 0.0
        try:
            # Commit only the current config, never other half-edited UI fields.
            save_config(config, self.config_path)
            self.diagnostics.current.event('configuration.adapter_selected' if serial else 'configuration.adapter_removed',
                                           previous_serial=previous, selected_serial=serial, reason=reason, busy=self.busy)
        except Exception as exc:
            self.config_error = '保存 CANable 选择失败：'+str(exc)
            self.settings_error.set(self.config_error)
            self.diagnostics.current.event('configuration.adapter_clear.failed', level='ERROR', error=exc)
            self.write_log(self.config_error)

    def finish_settings(self):
        if self.settings_save_pending:
            return
        if self.page == 'control' or self.control_page.active:
            self.settings_save_pending=True
            self.control_page.leave()
            self.settings_error.set('正在停止调试并释放连接，完成后保存配置并自动连接…')
            self.diagnostics.current.event('configuration.debug_release.requested')
            self.advance_settings_save()
            return
        if self.apply_config(commit_services=True):
            self.settings_window.withdraw()

    def advance_settings_save(self):
        if not self.settings_save_pending or self.control_page.active:
            return
        self.settings_save_pending=False
        session=self.control_page.session
        if session is not None and not session.cleanup_confirmed:
            self.handoff_error=session.failure or '调试连接释放未确认'
            self.settings_error.set('保存暂停：'+self.handoff_error)
            self.diagnostics.current.event('configuration.debug_release.failed',level='ERROR',reason=self.handoff_error)
            return
        if self.apply_config(commit_services=True):
            self.control_page.finish_storage()
            self.settings_window.withdraw()
            self.diagnostics.current.event('configuration.debug_release.complete')
            if self.page == 'control' and self.pending_page is None and not self.handoff_error:
                self.control_page.enter()

    def close_settings(self):
        if not self.settings_save_pending:
            self.settings_window.withdraw()

    def show_logs(self):
        self.log.see('end')
        self.log.focus_set()

    def switch_page(self, name):
        if name not in ('production','control'):
            raise ValueError('未知页面')
        if name == self.page and self.pending_page is None:
            return
        with self.lock:
            previous = self.page
            self.production_pause.set()
            self.pending_page = name
            self.page = name
            self.start_requested = self.resume_requested = self.ready_to_start = False
            self.mode_generation += 1
        WindowConsole.cancelled.set()
        self.control_page.leave()
        self.production_page.pack_forget()
        self.control_page.frame.pack_forget()
        (self.production_page if name == 'production' else self.control_page.frame).pack(fill='both',expand=True)
        for key,button in self.nav_buttons.items():
            button.config(state='disabled' if key == name else 'normal')
        message = '正在结束产测并等待连接释放…' if name=='control' else '正在失能并退出调试，随后恢复产测设备检查…'
        self.control_page.message.set(message)
        self.banner.set(message)
        self.stop.config(state='disabled')
        self.diagnostics.current.event('mode.handoff.requested', previous_page=previous, target_page=name,
            generation=self.mode_generation, production_busy=self.busy, scanning=self.scan_in_progress)
        self.advance_page_transition()

    def station_cancelled(self):
        return self.shutdown.is_set() or self.production_pause.is_set() or WindowConsole.cancelled.is_set()

    def advance_page_transition(self):
        if self.pending_page is None or self.busy or self.scan_in_progress or self.control_page.active:
            return
        target, self.pending_page = self.pending_page, None
        session = self.control_page.session
        if session is not None and not session.cleanup_confirmed:
            self.handoff_error = session.failure or '调试连接的失能或资源释放未确认'
        if self.handoff_error:
            self.control_page.want_connection=False
            text='模式切换暂停：'+self.handoff_error
            self.control_page.message.set(text)
            self.banner.set(text)
            self.diagnostics.current.event('mode.handoff.failed', level='ERROR', target_page=target, reason=self.handoff_error)
            return
        self.control_page.finish_storage()
        self.diagnostics.current.event('mode.handoff.complete', target_page=target, generation=self.mode_generation)
        if target == 'control':
            self.control_page.enter()
        else:
            self.last_health=0.0
            self.latch.stable=0
            WindowConsole.cancelled.clear()
            self.production_pause.clear()
            self.banner.set('正在恢复产测设备检查')

    def start_or_stop(self):
        if self.busy:
            self.request_stop()
            return
        if self.page != 'production' or self.control_page.active or not self.ready_to_start:
            return
        self.auto.set(True)
        if not self.apply_config():
            return
        WindowConsole.cancelled.clear()
        self.start_requested = True
        self.diagnostics.current.event('operator.start_requested', '工人点击开始',config=self.config,
                                       device_health=self.health_states)
        self.stop.config(state='disabled')
        self.banner.set('正在开始本件测试')
        self.hint.set('测试将自动完成，请等待结果。')

    def device_state(self, name, state, detail):
        titles={'READY':'已连接','OFFLINE':'未连接','ERROR':'连接异常','SIMULATED':'模拟就绪',
                'CHECKING':'检测中','IN_USE':'测试使用中','UNKNOWN':'待确认'}
        colors={'READY':'#138365','SIMULATED':'#138365','ERROR':'#C13F50','OFFLINE':'#8491A4',
                'CHECKING':'#2563EB','IN_USE':'#2563EB','UNKNOWN':'#B27813'}
        value,label,hint=self.device_widgets[name]
        value.set('● '+titles.get(state,state))
        label.config(foreground=colors.get(state,'#8491A4'))
        hint.set(detail[:65])

    def selection_changed(self, name):
        if name in ('project','category','subcategory','factory'):
            self.vars[name].set(self.vars[name].get().strip().upper())
        if name in ('project', 'category'):
            self.category['values'] = material_options(self.vars['project'].get())
            self.subcategory['values'] = material_options(self.vars['project'].get(),self.vars['category'].get())
        return self.apply_config()

    def hardware_changed(self, part):
        return self.apply_config()

    def apply_config(self, *, commit_services=False):
        if self.busy or self.control_page.active:
            if self.control_page.active:
                self.settings_error.set('调试连接中；修改将在“保存并关闭”后生效。')
            return False
        try:
            values = {k: v.get().strip() for k, v in self.vars.items()}
            if not commit_services:
                values = {key:value for key,value in values.items() if key not in self.service_fields}
            hardware = {part:hardware_input_value(part,self.hardware_vars[part].get()) for part in 'ABCD'}
            values['hardware_version'] = ''.join(hardware.values())
            for name in ('project','category','subcategory','factory'):
                values[name] = values[name].upper()
            if values['production_date'].lower() in ('auto','自动当天'):
                values['production_date'] = ''
            values['model'] = values['hardware_version'][1:]
            values['target_id'] = int(values['target_id'])
            if 'printer_copies' in values:
                values['printer_copies'] = int(values['printer_copies'])
            config = replace(self.config, **values, automatic=self.auto.get(),
                             simulation=bool(self.preview_only))
            desired = {key:variable.get().strip() for key,variable in self.secret_vars.items()} if commit_services else self.saved_credentials
            for value in desired.values():
                self.diagnostics.register_secret(value)
            credentials_changed = commit_services and (desired!=self.saved_credentials or bool(self.credential_error and any(desired.values())))
            save_config(config, self.config_path,credentials=desired if credentials_changed else None)
            with self.lock:
                previous_config = self.config
                changed_mode = config.simulation != self.config.simulation
                reenabled = config.automatic and not self.config.automatic
                delivery_fields = ('report_upload_url','report_api_key_header','upload_config','oss_endpoint','oss_bucket','oss_region','oss_prefix')
                changed_delivery = changed_mode or credentials_changed or any(
                    getattr(config,key)!=getattr(self.config,key) for key in delivery_fields)
                self.config = config
            if credentials_changed:
                changed = [key for key in SECRET_FIELDS if desired[key]!=self.saved_credentials[key]]
                self.saved_credentials = dict(desired)
                self.credential_error = ''
                self.credential_notice.set('密钥已在本机加密保存。')
                self.diagnostics.current.event('configuration.credentials.changed',fields=changed,
                                               configured_fields=[key for key,value in desired.items() if value])
            changes = {key:dict(before=asdict(previous_config)[key],after=value)
                       for key,value in asdict(config).items() if asdict(previous_config)[key] != value}
            self.diagnostics.register_secret(os.environ.get(config.api_key_env, ''))
            if changes:
                self.diagnostics.current.event('configuration.changed', changes=changes)
            had_error = bool(self.config_error)
            self.config_error = ''
            self.settings_error.set('')
            for name in ('project','category','subcategory','factory','hardware_version'):
                self.vars[name].set(values[name])
            self.category.configure(values=material_options(config.project))
            self.subcategory.configure(values=material_options(config.project,config.category))
            self.vars['production_date'].set(values['production_date'] or '自动当天')
            self.vars['adapter_sn'].set(values['adapter_sn'])
            if config.adapter_sn != previous_config.adapter_sn:
                self.last_health = 0.0
                self.show_adapter_status()
            if commit_services:
                self.last_health = 0.0
                self.show_service_keys.set(False)
                for widget in self.secret_widgets.values():
                    widget.configure(show='*')
            for part,code in hardware.items():
                self.hardware_vars[part].set(code)
            if had_error:
                self.banner.set('配置已保存，等待设备就绪')
            if reenabled:
                WindowConsole.cancelled.clear()
            if changed_mode:
                self.diagnostics.finish_connection('mode_changed')
                self.diagnostics.simulation = config.simulation
                self.latch = PresenceLatch()
                self.latest_result = None
                self.current_sn.set('—')
                self.sn_phase.set('SN · 等待识别')
                self.last_health = 0.0
            if self.dependencies_ready and changed_delivery:
                self.configure_delivery()
            return True
        except Exception as exc:
            message = '配置未保存：' + str(exc)
            if message != self.config_error:
                self.diagnostics.current.event('configuration.rejected', '工位配置未保存',
                                               level='ERROR', error=exc)
                self.write_log(message)
            self.config_error = message
            self.settings_error.set(message)
            self.ready_to_start = False
            self.start_requested = False
            self.stop.config(state='disabled')
            self.banner.set(message)
            return False

    def write_log(self, value, *, persist=True):
        value = self.diagnostics.redact(str(value))
        self.log.config(state='normal')
        self.log.insert('end', value.rstrip()+'\n')
        self.log.see('end')
        self.log.config(state='disabled')
        # The UI is bounded; detailed evidence is written independently by the worker.
        if int(self.log.index('end-1c').split('.')[0]) > 2200:
            self.log.config(state='normal')
            self.log.delete('1.0','201.0')
            self.log.config(state='disabled')
        if persist:
            self.diagnostics.current.event('ui.message', value.rstrip())

    def callback_exception(self, exception_type, exception, tb):
        self.diagnostics.current.event('application.tk_exception', '界面回调异常', level='ERROR',
                                       error=exception.with_traceback(tb))
        self.write_log('界面操作异常，完整堆栈已保存到日志目录。',persist=False)

    def bootstrap_dependencies(self):
        def worker():
            missing = [package for module, package in PYTHON_DEPENDENCIES.items()
                       if importlib.util.find_spec(module) is None]
            self.events.put(('ready', missing))
        threading.Thread(target=worker, daemon=True).start()

    def controller(self):
        """单所有者设备检查；每件必须收到工人的开始指令才会写入或使能。"""
        last_message = ''
        while not self.shutdown.wait(.6):
            self.diagnostics.cleanup()
            with self.lock:
                config = replace(self.config)
                if (not self.dependencies_ready or not config.automatic or self.page != 'production'
                        or self.production_pause.is_set() or self.control_page.active):
                    continue
                self.scan_in_progress = True
                WindowConsole.cancelled.clear()
            requested_adapter_sn = config.adapter_sn
            try:
                if time.monotonic()-self.last_health > 2:
                    with log_scope(self.diagnostics.current.bind(phase='device_health')):
                        self.health_states = self.device_health.check(config,
                            emit=lambda name,info:self.events.put(('device_health',(name,info))),cancelled=self.station_cancelled)
                    self.last_health = time.monotonic()
                if self.station_cancelled():
                    raise InterruptedError('产测已暂停，正在交接连接')
                if not config.simulation:
                    config.validate()
                    if not config.adapter_sn:
                        raise RuntimeError('请在工位配置中选择已探测到的 CANable')
                    try:
                        with log_scope(self.diagnostics.current.bind(phase='discovery')):
                            incoming = Maintenance(config,self.station_cancelled,diagnostic_poll=True).discover(
                                fallback=not self.latch.wait_removal)
                        present,detected,detected_id = True,incoming['sn'],incoming['node_id']
                    except (OSError,TimeoutError,RuntimeError) as exc:
                        present,detected,detected_id = False,'',1
                        if str(exc) != last_message:
                            self.diagnostics.current.event('discovery.failed', '电机探测未收到有效结果',
                                                           level='WARNING', error=exc)
                            self.events.put(('log','电机探测：'+str(exc)))
                            last_message = str(exc)
                else:
                    present,detected,detected_id = self.demo_present,'DEMO-UNPROGRAMMED',1
                if present:
                    last_message = ''
                self.diagnostics.observe(present,identity=dict(sn=detected,node_id=detected_id),config=config)
                self.diagnostics.snapshot('devices.health',self.health_states)
                was_waiting = self.latch.wait_removal
                self.latch.observe(present,time.monotonic())
                if was_waiting and not self.latch.wait_removal:
                    self.events.put(('removed',None))
                support_ready = config.simulation or all(self.health_states.get(k,{}).get('state')=='READY'
                    for k in ('can','dyno','power','printer'))
                ready = present and not self.latch.wait_removal and self.latch.stable >= 2 and support_ready and config.test_profile_available
                self.diagnostics.snapshot('devices.readiness',dict(present=present,ready=ready,
                    waiting_removal=self.latch.wait_removal,support_ready=support_ready,
                    profile_ready=config.test_profile_available))
                self.events.put(('candidate',dict(present=present,ready=ready,waiting_removal=self.latch.wait_removal,
                    sn=detected,node_id=detected_id,target_id=config.target_id,support_ready=support_ready,
                    profile_ready=config.test_profile_available)))
                resume = self.resume_requested
                if not (resume or (self.start_requested and ready)):
                    continue
                if self.page != 'production':
                    continue
                self.start_requested = self.resume_requested = False
                with self.lock:
                    latest = replace(self.config)
                    if self.production_pause.is_set() or self.page != 'production' or latest.adapter_sn != requested_adapter_sn or (latest.target_id,latest.model,latest.simulation,latest.report_upload_url,latest.upload_config) != (
                            config.target_id,config.model,config.simulation,config.report_upload_url,config.upload_config):
                        self.latch.stable = 0
                        continue
                    self.busy = True
                self.scan_in_progress = False
                self.events.put(('busy',True))
                def emit(kind,value):
                    self.events.put((kind,value))
                    if config.simulation and kind=='step' and value['status']=='RUNNING':
                        WindowConsole.cancelled.wait(.55)  # 预览可看到真实状态机的各步骤。
                runner = StationRunner(config,root=self.storage_root,emit=emit,cancelled=self.station_cancelled,
                                       delivery=self.delivery,log_target=self.diagnostics.current)
                if not resume and runner.already_completed(detected):
                    self.events.put(('failed',dict(message='当前 SN 已完成生产，不能重复取号',notes=[])))
                    self.latch.completed()
                    continue
                if not resume and runner.pending():
                    # 断电遗留流程不盲目续写、续打；记录保留供工程人员处理。
                    runner.close_pending('新工件开始前自动隔离上次未完成记录；未重发取号或打印请求')
                writer = EventWriter(self.events,self.diagnostics.current)
                errors = EventWriter(self.events,self.diagnostics.current,'ERROR')
                try:
                    with log_scope(self.diagnostics.current),redirect_stdout(writer),redirect_stderr(errors):
                        record = runner.resume() if resume else runner.begin(detected,detected_id)
                finally:
                    writer.finish()
                    errors.finish()
                self.latch.completed()
                if self.production_pause.is_set() and record.get('error_notes'):
                    self.handoff_error='；'.join(record['error_notes'])
                if record['status'] != 'COMPLETE':
                    runner.close_pending('工位异常自动归档，保留原 SN、requestId、打印状态和错误日志；禁止盲重试')
                self.started_sessions += 1
            except BaseException as exc:
                changed = self.busy or str(exc) != last_message
                if changed:
                    expected = self.production_pause.is_set() and isinstance(exc,InterruptedError)
                    self.diagnostics.current.event('controller.paused' if expected else 'controller.exception',
                                                   '产测暂停并交接连接' if expected else '工位控制线程异常',
                                                   level='INFO' if expected else 'ERROR', error=exc,
                                                   busy=self.busy, config=config)
                if self.production_pause.is_set() and getattr(exc,'__notes__',None):
                    self.handoff_error='；'.join(exc.__notes__)
                if not self.production_pause.is_set():
                    self.diagnostics.observe(False,config=config)
                if changed:
                    self.events.put(('logged_line',str(exc)))
                if self.busy:
                    self.latch.completed()
                    self.events.put(('failed',dict(message=self.diagnostics.redact(str(exc)),
                        notes=self.diagnostics.redact(getattr(exc,'__notes__',[])),
                        diagnostic=self.diagnostics.redact(error_details(exc)),
                        log_path=str(self.diagnostics.current.path))))
                elif changed:
                    self.events.put(('waiting','等待设备就绪'))
                    last_message = str(exc)
            finally:
                self.scan_in_progress = False
                if self.busy:
                    self.busy = False
                    self.events.put(('busy',False))

    def refresh_progress(self):
        completed=sum(self.steps.item(name,'values')[1]=='完成' for name,_ in STEPS)
        self.progress['value']=completed
        self.progress_text.set(f'{completed} / {len(STEPS)}')

    def poll(self):
        try:
            self.process_events()
        except Exception as exc:
            self.callback_exception(type(exc),exc,exc.__traceback__)
        finally:
            if not self.shutdown.is_set():
                self.root.after(10 if not self.events.empty() else 16 if self.page=='control' else 50,self.poll)

    def process_events(self):
        statuses=dict(RUNNING='进行中',DONE='完成',FAILED='失败',PENDING='待配置',UNKNOWN='异常已记录')
        started=time.monotonic()
        handled=0
        while (not self.events.empty() and handled<GUI_EVENT_LIMIT
               and time.monotonic()-started<GUI_EVENT_BUDGET_S):
            kind,value=self.events.get_nowait()
            handled+=1
            if kind.startswith('control_'):
                self.control_page.event(kind,value)
                continue
            if kind=='ready':
                if value:
                    self.banner.set('运行环境未就绪')
                    self.hint.set('请联系工程人员处理，详细原因已记录。')
                    self.write_log('缺少依赖：'+', '.join(value))
                else:
                    self.dependencies_ready=True
                    self.configure_delivery()
                    self.banner.set('请接入待测电机')
                    self.worker=threading.Thread(target=self.controller,name='factory-station',daemon=False)
                    self.worker.start()
                    if self.adapter_monitor is None:
                        self.adapter_monitor = AdapterMonitor(lambda value:self.events.put(('adapters',value)), self.shutdown)
                        self.adapter_monitor.start()
            elif kind=='adapters':
                self.update_adapters(value)
            elif kind=='device_health':
                name, info = value
                if name != 'can' or self.adapter_monitor is None:
                    self.device_state(name, info['state'], info['detail'])
            elif kind=='health':
                for name,info in value.items(): self.device_state(name,info['state'],info['detail'])
            elif kind=='report_delivery':
                pending,blocked,done=(value.get(k,0) for k in ('pending','blocked','succeeded'))
                text=f'报告上传：待传 {pending} · 需处理 {blocked} · 已完成 {done}'
                if value.get('active'): text+=' · 上传中'
                elif value.get('error') and not pending and not blocked: text='报告上传：等待配置'
                self.upload_status.set(text)
                message=value.get('notice') or value.get('error')
                if message and message != self.last_delivery_message:
                    self.last_delivery_message=message
                    self.write_log(message,persist=False)  # Upload worker has the original motor's log target.
            elif kind=='candidate':
                self.ready_to_start=value['ready'] and not self.config_error
                self.device_state('motor','SIMULATED' if self.config.simulation and value['present'] else 'READY' if value['present'] else 'OFFLINE',
                    f'来料 ID {value["node_id"]}' if value['present'] else '请连接电源和 CAN 线')
                if not self.busy:
                    self.stop.config(text='开始测试',state='normal' if self.ready_to_start else 'disabled')
                    if self.config_error:
                        self.banner.set(self.config_error)
                        continue
                    if not value['waiting_removal'] and value['present']:
                        self.current_sn.set(value['sn'])
                        self.sn_phase.set('来料 SN · 已识别')
                        self.connection.set(f'来料 ID {value["node_id"]}  →  工位 ID {value["target_id"]}')
                        self.banner_label.config(foreground='#24364D')
                        self.banner.set('设备已就绪，请点击开始' if value['ready'] else '正在确认设备连接')
                        self.hint.set('点击后将自动校准、测试、写号、打印并保存报告。' if value['support_ready'] else '等待测功机、电源和打印机就绪。')
                        if not value.get('profile_ready',True):
                            self.banner.set('当前型号的产测参数待配置')
                            self.hint.set('工位产品信息已保存，请由工程人员补齐该平台与电机组合的测试参数。')
            elif kind=='removed':
                self.current_sn.set('—')
                self.sn_phase.set('SN · 等待识别')
                self.banner.set('请接入下一台电机')
                self.hint.set('连接电源和 CAN 线后，点击“开始测试”。')
                self.banner_label.config(foreground='#24364D')
            elif kind=='busy':
                for widget,mode in self.fields: widget.config(state='disabled' if value else mode)
                for widget in (self.auto_control,self.resume_button,self.resolve_button):
                    widget.config(state='disabled' if value else 'normal')
                self.page_menu.entryconfigure(2,state='disabled' if value else 'normal')
                self.stop.config(text='停止测试' if value else '开始测试',state='normal' if value else 'disabled')
                if value:
                    self.banner_label.config(foreground='#2563EB')
                    self.hint.set('测试正在自动进行，请等待结果。')
                    for index,(name,label) in enumerate(STEPS,1): self.steps.item(name,values=(f'{index:02d}   {label}','等待'),tags=())
                    for entry in self.result_values.values(): entry.set('等待测试')
                    self.refresh_progress()
                    for name in ('dyno','power'):
                        self.device_state(name,'IN_USE','由当前测试流程管理连接')
                else:
                    self.last_health=0
            elif kind=='step':
                name,state=value['name'],value['status']
                if self.steps.exists(name):
                    label=dict(STEPS)[name]
                    index=[n for n,_ in STEPS].index(name)+1
                    self.steps.item(name,values=(f'{index:02d}   {label}',statuses[state]),tags=(state,))
                    if state=='RUNNING':
                        self.banner.set(label)
                        self.banner_label.config(foreground='#2563EB')
                    if name in self.result_values: self.result_values[name].set(statuses[state])
                    self.refresh_progress()
                self.write_log(dict(STEPS).get(name,'两项性能测试')+'：'+statuses[state],persist=False)
            elif kind=='sn': self.current_sn.set(value)
            elif kind=='sn_phase': self.sn_phase.set(value)
            elif kind=='metrics':
                for section in value.get('sections',[]):
                    if section['name'] in self.result_values:
                        metrics=section.get('metrics',[])
                        if section['name']=='kt':
                            text=section['status']
                            if metrics: text+='  '+str(metrics[0]['shown'])+metrics[0]['unit']
                        else:
                            text='\n'.join(m['label']+'  '+m['shown']+' '+m['unit'] for m in metrics)
                        self.result_values[section['name']].set(text or '完成')
            elif kind=='resultdir': self.latest_result=Path(value)
            elif kind=='complete':
                self.banner_label.config(foreground='#138365')
                self.banner.set('本件完成，请取下电机并贴好标签')
                self.hint.set('拔下当前电机，接入下一台后再次点击“开始测试”。')
                self.current_sn.set(value['sn'])
                self.sn_phase.set('正式 SN · 已写入核对')
            elif kind=='failed':
                self.banner_label.config(foreground='#C13F50')
                self.banner.set('本件异常，请取下更换')
                self.hint.set('流程已终止，错误日志与本件记录已保存。')
                self.write_log(value['message'],persist=False)
                diagnostic = value.get('diagnostic')
                if diagnostic:
                    stage = value.get('step_label') or dict(STEPS).get(value.get('step'), value.get('step') or '工位流程')
                    self.write_log(f'阶段：{stage}；错误编号：{diagnostic["code"]}',persist=False)
                    self.write_log('排查方向：'+diagnostic['hint'],persist=False)
                    self.hint.set(f'{stage}异常，详细记录已保存。')
                if value.get('log_path'):
                    self.write_log('本次日志：'+value['log_path'],persist=False)
                for note in value.get('notes',[]): self.write_log(note,persist=False)
                if any('校准停止' in note or '失能' in note for note in value.get('notes',[])):
                    self.hint.set('请先断开工装电源，再取下更换；错误已记录。')
            elif kind=='waiting':
                if not self.latch.wait_removal: self.banner.set(value)
            elif kind=='log_failure':
                self.write_log(value,persist=False)
                self.hint.set('日志保存异常，请联系维护人员检查存储空间和目录权限。')
            elif kind in ('log','logged_line'):
                if value.startswith('@panel '):
                    try: self.write_log(json.loads(value[7:]),persist=kind=='log')
                    except ValueError: self.write_log(value,persist=kind=='log')
                else: self.write_log(value,persist=kind=='log')
        self.advance_page_transition()
        self.advance_settings_save()
        self.select_single_adapter()
        self.control_page.tick()

    def request_stop(self):
        self.diagnostics.current.event('operator.stop_requested', '请求结束测试', busy=self.busy)
        self.start_requested = False
        WindowConsole.cancelled.set()
        self.banner.set('正在结束当前测试…' if self.busy else '等待工人点击开始')

    def resume(self):
        if self.busy or not self.apply_config():
            return
        self.resume_requested = True
        self.auto.set(True)
        self.apply_config()

    def resolve_pending(self):
        if self.busy:
            return
        reason = simpledialog.askstring('异常工件归档',
            '填写处理说明（例如：已取下隔离、已人工核对标签）。\n'
            '原 SN、取号请求、报告和待上传任务全部保留，不会删除或重新生成。', parent=self.root)
        if not reason:
            return
        try:
            runner = StationRunner(replace(self.config), root=self.storage_root)
            record = runner.close_pending(reason)
            self.latest_result = Path(record['directory'])
            self.latch.completed()
            self.banner.set('异常工件已留痕归档；取下工件后，可重新开启自动工位')
            self.write_log('异常工件处理：'+reason)
        except Exception as exc:
            self.banner.set(str(exc))

    def open_results(self):
        path = self.latest_result or (self.storage_root or state_root())
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def open_error_log(self):
        path = Path(self.storage_root or state_root())/'logs'/('preview' if self.config.simulation else 'production')
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)
        except OSError as exc:
            self.diagnostics.current.event('ui.open_log_directory.failed',level='ERROR',error=exc,path=str(path))
            self.write_log('无法打开错误日志：'+str(exc))

    def open_test_report(self):
        path = None
        try:
            path = Path(self.storage_root or state_root())/('demo-data' if self.config.simulation else 'data')
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)
        except OSError as exc:
            self.diagnostics.current.event('ui.open_report_directory.failed',level='ERROR',error=exc,path=str(path))
            self.write_log('无法打开测试报告：'+str(exc))

    def close(self):
        self.diagnostics.current.event('application.shutdown.requested',busy=self.busy,
            debug_active=self.control_page.active,scan_in_progress=self.scan_in_progress,
            station_worker_alive=bool(self.worker and self.worker.is_alive()))
        if self.busy:
            self.request_stop()
            self.banner.set('已请求停止，请等待当前操作结束后关闭；校准停止未确认时请切断工装电源')
            return
        if self.control_page.active:
            self.control_page.disconnect()
            self.root.after(200,self.close)
            return
        self.shutdown.set()
        if self.delivery is not None:
            self.delivery.stop()
        WindowConsole.cancelled.set()
        self.control_page.finish_storage()
        self.diagnostics.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    window = FactoryWindow(root)
    previous_hook = threading.excepthook
    previous_unraisable = sys.unraisablehook
    def thread_exception(args):
        window.diagnostics.station.event('application.thread_exception', '后台线程未处理异常',level='ERROR',
            error=args.exc_value.with_traceback(args.exc_traceback),failed_thread=args.thread.name if args.thread else None,
            current_connection_log=window.diagnostics.current.reference)
        window.events.put(('logged_line','后台线程异常，完整堆栈已保存到日志目录。'))
    def unraisable(args):
        window.diagnostics.current.event('application.unraisable', '资源释放等回调中未抛出的异常',level='ERROR',
            error=args.exc_value.with_traceback(args.exc_traceback) if args.exc_value else None,
            error_message=args.err_msg,object_type=type(args.object).__name__)
    threading.excepthook = thread_exception
    sys.unraisablehook = unraisable
    try:
        root.mainloop()
    except BaseException as exc:
        window.diagnostics.current.event('application.unhandled',level='ERROR',error=exc)
        raise
    finally:
        threading.excepthook = previous_hook
        sys.unraisablehook = previous_unraisable


if __name__ == '__main__':
    from .desktop_entry import main as desktop_main
    desktop_main()
