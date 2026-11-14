import queue
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from scripts.factory_test.gui import FactoryWindow, WindowConsole, GUI_EVENT_LIMIT
from scripts.factory_test.manual_control import ControlPage, ManualSession
from scripts.factory_test.station import PresenceLatch
from scripts.factory_test.station_config import StationConfig
from motor_platform.runtime import RuntimeShutdownError
from motor_platform import MitCommand


def wait_for(predicate):
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        if predicate(): return
        time.sleep(.005)
    raise AssertionError('Background test did not complete')


def window():
    host=FactoryWindow.__new__(FactoryWindow)
    host.config=StationConfig(adapter_sn='chosen')
    host.status_messages={}
    host.write_log=Mock()
    host.network_snapshot=None
    host.network_monitor=None
    host.bootstrap_thread=None
    host.lock=threading.Lock()
    host.shutdown=threading.Event()
    host.production_pause=threading.Event()
    host.page='production'
    host.pending_page=None
    host.settings_save_pending=False
    host.handoff_error=''
    host.mode_generation=0
    host.config_revision=0
    host.configuring=host.closing=False
    host.delivery_revision=0
    host.retiring_deliveries=[]
    host.printer_service=None
    host.retiring_printer_services=[]
    host.printer_check_thread=None
    host.export_thread=None
    host.firmware_panel=Mock(thread=None)
    host.device_display={}
    host.poll_due=host.ui_interval_started=time.monotonic()
    host.ui_timing=dict(ticks=0,max_lag_ms=0,max_work_ms=0,max_queue=0,stalls=0)
    host.ready_identity=host.requested_identity=None
    host.busy=host.scan_in_progress=False
    host.start_requested=host.resume_requested=host.ready_to_start=True
    host.control_page=Mock(active=False,session=None)
    host.production_page=Mock()
    host.nav_buttons={}
    host.banner=Mock(); host.hint=Mock(); host.stop=Mock()
    host.diagnostics=Mock()
    host.diagnostics.redact.side_effect=lambda value:value
    host.latch=PresenceLatch()
    host.events=queue.Queue()
    host.dependencies_ready=True
    host.last_health=0
    host.health_states={}
    host.adapter_error=None
    host.adapter_devices=[]
    host.device_health=Mock()
    host.device_health.printer.thread=None
    host.device_health.check.return_value={}
    return host


class SelectionTests(unittest.TestCase):
    def test_single_adapter_is_selected_only_while_idle(self):
        host=window()
        host.config=StationConfig()
        host.adapter_devices=[dict(serial_number='only',path='path')]
        host.adapter_error=None
        host.save_adapter_selection=Mock()
        host.select_single_adapter()
        host.save_adapter_selection.assert_called_once_with('only',reason='only_adapter')
        host.save_adapter_selection.reset_mock(); host.busy=True
        host.select_single_adapter(); host.save_adapter_selection.assert_not_called()
        host.busy=False
        host.control_page.active=True
        host.select_single_adapter(); host.save_adapter_selection.assert_not_called()

    def test_multiple_adapters_never_choose_or_replace_a_selection(self):
        host=window()
        host.adapter_devices=[dict(serial_number=sn,path=sn) for sn in ('one','two')]
        host.adapter_error=None
        host.save_adapter_selection=Mock()
        host.select_single_adapter()
        host.save_adapter_selection.assert_not_called()
        self.assertEqual(host.config.adapter_sn,'chosen')


