from types import SimpleNamespace as NS
import os
import queue
import threading
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from motor_platform import MitCommand
from scripts.factory_test.device_status import DeviceHealth, AdapterMonitor
from scripts.factory_test.manual_control import ManualSession
from scripts.factory_test.station_config import StationConfig


class FileActionTests(unittest.TestCase):
    def window(self, folder):
        from scripts.factory_test.gui import FactoryWindow
        window=FactoryWindow.__new__(FactoryWindow)
        window.storage_root=Path(folder)
        window.latest_result=None
        window.config=StationConfig(simulation=True)
        window.diagnostics=Mock()
        window.write_log=Mock()
        return window

    def test_log_action_opens_mode_directory_and_preserves_existing_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            window=self.window(folder)
            path=Path(folder)/'operator.log'
            path.write_text('SDO timeout\n',encoding='utf-8')
            with patch('scripts.factory_test.gui.os.startfile') as opener:
                window.open_error_log()
            expected=Path(folder)/'logs/preview'
            opener.assert_called_once_with(expected)
            self.assertTrue(expected.is_dir())
            self.assertEqual(path.read_text(encoding='utf-8'),'SDO timeout\n')

    def test_report_action_opens_report_root_regardless_of_latest_result(self):
        with tempfile.TemporaryDirectory() as folder:
            window=self.window(folder)
            previous=Path(folder)/'demo-data/previous/report.png'
            previous.parent.mkdir(parents=True)
            previous.write_bytes(b'previous report')
            window.latest_result=Path(folder)/'demo-data/current'
            window.latest_result.mkdir()
            with patch('scripts.factory_test.gui.os.startfile') as opener:
                window.open_test_report()
                opener.assert_called_once_with(Path(folder)/'demo-data')
                current=window.latest_result/'report.png'
                current.write_bytes(b'current report')
                window.open_test_report()
                self.assertEqual(opener.call_args.args[0],Path(folder)/'demo-data')

    def test_report_directory_is_separate_for_preview_and_production(self):
        with tempfile.TemporaryDirectory() as folder:
            window=self.window(folder)
            old=Path(folder)/'demo-data/old/report.png'
            new=Path(folder)/'demo-data/new/report.png'
            real=Path(folder)/'data/real/report.png'
            for i,path in enumerate((old,new,real),1):
                path.parent.mkdir(parents=True)
                path.write_bytes(b'report')
                os.utime(path,ns=(i*1000000000,i*1000000000))
            with patch('scripts.factory_test.gui.os.startfile') as opener:
                window.open_test_report()
                opener.assert_called_once_with(Path(folder)/'demo-data')
                window.config.simulation=False
                window.open_test_report()
                self.assertEqual(opener.call_args.args[0],Path(folder)/'data')

    def test_no_reports_and_open_failure_are_logged(self):
        with tempfile.TemporaryDirectory() as folder:
            window=self.window(folder)
            with patch('scripts.factory_test.gui.os.startfile') as opener:
                window.open_test_report()
                opener.assert_called_once_with(Path(folder)/'demo-data')
                self.assertTrue((Path(folder)/'demo-data').is_dir())
            with patch('scripts.factory_test.gui.os.startfile',side_effect=OSError('viewer missing')):
                window.open_error_log()
                self.assertIn('无法打开错误日志',window.write_log.call_args.args[0])
                self.assertEqual(window.diagnostics.current.event.call_args.args[0], 'ui.open_log_directory.failed')


def wait_until(predicate):
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        if predicate(): return
        time.sleep(.01)
    raise AssertionError('后台操作未完成')


