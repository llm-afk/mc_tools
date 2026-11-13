import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.factory_test.upload import (Credentials, MES_URL, MesClient, ReportUploader,
                                        UploadError, UploadPlan)


SN = '00000000000001100000000000'
KEY = f'20260915_205127_{SN}'


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)/KEY
        (self.directory/'kt').mkdir(parents=True)
        (self.directory/'tn').mkdir()
        for name, content in (('report.png', b'png'), ('kt/kt_curve.csv', b'time,kt\n0,1'),
                              ('tn/curve.csv', b'time,tn\n0,2')):
            (self.directory/name).write_bytes(content)
        self.store = Mock()
        self.store.put.return_value = {'etag': 'etag', 'request_id': 'request'}
        self.mes = Mock()
        self.mes.register.return_value = dict(reportId=123, reportKey=KEY, duplicated=False)
        self.sleep = Mock()
        self.uploader = ReportUploader(store=self.store, mes=self.mes, sleep=self.sleep)

    def state(self):
        return json.loads((self.directory/'upload-state.json').read_text(encoding='utf-8'))

    def pending(self):
        return self.directory/'pending'/f'{KEY}.json'

    def test_plan_uses_directory_time_and_flat_object_keys_for_exactly_three_files(self):
        plan = UploadPlan.from_directory(self.directory)
        self.assertEqual(set(plan.request), {'sn', 'reportKey', 'testTime', 'files', 'fileCount'})
        self.assertEqual(plan.request['testTime'], '2026-09-15 20:51:27')
        self.assertEqual(plan.request['sn'], SN)
        self.assertEqual(plan.request['fileCount'], 3)
        self.assertEqual(plan.request['files'], [f'test-report/202609/{SN}/{KEY}/{name}'
            for name in ('report.png', 'kt_curve.csv', 'curve.csv')])

    def test_success_requires_all_three_oss_receipts_before_mes_and_preserves_originals(self):
        def register(body):
            self.assertEqual(len(self.state()['uploaded']), 3)
            self.assertEqual(json.loads(self.pending().read_text()), body)
            return dict(reportId=123, reportKey=KEY, duplicated=False)
        self.mes.register.side_effect = register
        receipt = self.uploader(self.directory)
        self.assertEqual(receipt['reportId'], 123)
        self.assertEqual(self.state()['status'], 'SUCCEEDED')
        self.assertFalse(self.pending().exists())
        self.assertEqual((self.directory/'report.png').read_bytes(), b'png')
        self.assertEqual((self.directory/'kt/kt_curve.csv').read_bytes(), b'time,kt\n0,1')
        self.assertEqual((self.directory/'tn/curve.csv').read_bytes(), b'time,tn\n0,2')

    def test_missing_file_or_mismatching_sn_does_not_upload_anything(self):
        with self.assertRaises(UploadError):
            self.uploader(self.directory, {'motor_sn': 'A'*26})
        (self.directory/'tn/curve.csv').unlink()
        with self.assertRaises(UploadError):
            self.uploader(self.directory)
        self.store.put.assert_not_called()
        self.mes.register.assert_not_called()

    def test_partial_oss_failure_resumes_only_missing_file_and_keeps_same_mes_request(self):
        self.uploader.max_attempts = 1
        self.store.put.side_effect = [dict(etag='1'), dict(etag='2'), UploadError('OSS unavailable', retryable=True)]
        with self.assertRaises(UploadError):
            self.uploader(self.directory)
        before = self.pending().read_bytes()
        self.assertEqual(len(self.state()['uploaded']), 2)
        self.mes.register.assert_not_called()
        self.store.put.reset_mock(side_effect=True)
        self.uploader(self.directory)
        self.store.put.assert_called_once()
        self.assertTrue(self.store.put.call_args.args[0].key.endswith('/curve.csv'))
        self.assertEqual(json.loads(before), self.mes.register.call_args.args[0])

    def test_mes_business_failure_keeps_pending_without_retries_or_reuploading_files(self):
        self.mes.register.side_effect = UploadError('SN not registered')
        with self.assertRaises(UploadError):
            self.uploader(self.directory)
        self.mes.register.assert_called_once()
        self.sleep.assert_not_called()
        self.assertTrue(self.pending().exists())
        self.assertEqual(self.state()['status'], 'FAILED')
        self.mes.register.side_effect = None
        self.mes.register.return_value['duplicated'] = True
        self.store.put.reset_mock()
        self.assertTrue(self.uploader(self.directory)['duplicated'])
        self.store.put.assert_not_called()

    def test_network_failure_retries_same_payload_with_backoff(self):
        success = self.mes.register.return_value
        self.mes.register.side_effect = [UploadError('network', retryable=True),
                                        UploadError('network', retryable=True), success]
        self.uploader(self.directory)
        self.assertEqual([c.args for c in self.sleep.call_args_list], [(1,), (5,)])
        self.assertEqual(self.store.put.call_count, 3)
        bodies = [c.args[0] for c in self.mes.register.call_args_list]
        self.assertEqual(bodies, [bodies[0]]*3)

    def test_lost_oss_ack_retries_identical_bytes_without_read_permission(self):
        first = True
        sent = []
        def put(file):
            nonlocal first
            sent.append((file.key, file.content))
            if first:
                first = False
                raise UploadError('ACK lost', retryable=True)
            return dict(etag='ok')
        self.store.put.side_effect = put
        self.uploader(self.directory)
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(len(sent), 4)

    def test_modified_file_cannot_overwrite_previously_uploaded_report(self):
        self.uploader(self.directory)
        self.store.put.reset_mock()
        self.mes.register.reset_mock()
        (self.directory/'report.png').write_bytes(b'new-report')
        with self.assertRaisesRegex(UploadError, '改变'):
            self.uploader(self.directory)
        self.store.put.assert_not_called()
        self.mes.register.assert_not_called()