class ModeTests(unittest.TestCase):
    def tearDown(self):
        WindowConsole.cancelled.clear()

    def test_enter_debug_waits_for_production_scan_release_without_blocking_ui(self):
        host=window(); host.scan_in_progress=True
        host.switch_page('control')
        self.assertEqual(host.page,'control')
        self.assertEqual(host.pending_page,'control')
        self.assertTrue(host.station_cancelled())
        host.control_page.enter.assert_not_called()
        host.scan_in_progress=False
        host.advance_page_transition()
        host.control_page.enter.assert_called_once()
        self.assertIsNone(host.pending_page)
        self.assertTrue(host.production_pause.is_set())

    def test_switching_an_active_test_requests_stop_and_waits_for_cleanup(self):
        host=window(); host.busy=True
        host.switch_page('control')
        host.control_page.enter.assert_not_called()
        self.assertFalse(host.start_requested)
        self.assertTrue(WindowConsole.cancelled.is_set())
        host.busy=False
        host.advance_page_transition()
        host.control_page.enter.assert_called_once()

    def test_return_to_production_waits_for_debug_thread_to_release_can(self):
        host=window(); host.page='control'
        host.control_page.active=True
        host.control_page.session=NS(cleanup_confirmed=True,failure=None)
        host.switch_page('production')
        host.control_page.leave.assert_called_once()
        self.assertTrue(host.production_pause.is_set())
        self.assertEqual(host.pending_page,'production')
        host.control_page.active=False
        host.advance_page_transition()
        self.assertFalse(host.production_pause.is_set())
        self.assertFalse(WindowConsole.cancelled.is_set())
        self.assertFalse(host.start_requested)
        self.assertEqual(host.last_health,0)

    def test_rapid_page_changes_use_only_the_latest_target(self):
        host=window(); host.scan_in_progress=True
        host.switch_page('control'); host.switch_page('production')
        host.scan_in_progress=False
        host.advance_page_transition()
        host.control_page.enter.assert_not_called()
        self.assertEqual(host.page,'production')
        self.assertFalse(host.production_pause.is_set())

    def test_unconfirmed_cleanup_blocks_the_next_mode(self):
        host=window()
        host.control_page.session=NS(cleanup_confirmed=False,failure='disable ACK missing')
        host.switch_page('control')
        self.assertTrue(host.handoff_error)
        self.assertTrue(host.production_pause.is_set())
        host.control_page.enter.assert_not_called()

    def test_pausing_production_does_not_report_a_physical_disconnect(self):
        host=window()
        host.shutdown=Mock()
        host.shutdown.wait.side_effect=[False,True]
        host.shutdown.is_set.return_value=False
        def pause(*args,**kwargs):
            host.production_pause.set()
            return {}
        host.device_health.check.side_effect=pause
        with patch('scripts.factory_test.gui.Maintenance') as maintenance:
            maintenance.return_value.discover.return_value=dict(node_id=1,sn='example')
            host.controller()
        host.diagnostics.observe.assert_not_called()
        names=[call.args[0] for call in host.diagnostics.current.event.call_args_list]
        self.assertIn('controller.paused',names)


class GuiSchedulingTests(unittest.TestCase):
    def test_debug_ui_poll_does_not_limit_waveforms_to_twenty_frames(self):
        host=window();host.page='control';host.root=Mock();host.process_events=Mock()
        host.poll()
        host.root.after.assert_called_once_with(16,host.poll)

    def test_terminal_console_retains_the_native_test_stop_contract(self):
        console=WindowConsole()
        try:
            WindowConsole.cancelled.clear()
            self.assertIsNone(console.poll())
            WindowConsole.cancelled.set()
            self.assertEqual(console.poll(),'quit')
        finally:
            WindowConsole.cancelled.clear()

    def test_sample_backlog_yields_before_draining_the_whole_queue(self):
        host=window(); host.root=Mock()
        host.advance_page_transition=Mock(); host.select_single_adapter=Mock()
        for i in range(GUI_EVENT_LIMIT+10): host.events.put(('control_sample',dict(number=i)))
        host.poll()
        self.assertGreater(host.events.qsize(),0)
        self.assertLessEqual(host.control_page.event.call_count,GUI_EVENT_LIMIT)
        host.root.after.assert_called_once_with(10,host.poll)

    def test_callback_error_does_not_stop_future_gui_polls(self):
        host=window(); host.root=Mock(); host.callback_exception=Mock()
        host.process_events=Mock(side_effect=RuntimeError('bad display sample'))
        host.poll()
        host.callback_exception.assert_called_once()
        host.root.after.assert_called_once_with(50,host.poll)

    def test_old_connection_events_cannot_overwrite_a_new_debug_session(self):
        page=ControlPage.__new__(ControlPage)
        page.session_generation=2; page.message=Mock()
        page.event('control_state',dict(session_generation=1))
        page.message.set.assert_not_called()

    def test_debug_connects_automatically_only_after_handoff_is_complete(self):
        host=window(); host.page='control'
        page=ControlPage.__new__(ControlPage)
        page.host=host; page.session=None; page.want_connection=True
        page.next_connect=0; page.connect=Mock(); page.message=Mock()
        host.pending_page='control'
        page.tick(); page.connect.assert_not_called()
        host.pending_page=None
        page.tick(); page.connect.assert_called_once_with(automatic=True)

    def test_adapter_unplug_stops_the_old_session_and_preserves_autoconnect_intent(self):
        host=window(); host.page='control'
        page=ControlPage.__new__(ControlPage)
        page.host=host; page.want_connection=True; page.message=Mock()
        page.session=NS(active=True,config=StationConfig(adapter_sn='gone'),disconnect=Mock())
        page.observe_adapters(['replacement'])
        page.session.disconnect.assert_called_once()
        self.assertTrue(page.want_connection)


