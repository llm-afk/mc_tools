from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.factory_test.diagnostics import DiagnosticLogs, log_scope
from scripts.factory_test.integrations import MesClient, PrinterClient, mes_payload
from scripts.factory_test.service_credentials import CredentialStore, CredentialStoreError, FIELDS, values_for
from scripts.factory_test.station_config import StationConfig, load_config, save_config
from scripts.factory_test.upload import (Credentials, ReportUploader, UploadError, credential_provider,
                                        storage_target, validate_endpoint, save_json, station_report_key)


KEYS = dict(sn_api_key='onsite-test-sn-key',mes_api_key='onsite-test-report-key',
            access_key_id='onsite-test-access-id',access_key_secret='onsite-test-access-secret')
SN='C2KNEE0STDXC02212611060001'
ENDPOINT='https://report.example.test/custom/report/register'


class ServiceSettingsTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        scratch=Path(__file__).resolve().parents[1]/'build/factory-services/settings-test-tmp'
        scratch.mkdir(parents=True,exist_ok=True)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=scratch)))
        self.path=self.root/'station.json'
        self.config=StationConfig(credentials_file=str(self.path),mes_url='https://sn.example.test/base',
            production_date='261106',mes_sn_path='/custom/sn',printer_status_path='/health',printer_print_path='/labels')
        for name in ('motor_platform.Canable.detect','motor_platform.Canable.open','serial.Serial',
                     'urllib.request.OpenerDirector.open','requests.sessions.Session.request','socket.socket.connect'):
            self.stack.enter_context(patch(name,side_effect=AssertionError('Live I/O forbidden')))

    def save(self,keys=KEYS):
        save_config(self.config,self.path,credentials=keys)

    def test_windows_user_encryption_roundtrip_and_public_changes_keep_credentials(self):
        self.save()
        raw=self.path.read_text(encoding='utf-8')
        self.assertTrue(all(value not in raw for value in KEYS.values()))
        self.assertEqual(values_for(load_config(self.path)),KEYS)
        blob=json.loads(raw)['protected_credentials']
        updated=replace(self.config,mes_url='https://replacement.example.test/base')
        save_config(updated,self.path)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['protected_credentials'],blob)
        self.assertEqual(CredentialStore(self.path).load(),KEYS)
        self.assertEqual(load_config(self.path).mes_url,updated.mes_url)

    def test_failed_encryption_does_not_partially_commit_new_address(self):
        self.save(); original=self.path.read_bytes()
        with patch('scripts.factory_test.service_credentials._crypt',side_effect=OSError('protect failed')):
            with self.assertRaises(OSError):
                save_config(replace(self.config,mes_url='https://new.example.test'),self.path,credentials=KEYS)
        self.assertEqual(self.path.read_bytes(),original)

    def test_corrupt_secret_can_be_reentered_in_gui_without_losing_public_settings(self):
        self.save()
        raw=json.loads(self.path.read_text(encoding='utf-8')); raw['protected_credentials']='corrupt'
        self.path.write_text(json.dumps(raw),encoding='utf-8')
        with self.assertRaises(CredentialStoreError): values_for(self.config)
        self.save()
        self.assertEqual(values_for(self.config),KEYS)
        self.assertEqual(load_config(self.path).hardware_version,'0221')

    def test_inline_sn_key_and_custom_path_are_used_without_environment_changes(self):
        self.save(); payload=mes_payload(self.config,'station-request')
        http=Mock(return_value=dict(code=200,data=dict(requestId='station-request',count=1,sns=[SN])))
        with patch.dict(os.environ,MIRRORME_MES_API_KEY='old-environment-key'):
            self.assertEqual(MesClient(self.config,http).allocate(payload),SN)
        self.assertEqual(http.call_args.args[0],'https://sn.example.test/base/custom/sn')
        self.assertEqual(http.call_args.kwargs['headers']['X-Api-Key'],KEYS['sn_api_key'])
        self.assertEqual(http.call_args.kwargs['timeout'],10)

    def test_printer_paths_change_while_timeouts_and_single_label_contract_stay_default(self):
        http=Mock(side_effect=[dict(data=dict(online=True)),dict(data=dict(results=[dict(sn=SN,success=True)]))])
        client=PrinterClient(self.config,http)
        client.ready(); client.print_sn(SN)
        self.assertEqual(http.call_args_list[0].args[0],'http://127.0.0.1:9876/health')
        self.assertEqual(http.call_args_list[1].args[0],'http://127.0.0.1:9876/labels')
        self.assertEqual(http.call_args_list[1].args[1],dict(items=[dict(sn=SN)],copies=1))
        self.assertEqual([c.kwargs['timeout'] for c in http.call_args_list],[5,10])

    def test_report_credentials_are_frozen_for_old_worker_and_refresh_for_new_worker(self):
        self.save()
        provider=credential_provider(self.config,self.root/'missing.json')
        updated={k:'new-'+v for k,v in KEYS.items()}
        self.save(updated)
        self.assertEqual(provider().mes_api_key,KEYS['mes_api_key'])
        self.assertEqual(credential_provider(self.config,self.root/'missing.json')().mes_api_key,updated['mes_api_key'])

    def test_legacy_credentials_file_can_fill_only_missing_inline_fields(self):
        self.save(dict.fromkeys(FIELDS,'')|dict(mes_api_key=KEYS['mes_api_key']))
        legacy=self.root/'upload-config.json'
        legacy.write_text(json.dumps(dict(access_key_id='legacy-id',access_key_secret='legacy-secret',mes_api_key='legacy-api')))
        credentials=credential_provider(self.config,legacy)()
        self.assertEqual((credentials.access_key_id,credentials.mes_api_key),('legacy-id',KEYS['mes_api_key']))

    def report(self):
        directory=self.root/('20261106_120000_'+SN)
        directory.mkdir()
        for name in ('report.png','kt_curve.csv','curve.csv'):
            (directory/name).write_bytes(b'simulation-only')
        return directory

    def test_oss_target_is_pinned_before_upload_and_cannot_mix_two_buckets(self):
        directory=self.report()
        store,mes=Mock(),Mock()
        store.put.side_effect=[dict(etag='first'),UploadError('injected failure',retryable=True)]
        first=ReportUploader(store=store,mes=mes,endpoint=ENDPOINT,max_attempts=1,
            oss_endpoint='https://storage.example.test',oss_bucket='bucket-a',oss_region='region-a')
        with self.assertRaises(UploadError): first(directory)
        state=json.loads((directory/'upload-state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['oss_target'],storage_target('https://storage.example.test','bucket-a','region-a'))
        store.reset_mock(side_effect=True)
        changed=ReportUploader(store=store,mes=mes,endpoint=ENDPOINT,max_attempts=1,
            oss_endpoint='https://storage.example.test',oss_bucket='bucket-b',oss_region='region-a')
        with self.assertRaises(UploadError) as error: changed(directory)
        self.assertEqual(error.exception.diagnostic_code,'OSS_DESTINATION_MISMATCH')
        store.put.assert_not_called(); mes.register.assert_not_called()

    def test_report_full_custom_path_is_accepted_but_credentials_in_url_are_rejected(self):
        self.assertEqual(validate_endpoint(ENDPOINT),ENDPOINT)
        for invalid in ('https://example.test','https://user:password@example.test/report','https://example.test/report?key=abc'):
            with self.assertRaises(UploadError): validate_endpoint(invalid)

    def test_inline_keys_stay_out_of_public_config_snapshots_and_logs(self):
        self.save()
        logs=DiagnosticLogs(self.root/'audit',simulation=True)
        self.addCleanup(logs.close)
        payload=mes_payload(self.config,'logged-request')
        http=Mock(return_value=dict(code=500,msg='echo '+KEYS['sn_api_key']))
        with log_scope(logs.station), self.assertRaises(Exception):
            MesClient(self.config,http).allocate(payload)
        text=logs.station.path.read_text(encoding='utf-8')
        self.assertNotIn(KEYS['sn_api_key'],text)
        self.assertIn('[REDACTED]',text)

    def test_full_delivery_uses_gui_keys_custom_MES_path_and_configured_OSS_target(self):
        import uuid
        from scripts.factory_test.delivery import ReportDelivery
        self.config=replace(self.config,report_upload_url=ENDPOINT,report_api_key_header='X-Onsite-Report-Key',
            oss_endpoint='https://storage.example.test',oss_bucket='onsite-bucket',oss_region='onsite-region',
            oss_prefix='factory-a/reports')
        self.save()
        session=str(uuid.uuid4())
        directory=self.root/'data'/session
        directory.mkdir(parents=True)
        summary=dict(station_version=2,station_session=session,station_ready=True,status='PASS',demo=False,
                     motor_sn=SN,module_sn=SN,started_at='2026-11-06T12:00:00+08:00')
        summary['report_key']=station_report_key(summary,session)
        save_json(directory/'summary.json',summary)
        for name in ('report.png','kt_curve.csv','curve.csv'): (directory/name).write_bytes(b'offline-fixture')
        bucket=Mock()
        bucket.put_object.return_value=Mock(status=200,etag='offline-etag',request_id='offline-request')
        response=Mock(status_code=200,content=b'{}',headers={})
        response.json.return_value=dict(code=200,data=dict(reportKey=summary['report_key'],reportId=123))
        with patch('oss2.AuthV4',return_value=object()) as auth, \
             patch('oss2.Bucket',return_value=bucket) as factory,patch('requests.post',return_value=response) as post:
            delivery=ReportDelivery(self.root,self.config)
            delivery.enqueue(directory); delivery.step()
        auth.assert_called_once_with(KEYS['access_key_id'],KEYS['access_key_secret'])
        self.assertEqual(factory.call_args.args[1:],('https://storage.example.test','onsite-bucket'))
        self.assertEqual(factory.call_args.kwargs['region'],'onsite-region')
        self.assertEqual(bucket.put_object.call_count,3)
        self.assertEqual(post.call_args.args[0],ENDPOINT)
        self.assertEqual(post.call_args.kwargs['headers']['X-Onsite-Report-Key'],KEYS['mes_api_key'])
        self.assertEqual(post.call_args.kwargs['json']['sn'],SN)
        self.assertTrue(all(key.startswith('factory-a/reports/') for key in post.call_args.kwargs['json']['files']))
        state=json.loads((directory/'upload-state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'],'SUCCEEDED')
        self.assertEqual(state['receipt']['bucket'],'onsite-bucket')

    def test_clearing_inline_keys_returns_to_legacy_fallback(self):
        self.save(); self.save(dict.fromkeys(FIELDS,''))
        self.assertFalse(any(values_for(self.config).values()))
        payload=mes_payload(self.config,'fallback-request')
        http=Mock(return_value=dict(code=200,data=dict(requestId='fallback-request',count=1,sns=[SN])))
        with patch.dict(os.environ,MIRRORME_MES_API_KEY='legacy-sn-environment-key'):
            self.assertEqual(MesClient(self.config,http).allocate(payload),SN)
        self.assertEqual(http.call_args.kwargs['headers']['X-Api-Key'],'legacy-sn-environment-key')

    def test_preexisting_OSS_receipt_without_route_keeps_original_default_binding(self):
        directory=self.report()
        store,mes=Mock(),Mock()
        store.put.return_value=dict(etag='old')
        mes.register.side_effect=lambda body:dict(reportId=1,reportKey=body['reportKey'])
        uploader=ReportUploader(store=store,mes=mes,endpoint=ENDPOINT)
        receipt=uploader(directory)
        path=directory/'upload-state.json'
        state=json.loads(path.read_text(encoding='utf-8')); state.pop('oss_target'); save_json(path,state)
        store.reset_mock(); mes.reset_mock()
        self.assertEqual(uploader(directory),receipt)
        changed=ReportUploader(store=store,mes=mes,endpoint=ENDPOINT,oss_bucket='new-bucket')
        with self.assertRaises(UploadError): changed(directory)
        store.put.assert_not_called(); mes.register.assert_not_called()

    def test_custom_sn_auth_header_is_used_and_key_is_not_logged(self):
        self.config=replace(self.config,sn_api_key_header='X-Onsite-SN-Key')
        self.save()
        http=Mock(return_value=dict(code=200,data=dict(requestId='header-request',count=1,sns=[SN])))
        payload=mes_payload(self.config,'header-request')
        logs=DiagnosticLogs(self.root/'headers',simulation=True); self.addCleanup(logs.close)
        with log_scope(logs.station): MesClient(self.config,http).allocate(payload)
        self.assertEqual(http.call_args.kwargs['headers'],{'X-Onsite-SN-Key':KEYS['sn_api_key']})
        self.assertNotIn(KEYS['sn_api_key'],logs.station.path.read_text(encoding='utf-8'))

    def test_print_multiple_copies_checks_item_count_and_declared_copies(self):
        config=replace(self.config,printer_copies=5)
        http=Mock(return_value=dict(success=True,data=dict(total=5,success=5,
            results=[dict(sn=SN,success=True,copies=5)])))
        PrinterClient(config,http).print_sn(SN)
        self.assertEqual(http.call_args.args[1]['copies'],5)
        self.assertGreaterEqual(http.call_args.kwargs['timeout'],15)
        http.return_value['data']['results'][0]['copies']=1
        from scripts.factory_test.integrations import PrintUnknown
        with self.assertRaises(PrintUnknown): PrinterClient(config,http).print_sn(SN)

    def test_legacy_three_field_storage_route_and_default_prefix_remain_compatible(self):
        directory=self.report()
        store,mes=Mock(),Mock()
        store.put.return_value=dict(etag='old'); mes.register.side_effect=lambda body:dict(reportId=1,reportKey=body['reportKey'])
        uploader=ReportUploader(store=store,mes=mes,endpoint=ENDPOINT)
        receipt=uploader(directory)
        path=directory/'upload-state.json'; state=json.loads(path.read_text(encoding='utf-8'))
        state['oss_target'].pop('prefix'); save_json(path,state)
        self.assertEqual(uploader(directory),receipt)

    def test_only_wire_format_checks_are_added_for_headers_prefix_and_copies(self):
        for changes in (dict(sn_api_key_header='bad\nheader'),dict(report_api_key_header='Content-Length'),
                        dict(oss_prefix='../another'),dict(printer_copies=0)):
            with self.assertRaises(ValueError): replace(self.config,**changes).validate()
        replace(self.config,oss_prefix='',printer_copies=51).validate()

    def test_old_workpiece_can_resume_with_new_default_service_fields_and_local_key_location(self):
        from scripts.factory_test.station import StationRunner, DemoServices
        from scripts.factory_test.integrations import PendingConfiguration
        config=StationConfig(simulation=True)
        services=DemoServices(config,lambda:False,lambda *_:None)
        services.measure=Mock(return_value=dict(status='PASS',sections=[],stages={},demo=True,
            started_at='2026-11-06T12:00:00+08:00'))
        services.render=Mock()
        services.allocate=Mock(side_effect=PendingConfiguration('offline fixture pause'))
        runner=StationRunner(config,root=self.root,services=services)
        record=runner.begin('DEMO-UNPROGRAMMED',1)
        self.assertEqual(record['steps']['allocate'],'PENDING')
        added={'credentials_file','mes_sn_path','printer_status_path','printer_print_path','oss_endpoint',
               'oss_bucket','oss_region','oss_prefix','sn_api_key_header','report_api_key_header','printer_copies'}
        record['config']={k:v for k,v in record['config'].items() if k not in added}
        save_json(Path(record['directory'])/'station.json',record)
        services.allocate=Mock(return_value=SN)
        restored=StationRunner(replace(config,credentials_file=str(self.path)),root=self.root,services=services).resume()
        self.assertEqual(restored['status'],'COMPLETE')
        self.assertEqual(restored['request_id'],record['request_id'])
