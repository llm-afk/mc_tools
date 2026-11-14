from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import replace
import errno
import io
import json
import os
from pathlib import Path
import queue
import tempfile
import time
import threading
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from scripts.factory_test.diagnostics import DiagnosticLogs, RETENTION_DAYS, log_scope, error_details, response_evidence
from scripts.factory_test.gui import EventWriter
from scripts.factory_test.integrations import MesClient, PrinterClient, PrintUnknown, IntegrationError, json_http, mes_payload
from scripts.factory_test.station import DemoServices, StationRunner
from scripts.factory_test.station_config import StationConfig


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.folder = Path(self.resources.enter_context(tempfile.TemporaryDirectory()))
        self.now = time.time()
        self.tick = 0.0
        self.logs = DiagnosticLogs(self.folder,clock=lambda:self.now,monotonic=lambda:self.tick)
        self.addCleanup(self.logs.close)
        self.config = StationConfig(simulation=False)
        # A logging check must never open a transport or contact a remote service.
        for name in ('motor_platform.Canable.detect','motor_platform.Canable.open',
                     'motor_platform.transports.canable._Usb','serial.Serial',
                     'urllib.request.OpenerDirector.open','requests.sessions.Session.request'):
            self.resources.enter_context(patch(name,side_effect=AssertionError('Live I/O forbidden')))

    def connect(self, sn='未写入 SN'):
        return self.logs.observe(True,identity=dict(node_id=1,sn=sn),config=self.config)

    def disconnect(self):
        self.logs.observe(False)
        self.tick += 2.1
        self.logs.observe(False)

    def test_health_evidence_and_counts_survive_recovery_and_connection_handoff(self):
        target = self.connect()
        self.logs.track_probe(target,'printer')
        for i in range(14):
            ready = i<10 or i==13
            self.logs.probe(target,'printer','READY' if ready else 'UNKNOWN','OK' if ready else 'TIMEOUT',
                elapsed=.5 if ready else 2, evidence={'port':'USB001'},error=None if ready else TimeoutError())
        self.logs.finish_connection('disconnect')
        self.logs.probe(target,'printer','READY','OK',elapsed=.1,evidence={})
        self.logs.track_probe(target,'printer',finished=True)
        text=target.path.read_text(encoding='utf-8')
        self.assertEqual(text.count('[devices.probe.changed]'),3)
        line=text.strip().splitlines()[-1]
        last=json.loads(line[line.index('{"schema_version"'):])
        self.assertEqual(last['counts'],dict(OK=12,TIMEOUT=3))
        self.assertEqual(last['max_elapsed_ms'],2000)
        self.assertEqual(last['max_consecutive_unready'],3)
        self.assertEqual(self.logs.probes,{})

    def test_export_only_current_boot_including_live_motor_stack_and_masked_fields(self):
        self.logs.station.event('other.boot','older run')
        other=DiagnosticLogs(self.folder,clock=lambda:self.now,monotonic=lambda:self.tick)
        self.addCleanup(other.close)
        target=other.observe(True,identity={'sn':'sample'},config=self.config)
        other.register_secret('sensitive-example')
        try: raise OSError('sensitive-example')
        except OSError as exc: target.event('sample.fault','multiple\nlines',level='ERROR',error=exc)
        self.logs.station.event('foreign.interleaved','different boot')
        other.station.event('later.event')
        (self.folder/'station.json').write_text('raw configuration',encoding='utf-8')
        out=self.folder/'现场诊断.zip'
        other.export_boot(out)
        with zipfile.ZipFile(out) as archive:
            self.assertEqual(set(archive.namelist()),{'诊断摘要.txt',other.station.reference,target.reference})
            content=''.join(archive.read(name).decode('utf-8-sig') for name in archive.namelist())
        for expected in ('sample.fault','Traceback','later.event',other.boot_id,'[REDACTED]'):
            self.assertIn(expected,content)
        for excluded in (self.logs.boot_id,'other.boot','foreign.interleaved','sensitive-example','raw configuration'):
            self.assertNotIn(excluded,content)

    def test_each_connection_including_an_untested_motor_has_one_file(self):
        first = self.connect()
        self.disconnect()
        second = self.connect()  # Same ID/SN still creates a distinct physical cycle.
        self.assertNotEqual(first.path,second.path)
        self.assertEqual(len(list((self.folder/'logs/production').glob('motor-*.log'))),2)
        text = first.path.read_text(encoding='utf-8')
        self.assertIn('connection.open',text)
        self.assertIn('disconnect_confirmed',text)
        self.assertNotIn('flow.start',text)

    def test_short_loss_and_id_sn_changes_do_not_split_a_connection(self):
        first = self.connect()
        self.logs.observe(False)
        self.tick += .7
        restored = self.logs.observe(True,identity=dict(node_id=3,sn=self.config.base_sn),config=self.config)
        self.assertEqual(restored.path,first.path)
        text = first.path.read_text(encoding='utf-8')
        self.assertIn('connection.restored',text)
        self.assertIn('connection.identity',text)
        self.assertNotIn('connection.close',text)

    def test_connection_file_stays_the_same_across_midnight_and_has_no_size_rotation(self):
        first = self.connect()
        self.now += 86400
        self.tick += 86400
        self.assertEqual(self.connect().path,first.path)
        self.logs.station.event('after_midnight')
        self.assertEqual(len(list((self.folder/'logs/production').glob('station-*.log'))),2)

    def test_retention_only_removes_expired_owned_logs_and_keeps_active_logs(self):
        active = self.connect()
        directory = active.path.parent
        expired = directory/('motor-20200101-000000-'+'a'*32+'.log')
        recent = directory/('motor-20200102-000000-'+'b'*32+'.log')
        unrelated = directory/'notes.log'
        report = self.folder/'data/case/report.png'
        report.parent.mkdir(parents=True)
        record = report.with_name('station.json')
        for path in (expired,recent,unrelated,report,record):
            path.write_text('evidence',encoding='utf-8')
        old = self.now-(RETENTION_DAYS+1)*86400
        for path in (active.path,expired,unrelated,report,record):
            os.utime(path,(old,old))
        os.utime(recent,(self.now-29*86400,self.now-29*86400))
        deleted = self.logs.cleanup(force=True)
        self.assertEqual(deleted,[expired.relative_to(self.logs.root).as_posix()])
        self.assertFalse(expired.exists())
        self.assertTrue(all(path.exists() for path in (active.path,recent,unrelated,report,record)))

    def test_late_background_result_goes_to_original_connection_not_next_motor(self):
        first = self.connect('first motor')
        directory = self.folder/'data/case'
        directory.mkdir(parents=True)
        (directory/'station.json').write_text(json.dumps(dict(connection_log=first.reference,
            session='case-session',request_id='same-request')),encoding='utf-8')
        self.disconnect()
        second = self.connect('second motor')
        self.logs.record_target(directory).event('delivery.receipt',report_id=123)
        self.assertIn('delivery.receipt',first.path.read_text(encoding='utf-8'))
        self.assertNotIn('delivery.receipt',second.path.read_text(encoding='utf-8'))
        old = self.now-31*86400
        os.utime(first.path,(old,old))
        self.logs.cleanup(force=True)
        self.assertFalse(first.path.exists())
        self.logs.record_target(directory).event('delivery.very_late_receipt',report_id=123)
        self.assertFalse(first.path.exists())
        self.assertIn('expired_connection_log',self.logs.station.path.read_text(encoding='utf-8'))

    def test_traceback_context_and_credentials_are_preserved_or_redacted(self):
        target = self.connect()
        secret = 'secret-content-1234'
        self.logs.register_secret(secret)
        try:
            try:
                raise KeyError('SDO timeout')
            except KeyError as cause:
                raise RuntimeError('X-Api-Key: '+secret) from cause
        except RuntimeError as exc:
            target.event('test.error',level='ERROR',error=exc,
                         credentials=dict(mes_api_key='another-secret',access_key_secret=secret),
                         endpoint='https://user:password@example.test/status?Signature=sign-secret&ok=1')
        text = target.path.read_text(encoding='utf-8')
        self.assertIn('Traceback',text)
        self.assertIn('KeyError',text)
        self.assertIn('RuntimeError',text)
        self.assertIn('SDO timeout',text)
        for value in (secret,'another-secret','sign-secret','user:password'):
            self.assertNotIn(value,text)

    def test_concurrent_writers_keep_complete_records(self):
        target = self.connect()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda index:target.event('parallel.event',index=index),range(80)))
        lines = [line for line in target.path.read_text(encoding='utf-8').splitlines() if '[parallel.event]' in line]
        self.assertEqual(len(lines),80)
        self.assertEqual({json.loads(line[line.index('{'):])['index'] for line in lines},set(range(80)))

    def test_retention_scan_does_not_hold_up_diagnostic_writers(self):
        target = self.connect()
        entered, release = threading.Event(), threading.Event()
        original_scan = os.scandir
        @contextmanager
        def delayed_scan(directory):
            entered.set()
            release.wait(2)
            with original_scan(directory) as entries:
                yield entries
        with patch('scripts.factory_test.diagnostics.os.scandir',side_effect=delayed_scan), \
             ThreadPoolExecutor(max_workers=2) as pool:
            cleaning = pool.submit(self.logs.cleanup,force=True)
            try:
                self.assertTrue(entered.wait(1))
                writing = pool.submit(target.event,'while.cleaning')
                writing.result(timeout=1)
            finally:
                release.set()
                cleaning.result(timeout=2)
        self.assertIn('while.cleaning',target.path.read_text(encoding='utf-8'))

    def test_storage_failure_is_reported_without_interrupting_cleanup(self):
        target = self.connect()
        self.logs.failure = Mock()
        with patch.object(Path,'open',side_effect=OSError('disk full')):
            target.event('failed.write')
            self.logs.finish_connection('stop_still_required')
        self.logs.failure.assert_called_once()
        self.assertIsNone(self.logs.active)

    def test_untrusted_or_corrupt_log_reference_cannot_redirect_writes(self):
        directory = self.folder/'data/bad-case'
        directory.mkdir(parents=True)
        record = directory/'station.json'
        outside = self.folder/'unrelated.log'
        for value in ([], dict(connection_log=None), dict(connection_log=str(outside))):
            record.write_text(json.dumps(value),encoding='utf-8')
            self.logs.record_target(directory).event('fallback.diagnostic')
        self.assertFalse(outside.exists())
        self.assertIn('fallback.diagnostic',self.logs.station.path.read_text(encoding='utf-8'))

    def test_controller_tracks_connections_before_worker_clicks_start(self):
        from scripts.factory_test.gui import FactoryWindow
        from scripts.factory_test.station import PresenceLatch
        host = FactoryWindow.__new__(FactoryWindow)
        host.diagnostics = self.logs
        host.config = replace(self.config,simulation=True)
        host.config_revision=host.mode_generation=0
        host.configuring=host.closing=False
        host.lock = threading.Lock()
        host.dependencies_ready = True
        host.page = 'production'
        host.control_page = SimpleNamespace(active=False)
        host.last_health = 0
        host.production_pause = threading.Event()
        host.health_states = {}
        host.device_health = Mock()
        host.device_health.check.return_value = {}
        host.latch = PresenceLatch()
        host.events = queue.Queue()
        host.resume_requested = host.start_requested = host.busy = False
        samples = iter((True,True,False,False,False,False,True,True))
        def wait(_):
            try:
                host.demo_present = next(samples)
                self.tick += .8
                return False
            except StopIteration:
                return True
        host.shutdown = Mock()
        host.shutdown.is_set.return_value = False
        host.shutdown.wait.side_effect = wait
        host.controller()
        logs = sorted((self.folder/'logs/preview').glob('motor-*.log'))
        self.assertEqual(len(logs),2)
        self.assertEqual(sum('disconnect_confirmed' in p.read_text(encoding='utf-8') for p in logs),1)
        self.assertTrue(all('flow.start' not in p.read_text(encoding='utf-8') for p in logs))
        probes = [event for kind,event in host.events.queue if kind=='motor_probe']
        self.assertEqual([value['motor_probe'] for value in probes], ['present','absent','present'])

    def test_early_probe_station_fault_and_recovery_publish_current_evidence(self):
        from scripts.factory_test.station import PresenceLatch
        from tests.test_factory_modes import window
        host=window()
        host.config=replace(self.config,simulation=True)
        host.diagnostics=self.logs
        host.latch=PresenceLatch()
        host.demo_present=True
        host.start_requested=host.resume_requested=False
        host.shutdown=Mock()
        host.shutdown.is_set.return_value=False
        host.shutdown.wait.side_effect=[False,False,False,True]
        seen_before_health=[]
        def health(*args,**kwargs):
            seen_before_health.append(host.events.queue[-1][0])
            if len(seen_before_health)<3: raise OSError('设备状态检查失败')
            return {}
        host.device_health.check.side_effect=health
        host.controller()
        self.assertEqual(seen_before_health[0],'motor_probe')
        outcomes=[item['motor_probe'] for kind,item in host.events.queue if kind=='motor_probe']
        self.assertEqual(outcomes,['present','station_error'])
        candidate=next(item for kind,item in host.events.queue if kind=='candidate')
        self.assertEqual(candidate['motor_probe'],'present')
        self.assertIn('stabilizing',candidate['blockers'])

    def test_start_clicked_after_probe_waits_for_next_identity_sample(self):
        from scripts.factory_test.station import PresenceLatch
        from tests.test_factory_modes import window
        host=window()
        host.config=replace(self.config,simulation=True)
        host.diagnostics=self.logs
        host.latch=PresenceLatch()
        host.start_requested=host.resume_requested=False
        host.shutdown=Mock()
        host.shutdown.is_set.return_value=False
        observations=iter((True,True,False))
        def wait(_):
            host.demo_present=next(observations,None)
            return host.demo_present is None
        host.shutdown.wait.side_effect=wait
        put=host.events.put
        def on_event(event):
            kind,value=event
            if kind=='candidate' and value['ready']:
                host.start_requested=True
                host.requested_identity=host.candidate_identity(value)
            put(event)
        host.events.put=on_event
        with patch('scripts.factory_test.gui.StationRunner') as runner:
            host.controller()
            runner.assert_not_called()
        self.assertFalse(host.start_requested)
        self.assertEqual([v['motor_probe'] for k,v in host.events.queue if k=='candidate'],['present','present','absent'])

    def test_unknown_motor_state_does_not_close_the_connection_log(self):
        target = self.connect()
        self.logs.observe(False)
        self.tick += 2.1
        self.logs.observe(None)
        self.tick += 3
        self.assertIs(self.logs.observe(False),target)
        self.tick += 2.1
        self.assertIsNone(self.logs.observe(False))

    def test_stdout_fragments_are_assembled_and_last_fragment_flushed(self):
        target = self.connect()
        events = queue.Queue()
        writer = EventWriter(events,target)
        writer.write('Native statistics: ')
        writer.write('{tx: 1000}\nlast fragment')
        writer.flush()
        text = target.path.read_text(encoding='utf-8')
        self.assertIn('Native statistics: {tx: 1000}',text)
        self.assertIn('last fragment',text)
        self.assertEqual(events.qsize(),2)

    def test_mes_retries_record_attempts_and_same_id_without_exposing_key(self):
        target = self.connect()
        config = replace(self.config,mes_url='https://example.test',production_date='261102')
        payload = mes_payload(config,'same-request')
        sn = 'C2KNEE0STDXC02212611020001'
        http = Mock(side_effect=[TimeoutError('network timeout'),dict(code=200,data=dict(
            requestId='same-request',count=1,sns=[sn]))])
        with patch.dict(os.environ,MIRRORME_MES_API_KEY='diagnostic-secret'), log_scope(target):
            self.assertEqual(MesClient(config,http,pause=Mock()).allocate(payload),sn)
        text = target.path.read_text(encoding='utf-8')
        self.assertIn('mes.allocate.retry',text)
        self.assertIn('same-request',text)
        self.assertIn('TimeoutError',text)
        self.assertNotIn('diagnostic-secret',text)

    def services(self, config):
        services = DemoServices(config,lambda:False,lambda *_:None)
        services.measure = Mock(return_value=dict(status='PASS',sections=[],stages={},demo=True,
            started_at='2026-11-02T12:00:00+08:00'))
        services.render = Mock()
        return services

    def test_flow_links_connection_before_sn_exists_and_keeps_all_steps(self):
        target = self.connect()
        config = replace(self.config,simulation=True)
        runner = StationRunner(config,root=self.folder,services=self.services(config),log_target=target)
        record = runner.begin('未写入 SN',detected_id=1)
        self.assertEqual(record['status'],'COMPLETE')
        self.assertEqual(record['connection_log'],target.reference)
        text = target.path.read_text(encoding='utf-8')
        for marker in ('flow.start','flow.step.start','flow.step.done','sn.allocated','flow.end',record['request_id']):
            self.assertIn(marker,text)
        self.assertNotIn('connection.close',text)  # Completion does not close the physical cycle.

    def test_failure_log_contains_original_stack_and_failed_stage(self):
        target = self.connect()
        config = replace(self.config,simulation=True)
        services = self.services(config)
        services.calibrate = Mock(side_effect=RuntimeError('driver error 0x4001'))
        record = StationRunner(config,root=self.folder,services=services,log_target=target).begin('未写入 SN',1)
        self.assertEqual(record['status'],'FAILED')
        text = target.path.read_text(encoding='utf-8')
        self.assertIn('flow.failed',text)
        self.assertIn('Traceback',text)
        self.assertIn('calibrate',text)
        self.assertIn('driver error 0x4001',text)

    def test_error_codes_keep_wrapped_network_reason_and_do_not_guess_unknown_causes(self):
        self.assertEqual(error_details(URLError(ConnectionRefusedError(errno.ECONNREFUSED,'refused')))['code'],
                         'CONNECTION_REFUSED')
        self.assertEqual(error_details(OSError(errno.ENOSPC,'disk full'))['code'],'DISK_FULL')
        self.assertEqual(error_details(OSError(errno.EIO,'I/O error'))['code'],'UNEXPECTED_EXCEPTION')
        self.assertEqual(error_details(RuntimeError('new firmware failure'))['code'],'UNEXPECTED_EXCEPTION')

    def response(self, body, status=200):
        value = io.BytesIO(body)
        value.status = status
        value.headers = {'Content-Type':'application/json','X-Request-Id':'server-request-123',
                         'Set-Cookie':'private-cookie'}
        return value

    def test_http_records_response_headers_body_timing_and_redacts_credentials(self):
        target = self.connect()
        self.logs.register_secret('credential-123')
        response = self.response(b'{"data":{"online":true},"token":"private-token"}')
        opener = Mock()
        opener.open.return_value = response
        with patch('scripts.factory_test.integrations.build_opener',return_value=opener), log_scope(target):
            self.assertTrue(json_http('http://127.0.0.1:9876/api/status',
                headers={'X-Api-Key':'credential-123'})['data']['online'])
        text = target.path.read_text(encoding='utf-8')
        for marker in ('http_id','server-request-123','response_bytes','elapsed_s','bypassed_localhost'):
            self.assertIn(marker,text)
        for secret in ('credential-123','private-token','private-cookie'):
            self.assertNotIn(secret,text)

    def test_http_200_non_json_and_http_503_preserve_original_response_evidence(self):
        target = self.connect()
        for response in (self.response(b'<html>wrong-api-route</html>'),
                HTTPError('http://127.0.0.1:9876/api/status',503,'unavailable',
                          {'Content-Type':'text/html','X-Request-ID':'upstream-503'},
                          io.BytesIO(b'printer-service-starting'))):
            opener = Mock()
            if isinstance(response,Exception):
                opener.open.side_effect=response
            else:
                opener.open.return_value=response
            with patch('scripts.factory_test.integrations.build_opener',return_value=opener), log_scope(target):
                with self.assertRaises((IntegrationError,HTTPError)):
                    json_http('http://127.0.0.1:9876/api/status')
        text = target.path.read_text(encoding='utf-8')
        for marker in ('HTTP_INVALID_JSON','wrong-api-route','printer-service-starting','upstream-503','503'):
            self.assertIn(marker,text)

    def test_http_large_response_is_explicitly_truncated_after_redaction(self):
        target = self.connect()
        self.logs.register_secret('boundary-secret-value')
        with log_scope(target):
            evidence = response_evidence(('x'*16380+'boundary-secret-value'+'y'*100).encode())
        self.assertTrue(evidence['response_truncated'])
        self.assertEqual(len(evidence['response_preview']),16384)
        self.assertNotIn('boundary',evidence['response_preview'])

    def test_malformed_print_receipts_are_all_unknown_without_a_second_submission(self):
        target = self.connect()
        number = 'C2KNEE0STDXC02212611020001'
        for reply in (None, [], dict(data=None), dict(data=dict(results='bad')),
                      dict(data=dict(results=[None])),
                      dict(data=dict(results=[dict(sn=number,success=1)])),
                      dict(data=dict(results=[dict(sn='other',success=True)])),
                      dict(success=True,data=dict(total=2,results=[dict(sn=number,success=True)])),
                      dict(data=dict(results=[dict(sn=number,success=True,copies=2)]))):
            http = Mock(return_value=reply)
            with log_scope(target), self.assertRaises(PrintUnknown) as error:
                PrinterClient(self.config,http).print_sn(number)
            self.assertIn(error.exception.diagnostic_code,('PRINT_ACK_INVALID','PRINT_ACK_CONFLICT'))
            http.assert_called_once()
        text = target.path.read_text(encoding='utf-8')
        self.assertIn('print_job_id',text)
        self.assertIn('PRINT_ACK_INVALID',text)
        self.assertNotIn('printer.confirmed',text)

    def test_printer_offline_is_distinguished_from_unreachable_and_a_failed_print_item(self):
        target = self.connect()
        http = Mock(return_value=dict(data=dict(online=False,message='USB disconnected')))
        with log_scope(target),self.assertRaises(IntegrationError) as error:
            PrinterClient(self.config,http).ready()
        self.assertEqual(error.exception.diagnostic_code,'PRINTER_OFFLINE')
        self.assertIn('USB disconnected',str(error.exception))
        text = target.path.read_text(encoding='utf-8')
        self.assertIn('USB disconnected',text)
        self.assertIn('print_submitted',text)
        self.assertNotIn('printer.submit',text)

    def test_printer_refusal_timeout_and_missing_route_have_distinct_actions_and_keep_causes(self):
        target = self.connect()
        failures = ((URLError(ConnectionRefusedError(errno.ECONNREFUSED,'refused')),
                     'PRINTER_SERVICE_UNREACHABLE','本机打印服务'),
                    (URLError(TimeoutError('status slow while printing')),
                     'PRINTER_STATUS_TIMEOUT','查询超时'),
                    (HTTPError('http://127.0.0.1:9876/missing',404,'not found',{},None),
                     'PRINTER_STATUS_HTTP','HTTP 404'))
        for original,code,message in failures:
            http=Mock(side_effect=original)
            with self.subTest(code=code),log_scope(target),self.assertRaises(IntegrationError) as failure:
                PrinterClient(self.config,http).ready()
            self.assertEqual(failure.exception.diagnostic_code,code)
            self.assertIn(message,str(failure.exception))
            self.assertIs(failure.exception.__cause__,original)
            self.assertEqual(http.call_count,2 if isinstance(original,HTTPError) and original.code==404 else 1)
            self.assertFalse(failure.exception.diagnostic_context['print_submitted'])
        text=target.path.read_text(encoding='utf-8')
        self.assertIn('status slow while printing',text)
        self.assertIn('CONNECTION_REFUSED',text)
        self.assertIn('PRINTER_STATUS_HTTP',text)
        self.assertNotIn('printer.submit',text)

    def test_printer_queue_mismatch_and_explicit_failure_do_not_mark_ready(self):
        target=self.connect()
        for reply,code in ((dict(data=dict(online=True,printerName='Microsoft Print to PDF')),'PRINTER_TARGET_MISMATCH'),
                           (dict(success=False,data=dict(online=True)),'PRINTER_STATUS_REJECTED'),
                           (dict(data=dict(online='true')),'PRINTER_STATUS_INVALID')):
            http=Mock(return_value=reply)
            with self.subTest(code=code),log_scope(target),self.assertRaises(IntegrationError) as failure:
                PrinterClient(self.config,http).ready()
            self.assertEqual(failure.exception.diagnostic_code,code)
            http.assert_called_once()
        text=target.path.read_text(encoding='utf-8')
        self.assertNotIn('printer.status.ready',text)

    def test_printer_minimal_status_remains_compatible_without_optional_queue_metadata(self):
        for data in (dict(online=True),dict(online=True,printerName=self.config.printer_name.lower())):
            http=Mock(return_value=dict(code=200,data=data))
            self.assertTrue(PrinterClient(self.config,http).ready()['data']['online'])

    def test_sdo_timeout_preserves_address_raw_request_and_uncertain_state(self):
        from scripts.factory_test.maintenance import Maintenance
        target = self.connect()
        bus = SimpleNamespace(_lock=threading.RLock(),_check=Mock(),_sdo_uncertain=False,
            transport=SimpleNamespace(send=Mock()),_wait_sdo=Mock(side_effect=TimeoutError('no SDO ACK')))
        with log_scope(target), self.assertRaises(TimeoutError):
            Maintenance.read(SimpleNamespace(_bus=bus),3,0x2000,2)
        self.assertTrue(bus._sdo_uncertain)
        bus.transport.send.assert_called_once()
        text = target.path.read_text(encoding='utf-8')
        for marker in ('sdo.read.failed','0x2000','0x603','4000200000000000','no SDO ACK','timeout_s'):
            self.assertIn(marker,text)

    def test_ui_still_receives_original_failure_when_saving_failed_state_also_fails(self):
        from scripts.factory_test import station
        target = self.connect()
        config = replace(self.config,simulation=True)
        services = self.services(config)
        services.calibrate = Mock(side_effect=RuntimeError('original calibration problem'))
        original = station.atomic_json
        def save(path,value):
            if Path(path).name=='station.json' and value.get('status')=='FAILED':
                raise OSError(errno.ENOSPC,'no space to save failed state')
            return original(path,value)
        emit = Mock()
        with patch.object(station,'atomic_json',side_effect=save):
            record = StationRunner(config,root=self.folder,services=services,log_target=target,emit=emit).begin('blank',1)
        self.assertEqual(record['status'],'FAILED')
        value = next(call.args[1] for call in emit.call_args_list if call.args[0]=='failed')
        self.assertIn('original calibration problem',value['message'])
        self.assertEqual(value['log_path'],str(target.path))
        self.assertTrue(any('状态保存失败' in note for note in value['notes']))
        text = target.path.read_text(encoding='utf-8')
        for marker in ('flow.step.failed','flow.failure_record.failed','DISK_FULL','original calibration problem'):
            self.assertIn(marker,text)

    def test_report_http_failure_keeps_non_json_body_and_server_request_id(self):
        from scripts.factory_test.upload import Credentials, MesClient as ReportMes, UploadError
        import requests
        target = self.connect()
        response = requests.Response()
        response.status_code = 503
        response._content = b'<html>upstream-report-not-ready</html>'
        response.headers.update({'X-Request-Id':'report-upstream-id','Content-Type':'text/html'})
        with patch('requests.post',return_value=response), log_scope(target):
            with self.assertRaises(UploadError) as error:
                ReportMes(Credentials('test-id','test-secret','hidden-report-api'),
                          'https://example.test/open-api/test-report').register({'reportKey':'original-key'})
        self.assertEqual(error.exception.diagnostic_code,'REPORT_MES_HTTP')
        text = target.path.read_text(encoding='utf-8')
        self.assertIn('upstream-report-not-ready',text)
        self.assertIn('report-upstream-id',text)
        self.assertNotIn('hidden-report-api',text)

    def test_window_initialization_failure_gets_a_station_log_before_ui_exists(self):
        from scripts.factory_test import desktop_entry
        with patch('sys.argv',['factory-app']), \
             patch('scripts.factory_test.station_config.state_root',return_value=self.folder/'early-startup'), \
             patch('scripts.factory_test.gui.main',side_effect=RuntimeError('Tk startup unavailable')):
            with self.assertRaises(RuntimeError) as error:
                desktop_entry.main()
        files = list((self.folder/'early-startup/logs/production').glob('station-*.log'))
        self.assertEqual(len(files),1)
        text = files[0].read_text(encoding='utf-8')
        self.assertIn('application.startup.failed',text)
        self.assertIn('Tk startup unavailable',text)
        self.assertIn(str(files[0]),error.exception.__notes__[0])


if __name__ == '__main__':
    unittest.main()