class DebugSettingsTests(unittest.TestCase):
    def make_host(self):
        host=window(); host.page='control'; host.control_page.active=True
        host.control_page.session=NS(cleanup_confirmed=True,failure=None)
        host.root=Mock(); host.settings_window=Mock(); host.settings_error=Mock()
        host.show_service_keys=Mock(); host.secret_widgets={}
        host.refresh_environment=Mock()
        host.apply_config=Mock(return_value=True)
        return host

    def test_settings_can_open_while_debug_motor_is_connected(self):
        host=self.make_host()
        host.show_settings()
        host.settings_window.deiconify.assert_called_once()
        host.settings_window.lift.assert_called_once()
        host.control_page.leave.assert_not_called()

    def test_save_waits_for_cleanup_then_applies_and_reconnects_without_enable(self):
        host=self.make_host()
        host.finish_settings()
        host.control_page.leave.assert_called_once()
        host.apply_config.assert_not_called()
        host.settings_window.withdraw.assert_not_called()
        self.assertTrue(host.settings_save_pending)
        host.control_page.active=False
        host.advance_settings_save()
        host.apply_config.assert_called_once_with(commit_services=True)
        host.settings_window.withdraw.assert_called_once()
        host.control_page.enter.assert_called_once()
        self.assertFalse(host.settings_save_pending)

    def test_unconfirmed_cleanup_cannot_save_or_reconnect(self):
        host=self.make_host()
        host.finish_settings()
        host.control_page.session.cleanup_confirmed=False
        host.control_page.session.failure='disable ACK missing'
        host.control_page.active=False
        host.advance_settings_save()
        host.apply_config.assert_not_called()
        host.control_page.enter.assert_not_called()
        host.settings_window.withdraw.assert_not_called()
        self.assertEqual(host.handoff_error,'disable ACK missing')

    def test_settings_save_prevents_automatic_connection_until_old_owner_exits(self):
        host=window(); host.page='control'; host.settings_save_pending=True
        page=ControlPage.__new__(ControlPage)
        page.host=host; page.session=None; page.want_connection=True
        page.next_connect=0; page.connect=Mock(); page.message=Mock()
        page.tick()
        page.connect.assert_not_called()


