"""产线桌面入口：python -m scripts.factory_test.gui。"""
import importlib.util
import os
import platform
from pathlib import Path
import queue
import sys
import threading
import json
import time
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from dataclasses import asdict, replace
from contextlib import redirect_stdout, redirect_stderr

from .station_config import (StationConfig, MATERIALS,
                             hardware_input_value, material_options, load_config, save_config, state_root,
                             compare_incoming_sn, READINESS_LABELS)
from .station import StationRunner, PresenceLatch, STEPS
from .measurements.console import WindowConsole
from .maintenance import Maintenance, MotorAbsent, MultipleMotors, MotorIdentityUnreadable
from .device_status import DeviceHealth, AdapterMonitor, adapter_status
from .diagnostics import DiagnosticLogs, log_scope, error_details, LOCAL_TIME
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
        self.help_view = None
        self.network_monitor = None
        self.network_snapshot = None
        self.bootstrap_thread = None
        self.driver_manager = None
        self.driver_request = None
        self.driver_pending = self.driver_installing = False
        self.driver_preferred = None
        self.driver_snapshot = None
        self.status_messages = {}
        self.config_existed = False
        self.events = queue.Queue()
        self.shutdown = threading.Event()
        self.lock = threading.Lock()
        self.busy = False
        self.closing = False
        self.configuring = False
        self.config_revision = 0
        self.dependencies_ready = False
        self.dependency_failures = None
        self.resume_requested = False
        self.demo_present = True
        self.terminal_prompt = None
        self.next_motor = False
        self.stopping = False
        self.last_probe = ''
        self.latest_result = None
        self.worker = None
        self.delivery = None
        self.retiring_deliveries = []
        self.delivery_revision = 0
        self.printer_service = None
        self.retiring_printer_services = []
        self.printer_service_revision = 0
        self.printer_check_thread = None
        self.export_thread = None
        self.last_delivery_message = None
        self.started_sessions = 0
        self.latch = PresenceLatch()
        try:
            self.config_existed = self.config_path.exists()
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
        self.ready_identity = self.requested_identity = None
        self.ready_to_start = False
        self.scan_in_progress = False
        self.pending_page = None
        self.settings_save_pending = False
        self.production_pause = threading.Event()
        self.handoff_error = ''
        self.mode_generation = 0
        self.health_states = {}
        self.device_health = DeviceHealth()
        self.device_display = {}
        self.poll_due = self.ui_interval_started = time.monotonic()
        self.ui_timing = dict(ticks=0, max_lag_ms=0, max_work_ms=0, max_queue=0, stalls=0)
        self.last_health = 0.0
        self.adapter_monitor = None
        self.adapter_devices = None
        self.adapter_error = None
        from .operator_view import build_view
        build_view(self)
        self.log_startup()
        if self.credential_error:
            self.write_log('[凭据] '+self.credential_error,level='ERROR')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.report_callback_exception = self.callback_exception
        root.after(100, self.poll)
        self.bootstrap_dependencies()

    def drivers_blocking(self):
        return bool(getattr(self, 'driver_pending', False) or getattr(self, 'driver_installing', False))

    def configure_driver_manager(self):
        if self.config.simulation or os.name != 'nt' or self.driver_manager is not None:
            return
        from .driver_setup import DriverManager
        def snapshot():
            with self.lock:
                return replace(self.config), self.config_revision
        self.write_log('[驱动] 开始核对已连接设备的驱动、USB 打印端口和打印队列。')
        self.driver_manager = DriverManager(self.storage_root or state_root(), snapshot,
            lambda value: self.events.put(('driver_setup', value)), diagnostics=self.diagnostics)
        self.driver_manager.start()

    def driver_event(self, value):
        state = value['state']
        if state == 'INVENTORY':
            self.driver_snapshot = value['inventory']
            self.write_log('[驱动] '+value['message'], persist=False)
            for issue in value['inventory'].get('issues', []):
                self.write_log('[驱动] '+issue, level='WARNING', persist=False)
            preferred = value['inventory'].get('preferred_printer')
            if preferred and value['revision'] == self.config_revision:
                self.driver_preferred = (preferred, value['revision'])
        elif state == 'NEEDED':
            if value['revision'] != self.config_revision:
                return
            with self.lock:
                self.driver_request = value
                self.driver_pending = True
                self.start_requested = self.ready_to_start = False
            if not self.busy:
                self.stop.config(state='disabled')
            self.write_log('[驱动] '+value['message'], persist=False)
            if not self.busy and self.page == 'production':
                self.show_idle_prompt()
        elif state == 'STAGE':
            event = value['event']
            self.write_log('[驱动] '+event['message'], persist=False,
                level='ERROR' if event['stage']=='failed' else 'INFO')
        elif state in ('DONE', 'FAILED', 'CANCELLED'):
            request = getattr(self, 'driver_request', None)
            if request and value.get('request_id') == request['request_id']:
                with self.lock:
                    self.driver_pending = self.driver_installing = False
                    self.driver_request = None
                self.invalidate_readiness()
            if value.get('message'):
                message = value['message']
                if state == 'FAILED':
                    message += '；请联系工程人员查看日志或运行根目录 install_drivers.cmd。'
                if value.get('reboot_required'):
                    message += '；Windows 要求重启，请由现场负责人安排。'
                self.write_log('[驱动] '+message, persist=False, level='ERROR' if state=='FAILED' else 'INFO')
            self.last_health = 0.0

    def advance_driver_install(self):
        manager = getattr(self, 'driver_manager', None)
        if manager is None:
            return
        with self.lock:
            idle = not (self.closing or self.busy or self.scan_in_progress or self.control_page.active
                        or self.configuring or self.pending_page is not None or self.settings_save_pending)
            request = getattr(self, 'driver_request', None)
            if not idle or getattr(self, 'driver_installing', False):
                return
            if request:
                if request['revision'] != self.config_revision or manager.request != request['request_id']:
                    return
                self.driver_installing = True
                self.driver_pending = False
                self.start_requested = self.ready_to_start = False
                manager.permission.set()
                self.stop.config(state='disabled')
                if self.page == 'production':
                    self.show_idle_prompt()
                return
            preferred = getattr(self, 'driver_preferred', None)
            self.driver_preferred = None
            if not preferred or preferred[1] != self.config_revision:
                return
            from .station_config import DEFAULT_PRINTER_NAME
            if self.config.printer_name not in ('', DEFAULT_PRINTER_NAME):
                return
            config = replace(self.config, printer_name=preferred[0])
            self.configuring = True
        try:
            save_config(config, self.config_path)
            with self.lock:
                self.config = config
                self.config_revision += 1
            self.vars['printer_name'].set(config.printer_name)
            self.configure_printer_service()
            self.write_log('[驱动] 已采用与标签打印机型号和 USB 端口匹配的现有队列：'+config.printer_name)
            self.invalidate_readiness()
        except Exception as exc:
            self.diagnostics.station.event('driver.queue_selection.failed', level='ERROR', error=exc)
            self.write_log('[驱动] 保存打印队列选择失败：'+str(exc), level='ERROR')
        finally:
            with self.lock:
                self.configuring = False

    def configure_printer_service(self):
        if self.closing: return
        from .printer_service import LocalPrinterService
        self.printer_service_revision += 1
        revision = self.printer_service_revision
        previous = self.printer_service
        if previous is not None:
            previous.stop()
            self.retiring_printer_services.append(previous)
        self.printer_service = None
        if self.config.simulation:
            self.printer_service_state.set('离线自检，不启动打印服务')
            return
        self.write_log('[打印服务] 检查配置地址并准备本机标签打印服务。')
        self.printer_service = LocalPrinterService(replace(self.config),self.storage_root or state_root(),
            diagnostics=self.diagnostics,previous=previous,
            notify=lambda value:self.events.put(('printer_service',dict(value,service_revision=revision))))
        self.printer_service.start()

    def configure_delivery(self):
        if self.closing:
            return
        self.delivery_revision += 1
        revision = self.delivery_revision
        if self.delivery is not None:
            self.delivery.stop()
            self.retiring_deliveries.append(self.delivery)
            self.delivery = None
        if self.config.simulation:
            self.upload_status.set('报告上传：离线预览')
            return
        from .delivery import ReportDelivery
        self.write_log('[报告队列] 正在恢复本机待传记录，检查报告服务与 OSS 配置。')
        self.delivery = ReportDelivery(self.storage_root or state_root(), replace(self.config),
            notify=lambda value:self.events.put(('report_delivery',dict(value,delivery_revision=revision))), diagnostics=self.diagnostics)
        self.delivery.start()

    def show_help(self, event=None):
        if self.closing:
            return 'break'
        try:
            if self.help_view is None:
                from .help_view import HelpWindow
                self.help_view = HelpWindow(self.root)
            self.help_view.show()
            self.diagnostics.current.event('ui.help.opened', page=self.page, busy=self.busy)
        except Exception as exc:
            self.diagnostics.current.event('ui.help.failed', level='ERROR', error=exc)
            self.write_log('使用手册无法打开：'+str(exc))
            messagebox.showerror('使用手册无法打开','请核对软件包是否完整；详细错误已保存在日志目录。',parent=self.root)
        return 'break'

    def show_settings(self):
        if self.closing:
            return
        if getattr(self, 'driver_installing', False):
            self.hint.set('正在安装设备驱动，完成后可修改工位设置。')
            return
        if self.busy:
            self.hint.set('任务运行中，结束后可修改工位设置。')
            return
        if self.control_page.active and not self.settings_save_pending:
            self.settings_error.set('调试连接中；保存后将自动停止电机、释放连接，再按新配置自动连接。')
        self.show_service_keys.set(False)
        for widget in self.secret_widgets.values():
            widget.configure(show='*')
        self.refresh_environment()
        self.settings_window.deiconify()
        self.settings_window.transient(self.root)
        self.settings_window.lift()
        self.settings_window.focus_set()
        self.firmware_panel.refresh()

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
        enumeration_error=str(value['error']) if value.get('error') else ''
        self.status_log('adapter.inventory',(tuple(serials),enumeration_error),
            '[CANable 枚举] '+('失败：'+enumeration_error if enumeration_error else
                f'发现 {len(serials)} 只：'+('、'.join(serials) or '未检测到适配器')),
            level='WARNING' if enumeration_error else 'INFO')
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
                and not self.busy and not self.closing and not self.control_page.active and self.pending_page is None):
            serial = self.adapter_devices[0]['serial_number']
            if self.config.adapter_sn != serial:
                self.save_adapter_selection(serial, reason='only_adapter')

    def save_adapter_selection(self, serial, *, reason):
        with self.lock:
            if self.closing or self.configuring:
                return
            self.configuring = True
            previous = self.config.adapter_sn
            config = replace(self.config, adapter_sn=serial)
        try:
            # Commit only the current config, never other half-edited UI fields.
            save_config(config, self.config_path)
            with self.lock:
                self.config = config
                self.config_revision += 1
                self.invalidate_readiness()
            self.vars['adapter_sn'].set(serial)
            self.diagnostics.current.event('configuration.adapter_selected' if serial else 'configuration.adapter_removed',
                                           previous_serial=previous, selected_serial=serial, reason=reason, busy=self.busy)
        except Exception as exc:
            self.config_error = '保存 CANable 选择失败：'+str(exc)
            self.settings_error.set(self.config_error)
            self.diagnostics.current.event('configuration.adapter_clear.failed', level='ERROR', error=exc)
            self.write_log(self.config_error)
            self.ready_to_start = self.start_requested = False
            self.stop.config(state='disabled')
        finally:
            with self.lock:
                self.configuring = False

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
        if self.closing or not self.settings_save_pending or self.control_page.active:
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
        if self.closing:
            return
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

    def sn_configuration_ready(self,config):
        return bool(config.simulation or not self.credential_error and (
            self.saved_credentials.get('sn_api_key') or os.environ.get(config.api_key_env,'')))

    def advance_page_transition(self):
        if self.closing or self.pending_page is None or self.busy or self.scan_in_progress or self.control_page.active:
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
            self.latch.absent_since=None
            WindowConsole.cancelled.clear()
            self.production_pause.clear()
            if getattr(self,'terminal_prompt',None):
                self.set_prompt(*self.terminal_prompt)
            else:
                self.banner.set('正在恢复产测设备检查')
                self.hint.set('请稍候，工位正在重新识别设备。')

    def start_or_stop(self):
        if self.closing:
            return
        if self.busy:
            self.request_stop()
            return
        if self.drivers_blocking() or self.page != 'production' or self.control_page.active or not self.ready_to_start:
            return
        WindowConsole.cancelled.clear()
        with self.lock:
            self.requested_identity = self.ready_identity
            self.start_requested = True
        self.diagnostics.current.event('operator.start_requested', '工人点击开始',config=self.config,
                                       device_health=self.health_states,identity=self.requested_identity)
        self.stop.config(state='disabled')
        self.banner.set('请保持连接，正在核对本件电机')
        self.hint.set('测试将自动完成，请等待结果。')

    def device_state(self, name, state, detail):
        if self.device_display.get(name)==(state,detail):
            return
        self.device_display[name]=(state,detail)
        titles={'READY':'已连接','OFFLINE':'未连接','ERROR':'连接异常','SIMULATED':'模拟就绪',
                'CHECKING':'检测中','IN_USE':'测试使用中','UNKNOWN':'待确认','WAITING':'等待检测'}
        colors={'READY':'#138365','SIMULATED':'#138365','ERROR':'#C13F50','OFFLINE':'#8491A4',
                'CHECKING':'#2563EB','IN_USE':'#2563EB','UNKNOWN':'#B27813'}
        route=str(detail)
        if name in ('dyno','power') and state=='READY':
            route=route.split(' · ',1)[0]
            if name=='power': route+=' · '+('输出开启' if '输出开启' in str(detail) else '输出关闭')
        title=READINESS_LABELS.get(name,name)
        self.status_log('device.'+name,(state,route),f'[{title}] '+titles.get(state,state)+'；'+str(detail),
            level='ERROR' if state=='ERROR' else 'WARNING' if state in ('OFFLINE','UNKNOWN') else 'INFO')
        value,label,hint=self.device_widgets[name]
        value.set('● '+titles.get(state,state))
        label.config(foreground=colors.get(state,'#8491A4'))
        detail=' '.join(self.diagnostics.redact(str(detail)).split())
        hint.set(detail if len(detail)<=65 else detail[:64]+'…')

    def selection_changed(self, name):
        if name in ('project','category','subcategory','factory'):
            self.vars[name].set(self.vars[name].get().strip().upper())
        if name in ('project', 'category'):
            self.category['values'] = material_options(self.vars['project'].get())
            self.subcategory['values'] = material_options(self.vars['project'].get(),self.vars['category'].get())
        return self.apply_config()

    def hardware_changed(self, part):
        return self.apply_config()

    def invalidate_readiness(self):
        self.last_health = 0.0
        self.health_states = {}
        self.latch.stable = 0
        self.latch.absent_since = None
        self.ready_to_start = self.start_requested = False
        if not self.busy:
            self.stop.config(state='disabled')
            if self.page == 'production':
                self.show_idle_prompt()
                for name in ('motor','dyno','power','printer'):
                    self.device_state(name,'CHECKING','配置已更新，正在重新检查')

    def production_event_current(self, value):
        return (not self.closing and not self.configuring and self.page == 'production'
            and self.pending_page is None and (self.busy or not self.drivers_blocking())
            and value.get('config_revision',self.config_revision) == self.config_revision
            and value.get('mode_generation',self.mode_generation) == self.mode_generation)

    def apply_config(self, *, commit_services=False):
        with self.lock:
            if self.closing or self.configuring or self.busy or self.control_page.active or getattr(self, 'driver_installing', False):
                if self.control_page.active:
                    self.settings_error.set('调试连接中；修改将在“保存并关闭”后生效。')
                return False
            self.configuring = True
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
            if 'printer_copies' in values:
                values['printer_copies'] = int(values['printer_copies'])
            config = replace(self.config, **values, automatic=self.auto.get(),
                             simulation=bool(self.preview_only))
            desired = {key:variable.get().strip() for key,variable in self.secret_vars.items()} if commit_services else self.saved_credentials
            for value in desired.values():
                self.diagnostics.register_secret(value)
            credentials_changed = commit_services and (desired!=self.saved_credentials or bool(self.credential_error and any(desired.values())))
            if config != self.config or credentials_changed or commit_services or not self.config_path.exists():
                save_config(config, self.config_path,credentials=desired if credentials_changed else None)
            with self.lock:
                previous_config = self.config
                changed_mode = config.simulation != self.config.simulation
                reenabled = config.automatic and not self.config.automatic
                delivery_fields = ('report_upload_url','report_api_key_header','upload_config','oss_endpoint','oss_bucket','oss_region','oss_prefix')
                changed_delivery = changed_mode or credentials_changed or any(
                    getattr(config,key)!=getattr(self.config,key) for key in delivery_fields)
                changed_printer = changed_mode or any(getattr(config,key)!=getattr(self.config,key) for key in (
                    'printer_url','printer_name','printer_status_path','printer_print_path'))
                self.config = config
                if config != previous_config or credentials_changed:
                    self.config_revision += 1
                    self.invalidate_readiness()
            if credentials_changed:
                changed = [key for key in SECRET_FIELDS if desired[key]!=self.saved_credentials[key]]
                self.saved_credentials = dict(desired)
                self.credential_error = ''
                self.diagnostics.current.event('configuration.credentials.changed',fields=changed,
                                               configured_fields=[key for key,value in desired.items() if value])
            previous_values = asdict(previous_config)
            changes = {key:dict(before=previous_values[key],after=value)
                       for key,value in asdict(config).items() if previous_values[key] != value}
            self.diagnostics.register_secret(os.environ.get(config.api_key_env, ''))
            if changes:
                self.diagnostics.current.event('configuration.changed', changes=changes)
            if commit_services:
                self.write_log('[配置] 工位设置已保存。')
                self.log_configuration()
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
                self.show_idle_prompt()
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
            if self.dependencies_ready and changed_printer:
                self.configure_printer_service()
            if config.hardware_version[1] != previous_config.hardware_version[1]:
                self.firmware_panel.refresh()
            self.refresh_environment()
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
            self.show_idle_prompt()
            return False
        finally:
            with self.lock:
                self.configuring = False

    def status_log(self, key, signature, message, *, level='INFO', persist=True):
        if self.status_messages.get(key)==signature:
            return
        self.status_messages[key]=signature
        self.write_log(message,level=level,persist=persist)

    def log_startup(self):
        from .version import VERSION_LABEL
        self.write_log(f'[启动] 上位机 {VERSION_LABEL} · {platform.system()} {platform.release()} · '+
            ('独立 EXE' if getattr(sys,'frozen',False) else 'Python 源码')+f' · 进程 {os.getpid()}')
        if getattr(self,'config_error',''):
            self.write_log('[配置] 载入失败，已使用默认参数并暂停设备监视；原文件：'+str(self.config_path)+
                '；'+self.config_error,level='ERROR')
        else:
            self.write_log('[配置] '+('已读取工位配置：' if self.config_existed else '首次启动，使用默认工位配置：')+str(self.config_path))
        self.write_log('[存储] 工位数据目录：'+str(self.storage_root or state_root()))
        self.write_log('[日志] 当前启动记录：'+str(self.diagnostics.station.path)+'；日志保留 30 天。')
        self.log_configuration()
        self.write_log('[探测] '+('设备监视已启用，等待环境就绪后探测硬件。' if self.config.automatic else
            '设备监视已暂停，可在工位配置 → 管理中启用。'))
        self.write_log('[探测] CANable：枚举适配器；电机：读取实际 ID / SN；测功机：监听有效反馈。')
        self.write_log('[探测] 电源：先确认测功机串口，再检查电源；打印机：核对服务和指定队列。')
        if self.config.simulation:
            self.write_log('[离线自检] 硬件与网络使用隔离模拟，不访问现场设备。')

    def log_configuration(self):
        from .environment import credential_status, CREDENTIAL_LABELS
        from .firmware import firmware_root
        config=self.config
        target = config.target_id if config.target_id is not None else '未定义'
        self.write_log(f'[工位] 产品目标 ID {target}；产品 {config.project} / {config.category} / '+
            f'{config.subcategory}；工厂 {config.factory}；硬件 ABCD {config.hardware_version}')
        self.write_log(f'[串口] 测功机 {config.dyno_port}；电源 {config.power_port}')
        self.write_log('[SN] 临时 SN '+config.base_sn+'；正式 SN 由 MES 分配。')
        self.write_log('[固件] APP 文件夹：'+str(firmware_root(config))+'；APP 文件校验在后台进行。')
        self.write_log('[取号配置] '+config.mes_url.rstrip('/')+config.mes_sn_path)
        self.write_log('[打印配置] '+config.printer_url.rstrip('/')+config.printer_status_path+
            '；队列 '+(config.printer_name or '服务默认队列')+f'；每台 {config.printer_copies} 份')
        self.write_log('[报告配置] '+(config.report_upload_url or '使用旧版配置中的报告地址'))
        self.write_log('[OSS 配置] '+config.oss_endpoint+' / '+config.oss_bucket+' / '+config.oss_region+
            '；对象前缀 '+(config.oss_prefix or '桶根目录'))
        states=credential_status(config,self.saved_credentials,credential_error=self.credential_error)
        self.write_log('[凭据] '+'；'.join(CREDENTIAL_LABELS[name]+' '+state for name,state in states.items())+
            '。仅检查配置是否存在，鉴权结果以实际请求为准。')

    def write_log(self, value, *, persist=True, level='INFO'):
        value = self.diagnostics.redact(str(value))
        following = not self.log.winfo_ismapped() or self.log.yview()[1]>=.995
        self.log.config(state='normal')
        stamp=datetime.now(LOCAL_TIME).strftime('%H:%M:%S.%f')[:-3]
        self.log.insert('end', f'[{stamp}] '+value.rstrip()+'\n',level)
        if following: self.log.see('end')
        self.log.config(state='disabled')
        # The UI is bounded; detailed evidence is written independently by the worker.
        lines=int(self.log.index('end-1c').split('.')[0])
        if lines > 2200:
            self.log.config(state='normal')
            self.log.delete('1.0',f'{lines-2000}.0')
            self.log.config(state='disabled')
        if persist:
            self.diagnostics.current.event('ui.message', value.rstrip(),level=level)

    def callback_exception(self, exception_type, exception, tb):
        self.diagnostics.current.event('application.tk_exception', '界面回调异常', level='ERROR',
                                       error=exception.with_traceback(tb))
        self.write_log('界面操作异常，完整堆栈已保存到日志目录。',persist=False)

    def bootstrap_dependencies(self):
        self.write_log('[环境] 开始加载串口、报告渲染和云端客户端组件。')
        if not self.config.simulation:
            from .network_status import NetworkMonitor
            self.write_log('[网络] 正在读取 Wi-Fi / 有线网络、IP、网关和 DNS 配置。')
            self.network_monitor = NetworkMonitor(lambda value:self.events.put(('network_status',value)),
                self.shutdown,self.diagnostics)
            self.network_monitor.start()
        def worker():
            missing = []
            for module, package in PYTHON_DEPENDENCIES.items():
                if self.shutdown.is_set(): return
                started = time.perf_counter()
                self.diagnostics.station.event('environment.dependency.checking',module=module,package=package)
                try:
                    importlib.import_module(module)
                except Exception as exc:
                    missing.append(package)
                    self.diagnostics.station.event('environment.dependency.failed', level='ERROR',
                        error=exc, module=module, package=package)
                    self.events.put(('dependency',dict(module=module,package=package,ok=False,error=str(exc))))
                else:
                    self.diagnostics.station.event('environment.dependency.ready', module=module,package=package,
                        elapsed_s=round(time.perf_counter()-started,4))
                    self.events.put(('dependency',dict(module=module,package=package,ok=True)))
            self.events.put(('ready', missing))
        self.bootstrap_thread = threading.Thread(target=worker,name='dependency-check',daemon=True)
        self.bootstrap_thread.start()

    def refresh_environment(self):
        from .environment import credential_status, environment_summary, CREDENTIAL_LABELS
        states = credential_status(self.config, self.saved_credentials, credential_error=self.credential_error)
        self.diagnostics.snapshot('configuration.credential_sources',
            [dict(field=name,status=state) for name,state in states.items()])
        self.credential_notice.set('；'.join(CREDENTIAL_LABELS[name]+'：'+value for name,value in states.items()) +
            '\n以上为已保存状态，未验证密钥有效性。修改后点击“保存并关闭”，密钥由 Windows 当前用户加密保存。')
        text = environment_summary(self.config, self.saved_credentials, config_path=self.config_path,
            storage_root=self.storage_root or state_root(), dependency_failures=self.dependency_failures,
            credential_error=self.credential_error, failure=getattr(self,'last_failure',None))
        text += '\n\n当前界面状态（最近一次检查）：'
        for name,title in (('can','CAN 适配器'),('motor','待测电机'),('dyno','测功机'),('power','电源'),('printer','标签打印机')):
            state,_,detail = self.device_widgets[name]
            text += '\n'+title+'：'+state.get()+'；'+detail.get()
        text += '\n'+self.upload_status.get()
        text += '\n本机打印服务：'+self.printer_service_state.get()
        if getattr(self,'network_snapshot',None) is not None:
            from .network_status import network_messages
            text += '\n\n本机网络状态（最近一次检查）：\n'+'\n'.join(message for _,message in network_messages(self.network_snapshot))
        if getattr(self,'driver_snapshot',None) is not None:
            text += '\n\n本机驱动检查（最近一次快照）：\n'+json.dumps(self.driver_snapshot,ensure_ascii=False,indent=2)
        text += '\n驱动准备：'+('安装中' if getattr(self,'driver_installing',False) else '等待工位空闲' if getattr(self,'driver_pending',False) else '空闲')
        text += '\n当前日志：'+str(self.diagnostics.current.path)
        if self.config_error or self.handoff_error:
            text += '\n当前异常：'+(self.config_error or self.handoff_error)
        self.environment_text.configure(state='normal')
        self.environment_text.delete('1.0','end')
        self.environment_text.insert('1.0',self.diagnostics.redact(text))
        self.environment_text.configure(state='disabled')

    def copy_environment(self):
        self.refresh_environment()
        self.root.clipboard_clear()
        self.root.clipboard_append(self.environment_text.get('1.0','end-1c'))
        self.settings_notice.set('诊断信息已复制；不包含密钥内容。')

    def export_session_logs(self):
        if self.closing or self.export_thread and self.export_thread.is_alive():
            return
        filename = filedialog.asksaveasfilename(parent=self.settings_window,title='导出本次启动日志',
            initialfile='工位诊断-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.zip',
            defaultextension='.zip',filetypes=[('ZIP 压缩文件','*.zip')])
        if not filename:
            return
        self.export_button.config(state='disabled')
        self.settings_notice.set('正在整理本次启动与电机连接日志…')
        def save():
            try:
                self.diagnostics.export_boot(filename)
                self.events.put(('logs_exported',(True,filename)))
            except Exception as exc:
                self.diagnostics.current.event('logs.export_failed',level='ERROR',error=exc)
                self.events.put(('logs_exported',(False,str(exc))))
        self.export_thread=threading.Thread(target=save,name='diagnostic-export',daemon=True)
        self.export_thread.start()
        self.diagnostics.current.event('ui.environment.copied', '已复制本机运行环境与配置摘要', contains_credentials=False)

    def open_printer_config(self):
        from .printer_service import prepare_config,local_endpoint,service_config_path
        if not local_endpoint(self.config) or self.printer_service is not None and self.printer_service.external:
            self.settings_notice.set('当前使用外部打印服务，请在该服务目录中修改标签配置。')
            return
        try:
            path=service_config_path(self.storage_root or state_root())
            if not path.exists(): path=prepare_config(self.config,self.storage_root or state_root())
            import subprocess
            subprocess.Popen([str(Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/notepad.exe'),str(path)])
            self.settings_notice.set('标签配置已打开；修改保存后重新启动上位机生效。')
            self.diagnostics.current.event('ui.printer_config.opened',path=str(path))
        except Exception as exc:
            self.settings_error.set('无法打开打印服务配置：'+str(exc))
            self.diagnostics.current.event('ui.printer_config.failed',level='ERROR',error=exc)

    def open_firmware_directory(self):
        from .firmware_library import firmware_root
        directory = firmware_root()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            os.startfile(directory)
        except OSError as exc:
            self.diagnostics.current.event('firmware.directory.failed', level='ERROR', error=exc, directory=str(directory))
            self.settings_error.set('无法打开固件目录：'+str(exc))

    def check_printer_service(self, *, test_print=False):
        if self.busy:
            self.settings_notice.set('产测进行中，结束后可检查或试打标签。')
            return
        if self.closing or self.printer_check_thread is not None and self.printer_check_thread.is_alive(): return
        config,revision=replace(self.config),self.config_revision
        if test_print: config=replace(config,printer_copies=1)
        self.settings_notice.set('正在试打一张 TEST-SN-0001 标签…' if test_print else '正在检查已保存的打印配置…')
        self.printer_check_button.config(state='disabled')
        self.printer_test_button.config(state='disabled')
        def check():
            from .integrations import PrinterClient
            with log_scope(self.diagnostics.current.bind(phase='printer_check')):
                try:
                    reply=PrinterClient(config).ready(fresh=True)
                    item=None
                    if test_print:
                        if self.closing: raise InterruptedError('上位机正在退出，未提交测试标签')
                        item=PrinterClient(config).print_sn('TEST-SN-0001')
                    self.events.put(('printer_check',dict(config_revision=revision,ok=True,response=reply,
                        test_print=test_print,print_result=item)))
                except Exception as exc:
                    context=getattr(exc,'diagnostic_context',{})
                    self.events.put(('printer_check',dict(config_revision=revision,ok=False,message=str(exc),
                        response=context.get('response',{}),diagnostic=self.diagnostics.redact(error_details(exc)))))
        self.printer_check_thread=threading.Thread(target=check,name='printer-service-check',daemon=True)
        self.printer_check_thread.start()

    def controller(self):
        """单所有者设备检查；每件必须收到工人的开始指令才会写入或使能。"""
        last_message = ''
        last_candidate = last_probe_key = last_station_error = None
        recent_node = None
        firmware_check_at = 0
        firmware_revision = None
        firmware_issue = firmware_file = firmware_sha256 = ''
        while not self.shutdown.wait(.6):
            self.diagnostics.cleanup()
            with self.lock:
                config = replace(self.config)
                revision, mode_generation = self.config_revision, self.mode_generation
                if self.drivers_blocking():
                    # Re-emit the current operator prompt when driver preparation ends.
                    last_candidate = last_probe_key = None
                if (self.closing or self.shutdown.is_set() or self.configuring
                        or not self.dependencies_ready or not config.automatic or self.page != 'production'
                        or self.production_pause.is_set() or self.control_page.active or self.drivers_blocking()):
                    continue
                self.scan_in_progress = True
                start_before_probe = self.start_requested
                WindowConsole.cancelled.clear()
            try:
                probe_latch = self.latch
                motor_state, detail = 'unavailable', '请连接或选择 CAN 适配器'
                detected, detected_id = '', None
                if config.simulation:
                    motor_state = 'present' if self.demo_present else 'absent'
                    if self.demo_present:
                        detected, detected_id = 'DEMO-UNPROGRAMMED', 1
                    detail = '离线模拟反馈'
                elif config.adapter_sn:
                    config.validate()
                    extra_ids = (recent_node,) if recent_node is not None else ()
                    try:
                        with log_scope(self.diagnostics.current.bind(phase='discovery')):
                            incoming = Maintenance(config,self.station_cancelled,diagnostic_poll=True).discover(
                                fallback=not probe_latch.wait_removal, recent_ids=extra_ids)
                        detected, detected_id = incoming['sn'], incoming['node_id']
                        motor_state, detail = 'present', f'已识别电机 ID {detected_id}'
                    except InterruptedError:
                        raise
                    except (OSError, RuntimeError, ValueError) as exc:
                        detail = str(exc)
                        if isinstance(exc, MotorAbsent):
                            motor_state = 'absent'
                        elif isinstance(exc, MultipleMotors):
                            motor_state = 'multiple'
                        elif isinstance(exc, MotorIdentityUnreadable):
                            motor_state, detected_id = 'identity_error', exc.node_id
                        else:
                            motor_state = 'error'
                        if detail != last_message:
                            self.diagnostics.current.event('discovery.failed', '电机探测未收到有效结果',
                                                           level='WARNING', error=exc, outcome=motor_state)
                            self.events.put(('log','电机探测：'+detail))
                            last_message = detail
                if self.station_cancelled():
                    raise InterruptedError('产测已暂停，正在交接连接')
                generation = dict(config_revision=revision, mode_generation=mode_generation)
                with self.lock:
                    if (self.configuring or revision != self.config_revision or config != self.config
                            or mode_generation != self.mode_generation or probe_latch is not self.latch):
                        continue
                present = motor_state == 'present'
                if present:
                    recent_node = detected_id
                    last_message = ''
                observation = present if motor_state in ('present','absent') else None
                probe = dict(motor_probe=motor_state, motor_detail=detail, present=present,
                             node_id=detected_id, sn=detected, target_id=config.target_id, **generation)
                known_connection = self.diagnostics.current.context.get('connection_id')
                probe.update(connection_id=known_connection, waiting_removal=probe_latch.wait_removal)
                preview_key = (revision, mode_generation, motor_state, detail, detected_id, detected, probe_latch.wait_removal)
                if preview_key != last_probe_key:
                    self.events.put(('motor_probe', probe.copy()))
                    last_probe_key = preview_key
                    last_station_error = None
                if time.monotonic()-self.last_health > 2:
                    with log_scope(self.diagnostics.current.bind(phase='device_health')):
                        health = self.device_health.check(config,
                            emit=lambda name,info:self.events.put(('device_health',dict(name=name,info=info,
                                config_revision=revision,mode_generation=mode_generation))),cancelled=self.station_cancelled,
                                generation=(revision,mode_generation),
                                inventory_emit=lambda info:self.events.put(('serial_inventory',dict(info,
                                    config_revision=revision,mode_generation=mode_generation))))
                    with self.lock:
                        if (self.configuring or revision != self.config_revision or config != self.config
                                or mode_generation != self.mode_generation):
                            continue
                        self.health_states = health
                        self.last_health = time.monotonic()
                if self.station_cancelled():
                    raise InterruptedError('产测已暂停，正在交接连接')
                with self.lock:
                    if (self.configuring or revision != self.config_revision or config != self.config
                            or mode_generation != self.mode_generation or probe_latch is not self.latch):
                        continue
                    was_waiting = probe_latch.wait_removal
                    probe_latch.observe(observation,time.monotonic(),(config.adapter_sn, detected_id, detected))
                if was_waiting and not probe_latch.wait_removal:
                    self.events.put(('removed', generation))
                    recent_node = None
                connection = self.diagnostics.observe(observation,identity=dict(sn=detected,node_id=detected_id),config=config)
                probe.update(connection_id=connection.context.get('connection_id') if connection else None,
                             waiting_removal=probe_latch.wait_removal)
                if not config.simulation:
                    with log_scope(self.diagnostics.current.bind(phase='printer_health')):
                        printer_info = self.device_health.printer.poll(config,(revision,mode_generation))
                    with self.lock:
                        if revision != self.config_revision or mode_generation != self.mode_generation or self.configuring:
                            continue
                        if self.health_states.get('printer') != printer_info:
                            self.health_states = dict(self.health_states,printer=printer_info)
                            self.events.put(('device_health',dict(name='printer',info=printer_info,
                                config_revision=revision,mode_generation=mode_generation)))
                states = {name:info['state'] for name,info in self.health_states.items()}
                self.diagnostics.snapshot('devices.health',states)
                needed = [] if config.simulation else [key for key in ('can','dyno','power','printer') if states.get(key)!='READY']
                support_ready = not needed
                sn_configured=self.sn_configuration_ready(config)
                if not config.simulation and (revision!=firmware_revision or time.monotonic()>=firmware_check_at):
                    from .firmware_library import load_target, FirmwareError
                    try:
                        _, firmware_info = load_target(config)
                        firmware_file,firmware_sha256,firmware_issue=firmware_info['file'],firmware_info['sha256'],''
                    except (OSError,FirmwareError,ValueError) as exc:
                        firmware_file,firmware_sha256,firmware_issue='','',str(exc)
                    firmware_revision,firmware_check_at=revision,time.monotonic()+1
                if firmware_issue and not config.simulation: needed.append('firmware')
                if not present: needed.append('motor')
                if not sn_configured: needed.append('sn_key')
                if not config.mes_url and not config.simulation: needed.append('mes_url')
                if not config.test_profile_available: needed.append('profile')
                if config.target_id is None: needed.append('target_id')
                if self.latch.wait_removal: needed.append('remove')
                elif present and self.latch.stable < 2: needed.append('stabilizing')
                ready = not needed
                profile = compare_incoming_sn(detected,config) if present else {}
                if present:
                    self.diagnostics.snapshot('connection.product_profile',profile)
                candidate = dict(probe,ready=ready,support_ready=support_ready,
                    profile_ready=config.test_profile_available,sn_configured=sn_configured,blockers=needed,product=profile)
                candidate.update(firmware_file=firmware_file,firmware_sha256=firmware_sha256,firmware_issue=firmware_issue)
                last_station_error = None
                if candidate != last_candidate:
                    self.diagnostics.snapshot('devices.readiness',candidate)
                    self.events.put(('candidate',candidate))
                    last_candidate = candidate
                with self.lock:
                    resume = self.resume_requested
                    # A click during a slow peripheral check needs a new motor identity sample.
                    if not (resume or self.start_requested and start_before_probe):
                        continue
                    self.start_requested = self.resume_requested = False
                    last_candidate = None
                    if not resume and (not ready or self.requested_identity != self.candidate_identity(candidate)):
                        self.diagnostics.current.event('operator.start_cancelled','开测前连接或就绪条件已改变',
                            requested_identity=self.requested_identity,blockers=needed,current=self.candidate_identity(candidate))
                        continue
                    latest = replace(self.config)
                    if (self.closing or self.shutdown.is_set() or self.configuring or self.production_pause.is_set()
                            or self.page != 'production' or self.config_revision != revision or latest != config):
                        self.latch.stable = 0
                        continue
                    self.busy = True
                self.scan_in_progress = False
                self.events.put(('busy',True))
                def emit(kind,value):
                    self.events.put((kind,value))
                    if config.simulation and kind=='step' and value['status']=='RUNNING':
                        WindowConsole.cancelled.wait(.55)  # 预览可看到真实状态机的各步骤。
                if not config.simulation:
                    # Files are maintained directly: capture the latest APP at actual start.
                    from .firmware_library import load_target
                    _, start_app = load_target(config)
                    firmware_sha256 = start_app['sha256']
                    self.diagnostics.current.event('firmware.start_snapshot', **start_app)
                runner = StationRunner(config,root=self.storage_root,emit=emit,cancelled=self.station_cancelled,
                                       delivery=self.delivery,log_target=self.diagnostics.current,
                                       firmware_sha256=firmware_sha256)
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
                last_candidate = None
                changed = self.busy or str(exc) != last_message
                if changed:
                    expected = self.station_cancelled() and isinstance(exc,InterruptedError)
                    self.diagnostics.current.event('controller.paused' if expected else 'controller.exception',
                                                   '产测暂停并交接连接' if expected else '工位控制线程异常',
                                                   level='INFO' if expected else 'ERROR', error=None if expected else exc,
                                                   busy=self.busy, config=config)
                if self.production_pause.is_set() and getattr(exc,'__notes__',None):
                    self.handoff_error='；'.join(exc.__notes__)
                with self.lock:
                    current_scan = not self.station_cancelled() and not self.configuring and (
                        revision, mode_generation) == (self.config_revision, self.mode_generation)
                    if current_scan and not self.busy:
                        self.start_requested = False
                        self.latch.observe(None,time.monotonic())
                if current_scan:
                    self.diagnostics.observe(None,config=config)
                    if not self.busy:
                        fault = revision, mode_generation, str(exc), self.latch.wait_removal
                        if fault != last_station_error:
                            last_station_error = fault
                            self.events.put(('motor_probe',dict(motor_probe='station_error',motor_detail=str(exc),
                                present=False,node_id=None,sn='',target_id=config.target_id,
                                connection_id=None,waiting_removal=self.latch.wait_removal,
                                config_revision=revision,mode_generation=mode_generation)))
                if changed:
                    self.events.put(('logged_line',str(exc)))
                if self.busy:
                    self.latch.completed()
                    self.events.put(('failed',dict(message=self.diagnostics.redact(str(exc)),
                        notes=self.diagnostics.redact(getattr(exc,'__notes__',[])),
                        diagnostic=self.diagnostics.redact(error_details(exc)),
                        log_path=str(self.diagnostics.current.path))))
                elif changed:
                    last_message = str(exc)
            finally:
                self.scan_in_progress = False
                if self.busy:
                    self.busy = False
                    self.events.put(('busy',False))

    def refresh_progress(self):
        completed=sum(self.steps.item(name,'values')[1] in ('完成','沿用旧流程') for name,_ in STEPS)
        self.progress['value']=completed
        self.progress_text.set(f'{completed} / {len(STEPS)}')

    @staticmethod
    def candidate_identity(value):
        return (value['config_revision'], value['mode_generation'], value.get('connection_id'),
                value.get('node_id'), value.get('sn'))

    def set_prompt(self, title, hint, color='#24364D'):
        self.banner.set(title)
        self.hint.set(hint)
        self.banner_label.configure(foreground=color)

    def show_idle_prompt(self, candidate=None):
        if self.terminal_prompt:
            self.set_prompt(*self.terminal_prompt)
        elif self.drivers_blocking():
            self.set_prompt('正在准备设备驱动，请稍候','如 Windows 提示管理员授权，请联系现场负责人允许安装。')
        elif self.config_error:
            self.set_prompt('请联系工程人员检查工位配置',self.config_error,'#C13F50')
        elif not self.config.automatic:
            self.set_prompt('设备监视已暂停','请联系工程人员在工位配置中启用设备监视。')
        elif candidate is None:
            self.set_prompt('正在检查工位，请稍候','正在识别设备并核对开测条件。')
        else:
            title,hint=self.candidate_prompt(candidate, next_motor=self.next_motor)
            self.set_prompt(title,hint,'#138365' if candidate.get('ready') else '#24364D')

    @staticmethod
    def candidate_prompt(value, *, next_motor=False):
        blockers=value.get('blockers',())
        probe=value.get('motor_probe')
        if probe=='station_error':
            return '工位检查异常，请联系工程人员','开测已暂停，具体错误见下方日志。'
        if probe=='unavailable' or 'can' in blockers:
            return '请检查 CAN 适配器','确认 USB 已接好。接有多只适配器时，请在工位设备中选择本工位的 CANable。'
        if probe=='multiple':
            return '请只连接一台待测电机','当前有多个节点应答，请核对工装接线。'
        if probe=='identity_error':
            return '请检查电机连接，身份读取未完成','已收到节点应答，请检查电源和 CAN 线，等待读取电机身份。'
        if probe=='error':
            return '请检查电机通信','核对工装电源和 CAN 接线，通信恢复后会自动重新识别。'
        if not value['present']:
            if value.get('connection_id') and not next_motor:
                return '电机反馈中断，请检查接线','如已取下电机，请等待工位确认断开。'
            return ('请接入下一台电机' if next_motor else '请接入待测电机'), '固定好电机，接好电源和 CAN 线，等候自动识别。'
        missing = [READINESS_LABELS.get(k,k) for k in blockers if k not in ('remove','stabilizing')]
        if missing:
            if any(k in blockers for k in ('firmware','profile','target_id','sn_key','mes_url')):
                title='请联系工程人员完善工位配置'
            elif 'dyno' in blockers:
                title='请检查测功机连接'
            elif 'power' in blockers:
                title='请检查工装电源连接'
            elif 'printer' in blockers:
                title='请检查标签打印机'
            else:
                title='请等待工位就绪'
            hint='待就绪：'+'、'.join(missing)+'。'
            if value.get('firmware_issue'):
                hint+='请在工位配置中核对 APP 文件，详情见日志。'
        elif value.get('ready'):
            title,hint='电机已就绪，请点击开始测试','点击一次即可，工位将自动完成本件测试。'
        else:
            title,hint='已检测到电机，请稍候','正在确认来料身份和工位设备。'
        product = value.get('product',{})
        if product.get('differences'):
            keys = product['differences']
            old = ' / '.join(product['observed'][key] for key in keys)
            new = ' / '.join(product['expected'][key] for key in keys)
            advisory = f'请核对来料 SN 中的 {old} 与工位配置 {new}。'
            hint = hint+advisory if missing else advisory
        return title,hint

    def poll(self):
        start = time.monotonic()
        lag = max(0,start-self.poll_due)
        timing = self.ui_timing
        timing['ticks'] += 1
        timing['max_lag_ms'] = max(timing['max_lag_ms'],round(lag*1000,1))
        timing['max_queue'] = max(timing['max_queue'],self.events.qsize())
        try:
            self.process_events()
        except Exception as exc:
            self.callback_exception(type(exc),exc,exc.__traceback__)
        finally:
            elapsed = time.monotonic()-start
            timing['max_work_ms'] = max(timing['max_work_ms'],round(elapsed*1000,1))
            if max(lag,elapsed)>=.25:
                timing['stalls'] += 1
            if start-self.ui_interval_started>=30 or timing['stalls'] and start-self.ui_interval_started>=5:
                self.diagnostics.current.event('ui.performance',page=self.page,busy=self.busy,
                    interval_s=round(start-self.ui_interval_started,2),**timing)
                self.ui_interval_started=start
                self.ui_timing=dict.fromkeys(timing,0)
            if not self.shutdown.is_set():
                delay = 10 if not self.events.empty() else 16 if self.page=='control' else 50
                self.poll_due=time.monotonic()+delay/1000
                self.root.after(delay,self.poll)

    def process_events(self):
        if self.closing:
            return
        statuses=dict(RUNNING='进行中',DONE='完成',FAILED='失败',PENDING='待配置',UNKNOWN='异常已记录',LEGACY='沿用旧流程')
        started=time.monotonic()
        handled=0
        while (not self.events.empty() and handled<GUI_EVENT_LIMIT
               and time.monotonic()-started<GUI_EVENT_BUDGET_S):
            kind,value=self.events.get_nowait()
            handled+=1
            if kind.startswith('control_'):
                self.control_page.event(kind,value)
                continue
            if kind in ('device_health','motor_probe','candidate','removed','serial_inventory') and not self.production_event_current(value):
                continue
            if kind=='driver_setup':
                self.driver_event(value)
            elif kind=='network_status':
                from .network_status import network_messages
                self.network_snapshot=value
                for level,message in network_messages(value):
                    self.write_log('[网络] '+message,level=level,persist=False)
            elif kind=='dependency':
                label={'serial':'串口通信','reportlab':'报告生成','fitz':'报告图片渲染','oss2':'OSS 上传','requests':'HTTP 客户端'}.get(value['module'],value['module'])
                self.write_log('[环境] '+label+'：'+('加载成功' if value['ok'] else '加载失败：'+value['error']),
                    level='INFO' if value['ok'] else 'ERROR',persist=False)
            elif kind=='serial_inventory':
                ports=value['ports']
                description='；'.join(item['device']+' · '+(item.get('description') or '未提供描述') for item in ports) or '未检测到串口'
                if value.get('error'): description='串口枚举失败：'+value['error']
                signature=(tuple((item['device'],item.get('description')) for item in ports),value.get('error'))
                self.status_log('serial.inventory',signature,'[串口枚举] '+description,
                    level='WARNING' if value.get('error') else 'INFO')
            elif kind=='ready':
                self.dependency_failures=list(value)
                self.refresh_environment()
                self.write_log('[环境] '+('依赖加载失败，设备探测未启动。' if value else '全部运行依赖已就绪，启动后台服务和 CANable 枚举；设备监视按工位配置执行。'),level='ERROR' if value else 'INFO')
                if value:
                    self.banner.set('运行环境未就绪')
                    self.hint.set('请联系工程人员处理，详细原因已记录。')
                    self.write_log('缺少依赖：'+', '.join(value))
                else:
                    self.dependencies_ready=True
                    self.configure_driver_manager()
                    self.configure_printer_service()
                    self.configure_delivery()
                    self.show_idle_prompt()
                    self.worker=threading.Thread(target=self.controller,name='factory-station',daemon=False)
                    self.worker.start()
                    if self.adapter_monitor is None:
                        self.adapter_monitor = AdapterMonitor(lambda value:self.events.put(('adapters',value)), self.shutdown)
                        self.adapter_monitor.start()
            elif kind=='firmware_library':
                self.firmware_panel.complete(value)
                self.status_log('firmware.library',(self.firmware_panel.target.get(),self.firmware_panel.note.get()),
                    '[固件] '+self.firmware_panel.target.get()+'；'+self.firmware_panel.note.get())
            elif kind=='adapters':
                self.update_adapters(value)
            elif kind=='device_health':
                name, info = value['name'], value['info']
                if name=='printer' and 'printer_names' in info:
                    self.field_widgets['printer_name'].configure(values=info['printer_names'])
                if name != 'can' or self.adapter_monitor is None:
                    self.device_state(name, info['state'], info['detail'])
            elif kind=='health':
                for name,info in value.items(): self.device_state(name,info['state'],info['detail'])
            elif kind=='report_delivery':
                if value.get('delivery_revision',self.delivery_revision) != self.delivery_revision:
                    continue
                pending,blocked,done=(value.get(k,0) for k in ('pending','blocked','succeeded'))
                text=f'报告上传：待传 {pending} · 需处理 {blocked} · 已完成 {done}'
                if value.get('active'): text+=' · 上传中'
                elif value.get('error') and not pending and not blocked: text='报告上传：等待配置'
                self.upload_status.set(text)
                self.status_log('delivery.queue',(pending,blocked,done,bool(value.get('active')),value.get('error')),
                    '[报告队列] '+text+('；'+value['error'] if value.get('error') else ''),
                    level='WARNING' if blocked or value.get('error') else 'INFO')
                message=value.get('notice') or value.get('error')
                if message and message != self.last_delivery_message:
                    self.last_delivery_message=message
                    self.write_log(message,persist=False)  # Upload worker has the original motor's log target.
            elif kind=='printer_service':
                if value['service_revision']!=self.printer_service_revision: continue
                self.printer_service_state.set(value['detail'])
                self.status_log('printer.service',(value['state'],value['detail']),'[打印服务] '+value['detail'],
                    level='ERROR' if value['state']=='ERROR' else 'INFO')
                self.last_health=0.0
                if value['state']=='ERROR': self.device_state('printer','ERROR',value['detail'])
                elif value['state']=='STARTING': self.device_state('printer','CHECKING',value['detail'])
            elif kind=='printer_check':
                self.printer_check_button.config(state='normal')
                self.printer_test_button.config(state='normal')
                if value['config_revision']!=self.config_revision: continue
                data=value.get('response',{}).get('data',{})
                if 'allPrinters' in data: self.field_widgets['printer_name'].configure(values=data['allPrinters'])
                if value['ok']:
                    if not self.config_error: self.settings_error.set('')
                    self.settings_notice.set('测试标签已提交队列，请核对实际出纸和扫码。' if value.get('test_print') else
                        '打印服务及指定队列已就绪；本次检查未提交打印任务。')
                else:
                    self.settings_notice.set('')
                    self.settings_error.set(value['message'])
            elif kind=='logs_exported':
                ok,detail = value
                self.export_button.config(state='normal')
                self.settings_notice.set(('诊断包已保存至 ' if ok else '诊断包导出失败：')+detail)
            elif kind in ('motor_probe','candidate'):
                probe=value.get('motor_probe') or ('present' if value['present'] else 'absent')
                if probe!='station_error':
                    motor_states={'present':'SIMULATED' if self.config.simulation else 'READY','absent':'OFFLINE',
                                  'multiple':'ERROR','identity_error':'ERROR','error':'ERROR','unavailable':'WAITING'}
                    detail=f'来料 ID {value["node_id"]}' if value['present'] else value.get('motor_detail') or '等待电机反馈'
                    self.device_state('motor',motor_states.get(probe,'CHECKING'),detail)
                if kind=='motor_probe':
                    if self.busy or self.terminal_prompt or value.get('waiting_removal'):
                        continue
                    if not value['present'] or self.ready_identity!=self.candidate_identity(value):
                        self.ready_to_start=False
                        self.ready_identity=None
                        self.stop.config(state='disabled')
                        self.show_idle_prompt(value)
                    continue
                blockers=tuple(value.get('blockers',()))
                condition='已就绪，等待点击开始测试。' if value['ready'] else '等待 '+('、'.join(READINESS_LABELS.get(item,item) for item in blockers) or '设备识别完成')
                self.status_log('readiness',(value['ready'],blockers),'[开测条件] '+condition)
                if value.get('present'):
                    self.status_log('motor.identity',(value.get('connection_id'),value.get('node_id'),value.get('sn')),
                        f'[电机识别] 实际 ID {value.get("node_id")}；SN {value.get("sn")}')
                if 'firmware_issue' in value:
                    issue=value['firmware_issue']
                    self.status_log('firmware.target',(value.get('firmware_file'),value.get('firmware_sha256'),issue),
                        '[固件检查] '+(issue or '目标 APP 已校验：'+str(value.get('firmware_file'))),
                        level='WARNING' if issue else 'INFO')
                if self.terminal_prompt and not self.busy and not value['waiting_removal']:
                    # A configuration/page handoff may discard the one-shot removal event.
                    # The controller's current snapshot still carries the removal latch.
                    self.terminal_prompt=None
                    self.next_motor=True
                self.ready_to_start=bool(value['ready'] and not value['waiting_removal'] and not self.terminal_prompt
                                         and not self.busy and not self.config_error and self.config.automatic)
                self.ready_identity=self.candidate_identity(value) if self.ready_to_start else None
                if not self.busy:
                    self.stop.config(text='开始测试',state='normal' if self.ready_to_start and not self.start_requested else 'disabled')
                    if not value['waiting_removal'] and value['present']:
                        self.current_sn.set(value['sn'])
                        self.sn_phase.set('来料 SN · 已识别')
                        self.connection.set(f'来料 ID {value["node_id"]}  →  产品目标 ID {value["target_id"] or "未定义"}')
                    if not self.start_requested and (self.terminal_prompt or not value['waiting_removal']):
                        self.show_idle_prompt(value)
            elif kind=='removed':
                self.current_sn.set('—')
                self.sn_phase.set('SN · 等待识别')
                self.terminal_prompt=None
                self.next_motor=True
                self.show_idle_prompt(dict(present=False,ready=False))
            elif kind=='busy':
                for widget,mode in self.fields: widget.config(state='disabled' if value else mode)
                for widget in (self.auto_control,self.resume_button,self.resolve_button):
                    widget.config(state='disabled' if value else 'normal')
                self.page_menu.entryconfigure(2,state='disabled' if value else 'normal')
                self.stop.config(text='停止测试' if value else '开始测试',state='normal' if value and not self.stopping else 'disabled')
                if value:
                    self.terminal_prompt=None
                    self.next_motor=False
                    self.ready_to_start=False
                    if not self.stopping:
                        self.set_prompt('测试开始，请保持连接','工位正在处理本件电机。','#2563EB')
                    for index,(name,label) in enumerate(STEPS,1): self.steps.item(name,values=(f'{index:02d}   {label}','等待'),tags=())
                    self.refresh_progress()
                    for name in ('dyno','power'):
                        self.device_state(name,'IN_USE','由当前测试流程管理连接')
                else:
                    self.stopping=False
                    self.last_health=0
            elif kind=='step':
                name,state=value['name'],value['status']
                if self.steps.exists(name):
                    label=dict(STEPS)[name]
                    index=[n for n,_ in STEPS].index(name)+1
                    self.steps.item(name,values=(f'{index:02d}   {label}',statuses[state]),tags=(state,))
                    if state=='RUNNING' and not self.stopping and not self.terminal_prompt:
                        title='请保持连接，等待自动测试'
                        hint=label+'进行中，请保持电源和 CAN 连接。'
                        if name in ('calibrate','kt','tn'):
                            title='电机可能转动，请勿触碰'
                        elif name=='print':
                            title,hint='请等待标签打印','本件完成后，再核对标签并贴标。'
                        elif name in ('report','upload'):
                            title,hint='请稍候，正在保存本件结果',label+'完成后会提示取件。'
                        self.set_prompt(title,hint,'#2563EB')
                    self.refresh_progress()
                self.write_log(dict(STEPS).get(name,'两项性能测试')+'：'+statuses[state],persist=False)
            elif kind=='firmware_progress':
                if not self.stopping and not self.terminal_prompt:
                    self.set_prompt('请保持供电和 CAN 连接',value,'#2563EB')
                self.write_log(value,persist=False)
            elif kind=='sn': self.current_sn.set(value)
            elif kind=='sn_phase': self.sn_phase.set(value)
            elif kind=='metrics':
                labels={'PASS':'通过','FAIL':'未通过','PENDING':'待定','INCOMPLETE':'未完成'}
                for section in value.get('sections',[]):
                    status=section['status']
                    details=[f"{m['label']} {m['shown']} {m['unit']}".rstrip() for m in section['metrics']]
                    if section.get('note'): details.append(section['note'])
                    line=f"[测量] {section['title']} · {labels.get(status,status)}"
                    if details: line+='　'+'，'.join(details)
                    level='ERROR' if status in ('FAIL','INCOMPLETE') else 'WARNING' if status=='PENDING' else 'INFO'
                    self.write_log(line,persist=False,level=level)
            elif kind=='resultdir': self.latest_result=Path(value)
            elif kind=='complete':
                self.terminal_prompt=('本件通过，请取下电机并核对标签',
                    f'核对标签 SN {value["sn"]} 后贴标。','#138365')
                self.set_prompt(*self.terminal_prompt)
                self.current_sn.set(value['sn'])
                self.sn_phase.set('正式 SN · 已写入核对')
            elif kind=='failed':
                self.last_failure=self.diagnostics.redact(dict(value,at=datetime.now().astimezone().isoformat(),motor_sn=self.current_sn.get()))
                self.terminal_prompt=(*self.failure_prompt(value),'#C13F50')
                self.set_prompt(*self.terminal_prompt)
                self.write_log(value['message'],persist=False,level='ERROR')
                diagnostic = value.get('diagnostic')
                if diagnostic:
                    stage=value.get('step_label') or dict(STEPS).get(value.get('step'),'工位流程')
                    self.write_log(f'阶段：{stage}；错误编号：{diagnostic["code"]}',persist=False,level='ERROR')
                    self.write_log('排查方向：'+diagnostic['hint'],persist=False)
                if value.get('log_path'):
                    self.write_log('本次日志：'+value['log_path'],persist=False)
                for note in value.get('notes',[]): self.write_log(note,persist=False,level='ERROR')
            elif kind=='log_failure':
                self.write_log(value,persist=False)
                if not self.terminal_prompt and not self.stopping:
                    self.hint.set('日志保存异常，请联系维护人员检查存储空间和目录权限。')
            elif kind in ('log','logged_line'):
                if value.startswith('@panel '):
                    try: self.write_log(json.loads(value[7:]),persist=kind=='log')
                    except ValueError: self.write_log(value,persist=kind=='log')
                else: self.write_log(value,persist=kind=='log')
        self.advance_page_transition()
        self.advance_settings_save()
        self.advance_driver_install()
        self.select_single_adapter()
        self.control_page.tick()
        self.retiring_deliveries = [item for item in self.retiring_deliveries
            if item.thread is not None and item.thread.is_alive()]
        self.retiring_printer_services = [item for item in self.retiring_printer_services
            if item.thread is not None and item.thread.is_alive()]

    @staticmethod
    def failure_prompt(value):
        diagnostic=value.get('diagnostic') or {}
        notes=' '.join(value.get('notes',[])).lower()
        if any(marker in notes for marker in ('校准停止','失能','停机未确认','切断工装电源','disable','motor shutdown',
                                               'power shutdown','load shutdown','best-effort off')):
            return '请先断开工装电源','停机未确认。确认电机和负载停止后再取下隔离，联系工程人员。'
        code=diagnostic.get('code','')
        step=value.get('step')
        if step=='print' and (value.get('status')=='UNKNOWN' or code in
                              ('PRINT_OUTCOME_UNKNOWN','PRINT_ACK_INVALID','PRINT_SERVICE_OUTCOME_UNKNOWN','PRINT_ACK_CONFLICT')):
            return '打印结果未确认，请隔离本件','勿补打或贴标。请工程人员核对实体标签和打印记录。'
        stage=value.get('step_label') or dict(STEPS).get(step,'本件测试')
        return '请取下并隔离本件电机',f'{stage}未完成。请勿贴合格标签，详情见日志。'

    def request_stop(self):
        self.diagnostics.current.event('operator.stop_requested', '请求结束测试', busy=self.busy)
        self.start_requested = False
        WindowConsole.cancelled.set()
        if self.busy:
            self.stopping=True
            self.stop.config(state='disabled')
            self.set_prompt('正在停止，请等待电机停稳','工位正在执行收尾，请等待取件提示。','#B27813')

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
            self.terminal_prompt=('请取下已归档的异常工件','将本件放入隔离区，等待工位确认断开。','#B27813')
            self.set_prompt(*self.terminal_prompt)
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
        if self.closing:
            return
        with self.lock:
            self.closing = True
            self.shutdown_started = time.monotonic()
            self.start_requested = self.resume_requested = self.ready_to_start = False
            self.pending_page = None
            self.settings_save_pending = False
            self.production_pause.set()
            self.shutdown.set()
        WindowConsole.cancelled.set()
        self.diagnostics.current.event('application.shutdown.requested',busy=self.busy,
            debug_active=self.control_page.active,scan_in_progress=self.scan_in_progress,
            station_worker_alive=bool(self.worker and self.worker.is_alive()))
        self.control_page.leave()
        if getattr(self, 'driver_manager', None) is not None:
            self.driver_manager.stop()
        for delivery in [*self.retiring_deliveries,self.delivery]:
            if delivery is not None:
                delivery.stop()
        for service in [*self.retiring_printer_services,self.printer_service]:
            if service is not None: service.stop()
        self.settings_window.withdraw()
        if getattr(self,'help_view',None) is not None:
            self.help_view.hide()
        self.stop.config(state='disabled')
        for index in range(4): self.page_menu.entryconfigure(index,state='disabled')
        self.advance_shutdown()

    def owned_threads(self):
        session = self.control_page.session
        candidates = [self.worker,session.thread if session is not None else None]
        candidates.append(self.printer_check_thread)
        candidates.append(getattr(self,'export_thread',None))
        candidates.append(getattr(self,'bootstrap_thread',None))
        candidates.append(getattr(getattr(self,'driver_manager',None),'thread',None))
        candidates.append(getattr(getattr(self,'network_monitor',None),'thread',None))
        candidates.append(getattr(getattr(self,'firmware_panel',None),'thread',None))
        health = getattr(self,'device_health',None)
        if health is not None:
            candidates.append(health.printer.thread)
        candidates.extend(item.thread for item in [*self.retiring_deliveries,self.delivery] if item is not None)
        candidates.extend(item.thread for item in [*self.retiring_printer_services,self.printer_service] if item is not None)
        return list({id(thread):thread for thread in candidates if thread is not None}.values())

    def advance_shutdown(self):
        pending = [thread.name for thread in self.owned_threads() if thread.is_alive()]
        if self.busy or self.scan_in_progress or self.control_page.active or pending:
            message = ('正在停止电机并释放设备连接…' if self.busy or self.scan_in_progress or self.control_page.active
                       else '正在结束本机打印服务，完成后自动关闭…' if any(name.startswith('printer-service') for name in pending)
                       else '正在等待设备驱动准备结束，完成后自动关闭…' if 'device-driver-setup' in pending
                       else '正在结束本机网络检查，完成后自动关闭…' if 'network-status' in pending
                       else '正在保存诊断包，完成后自动关闭…' if 'diagnostic-export' in pending
                       else '正在等待当前报告请求结束并保存状态，完成后自动关闭…')
            self.banner.set(message)
            self.control_page.message.set(message)
            self.hint.set('请等待自动关闭，尚未上传的报告会在下次启动后继续补传。')
            self.diagnostics.current.sample('application.shutdown.waiting',interval=5,
                workers=pending,busy=self.busy,scanning=self.scan_in_progress,debug_active=self.control_page.active,
                elapsed_s=round(time.monotonic()-self.shutdown_started,3),handoff_error=self.handoff_error)
            self.root.after(100,self.advance_shutdown)
            return
        session = self.control_page.session
        unconfirmed = self.handoff_error or (session.failure or '调试失能或释放未确认'
            if session is not None and not session.cleanup_confirmed else '')
        needs_power_off = bool(unconfirmed)
        try:
            self.control_page.finish_storage()
        except OSError as exc:
            self.diagnostics.current.event('application.shutdown.storage_failed',level='ERROR',error=exc)
            unconfirmed = unconfirmed or '显示数据文件关闭失败，请检查日志和磁盘状态'
        self.diagnostics.current.event('application.shutdown.complete',level='ERROR' if unconfirmed else 'INFO',cleanup_confirmed=not bool(unconfirmed),
            cleanup_note=unconfirmed,workers_released=True)
        if unconfirmed:
            messagebox.showwarning('退出前状态未确认',self.diagnostics.redact(
                unconfirmed+('\n请先断开工装电源。' if needs_power_off else '\n显示数据可能未完整保存，请核对文件。')+
                '\n错误和收尾记录已保存在日志目录。'),parent=self.root)
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
        # Keep the process-level station lock through cleanup even if Tk's
        # mainloop exits unexpectedly instead of following WM_DELETE_WINDOW.
        window.shutdown.set()
        window.production_pause.set()
        WindowConsole.cancelled.set()
        session = window.control_page.session
        if session is not None:
            session.disconnect()
        for delivery in [*window.retiring_deliveries,window.delivery]:
            if delivery is not None: delivery.stop()
        for service in [*window.retiring_printer_services,window.printer_service]:
            if service is not None: service.stop()
        for thread in window.owned_threads():
            if thread is not threading.current_thread() and thread.is_alive(): thread.join()
        window.diagnostics.close()
        threading.excepthook = previous_hook
        sys.unraisablehook = previous_unraisable


if __name__ == '__main__':
    from .desktop_entry import main as desktop_main
    desktop_main()
