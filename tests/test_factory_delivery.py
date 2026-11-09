import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import Mock
from types import MethodType

from scripts.factory_test.delivery import ReportDelivery
from scripts.factory_test.preview import create_demo
from scripts.factory_test.station_config import StationConfig
from scripts.factory_test.station import DemoServices, Services, StationRunner
from scripts.factory_test.upload import ReportUploader, UploadError, UploadPlan, save_json, station_report_key


SN = 'C2KNEE0STDXC02212611020001'
ENDPOINT = 'https://mes.example.test/open-api/test-report'


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = StationConfig(report_upload_url=ENDPOINT, upload_config=str(self.root/'missing.json'))
        self.store = Mock()
        self.store.put.return_value = dict(etag='test', request_id='test')
        self.mes = Mock()
        self.mes.register.side_effect = lambda body: dict(reportId=100, reportKey=body['reportKey'], duplicated=False)

    def report(self, ready=True, demo=False):
        session = str(uuid.uuid4())
        directory = self.root/'data'/session
        directory.mkdir(parents=True)
        summary = create_demo(directory)
        summary.update(demo=demo, motor_sn=SN, module_sn=SN, status='PASS',
                       station_version=2, station_session=session, station_ready=ready)
        summary['report_key'] = station_report_key(summary, session)
        save_json(directory/'summary.json', summary)
        (directory/'report.png').write_bytes(b'test-only-png')
        return directory, summary

    def delivery(self, config=None):
        config = config or self.config
        uploader = ReportUploader(store=self.store, mes=self.mes, endpoint=config.report_upload_url, max_attempts=1)
        delivery = ReportDelivery(self.root, config, uploader=uploader)
        if delivery.queue:
            delivery.queue.clock = lambda: 10000
        return delivery

    def test_uuid_folder_maps_to_stable_final_sn_contract_without_moving_data(self):
        directory, summary = self.report()
        plan = UploadPlan.from_directory(directory)
        self.assertEqual(plan.request['sn'], SN)
        self.assertEqual(plan.request['reportKey'], summary['report_key'])
        self.assertEqual(plan.request['fileCount'], 3)
        self.assertNotIn('-', plan.request['reportKey'])
        self.assertIn('/'+SN+'/'+summary['report_key']+'/', plan.files[0].key)
        self.assertTrue(directory.exists())

    def test_missing_address_is_durably_pending_without_guessed_environment(self):
        directory, _ = self.report()
        config = StationConfig(report_upload_url='', upload_config=str(self.root/'missing.json'))
        delivery = ReportDelivery(self.root, config)
        self.assertIsNone(delivery.queue)
        self.assertEqual(delivery.enqueue(directory)['status'], 'BLOCKED_CONFIGURATION')
        intent = json.loads((directory/'upload-pending.json').read_text(encoding='utf-8'))
        self.assertEqual(len(intent['sha256']), 3)
        self.assertEqual(intent['request']['sn'], SN)
        self.store.put.assert_not_called()
        self.mes.register.assert_not_called()

    def test_enqueue_has_no_network_and_background_saves_matching_receipt(self):
        directory, _ = self.report()
        delivery = self.delivery()
        self.assertEqual(delivery.enqueue(directory)['status'], 'PENDING')
        self.store.put.assert_not_called()
        delivery.step()
        self.assertEqual(self.store.put.call_count, 3)
        self.mes.register.assert_called_once()
        state = json.loads((directory/'upload-state.json').read_text(encoding='utf-8'))
        summary = json.loads((directory/'summary.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'], 'SUCCEEDED')
        self.assertEqual(summary['upload']['receipt']['reportId'], 100)
        intent = json.loads((directory/'upload-pending.json').read_text(encoding='utf-8'))
        self.assertEqual(intent['status'], 'SUCCEEDED')
        self.assertEqual(state['receipt']['reportKey'], summary['report_key'])

    def test_restart_after_partial_oss_failure_only_sends_remaining_files(self):
        directory, _ = self.report()
        delivery = self.delivery()
        delivery.queue.clock = lambda: 100
        delivery.enqueue(directory)
        self.store.put.side_effect = [dict(etag='first'), UploadError('network lost', retryable=True)]
        delivery.step()
        self.mes.register.assert_not_called()
        self.store.put.reset_mock(side_effect=True)
        restarted = self.delivery()
        restarted.step()
        self.assertEqual(self.store.put.call_count, 2)
        self.assertTrue(all(not call.args[0].key.endswith('/report.png') for call in self.store.put.call_args_list))
        self.mes.register.assert_called_once()

    def test_lost_mes_response_reuses_key_without_reuploading_oss(self):
        directory, _ = self.report()
        delivery = self.delivery()
        delivery.queue.clock = lambda: 100
        delivery.enqueue(directory)
        self.mes.register.side_effect = UploadError('response lost', retryable=True)
        delivery.step()
        original = self.mes.register.call_args.args[0]
        self.store.put.reset_mock()
        self.mes.register.side_effect = lambda body: dict(reportId=101, reportKey=body['reportKey'], duplicated=True)
        self.delivery().step()
        self.store.put.assert_not_called()
        self.assertEqual(self.mes.register.call_args.args[0], original)
        self.assertTrue(json.loads((directory/'upload-state.json').read_text())['receipt']['duplicated'])

    def test_simulated_and_not_finalized_reports_are_never_discovered(self):
        for ready, demo in ((False, False), (True, True)):
            directory, _ = self.report(ready=ready, demo=demo)
            delivery = self.delivery()
            delivery.step()
            self.assertFalse((directory/'upload-queue.json').exists())
            with self.assertRaises(UploadError):
                delivery.enqueue(directory)
        self.store.put.assert_not_called()

    def test_sealed_report_cannot_change_while_configuration_is_pending(self):
        directory, _ = self.report()
        ReportDelivery(self.root, StationConfig(report_upload_url='', upload_config=str(self.root/'missing.json'))).enqueue(directory)
        (directory/'report.png').write_bytes(b'changed')
        with self.assertRaisesRegex(UploadError, '改变'):
            self.delivery().enqueue(directory)
        self.store.put.assert_not_called()

    def test_receipt_from_other_environment_is_not_reused_or_migrated(self):
        directory, _ = self.report()
        self.delivery().enqueue(directory)
        config = StationConfig(report_upload_url='https://other.example.test/open-api/test-report',
                               upload_config=str(self.root/'missing.json'))
        self.delivery(config).step()
        self.store.put.assert_not_called()
        state = json.loads((directory/'upload-state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['endpoint'], ENDPOINT)

    def test_config_file_supplies_original_address_without_logging_credentials(self):
        path = self.root/'upload-config.json'
        save_json(path, dict(mes_url=ENDPOINT, access_key_id='private-id', access_key_secret='private-secret', mes_api_key='key'))
        delivery = ReportDelivery(self.root, StationConfig(report_upload_url='', upload_config=str(path)))
        self.assertEqual(delivery.queue.endpoint, ENDPOINT)
        self.assertEqual(delivery.queue.uploader.config, path)

    def test_real_workflow_submits_locally_even_when_upload_configuration_is_missing(self):
        config = StationConfig(mes_url='https://sn.example.test', report_upload_url='',
                               upload_config=str(self.root/'missing.json'))
        delivery = ReportDelivery(self.root, config)
        services = DemoServices(config, lambda:False, lambda *_:None, delivery=delivery)
        measure = services.measure
        def synthetic_measure(*args):
            summary = measure(*args)
            summary['demo'] = False
            return summary
        services.measure = synthetic_measure
        services.allocate = Mock(return_value=SN)
        services.render = lambda directory, summary:(directory/'report.png').write_bytes(b'test-only-png')
        services.upload = MethodType(Services.upload, services)
        record = StationRunner(config, root=self.root, services=services).begin('test-only-incoming', 1)
        self.assertEqual(record['status'], 'COMPLETE')
        self.assertEqual(record['summary']['status'], 'PASS')
        self.assertEqual(record['report_delivery']['status'], 'BLOCKED_CONFIGURATION')
        self.assertTrue((Path(record['directory'])/'upload-pending.json').exists())

    def test_local_archiving_failure_is_not_hidden_as_network_pending(self):
        config = StationConfig(simulation=True)
        services = DemoServices(config, lambda:False, lambda *_:None)
        services.render = Mock()
        services.upload = Mock(side_effect=OSError('disk full'))
        record = StationRunner(config, root=self.root, services=services).begin('test-only-incoming', 1)
        self.assertEqual(record['status'], 'FAILED')
        self.assertEqual(record['steps']['upload'], 'FAILED')