class DebugMonitoringTests(unittest.TestCase):
    def make_session(self):
        session=ManualSession(StationConfig(adapter_sn='test-only'),1,lambda *args:None)
        session.profile_known=True
        bench,channel=Mock(),Mock()
        bench.start.return_value.motor.return_value=channel
        bench.motor.return_value.read_serial_bytes.return_value=b'C2KNEE0STDXC02212610280001\0\0'
        channel.stats.return_value=dict(fault=None)
        channel.state.side_effect=lambda **kw:NS(received_at=time.perf_counter(),error_code=0,
            position=0,velocity=0,torque=0,motor_temperature=25,board_temperature=25)
        return session,bench,channel

    def test_connection_starts_monitoring_before_enable_and_disable_keeps_feedback(self):
        session,bench,channel=self.make_session()
        session.set_target(MitCommand(torque=2))
        disabled=threading.Event()
        samples=[]
        def emit(kind,value):
            if kind=='control_state' and value['message'].startswith('电机已失能'): disabled.set()
            if kind=='control_sample': samples.append(value)
        session.emit=emit
        with patch('scripts.factory_test.manual_control.Canable'), \
             patch('scripts.factory_test.manual_control.Bench',return_value=bench), \
             patch('scripts.factory_test.manual_control.Maintenance.read',return_value=0x0201):
            session.start()
            try:
                wait_for(lambda:session.connected)
                bench.start.assert_called_once_with(rate_hz=1000,command_timeout=.25)
                channel.monitor.assert_called_once()
                channel.enable.assert_not_called()
                channel.set_mit.assert_not_called()
                wait_for(lambda:len(samples)>0)
                self.assertFalse(samples[-1]['enabled'])
                self.assertEqual(samples[-1]['sent_target']['torque'],0)
                self.assertEqual(samples[-1]['target']['torque'],2)
                session.command('enable')
                wait_for(lambda:session.enabled)
                wait_for(lambda:channel.set_mit.called)
                self.assertEqual(channel.set_mit.call_args.args[0],MitCommand(torque=2))
                channel.enable.assert_called_once()
                session.command('disable')
                wait_for(disabled.is_set)
                n=len(samples)
                wait_for(lambda:len(samples)>n)
                self.assertFalse(samples[-1]['enabled'])
                bench.close.assert_not_called()
            finally:
                session.disconnect(); session.thread.join(2)
        self.assertFalse(session.active)
        bench.close.assert_called_once()
        bench.start.assert_called_once()

    def test_idle_motor_loss_allows_readonly_automatic_reconnection(self):
        session,bench,channel=self.make_session()
        channel.state.side_effect=TimeoutError('motor feedback stale')
        bench.close.side_effect=RuntimeShutdownError('motor feedback stale',cleanup_confirmed=True)
        with patch.object(session,'_open_hardware',return_value=(bench,channel)):
            session.start()
            try:
                wait_for(lambda:not session.active)
            finally:
                session.disconnect(); session.thread.join(2)
        self.assertTrue(session.reconnect_allowed)
        self.assertTrue(session.cleanup_confirmed)
        channel.enable.assert_not_called()
        bench.start.assert_not_called()
        bench.close.assert_called_once()

    def test_failed_state_changing_command_is_not_automatically_replayed(self):
        session,bench,channel=self.make_session()
        channel.enable.side_effect=TimeoutError('enable outcome unknown')
        with patch.object(session,'_open_hardware',return_value=(bench,channel)):
            session.start()
            try:
                wait_for(lambda:session.connected)
                session.command('enable')
                wait_for(lambda:not session.active)
            finally:
                session.disconnect(); session.thread.join(2)
        self.assertFalse(session.reconnect_allowed)
        self.assertIn('enable outcome unknown',session.failure)
        channel.enable.assert_called_once()
        bench.close.assert_called_once()

    def test_idle_driver_fault_stays_connected_for_error_clear_and_telemetry(self):
        session,bench,channel=self.make_session()
        channel.state.side_effect=lambda **kw:NS(received_at=time.perf_counter(),error_code=0x40,
            position=2,velocity=0,torque=0,motor_temperature=28,board_temperature=29)
        samples=[]
        session.emit=lambda kind,value:samples.append(value) if kind=='control_sample' else None
        with patch.object(session,'_open_hardware',return_value=(bench,channel)):
            session.start()
            try:
                wait_for(lambda:len(samples)>0)
                self.assertTrue(session.connected)
                self.assertFalse(session.enabled)
                self.assertEqual(samples[-1]['error_code'],0x40)
            finally:
                session.disconnect(); session.thread.join(2)
        self.assertIsNone(session.failure)
        channel.enable.assert_not_called()

    def test_cleanup_warning_notes_block_automatic_mode_handoff(self):
        session,bench,channel=self.make_session()
        error=InterruptedError('operation cancelled')
        error.add_note('校准停止未确认，请断开工装电源')
        with patch.object(session,'_open_hardware',side_effect=error):
            session.start(); session.thread.join(2)
        self.assertFalse(session.cleanup_confirmed)
        self.assertFalse(session.reconnect_allowed)
