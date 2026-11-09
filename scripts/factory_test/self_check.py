"""Check the bundled UI, persistence and simulated report without device I/O."""
from contextlib import ExitStack
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
import traceback
from unittest.mock import Mock, patch
from .version import VERSION, VERSION_LABEL


def check_debug_monitor():
    """Exercise the actual CAN scheduler against an in-memory GD/ADM peer."""
    import queue
    import threading
    from motor_platform import MotorBus, CanFrame, MitCommand
    from motor_platform.runtime import ControlRuntime
    class Peer:
        def __init__(self):
            self.frames=[]; self.rx=queue.Queue(); self.owners=set()
        def send(self,frame):
            self.owners.add(threading.get_ident())
            self.frames.append(frame)
            assert frame.can_id==0x103 and frame.data==bytes(16)
            self.rx.put(CanFrame(0x193,struct.pack('<fffhh',1.25,-.5,.1,270,280)))
        def recv(self,timeout):
            self.owners.add(threading.get_ident())
            try: return self.rx.get(timeout=timeout)
            except queue.Empty: return None
        def close(self): pass
    peer=Peer()
    bus=MotorBus(peer)
    runtime=ControlRuntime(bus,{3:bus.motor(3)},rate_hz=1000,command_timeout=.025).start()
    try:
        channel=runtime.motor(3)
        channel.set_mit(MitCommand(torque=8,kp=20))
        channel.monitor()
        deadline=time.monotonic()+1
        while True:
            try:
                state=channel.state(max_age=.5)
                break
            except TimeoutError:
                if time.monotonic()>=deadline: raise
                time.sleep(.005)
        time.sleep(.04)
        stats=channel.stats()
        assert state.position==1.25 and state.motor_temperature==27
        assert stats['target_hz']==1000 and stats['tx_count']>0 and stats['rx_count']>0
        assert stats['monitoring'] and stats['periodic_active'] and not stats['control_active']
        assert not stats['enabled'] and not stats['fault']
    finally:
        runtime.close(); bus.close()
    assert peer.frames and len(peer.owners)==1 and bus._io_owner is None
    assert all(frame.can_id==0x103 and frame.data==bytes(16) for frame in peer.frames)