class HealthTests(unittest.TestCase):
    def test_production_controller_requires_selection_before_opening_can(self):
        from scripts.factory_test.gui import FactoryWindow
        host = FactoryWindow.__new__(FactoryWindow)
        host.config = StationConfig()
        host.lock = threading.Lock()
        host.dependencies_ready = True
        host.page = 'production'
        host.control_page = NS(active=False)
        host.last_health = 0
        host.production_pause = threading.Event()
        host.device_health = Mock()
        host.device_health.check.return_value = {}
        host.diagnostics = Mock()
        host.events = queue.Queue()
        host.start_requested = host.resume_requested = host.busy = False
        host.shutdown = Mock()
        host.shutdown.is_set.return_value = False
        host.shutdown.wait.side_effect = [False, True]
        with patch('scripts.factory_test.gui.Maintenance') as maintenance:
            host.controller()
        maintenance.assert_not_called()
        messages = [str(call.kwargs.get('error','')) for call in host.diagnostics.current.event.call_args_list]
        self.assertTrue(any('选择' in message for message in messages))

    def test_adapter_monitor_reports_hotplug_changes_without_can_or_serial_io(self):
        stop, changes = threading.Event(), []
        first = dict(serial_number='first',path='first-path')
        second = dict(serial_number='second',path='second-path')
        detect = Mock(side_effect=[[first],[first],[first,second],[],OSError('PnP unavailable')])
        def emit(value):
            changes.append(value)
            if len(changes) == 4: stop.set()
        with patch('motor_platform.Canable.open',side_effect=AssertionError('CAN open')), \
             patch('serial.Serial',side_effect=AssertionError('Serial open')):
            AdapterMonitor(emit,stop,detect=detect,interval=0).run()
        self.assertEqual(detect.call_count,5)
        self.assertEqual([value['devices'] for value in changes],[[first],[first,second],[],[]])
        self.assertIsInstance(changes[-1]['error'],OSError)

    def test_device_results_are_emitted_before_the_last_service_finishes(self):
        emitted=[]
        def printer(*args, **kwargs):
            self.assertEqual([name for name,info in emitted],['can','dyno','power'])
            return dict(data=dict(online=True))
        with patch('motor_platform.Canable.detect',return_value=[dict(serial_number='adapter')]), \
             patch('serial.tools.list_ports.comports',return_value=[]), \
             patch('motor_platform.components.dynamometer.detect_dynamometers',return_value=[]), \
             patch('scripts.factory_test.device_status.json_http',side_effect=printer):
            DeviceHealth().check(StationConfig(),emit=lambda name,info:emitted.append((name,info)))
        self.assertEqual([name for name,info in emitted],['can','dyno','power','printer'])

    def test_adapter_sn_mismatch_displays_saved_and_present_serials(self):
        with patch('motor_platform.Canable.detect', return_value=[dict(serial_number='present-adapter')]), \
             patch('serial.tools.list_ports.comports', return_value=[]), \
             patch('motor_platform.components.dynamometer.detect_dynamometers', return_value=[]), \
             patch('scripts.factory_test.device_status.json_http', return_value=dict(data=dict(online=True))):
            result = DeviceHealth().check(StationConfig(adapter_sn='saved-adapter'))
        self.assertEqual(result['can']['state'], 'OFFLINE')
        self.assertIn('saved-adapter', result['can']['detail'])
        self.assertIn('present-adapter', result['can']['detail'])

    def test_preview_health_never_probes_real_hardware(self):
        with patch('motor_platform.Canable',side_effect=AssertionError('USB')), \
             patch('motor_platform.PowerSupply',side_effect=AssertionError('power')), \
             patch('scripts.factory_test.device_status.json_http',side_effect=AssertionError('HTTP')):
            values=DeviceHealth().check(StationConfig(simulation=True))
        self.assertEqual(set(values),{'can','dyno','power','printer'})
        self.assertTrue(all(v['state']=='SIMULATED' for v in values.values()))

    def test_readonly_health_excludes_dyno_port_from_power_probe(self):
        power=Mock()
        power.__enter__=Mock(return_value=power)
        power.__exit__=Mock(return_value=False)
        power.read_state.return_value=NS(voltage=0,output=False)
        dyno=Mock()
        dyno.__enter__=Mock(return_value=dyno)
        dyno.__exit__=Mock(return_value=False)
        dyno.receive_state.return_value=NS(torque=0,speed=0)
        with patch('motor_platform.Canable.detect',return_value=[dict(serial_number='adapter')]), \
             patch('serial.tools.list_ports.comports',return_value=[NS(device='COM1'),NS(device='COM2')]), \
             patch('motor_platform.components.dynamometer.detect_dynamometers',return_value=['COM1']), \
             patch('motor_platform.components.power_supply.discovery.detect_power_supplies',return_value={'COM2':'UDP6721'}) as detect_power, \
             patch('motor_platform.PowerSupply',return_value=power), \
             patch('motor_platform.Dynamometer',return_value=dyno), \
             patch('scripts.factory_test.device_status.json_http',return_value=dict(data=dict(online=True))):
            result=DeviceHealth().check(StationConfig(simulation=False,adapter_sn='adapter'))
        self.assertEqual(detect_power.call_args.kwargs['ports'],['COM2'])
        self.assertTrue(all(v['state']=='READY' for v in result.values()))
        power.enable.assert_not_called()
        power.disable.assert_not_called()
        power.set_voltage.assert_not_called()
        self.assertIn('输出关闭',result['power']['detail'])


