import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from scripts.factory_test.gui import FactoryWindow
from scripts.factory_test.network_status import (NetworkMonitor, read_network_snapshot, network_messages,
                                                 NETWORK_TIMEOUT_S)
from scripts.factory_test.station_config import StationConfig
from tests.test_factory_modes import window
from tests import test_factory_lifecycle as lifecycle_tests


def snapshot(state='Up'):
    return dict(available=state=='Up',interfaces=[dict(name='WLAN',description='Test adapter',
        kind='Wireless80211',state=state,addresses=['192.0.2.10'],gateways=['192.0.2.1'],dns=['192.0.2.53'])])


class NetworkTests(unittest.TestCase):
    def test_local_snapshot_parses_unicode_and_keeps_dns_without_requesting_services(self):
        data=snapshot()
        data['interfaces'][0]['name']='无线网络'
        data['interfaces'][0]['addresses']=['192.0.2.11','192.0.2.10','192.0.2.10']
        with patch('scripts.factory_test.network_status.spawn_snapshot',return_value=NS(stdout='\ufeff'+json.dumps(data))):
            result=read_network_snapshot()
        self.assertEqual(result['interfaces'][0]['addresses'],['192.0.2.10','192.0.2.11'])
        self.assertEqual(result['interfaces'][0]['dns'],['192.0.2.53'])
        text='\n'.join(message for level,message in network_messages(result))
        self.assertIn('Wi-Fi',text)
        self.assertIn('链路已连接',text)
        self.assertIn('实际请求回执',text)

    def test_bad_system_reply_is_rejected_instead_of_claiming_connectivity(self):
        for value in ('bad json','{}','{"available":true,"interfaces":{}}',
                      '{"available":true,"interfaces":[{"name":"bad"}]}'):
            with self.subTest(value=value), patch('scripts.factory_test.network_status.spawn_snapshot',return_value=NS(stdout=value)):
                with self.assertRaises(ValueError): read_network_snapshot()

    def test_disconnected_and_missing_ip_are_shown_without_marking_cloud_online(self):
        data=snapshot('Down')
        text='\n'.join(message for level,message in network_messages(data))
        self.assertIn('未连接',text)
        self.assertIn('请检查',text)
        data=snapshot();data['interfaces'][0]['addresses']=[]
        text='\n'.join(message for level,message in network_messages(data))
        self.assertIn('尚未获得 IP',text)
        self.assertIn('请检查',text)

    def test_monitor_publishes_only_changes_and_retains_background_heartbeat(self):
        stop=threading.Event(); replies=iter([snapshot(),snapshot(),snapshot('Down')])
        output=[]; count=[]; diagnostics=Mock()
        def probe():
            count.append(1)
            return next(replies)
        def wait(_):
            if len(count)==3: stop.set()
        with patch.object(stop,'wait',side_effect=wait):
            NetworkMonitor(output.append,stop,diagnostics,probe=probe).run()
        self.assertEqual(len(output),2)
        self.assertEqual(diagnostics.station.event.call_count,2)
        diagnostics.station.sample.assert_called_once()
        self.assertFalse(diagnostics.station.event.call_args.kwargs['cloud_verified'])

    def test_timeout_is_bounded_and_surfaces_a_short_operator_message(self):
        stop=threading.Event(); diagnostics=Mock(); output=[]
        def probe(): raise subprocess.TimeoutExpired('long system query',NETWORK_TIMEOUT_S)
        with patch.object(stop,'wait',side_effect=lambda _:stop.set()):
            NetworkMonitor(output.append,stop,diagnostics,probe=probe).run()
        self.assertIn('超时',output[0]['error'])
        self.assertNotIn('long system query',output[0]['error'])
        self.assertEqual(diagnostics.station.event.call_args.kwargs['level'],'WARNING')
        self.assertIsInstance(diagnostics.station.event.call_args.kwargs['error'],subprocess.TimeoutExpired)

    def test_stop_during_system_query_does_not_write_to_closed_diagnostics(self):
        stop=threading.Event();diagnostics=Mock();emit=Mock()
        def probe():
            stop.set()
            return snapshot()
        NetworkMonitor(emit,stop,diagnostics,probe=probe).run()
        emit.assert_not_called();diagnostics.station.event.assert_not_called()


