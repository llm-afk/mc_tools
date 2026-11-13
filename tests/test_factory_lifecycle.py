from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from scripts.factory_test.lifecycle import AlreadyRunning, StationInstance
from scripts.factory_test.gui import WindowConsole
from scripts.factory_test.manual_control import ControlPage
from scripts.factory_test.station import PresenceLatch
from scripts.factory_test.station_config import StationConfig
from tests.test_factory_modes import window


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_exclusion_stale_file_and_exception_cleanup(self):
        (self.root/'station-instance.lock').write_bytes(b'stale process information')
        with StationInstance(self.root):
            with self.assertRaises(AlreadyRunning):
                with StationInstance(self.root): self.fail('Second owner')
        with self.assertRaisesRegex(RuntimeError,'simulated failure'):
            with StationInstance(self.root): raise RuntimeError('simulated failure')
        with StationInstance(self.root): pass
        self.assertTrue((self.root/'station-instance.lock').exists())

    def run_child(self, script):
        return subprocess.run([sys.executable,'-B','-c',script,str(self.root)],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=10,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

    def test_os_excludes_another_process_and_releases_lock_after_crash(self):
        probe='''import sys
from scripts.factory_test.lifecycle import StationInstance, AlreadyRunning
try:
    with StationInstance(sys.argv[1]): print('OWNED')
except AlreadyRunning: print('BUSY')
'''
        with StationInstance(self.root):
            child=self.run_child(probe)
            self.assertEqual((child.returncode,child.stdout.strip()),(0,'BUSY'),child.stderr)
        crashed=self.run_child('''import os, sys
from scripts.factory_test.lifecycle import StationInstance
owner=StationInstance(sys.argv[1]); owner.__enter__()
os._exit(7)
''')
        self.assertEqual(crashed.returncode,7,crashed.stderr)
        with StationInstance(self.root): pass

    def test_permission_failure_is_not_misreported_as_another_instance(self):
        with patch('pathlib.Path.open',side_effect=PermissionError('read-only folder')):
            with self.assertRaises(PermissionError):
                with StationInstance(self.root): pass

    def test_entry_does_not_initialize_gui_when_station_is_owned(self):
        from scripts.factory_test.desktop_entry import main
        with StationInstance(self.root),patch('scripts.factory_test.station_config.state_root',return_value=self.root), \
             patch.object(sys,'argv',['station.exe']),patch('scripts.factory_test.gui.main') as gui, \
             patch('scripts.factory_test.desktop_entry.already_running_notice') as notice:
            main()
            gui.assert_not_called(); notice.assert_called_once()

    def test_entry_keeps_lock_through_gui_lifetime_and_releases_after_return(self):
        from scripts.factory_test.desktop_entry import main
        def gui():
            with self.assertRaises(AlreadyRunning):
                with StationInstance(self.root): pass
        with patch('scripts.factory_test.station_config.state_root',return_value=self.root), \
             patch.object(sys,'argv',['station.exe']),patch('scripts.factory_test.gui.main',side_effect=gui):
            main()
        with StationInstance(self.root): pass


class ConfigurationRaceTests(unittest.TestCase):
    def tearDown(self):
        WindowConsole.cancelled.clear()

    def test_stale_health_candidate_and_previous_mode_events_do_not_enable_start(self):
        host=window(); host.ready_to_start=False
        host.config_revision=2; host.mode_generation=3; host.device_state=Mock()
        for kind,value in (
            ('candidate',dict(config_revision=1,mode_generation=3,ready=True)),
            ('device_health',dict(config_revision=1,mode_generation=3,name='printer',info=dict(state='READY',detail='old queue'))),
            ('candidate',dict(config_revision=2,mode_generation=2,ready=True)),
            ('device_health',dict(config_revision=2,mode_generation=3,name='printer',info=dict(state='OFFLINE',detail='new queue offline')))):
            host.events.put((kind,value))
        host.process_events()
        self.assertFalse(host.ready_to_start)
        host.device_state.assert_called_once_with('printer','OFFLINE','new queue offline')

    def run_scan_with_edit(self, changes=None, *, credentials=False, configuring=False,wrong_connection=False):
        host=window(); host.config=StationConfig(simulation=True)
        host.start_requested=host.resume_requested=False
        host.demo_present=True; host.last_health=time.monotonic()
        host.latch=PresenceLatch(); host.latch.stable=3
        host.shutdown=Mock(); host.shutdown.is_set.return_value=False
        host.shutdown.wait.side_effect=[False,True]
        host.storage_root=None; host.delivery=None; host.started_sessions=0
        real_put=host.events.put
        def put(event):
            if event[0]=='candidate':
                host.requested_identity=host.candidate_identity(event[1])
                if wrong_connection:
                    host.requested_identity=host.candidate_identity(dict(event[1],connection_id='previous'))
                host.start_requested=True
                if changes: host.config=replace(host.config,**changes)
                if credentials: host.config_revision+=1
                if configuring: host.configuring=True
            real_put(event)
        host.events.put=put
        with patch('scripts.factory_test.gui.StationRunner') as runner:
            runner.return_value.already_completed.return_value=False
            runner.return_value.pending.return_value=False
            runner.return_value.begin.return_value=dict(status='COMPLETE')
            host.controller()
        return runner,host

    def test_all_product_printer_and_cloud_changes_invalidate_pre_start_snapshot(self):
        for changes in (dict(factory='JS'),dict(project='A2'),dict(hardware_version='1221'),
                        dict(production_date='261110'),dict(printer_copies=2),dict(oss_bucket='another-bucket'),
                        dict(mes_url='https://new.example.test')):
            with self.subTest(changes=changes):
                runner,_=self.run_scan_with_edit(changes)
                runner.assert_not_called()

    def test_key_rotation_and_in_progress_config_save_also_block_stale_start(self):
        for flags in (dict(credentials=True),dict(configuring=True)):
            runner,_=self.run_scan_with_edit(**flags)
            runner.assert_not_called()

    def test_fresh_snapshot_still_starts_normally(self):
        runner,host=self.run_scan_with_edit()
        runner.return_value.begin.assert_called_once()
        self.assertEqual(runner.call_args.args[0],host.config)

    def test_start_for_previous_connection_is_consumed_without_starting_replacement(self):
        runner,host=self.run_scan_with_edit(wrong_connection=True)
        runner.assert_not_called()
        self.assertFalse(host.start_requested)
        self.assertIn('operator.start_cancelled',[c.args[0] for c in host.diagnostics.current.event.call_args_list])

    def test_old_delivery_worker_cannot_replace_new_delivery_status(self):
        host=window();host.delivery_revision=2;host.upload_status=Mock()
        host.last_delivery_message=None;host.write_log=Mock()
        host.events.put(('report_delivery',dict(delivery_revision=1,pending=10,blocked=9,succeeded=8,error='old error')))
        host.process_events()
        host.upload_status.set.assert_not_called()
        host.write_log.assert_not_called()


class ShutdownTests(unittest.TestCase):
    def tearDown(self):
        WindowConsole.cancelled.clear()

    def make_host(self):
        host=window();host.root=Mock();host.settings_window=Mock();host.page_menu=Mock()
        host.worker=NS(name='scanner',is_alive=Mock(return_value=True))
        old=NS(stop=Mock(),thread=NS(name='old-upload',is_alive=Mock(return_value=True)))
        host.delivery=NS(stop=Mock(),thread=None)
        host.retiring_deliveries=[old]
        host.scan_in_progress=True
        return host,old

    def test_close_is_one_request_and_waits_for_scanner_and_retired_upload(self):
        host,old=self.make_host()
        with patch('scripts.factory_test.gui.messagebox.showwarning') as warning:
            host.close();host.close()
            self.assertTrue(host.shutdown.is_set() and host.closing)
            self.assertTrue(WindowConsole.cancelled.is_set())
            host.control_page.leave.assert_called_once()
            host.root.destroy.assert_not_called();host.diagnostics.close.assert_not_called()
            host.root.after.assert_called_once_with(100,host.advance_shutdown)
            host.scan_in_progress=False;host.worker.is_alive.return_value=False
            host.advance_shutdown()
            host.root.destroy.assert_not_called()
            old.thread.is_alive.return_value=False
            host.advance_shutdown()
            host.control_page.finish_storage.assert_called_once()
            host.root.destroy.assert_called_once();host.diagnostics.close.assert_called_once()
            warning.assert_not_called()

    def test_close_cancels_pending_mode_or_settings_reconnect_and_environment_start(self):
        host,old=self.make_host()
        host.pending_page='control';host.settings_save_pending=True
        host.apply_config=Mock();host.configure_delivery=Mock()
        host.close()
        host.events.put(('ready',[]))
        host.process_events();host.advance_page_transition();host.advance_settings_save()
        host.control_page.enter.assert_not_called();host.apply_config.assert_not_called()
        host.configure_delivery.assert_not_called()

    def test_unconfirmed_disable_is_visible_after_threads_release(self):
        host,old=self.make_host()
        host.worker.is_alive.return_value=old.thread.is_alive.return_value=False
        host.scan_in_progress=False
        host.control_page.session=NS(thread=None,cleanup_confirmed=False,failure='disable ACK missing')
        with patch('scripts.factory_test.gui.messagebox.showwarning') as warning:
            host.close()
        self.assertIn('disable ACK missing',warning.call_args.args[1])
        host.root.destroy.assert_called_once()

    def test_closing_debug_page_cannot_connect_or_enable_again(self):
        host=window();host.shutdown.set()
        page=ControlPage.__new__(ControlPage);page.host=host;page.session=Mock();page.apply=Mock()
        page.connect();page.send('enable')
        page.session.command.assert_not_called();page.apply.assert_not_called()

    def test_display_file_error_is_not_reported_as_unconfirmed_motor_disable(self):
        host,old=self.make_host()
        host.worker.is_alive.return_value=old.thread.is_alive.return_value=False
        host.scan_in_progress=False
        host.control_page.finish_storage.side_effect=OSError('disk write failed')
        with patch('scripts.factory_test.gui.messagebox.showwarning') as warning:
            host.close()
        self.assertIn('显示数据可能未完整保存',warning.call_args.args[1])
        self.assertNotIn('断开工装电源',warning.call_args.args[1])
        host.root.destroy.assert_called_once()
