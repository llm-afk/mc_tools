import errno
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError,URLError

from scripts.factory_test.printer_service import LocalPrinterService,prepare_config,child_environment
from scripts.factory_test.station_config import StationConfig
from scripts.factory_test.integrations import PrinterClient,PrintUnknown
from tests import test_factory_lifecycle as lifecycle_tests
from scripts.factory_test.gui import WindowConsole
from scripts.factory_test.gui import FactoryWindow
from scripts.factory_test.device_status import PrinterMonitor, check_printer
from dataclasses import replace,asdict
from scripts.factory_test.station_config import load_config,save_config
from scripts.factory_test.service_credentials import CredentialStore,FIELDS
from unittest.mock import patch


class FakeProcess:
    def __init__(self):
        self.pid=12345;self.returncode=None;self.closed=threading.Event()
        self.stdin=Mock();self.stdin.close.side_effect=self.closed.set
        self.stdout=io.StringIO('service boot\n')
    def poll(self): return self.returncode
    def wait(self):
        if not self.closed.wait(3): raise AssertionError('Owner did not close stdin')
        self.returncode=0
        return 0


class PrinterServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.runtime=self.root/'runtime';self.runtime.mkdir()
        (self.runtime/'node.exe').write_bytes(b'fake node, never executed')
        (self.runtime/'node_modules').mkdir()
        self.source=Path(__file__).resolve().parents[1]/'services/tsc-print-service'
        self.config=StationConfig()
        self.addCleanup(WindowConsole.cancelled.clear)

    def test_runtime_config_keeps_custom_label_settings_and_uses_saved_queue_and_routes(self):
        path=prepare_config(self.config,self.root,self.source)
        value=json.loads(path.read_text(encoding='utf-8'));value['label']['width']=60
        path.write_text(json.dumps(value),encoding='utf-8')
        self.config.printer_name='USB & 标签机';self.config.printer_status_path='/custom/status'
        prepare_config(self.config,self.root,self.source)
        value=json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(value['label']['width'],60)
        self.assertEqual(value['printer']['name'],'USB & 标签机')
        self.assertEqual(value['httpRoutes']['status'],'/custom/status')
        self.assertFalse(value['printerDiscovery']['serialScan'])

    def test_owned_process_uses_hidden_shell_free_launch_and_stops_by_stdin_eof(self):
        process=FakeProcess();popen=Mock(return_value=process);events=queue.Queue()
        http=Mock(side_effect=[URLError(ConnectionRefusedError(errno.ECONNREFUSED,'refused')),
                              dict(data=dict(service='tsc-print-service',pid=process.pid))])
        manager=LocalPrinterService(self.config,self.root,http=http,popen=popen,notify=events.put,
            paths=(self.source,self.runtime))
        manager.start()
        try:
            while events.get(timeout=3)['state']!='RUNNING': pass
            arguments=popen.call_args.args[0]
            self.assertIn('--managed',arguments)
            self.assertFalse(popen.call_args.kwargs.get('shell',False))
            self.assertEqual(popen.call_args.kwargs['encoding'],'utf-8')
        finally:
            manager.stop();manager.thread.join(timeout=3)
        self.assertFalse(manager.thread.is_alive())
        self.assertTrue(process.closed.is_set())
        self.assertIsNone(manager.error)

    def test_existing_external_service_is_never_spawned_or_stopped(self):
        for reply in (dict(data=dict(service='tsc-print-service',pid=999)),
                      HTTPError('http://127.0.0.1/api/health',404,'old service',{},None)):
            http=Mock(side_effect=reply) if isinstance(reply,Exception) else Mock(return_value=reply)
            popen=Mock();manager=LocalPrinterService(self.config,self.root,http=http,popen=popen)
            manager.run();manager.stop()
            self.assertTrue(manager.external);popen.assert_not_called()

    def test_remote_service_address_does_not_launch_a_local_process_or_contact_remote_host(self):
        self.config.printer_url='https://printer.example.test'
        http,popen=Mock(),Mock()
        manager=LocalPrinterService(self.config,self.root,http=http,popen=popen)
        manager.run()
        http.assert_not_called();popen.assert_not_called();self.assertTrue(manager.external)

    def test_unknown_service_timeout_does_not_start_another_process(self):
        http=Mock(side_effect=URLError(TimeoutError('service busy')));popen=Mock()
        manager=LocalPrinterService(self.config,self.root,http=http,popen=popen)
        manager.run()
        popen.assert_not_called();self.assertIsNotNone(manager.error)

    def test_unknown_raw_receipt_stays_unknown_without_replay(self):
        http=Mock(return_value=dict(success=False,data=dict(results=[
            dict(sn='TEST-SN-0001',success=False,outcome='UNKNOWN',jobs=[dict(jobId=123)])])))
        with self.assertRaises(PrintUnknown) as error:
            PrinterClient(self.config,http).print_sn('TEST-SN-0001')
        self.assertEqual(error.exception.diagnostic_code,'PRINT_SERVICE_OUTCOME_UNKNOWN')
        http.assert_called_once()

    def test_legacy_query_fallback_still_verifies_the_returned_queue(self):
        http=Mock(side_effect=[HTTPError('http://localhost/api/status?printerName=x',404,'old route',{},None),
                              dict(data=dict(online=True,printerName=self.config.printer_name))])
        PrinterClient(self.config,http).ready()
        self.assertEqual(http.call_count,2)
        self.assertNotIn('?',http.call_args.args[0])

    def test_gui_exit_waits_for_its_service_owner_thread(self):
        host,old=lifecycle_tests.ShutdownTests().make_host()
        host.worker.is_alive.return_value=old.thread.is_alive.return_value=False
        host.scan_in_progress=False
        service=NS(stop=Mock(),thread=NS(name='printer-service-owner',is_alive=Mock(return_value=True)))
        host.printer_service=service
        host.close();service.stop.assert_called_once();host.root.destroy.assert_not_called()
        service.thread.is_alive.return_value=False;host.advance_shutdown()
        host.root.destroy.assert_called_once()

    def test_missing_sn_key_blocks_only_full_production_readiness_without_key_format_rules(self):
        host=FactoryWindow.__new__(FactoryWindow)
        host.credential_error='';host.saved_credentials=dict.fromkeys(FIELDS,'')
        with patch.dict('os.environ',{},clear=True):
            self.assertFalse(host.sn_configuration_ready(self.config))
            host.saved_credentials['sn_api_key']='x'
            self.assertTrue(host.sn_configuration_ready(self.config))
            host.credential_error='cannot decrypt'
            self.assertFalse(host.sn_configuration_ready(self.config))
            self.assertTrue(host.sn_configuration_ready(replace(self.config,simulation=True)))
        host.credential_error='';host.saved_credentials['sn_api_key']=''
        with patch.dict('os.environ',{self.config.api_key_env:'legacy-key'},clear=True):
            self.assertTrue(host.sn_configuration_ready(self.config))

    def test_custom_station_credentials_and_label_settings_survive_restart(self):
        path=self.root/'station.json'
        config=replace(self.config,target_id=7,hardware_version='1223',model='223',factory='JS',
            printer_name='Custom USB',printer_copies=2,printer_url='http://127.0.0.1:19876',
            credentials_file=str(path),mes_url='https://sn.example.test',
            report_upload_url='https://report.example.test/report',oss_bucket='custom-bucket')
        keys={name:'fixture-test-'+name for name in FIELDS}
        save_config(config,path,credentials=keys)
        label_path=prepare_config(config,self.root,self.source)
        label=json.loads(label_path.read_text(encoding='utf-8'));label['label']['density']=7
        label_path.write_text(json.dumps(label),encoding='utf-8')
        restored=load_config(path)
        self.assertEqual(asdict(restored),asdict(config))
        self.assertEqual(CredentialStore(path).load(),keys)
        prepare_config(restored,self.root,self.source)
        self.assertEqual(json.loads(label_path.read_text(encoding='utf-8'))['label']['density'],7)
        self.assertTrue(all(value not in path.read_text(encoding='utf-8') for value in keys.values()))

    def test_service_child_does_not_inherit_production_keys_or_developer_node_options(self):
        with patch.dict('os.environ',dict(MIRRORME_MES_API_KEY='secret',NODE_OPTIONS='--bad-developer-option',
                        SystemRoot=r'C:\Windows'),clear=True):
            env=child_environment(self.runtime)
        self.assertNotIn('MIRRORME_MES_API_KEY',env)
        self.assertNotIn('NODE_OPTIONS',env)
        self.assertEqual(env['NODE_PATH'],str(self.runtime/'node_modules'))

    def test_engineer_trial_always_submits_one_test_label_without_mes_credentials(self):
        host=FactoryWindow.__new__(FactoryWindow)
        host.busy=host.closing=False;host.config=replace(self.config,printer_copies=5)
        host.config_revision=1;host.printer_check_thread=None;host.events=queue.Queue()
        host.settings_notice=Mock();host.printer_check_button=Mock();host.printer_test_button=Mock()
        host.diagnostics=Mock()
        with patch('scripts.factory_test.integrations.PrinterClient') as client:
            client.return_value.ready.return_value=dict(data=dict(online=True))
            client.return_value.print_sn.return_value=dict(sn='TEST-SN-0001',success=True)
            host.check_printer_service(test_print=True)
            kind,result=host.events.get(timeout=3)
            host.printer_check_thread.join(timeout=3)
        self.assertEqual(kind,'printer_check');self.assertTrue(result['ok'])
        client.return_value.print_sn.assert_called_once_with('TEST-SN-0001')
        self.assertTrue(all(call.args[0].printer_copies==1 for call in client.call_args_list))
        self.assertEqual(host.config.printer_copies,5)

    def test_slow_status_never_blocks_poll_or_returns_a_previous_generation(self):
        entered, release = threading.Event(), threading.Event()
        def slow(config):
            entered.set()
            if not release.wait(2):
                raise AssertionError('poll blocked on the Windows queue')
            return dict(state='READY', detail=config.printer_name), 0
        check = Mock(side_effect=slow)
        monitor = PrinterMonitor(check)
        try:
            self.assertEqual(monitor.poll(self.config,(1,1))['state'],'CHECKING')
            self.assertTrue(entered.wait(1))
            replacement=replace(self.config,printer_name='replacement')
            self.assertEqual(monitor.poll(replacement,(2,1))['state'],'CHECKING')
            self.assertEqual(check.call_count,1)
        finally:
            release.set()
            monitor.thread.join(2)
        self.assertIsNone(monitor.result)
        monitor.poll(replacement,(2,1))
        monitor.thread.join(2)
        self.assertEqual(monitor.poll(replacement,(2,1)),dict(state='READY',detail='replacement'))

    def test_stale_windows_sample_expires_while_next_check_is_running(self):
        clock=Mock(return_value=100)
        hold=threading.Event()
        def check(config):
            if clock()==104:
                hold.wait(2)
            return dict(state='READY',detail='USB001'), 6
        monitor=PrinterMonitor(check,clock=clock)
        monitor.poll(self.config,1)
        monitor.thread.join(2)
        self.assertEqual(monitor.poll(self.config,1)['state'],'READY')
        clock.return_value=104
        try:
            self.assertEqual(monitor.poll(self.config,1)['state'],'UNKNOWN')
        finally:
            hold.set()
            monitor.thread.join(2)

    def test_timeout_query_failure_and_confirmed_offline_are_distinct(self):
        responses=(TimeoutError(),dict(data=dict(online=False,errorCode='WINDOWS_PRINTER_QUERY_FAILED')),
                   dict(data=dict(online=False,reason='缺纸')),dict(data=None))
        for response,expected in zip(responses,('UNKNOWN','UNKNOWN','OFFLINE','ERROR')):
            with self.subTest(response=response),patch('scripts.factory_test.device_status.json_http') as http:
                if isinstance(response,Exception): http.side_effect=response
                else: http.return_value=response
                self.assertEqual(check_printer(self.config)[0]['state'],expected)
                http.assert_called_once()