class ManualTests(unittest.TestCase):
    def test_debug_ui_requires_an_explicit_adapter_selection(self):
        from scripts.factory_test.manual_control import ControlPage
        page = ControlPage.__new__(ControlPage)
        page.session = None
        page.host = NS(busy=False, config=StationConfig())
        page.message = Mock()
        with patch('scripts.factory_test.manual_control.ManualSession') as session:
            page.connect()
        session.assert_not_called()
        self.assertIn('选择', page.message.set.call_args.args[0])

    def test_authorized_maintenance_uses_correct_firmware_objects(self):
        for model,kind,index,value in (('221','zero',0x2070,0),('111','zero',0x2002,3),
                                      ('221','clear_error',0x2002,0xFF)):
            with self.subTest(model=model,kind=kind):
                config=StationConfig(model=model,hardware_version='0'+model)
                session=ManualSession(config,3,lambda *_:None)
                session.profile_known=True
                maintenance=Mock()
                connection=Mock()
                maintenance.connect.return_value.__enter__=Mock(return_value=connection)
                maintenance.connect.return_value.__exit__=Mock(return_value=False)
                maintenance.read.return_value=0
                with patch('scripts.factory_test.manual_control.Maintenance',return_value=maintenance):
                    session._maintain(kind)
                maintenance.write.assert_called_once_with(connection,3,index,2,value)

    def test_calibration_reuses_checked_completion_protocol(self):
        session=ManualSession(StationConfig(),3,lambda *_:None)
        session.profile_known=True
        with patch('scripts.factory_test.manual_control.Maintenance') as factory:
            session._maintain('calibrate')
            factory.return_value.calibrate.assert_called_once()

    def test_maintenance_rejected_while_motor_enabled(self):
        session=ManualSession(StationConfig(),3,lambda *_:None)
        session.connected=session.enabled=True
        for command in ('clear_error','zero','calibrate'):
            with self.assertRaisesRegex(RuntimeError,'先停止'): session.command(command)
        self.assertTrue(session.commands.empty())

    def test_port_dropdown_lists_auto_present_and_saved_port(self):
        from scripts.factory_test.operator_view import port_options
        with patch('serial.tools.list_ports.comports',return_value=[NS(device='COM3'),NS(device='COM5')]):
            self.assertEqual(port_options('COM9'),['auto','COM3','COM5','COM9'])

    def test_auto_connection_backfills_actual_node_and_hardware_profile(self):
        config=StationConfig(adapter_sn='adapter')
        session=ManualSession(config,None,lambda *_:None)
        bench=Mock()
        bench.motor.return_value.read_serial_bytes.return_value=b'C2KNEE0STDXC01112610280001\0\0'
        with patch('scripts.factory_test.manual_control.Canable'), \
             patch('scripts.factory_test.manual_control.Bench',return_value=bench) as factory, \
             patch('scripts.factory_test.manual_control.Maintenance') as maintenance:
            maintenance.return_value.discover.return_value=dict(node_id=7,sn='motor')
            maintenance.read.return_value=0x0201
            connected, channel = session._open_hardware()
        self.assertEqual(session.node,7)
        self.assertEqual(session.config.model,'111')
        self.assertTrue(session.profile_known)
        self.assertEqual(session.firmware,'513')
        self.assertEqual(factory.call_args.kwargs['motor_ids'],[7])
        self.assertIs(connected,bench)
        self.assertIs(channel,bench.start.return_value.motor.return_value)
        bench.start.assert_called_once_with(rate_hz=1000,command_timeout=.25)
        channel.monitor.assert_called_once()
        channel.enable.assert_not_called()
        self.assertEqual(config.model,'221')  # 不修改固定工位的产品选择。

    def test_unknown_hardware_blocks_zero_and_calibration(self):
        session=ManualSession(StationConfig(),1,lambda *_:None)
        session.connected=True
        session.command('enable')  # Generic GD/ADM enable does not depend on a registered SN/model.
        self.assertEqual(session.commands.get_nowait()[0],'enable')
        for kind in ('zero','calibrate'):
            with self.assertRaises(RuntimeError): session.command(kind)

    def test_new_hardware_revisions_do_not_block_generic_control_or_platform_maintenance(self):
        for hardware in ('0223','1229','0220'):
            session=ManualSession(StationConfig(adapter_sn='adapter'),7,lambda *_:None)
            bench=Mock()
            bench.motor.return_value.read_serial_bytes.return_value=('0'*12+hardware+'0'*10).encode()+b'\0\0'
            with patch('scripts.factory_test.manual_control.Canable'), \
                 patch('scripts.factory_test.manual_control.Bench',return_value=bench), \
                 patch('scripts.factory_test.manual_control.Maintenance.read',return_value=245):
                session._open_hardware()
            self.assertTrue(session.profile_known)
            self.assertEqual(session.config.hardware_version,hardware)
            self.assertEqual(session.config.torque,30)
            self.assertEqual(session.firmware,'245')
            session.connected=True
            session.command('enable')
            self.assertEqual(session.commands.get_nowait()[0],'enable')

    def test_connect_does_not_enable_and_presets_require_enable_to_drive(self):
        events=[]
        session=ManualSession(StationConfig(simulation=True),1,lambda k,v:events.append((k,v)))
        with patch('scripts.factory_test.manual_control.Canable',side_effect=AssertionError('USB')):
            session.start()
            try:
                wait_until(lambda:session.connected)
                self.assertFalse(session.enabled)
                wait_until(lambda:any(k=='control_sample' for k,_ in events))
                self.assertTrue(all(v['target']['torque']==0 and v['enabled'] is False
                                    for k,v in events if k=='control_sample'))
                session.command('target',MitCommand(torque=1))
                self.assertEqual(session.pending_target.torque,1)
                self.assertEqual(session.target,MitCommand())
                session.command('enable')
                wait_until(lambda:session.enabled)
                wait_until(lambda:any(k=='control_sample' and v['sent_target']['torque']==1 for k,v in events))
                session.command('disable')
                wait_until(lambda:not session.enabled)
                self.assertEqual(session.target,MitCommand())
                self.assertEqual(session.pending_target.torque,1)
            finally:
                session.disconnect()
                session.thread.join(2)
        self.assertFalse(session.active)

    def test_invalid_target_does_not_replace_active_target(self):
        session=ManualSession(StationConfig(simulation=True),1,lambda *_:None)
        session.start()
        try:
            wait_until(lambda:session.connected)
            session.command('enable')
            wait_until(lambda:session.enabled)
            with self.assertRaises(ValueError): session.command('target',MitCommand(torque=float('nan')))
            self.assertEqual(session.target,MitCommand())
        finally:
            session.disconnect();session.thread.join(2)