class VisibleLogTests(unittest.TestCase):
    def test_numeric_telemetry_does_not_spam_but_output_and_route_changes_are_logged(self):
        host=window();host.device_widgets={'power':(Mock(),Mock(),Mock())}
        host.device_state('power','READY','COM3 · 24.0 V · 输出关闭')
        host.device_state('power','READY','COM3 · 24.1 V · 输出关闭')
        self.assertEqual(host.write_log.call_count,1)
        host.device_state('power','READY','COM3 · 24.1 V · 输出开启')
        host.device_state('power','READY','COM4 · 24.1 V · 输出开启')
        self.assertEqual(host.write_log.call_count,3)
        host.device_state('power','ERROR','USB disconnected')
        self.assertEqual(host.write_log.call_args.kwargs['level'],'ERROR')

    def test_serial_inventory_from_old_configuration_is_discarded(self):
        host=window()
        host.events.put(('serial_inventory',dict(config_revision=-1,mode_generation=0,ports=[],error='stale')))
        host.process_events()
        host.write_log.assert_not_called()
        info=dict(config_revision=0,mode_generation=0,ports=[dict(device='COM3',description='Test serial')],error=None)
        host.events.put(('serial_inventory',info));host.events.put(('serial_inventory',info))
        host.process_events()
        host.write_log.assert_called_once()
        self.assertIn('COM3',host.write_log.call_args.args[0])

    def test_startup_summary_reports_key_presence_without_values(self):
        host=window();host.config_path=Path('station.json');host.config_existed=True
        host.storage_root=Path('local-test-data');host.credential_error=''
        host.saved_credentials=dict(sn_api_key='sn-private',mes_api_key='report-private',
                                    access_key_id='id-private',access_key_secret='secret-private')
        host.log_startup()
        text='\n'.join(call.args[0] for call in host.write_log.call_args_list)
        for marker in ('[启动]','[配置]','[存储]','[日志]','[串口]','[SN]','[固件]',
                       '[取号配置]','[打印配置]','[报告配置]','[OSS 配置]','[凭据]','[探测]'):
            self.assertIn(marker,text)
        for secret in host.saved_credentials.values(): self.assertNotIn(secret,text)

    def test_failed_configuration_never_reports_a_successful_load(self):
        host=window();host.config_path=Path('broken-station.json');host.config_existed=True
        host.storage_root=Path('local-test-data');host.credential_error='';host.saved_credentials={}
        host.config=StationConfig(automatic=False);host.config_error='JSON 格式损坏'
        host.log_startup()
        entries=[call for call in host.write_log.call_args_list if call.args[0].startswith('[配置]')]
        self.assertEqual(len(entries),1)
        self.assertIn('载入失败',entries[0].args[0])
        self.assertIn('暂停设备监视',entries[0].args[0])
        self.assertNotIn('已读取',entries[0].args[0])
        self.assertEqual(entries[0].kwargs['level'],'ERROR')

    def test_report_queue_initial_empty_state_and_changes_are_visible_without_repeats(self):
        host=window();host.upload_status=Mock();host.last_delivery_message=None
        status=dict(delivery_revision=0,pending=0,blocked=0,succeeded=0)
        host.events.put(('report_delivery',status));host.events.put(('report_delivery',status))
        host.process_events()
        host.write_log.assert_called_once()
        self.assertIn('待传 0',host.write_log.call_args.args[0])
        host.events.put(('report_delivery',dict(status,pending=1)))
        host.process_events()
        self.assertEqual(host.write_log.call_count,2)

    def test_shutdown_waits_for_the_bounded_local_network_query(self):
        host,old=lifecycle_tests.ShutdownTests().make_host()
        host.worker.is_alive.return_value=False;old.thread.is_alive.return_value=False
        host.scan_in_progress=False
        host.network_monitor=NS(thread=NS(name='network-status',is_alive=Mock(return_value=True)))
        host.close()
        host.root.destroy.assert_not_called()
        self.assertIn('网络检查',host.banner.set.call_args.args[0])
        host.network_monitor.thread.is_alive.return_value=False
        host.advance_shutdown()
        host.root.destroy.assert_called_once()


if __name__=='__main__':
    unittest.main()