def run(destination):
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = dict(ok=False, version=VERSION, channel='production', frozen=bool(getattr(sys, 'frozen', False)))
    started = time.perf_counter()
    root = None
    try:
        import tkinter as tk
        from tkinter import ttk
        from scripts.factory_test.gui import FactoryWindow, PYTHON_DEPENDENCIES
        from scripts.factory_test.station import STEPS, StationRunner
        from scripts.factory_test.station_config import (StationConfig, load_config, save_config, temporary_sn,
                                                        DEFAULT_MES_BASE_URL, DEFAULT_REPORT_URL)

        dependencies = {name: importlib.util.find_spec(name) is not None
                        for name in (*PYTHON_DEPENDENCIES, 'prompt_toolkit', 'serial.tools.list_ports_windows')}
        if not all(dependencies.values()):
            raise RuntimeError(f'Missing bundled dependencies: {dependencies}')
        with tempfile.TemporaryDirectory(prefix='factory-self-check-', dir=destination.parent) as temp, ExitStack() as stack:
            folder = Path(temp)
            # Even an accidental path into a live transport must fail this check.
            blocked = AssertionError('Device/network I/O is forbidden during self-check')
            io_guards = [stack.enter_context(patch(target, side_effect=blocked)) for target in (
                'motor_platform.Canable.detect', 'motor_platform.Canable.open',
                'motor_platform.transports.canable._Usb', 'serial.Serial',
                'urllib.request.OpenerDirector.open', 'requests.sessions.Session.request', 'socket.socket.connect')]
            check_debug_monitor()
            stack.enter_context(patch.object(FactoryWindow, 'bootstrap_dependencies'))
            stack.enter_context(patch('scripts.factory_test.operator_view.port_options', return_value=['auto', 'COM3']))
            # Force the bundled renderer; a developer's Poppler installation cannot mask a missing dependency.
            stack.enter_context(patch('scripts.factory_test.report._find_pdftoppm', return_value=None))
            config = StationConfig(simulation=True, automatic=False)
            config_path = folder/'station.json'
            save_config(config, config_path)
            assert asdict(load_config(config_path)) == asdict(config)
            assert config.mes_url == DEFAULT_MES_BASE_URL == 'https://www.mirrormetech.com/mes-api'
            assert config.report_upload_url == DEFAULT_REPORT_URL == 'https://www.mirrormetech.com/mes-api/open-api/test-report'
            assert config.base_sn == '0'*12+'0221'+'0'*10
            assert 'base_sn' not in asdict(config)
            from motor_platform import Canable
            assert Canable('').serial_number is None  # Constructing parameters never opens USB.
            root = tk.Tk()
            root.withdraw()
            window = FactoryWindow(root, config_path=config_path, storage_root=folder/'ui', preview_only=True)
            assert window.delivery is None
            root.update_idletasks()
            assert VERSION_LABEL in root.title() and window.version_label.cget('text')==VERSION_LABEL
            assert window.diagnostics.runtime_info()['version']==VERSION
            menu = [window.page_menu.entrycget(i, 'label') for i in range(3)]
            assert menu == ['自动产测', '电机调试', '工位配置']
            assert not hasattr(window, 'sim_control') and not hasattr(window, 'demo_button')
            # A saved preview flag from an older installation cannot turn the
            # only production entry into a simulated run. Bootstrap is guarded above.
            real_root = tk.Toplevel(root)
            real_root.withdraw()
            real_window = FactoryWindow(real_root, config_path=config_path, storage_root=folder/'production-ui')
            assert real_window.config.simulation is False
            real_window.shutdown.set(); real_window.diagnostics.close(); real_root.destroy()
            assert not hasattr(window, 'sn_entry')
            assert 'hardware_version' in window.vars
            assert set(window.hardware_vars) == set('ABCD')
            assert window.vars['hardware_version'].get() == '0221'
            assert all(str(widget.cget('state')) == 'normal' for widget in window.hardware_widgets.values())
            assert all(window.hardware_widgets[part].winfo_class()=='TEntry' for part in ('A','D'))
            assert all(window.hardware_widgets[part].winfo_class()=='TCombobox' for part in ('B','C'))
            assert 'upload_config' not in window.vars
            from types import SimpleNamespace
            control=window.control_page
            assert not hasattr(control,'send_mode') and not hasattr(control,'apply_button')
            control.slider('position','2.06')
            assert control.targets['position'].get()=='2.100'
            control.target_sliders['position'].set(0)
            for name,step in (('torque',.1),('velocity',.1),('position',.1),('kp',.1),('kd',.01)):
                assert control.target_sliders[name].bind('<MouseWheel>')
                control.wheel(name,SimpleNamespace(delta=120))
                assert abs(float(control.targets[name].get())-step)<1e-6
                control.wheel(name,SimpleNamespace(delta=-120))
                assert float(control.targets[name].get())==0
            control.wheel('torque',SimpleNamespace(delta=120000))
            assert float(control.targets['torque'].get())==30
            control.target_sliders['torque'].set(0)
            from .manual_control import ManualSession
            preset=ManualSession(StationConfig(simulation=True),3,lambda *_:None)
            control.session=preset
            control.targets['position'].set('1.25')
            control.targets['torque'].set('.4')
            deadline=time.monotonic()+.5
            while preset.pending_target.position!=1.25 and time.monotonic()<deadline:
                root.update();time.sleep(.002)
            assert preset.pending_target.position==1.25 and preset.pending_target.torque==.4
            assert not preset.enabled and preset.target.torque==0 and preset.commands.empty()
            assert abs(float(control.target_sliders['position'].get())-1.25)<1e-6
            control.target_sliders['position'].set(0)
            control.target_sliders['torque'].set(0)
            control.apply();control.session=None
            ids=control.canvas.find_all()
            from .waveform import FRAME_INTERVAL
            control.samples.extend(dict(at=at,position=.1,velocity=.2,torque=.3,
                target=dict(position=0.,velocity=0.,torque=0.)) for at in (100.,100.02,100.04))
            control.draw(); control.draw()
            assert control.canvas.find_all()==ids
            assert FRAME_INTERVAL<=1/60
            for row in control.plot.rows:
                assert [control.canvas.itemcget(i,'text') for i in row['xlabels']]==['0.0','1.0','2.0','3.0','4.0','5.0']
                assert control.canvas.itemcget(row['time'],'text')=='时间 / s'
                assert not control.canvas.itemcget(row['target'],'dash')
                assert float(control.canvas.itemcget(row['target'],'width'))<=1.5
                assert float(control.canvas.itemcget(row['feedback'],'width'))<=1
            control.samples.clear();control.plot.reset()
            bench=Mock()
            bench.motor.return_value.read_serial_bytes.return_value=b'C2KNEE0STDXC02232609260065\0\0'
            bench.start.return_value.motor.return_value.stats.return_value={}
            manual=ManualSession(StationConfig(adapter_sn='self-check-no-usb'),3,lambda *_:None)
            with patch('scripts.factory_test.manual_control.Bench',return_value=bench), \
                 patch('scripts.factory_test.manual_control.Maintenance.read',return_value=245):
                manual._open_hardware()
            assert manual.firmware=='245' and manual.profile_known and manual.config.torque==30
            manual.connected=True;manual.profile_known=False
            manual.command('enable')
            assert manual.commands.get_nowait()[0]=='enable'
            control.event('control_state',dict(connected=True,enabled=False,message='self-check',
                profile_known=False,firmware=manual.firmware,torque_limit=30))
            assert str(control.enable_button.cget('state'))=='normal'
            assert control.status_values['firmware'].get()=='245'
            assert all(str(window.field_widgets[name].cget('state')) == 'normal' for name in (
                'project','category','subcategory','factory','production_date'))
            assert str(window.power_port_combo.cget('state')) == 'readonly'
            assert window.devices.winfo_class() == 'TCombobox'
            assert str(window.devices.cget('state')) == 'readonly'
            adapters = [dict(serial_number='self-check-can-a', path='test-path-a'),
                        dict(serial_number='self-check-can-b', path='test-path-b')]
            window.update_adapters(dict(devices=adapters,error=None))
            assert tuple(window.devices.cget('values')) == ('self-check-can-a','self-check-can-b')
            assert window.vars['adapter_sn'].get() == '' and window.config.adapter_sn == ''
            window.vars['adapter_sn'].set('self-check-can-b')
            assert window.apply_config() is True
            window.update_adapters(dict(devices=adapters,error=None))
            assert window.vars['adapter_sn'].get() == 'self-check-can-b'
            saved_project = window.vars['project'].get()
            window.vars['project'].set('X')  # Hotplug cannot commit unrelated partial edits.
            window.update_adapters(dict(devices=adapters[:1],error=None))
            assert window.vars['adapter_sn'].get() == 'self-check-can-a'
            assert load_config(config_path).adapter_sn == 'self-check-can-a'
            assert load_config(config_path).project == saved_project
            window.vars['project'].set(saved_project)
            assert tuple(window.devices.cget('values')) == ('self-check-can-a',)
            popup_values = window.devices.tk.globalgetvar('ttk::combobox::Values('+str(window.devices)+')')
            assert tuple(window.devices.tk.splitlist(popup_values)) == ('self-check-can-a',)
            window.update_adapters(dict(devices=adapters,error=None))
            assert window.vars['adapter_sn'].get() == 'self-check-can-a'
            window.update_adapters(dict(devices=[],error=None))
            assert not window.devices.cget('values') and window.vars['adapter_sn'].get() == ''
            window.busy=True
            window.update_adapters(dict(devices=adapters[1:],error=None))
            assert window.config.adapter_sn == ''  # A running task must keep its own connection.
            window.busy=False
            window.select_single_adapter()
            assert window.config.adapter_sn == 'self-check-can-b'
            window.update_adapters(dict(devices=[],error=None))
            window.scan_in_progress=True
            window.switch_page('control')
            assert window.page=='control' and window.pending_page=='control'
            assert window.production_pause.is_set() and not window.control_page.want_connection
            window.scan_in_progress=False
            window.advance_page_transition()
            assert window.pending_page is None and window.control_page.want_connection
            window.dependencies_ready=True
            with patch.object(window.control_page,'connect') as auto_connect:
                window.control_page.tick()
                auto_connect.assert_called_once_with(automatic=True)
            window.dependencies_ready=False
            from types import SimpleNamespace
            debug = SimpleNamespace(active=True,cleanup_confirmed=True,failure=None,disconnect=Mock())
            window.control_page.session=debug
            with patch.object(window.settings_window,'deiconify') as show_dialog:
                window.show_settings()
                show_dialog.assert_called_once()
            window.finish_settings()
            assert window.settings_save_pending
            debug.disconnect.assert_called_once()
            debug.active=False
            window.advance_settings_save()
            assert not window.settings_save_pending and window.control_page.want_connection
            debug.active=True; debug.disconnect.reset_mock()
            window.control_page.session=debug
            window.switch_page('production')
            assert window.pending_page=='production' and window.production_pause.is_set()
            debug.disconnect.assert_called_once()
            debug.active=False
            window.advance_page_transition()
            assert window.pending_page is None and not window.production_pause.is_set()
            assert not window.control_page.want_connection
            window.control_page.session=None
            from .gui import GUI_EVENT_LIMIT
            for number in range(GUI_EVENT_LIMIT+10):
                window.events.put(('control_sample',dict(number=number)))
            with patch.object(window.control_page,'event') as sample_handler:
                window.process_events()
                assert 0 < sample_handler.call_count <= GUI_EVENT_LIMIT and not window.events.empty()
            while not window.events.empty(): window.events.get_nowait()
            from .service_credentials import CredentialStore
            service_names=('mes_url','mes_sn_path','printer_url','printer_status_path','printer_print_path',
                           'report_upload_url','sn_api_key_header','report_api_key_header','printer_copies',
                           'oss_endpoint','oss_bucket','oss_region','oss_prefix')
            assert all(name in window.vars for name in service_names)
            assert 'calibration_timeout' not in window.vars
            assert not any('retry' in name or 'timeout' in name for name in window.vars)
            keys=dict(sn_api_key='self-check-station-sn-key',mes_api_key='self-check-station-report-key',
                      access_key_id='self-check-station-access-id',access_key_secret='self-check-station-access-secret')
            assert set(window.secret_widgets)==set(keys)
            assert all(widget.cget('show')=='*' for widget in window.secret_widgets.values())
            initial={name:getattr(window.config,name) for name in service_names}
            window.vars['mes_url'].set('https://sn.example.test/base')
            window.vars['mes_sn_path'].set('/fixture/sn/create')
            window.vars['report_upload_url'].set('https://report.example.test/custom/reports')
            window.vars['oss_endpoint'].set('https://storage.example.test')
            window.vars['oss_bucket'].set('self-check-bucket')
            window.vars['oss_region'].set('self-check-region')
            window.vars['oss_prefix'].set('self-check/reports')
            window.vars['sn_api_key_header'].set('X-Station-Key')
            window.vars['report_api_key_header'].set('X-Report-Key')
            window.vars['printer_copies'].set('2')
            for name,value in keys.items(): window.secret_vars[name].set(value)
            assert window.apply_config() is True
            assert window.config.mes_url==initial['mes_url']  # Edits are staged until an explicit save.
            assert not any(CredentialStore(config_path).load().values())
            assert window.apply_config(commit_services=True) is True
            assert window.config.mes_sn_path=='/fixture/sn/create'
            assert window.config.oss_bucket=='self-check-bucket'
            assert window.config.oss_prefix=='self-check/reports' and window.config.printer_copies==2
            assert CredentialStore(config_path).load()==keys
            raw=config_path.read_text(encoding='utf-8')
            assert all(value not in raw for value in keys.values())
            replica_root=tk.Toplevel(root)
            replica_root.withdraw()
            replica=FactoryWindow(replica_root,config_path=config_path,storage_root=folder/'reload',preview_only=True)
            assert all(replica.secret_vars[name].get()==value for name,value in keys.items())
            assert replica.vars['oss_bucket'].get()=='self-check-bucket'
            replica.shutdown.set(); replica.diagnostics.close(); replica_root.destroy()
            for name,value in initial.items(): window.vars[name].set(value)
            for value in window.secret_vars.values(): value.set('')
            assert window.apply_config(commit_services=True) is True
            window.auto.set(True)
            window.hardware_vars['B'].set('1')
            window.hardware_vars['C'].set('3')
            window.hardware_vars['D'].set('2')
            window.hardware_changed('D')
            assert window.config.hardware_version == '0132' and window.config.torque == 50
            assert window.hardware_vars['B'].get() == '1'
            assert load_config(config_path).hardware_version == '0132'
            previous = asdict(window.config)
            window.hardware_vars['B'].set('12')
            assert window.apply_config() is False
            assert window.config_error and window.settings_error.get()
            assert asdict(window.config) == previous and asdict(load_config(config_path)) == previous
            assert window.auto.get() is True
            window.hardware_vars['B'].set('1')
            assert window.apply_config() is True and not window.config_error
            window.vars['project'].set('unknown')
            assert window.selection_changed('project') is False  # Editable input must never cause a dictionary KeyError.
            window.vars['project'].set('x2')
            window.vars['category'].set('abcd')
            window.vars['subcategory'].set('xy12')
            window.vars['factory'].set('z9')
            assert window.selection_changed('project') is True
            assert (window.config.project,window.config.category,window.config.subcategory,window.config.factory) == (
                'X2','ABCD','XY12','Z9')
            window.hardware_vars['A'].set('1')
            window.hardware_vars['D'].set('3')
            assert window.apply_config() is True and window.config.hardware_version == '1133'
            assert window.config.test_profile_available and window.config.torque==50  # A/D are revisions, not a whitelist.
            window.vars['project'].set('c2')
            window.vars['category'].set(config.category)
            window.vars['subcategory'].set(config.subcategory)
            assert window.selection_changed('project') is True
            window.vars['factory'].set('js')
            assert window.selection_changed('factory') is True and window.config.factory == 'JS'
            window.vars['production_date'].set('260230')
            assert window.apply_config() is False
            window.vars['production_date'].set('auto')
            assert window.apply_config() is True and window.config.production_date == ''
            for part,code in zip('ABCD','0221'):
                from .station_config import HARDWARE_FIELDS
                window.hardware_vars[part].set(code if part in ('A','D') else code+' · '+HARDWARE_FIELDS[part][code])
            window.auto.set(config.automatic)
            window.vars['factory'].set(config.factory)
            window.hardware_changed('B')
            assert window.config.hardware_version == '0221'
            for part,widget in window.hardware_widgets.items():
                before=widget.get()
                widget.insert('end','9')
                assert widget.get() == before
            from .operator_view import SN_INPUT_LENGTHS
            for name,maximum in SN_INPUT_LENGTHS.items():
                widget=window.field_widgets[name]
                before=widget.get()
                widget.insert('end','X'*(maximum+1))
                assert widget.get() == before
            hardware_input=window.hardware_widgets['B']
            hardware_input.delete(0,'end')
            hardware_input.insert(0,'22')
            assert hardware_input.get() == ''
            hardware_input.insert(0,'2')
            assert window.hardware_changed('B') is True
            style = ttk.Style(root)
            for name in ('TFrame', 'TLabel', 'TLabelframe', 'TLabelframe.Label', 'TNotebook', 'TNotebook.Tab'):
                for state in ((), ('active',), ('disabled',), ('selected',)):
                    assert style.lookup(name, 'background', state).lower() == '#ffffff', (name, state)
            for name in ('TEntry', 'TCombobox'):
                for state in ((), ('readonly',), ('readonly', 'focus'), ('disabled',)):
                    assert style.lookup(name, 'fieldbackground', state).lower() == '#ffffff', (name, state)
            events = []
            window.diagnostics.observe(True,identity=dict(node_id=1,sn='DEMO-UNPROGRAMMED'),config=config)
            connection_log = window.diagnostics.current.path
            record = StationRunner(config, root=folder/'flow', emit=lambda *event: events.append(event),
                                   log_target=window.diagnostics.current).begin(
                'DEMO-UNPROGRAMMED', detected_id=1)
            assert record['status'] == 'COMPLETE', record
            from .integrations import PrinterClient, PrintUnknown
            from .diagnostics import error_details, log_scope
            malformed = Mock(return_value=dict(data=None))
            with log_scope(window.diagnostics.current.bind(step='print',self_check=True)):
                try:
                    PrinterClient(config,malformed).print_sn(record['module_sn'])
                except PrintUnknown as exc:
                    assert error_details(exc)['code']=='PRINT_ACK_INVALID'
                else:
                    raise AssertionError('Malformed print ACK must never be accepted')
            assert malformed.call_count==1
            assert record['base_sn'] == temporary_sn(config.hardware_version)
            assert all(state == 'DONE' for state in record['steps'].values())
            completed = {value['name'] for kind, value in events if kind == 'step' and value['status'] == 'DONE'}
            assert all(name in completed for name, _ in STEPS), completed
            png = Path(record['directory'])/'report.png'
            header = png.read_bytes()[:24]
            assert header[:8] == b'\x89PNG\r\n\x1a\n'
            width, height = struct.unpack('>II', header[16:24])
            assert width > 500 and height > 500
            # Exercise the restored OSS/MES path with in-memory remote stand-ins.
            from .delivery import ReportDelivery
            from .upload import Credentials, OssStore, ReportUploader, save_json
            endpoint = 'https://self-check.example.test/open-api/test-report'
            store, mes = Mock(), Mock()
            store.put.return_value = dict(etag='offline', request_id='offline')
            mes.register.side_effect = lambda body:dict(reportId=1, reportKey=body['reportKey'], duplicated=False)
            summary = record['summary']
            summary['demo'] = False  # Synthetic fixture only, in an isolated temporary directory with I/O blocked.
            directory = Path(record['directory'])
            live_directory = folder/'delivery/data'/directory.name
            import shutil
            shutil.copytree(directory, live_directory)
            save_json(live_directory/'summary.json', summary)
            uploader = ReportUploader(store=store, mes=mes, endpoint=endpoint, max_attempts=1)
            delivery = ReportDelivery(folder/'delivery', StationConfig(report_upload_url=endpoint,
                upload_config=str(folder/'missing.json')), uploader=uploader,diagnostics=window.diagnostics)
            delivery.enqueue(live_directory)
            store.put.assert_not_called()
            delivery.step()
            assert store.put.call_count == 3 and mes.register.call_count == 1
            receipt_state = json.loads((live_directory/'upload-state.json').read_text(encoding='utf-8'))
            assert receipt_state['status'] == 'SUCCEEDED'
            assert receipt_state['receipt']['sn'] == record['module_sn']
            OssStore(Credentials('offline-id', 'offline-secret', 'offline-key'))  # SDK constructor makes no request.
            assert window.open_error_log_button.cget('text') == '打开日志目录'
            assert window.open_report_button.cget('text') == '打开报告目录'
            window.latest_result = Path(record['directory'])
            with patch('scripts.factory_test.gui.os.startfile') as opener:
                window.open_error_log_button.invoke()
                window.open_report_button.invoke()
                assert [call.args[0] for call in opener.call_args_list] == [folder/'ui/logs/preview', folder/'ui/demo-data']
            window.diagnostics.finish_connection('self_check_disconnect')
            saved_log = connection_log.read_text(encoding='utf-8')
            assert all(marker in saved_log for marker in ('connection.open','flow.start','flow.end',
                'delivery.succeeded','connection.close',record['request_id'],'PRINT_ACK_INVALID','schema_version'))
            assert record['connection_log'] == connection_log.relative_to(window.diagnostics.root).as_posix()
            for guard in io_guards:
                guard.assert_not_called()
            window.shutdown.set()
            window.diagnostics.close()
            root.destroy()
            root = None
            result.update(ok=True, dependencies=dependencies, menu=menu, white_theme=True,
                          production_defaults=True, single_production_ui=True, adapter_auto_selection=True,
                          adapter_dropdown_live=True, adapter_online_choices_only=True,
                          adapter_manual_selection=True, adapter_unplug_clears_selection=True,
                          adapter_single_auto_connect=True, adapter_busy_change_deferred=True,
                          mode_handoff_async=True, debug_auto_connect=True, gui_event_processing_bounded=True,
                          debug_monitor_on_connect=True,debug_settings_accessible=True,version_display=True,
                          waveform_retained=True,waveform_time_axis_stable=True,waveform_target_solid=True,
                          slider_wheel=True,hardware_revision_entries=True,revision_whitelist_removed=True,
                          debug_enable_without_sn_whitelist=True,firmware_decimal=True,legacy_config_field_removed=True,
                          live_targets_default=True,send_controls_removed=True,position_wheel_step_0_1=True,
                          waveform_thin=True,waveform_60fps=True,
                          motor_sn_widget_removed=True, config_roundtrip=True,
                          shared_hardware_dropdowns=True,
                          editable_sn_fields=True, invalid_edit_preserves_config=True,
                          sn_dropdowns_are_suggestions=True,
                          sn_input_length_limited=True,
                          temporary_sn_zero_filled=True,
                          file_action_buttons=True, usb_open_attempts=0,
                          directory_actions=True, connection_scoped_logs=True, log_retention_days=30,
                          structured_error_diagnostics=True,printer_invalid_ack_unknown=True,
                          service_addresses_editable=True,inline_credentials_encrypted=True,
                          credentials_reloaded_in_ui=True,service_changes_staged=True,
                          report_delivery=True, oss_files=3, mes_receipt_checked=True,
                          simulated_steps=len(STEPS), report_png=dict(width=width, height=height),
                          hardware_or_network_used=False)
    except Exception:
        result['error'] = traceback.format_exc()
    finally:
        if root is not None:
            root.destroy()
        result['elapsed_s'] = round(time.perf_counter()-started, 3)
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result