class MesTests(unittest.TestCase):
    credentials = Credentials('test-id', 'test-secret', 'test-api-key')

    def test_http_200_is_not_business_success_and_credentials_are_not_in_errors(self):
        for code, message, retry in ((401, 'bad test-api-key', False),
                (500, '模组SN未在生产系统中找到', False), (500, '系统异常', True),
                (500, 'No static resource open-api/test-report', False)):
            response = Mock(status_code=200)
            response.json.return_value = {'code': code, 'msg': message}
            with patch('requests.post', return_value=response) as post:
                with self.assertRaises(UploadError) as error:
                    MesClient(self.credentials).register({'reportKey': KEY})
            self.assertEqual(error.exception.retryable, retry)
            self.assertNotIn('test-api-key', str(error.exception))
            self.assertEqual(post.call_args.args[0], MES_URL)
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
            self.assertEqual(post.call_args.kwargs['timeout'], (5, 10))

    def test_duplicate_success_is_accepted_but_wrong_receipt_or_redirect_is_not(self):
        response = Mock(status_code=200)
        response.json.return_value = {'code': 200, 'data': {'reportKey': KEY, 'reportId': 1, 'duplicated': True}}
        with patch('requests.post', return_value=response):
            self.assertTrue(MesClient(self.credentials).register({'reportKey': KEY})['duplicated'])
            response.json.return_value['data']['reportKey'] = 'wrong'
            with self.assertRaises(UploadError):
                MesClient(self.credentials).register({'reportKey': KEY})
            response.status_code = 302
            with self.assertRaises(UploadError):
                MesClient(self.credentials).register({'reportKey': KEY})


class PortableConfigTests(unittest.TestCase):
    def test_missing_machine_config_uses_portable_config_and_explicit_path_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            local, portable, explicit = [Path(folder)/name for name in ('local.json', 'portable.json', 'explicit.json')]
            for path, key in ((portable, 'portable'), (explicit, 'explicit')):
                path.write_text(json.dumps(dict(access_key_id=key, access_key_secret='secret', mes_api_key='api')))
            with patch('scripts.factory_test.upload.config_path', return_value=local), \
                    patch('scripts.factory_test.upload.portable_config_path', return_value=portable):
                self.assertEqual(Credentials.load().access_key_id, 'portable')
                self.assertEqual(Credentials.load(explicit).access_key_id, 'explicit')
                local.write_text(json.dumps(dict(access_key_id='local', access_key_secret='secret', mes_api_key='api')))
                self.assertEqual(Credentials.load().access_key_id, 'portable')
                portable.write_text('{broken')
                with self.assertRaises(UploadError):
                    Credentials.load()

    def test_frozen_portable_path_is_next_to_exe_not_extraction_or_cwd(self):
        from scripts.factory_test.upload import portable_config_path
        with tempfile.TemporaryDirectory() as folder, \
                patch('scripts.factory_test.upload.sys.frozen', True, create=True), \
                patch('scripts.factory_test.upload.sys.executable', str(Path(folder)/'app.exe')):
            self.assertEqual(portable_config_path(), Path(folder).resolve()/'upload-config.json')


if __name__ == '__main__':
    unittest.main()
