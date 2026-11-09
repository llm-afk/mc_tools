import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from scripts.factory_test import upload
from scripts.factory_test.app import create_output_directory
from scripts.factory_test.preview import create_demo
from scripts.factory_test.upload_queue import UploadQueue, BLOCKED_RECHECK_S, default_roots

SN = 'C2KNEE0PROJS01412699990001'


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1000.
        self.notices = []
        self.store = Mock()
        self.store.put.return_value = dict(etag='ok', request_id='request')
        self.mes = Mock()
        self.mes.register.side_effect = lambda body: dict(reportId=12, reportKey=body['reportKey'], duplicated=False)
        self.uploader = upload.ReportUploader(store=self.store, mes=self.mes, max_attempts=1)
        self.queue = self.new_queue()

    def new_queue(self):
        return UploadQueue([self.root], uploader=self.uploader, clock=lambda: self.now, notify=self.notices.append)

    def make_report(self, suffix='', **overrides):
        directory = self.root/f'20260923_162447_{SN}{suffix}'
        (directory/'kt').mkdir(parents=True)
        (directory/'tn').mkdir()
        for name in ('report.png', 'kt/kt_curve.csv', 'tn/curve.csv'):
            (directory/name).write_bytes(b'original-' + name.encode())
        summary = dict(demo=False, motor_sn=SN, status='PASS', test_status='PASS', **overrides)
        upload.save_json(directory/'summary.json', summary)
        return directory

    def read(self, directory, filename='upload-queue.json'):
        return json.loads((directory/filename).read_text(encoding='utf-8'))

    def test_retry_failure_is_visible_to_panel_until_upload_succeeds(self):
        self.make_report()
        self.store.put.side_effect = upload.UploadError('network unavailable', retryable=True)
        self.queue.step()
        self.assertIn('network unavailable', self.notices[-1]['reason'])
        self.store.put.side_effect = None
        self.now += 6
        self.queue.step()
        self.assertIsNone(self.notices[-1]['reason'])

    def test_discovery_recovers_pass_manifest_without_pending_and_never_sends_demo_or_failed(self):
        good = self.make_report()
        demo = self.make_report('_2')
        upload.save_json(demo/'summary.json', dict(demo=True, status='PASS', motor_sn=SN))
        failed = self.make_report('_3')
        upload.save_json(failed/'summary.json', dict(demo=False, status='FAIL', motor_sn=SN))
        self.queue.step()
        self.assertEqual(self.read(good)['status'], 'SUCCEEDED')
        self.assertFalse((demo/'upload-state.json').exists())
        self.assertFalse((failed/'upload-state.json').exists())
        self.assertEqual(self.store.put.call_count, 3)
        self.assertEqual(self.mes.register.call_count, 1)
        self.now += 60
        self.new_queue().step()
        self.assertEqual(self.mes.register.call_count, 1)
        self.assertEqual(self.read(good, 'summary.json')['upload']['receipt']['reportId'], 12)
        self.assertTrue((good/'report.png').exists())

    def test_partial_oss_progress_survives_restart_and_only_missing_files_are_sent(self):
        directory = self.make_report()
        self.store.put.side_effect = [dict(etag='first'), upload.UploadError('offline', retryable=True)]
        self.queue.step()
        self.assertEqual(self.read(directory)['status'], 'RETRY')
        self.mes.register.assert_not_called()
        self.store.put.side_effect = None
        self.store.put.reset_mock()
        restarted = self.new_queue()
        restarted.step()
        self.store.put.assert_not_called()
        self.now += 6
        restarted.step()
        self.assertEqual(self.store.put.call_count, 2)
        self.assertEqual(self.read(directory)['status'], 'SUCCEEDED')

    def test_lost_mes_success_ack_replays_exact_request_and_uses_idempotent_receipt(self):
        directory = self.make_report()
        bodies = []
        def mes(body):
            bodies.append(body)
            if len(bodies) == 1:
                raise upload.UploadError('response lost', retryable=True)
            return dict(reportId=12, reportKey=body['reportKey'], duplicated=True)
        self.mes.register.side_effect = mes
        self.queue.step()
        self.assertEqual(self.store.put.call_count, 3)
        self.now += 6
        self.new_queue().step()
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(self.store.put.call_count, 3)
        self.assertTrue(self.read(directory)['receipt']['duplicated'])
        self.assertFalse((directory/'pending'/f'{directory.name}.json').exists())

    def test_two_instances_and_cli_cannot_upload_same_report_concurrently(self):
        directory = self.make_report()
        self.queue.enqueue(directory)
        other = self.new_queue()
        with upload.report_lock(directory, '.upload-queue.lock'):
            other.step()
            self.mes.register.assert_not_called()
        self.queue.step()
        other.step()
        self.mes.register.assert_called_once()
        with upload.report_lock(directory):
            with self.assertRaises(upload.UploadBusy):
                self.uploader(directory)

    def test_os_lock_is_released_when_owner_process_dies(self):
        directory = self.make_report()
        code = ('import os,sys; from scripts.factory_test.upload import report_lock; '
                'lock=report_lock(sys.argv[1]); lock.__enter__(); os._exit(0)')
        subprocess.run([sys.executable, '-c', code, str(directory)], check=True, timeout=15)
        self.queue.step()
        self.assertEqual(self.read(directory)['status'], 'SUCCEEDED')

    def test_disk_full_before_queue_commit_never_starts_network(self):
        directory = self.make_report()
        with patch('scripts.factory_test.upload.save_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.queue.enqueue(directory)
        self.store.put.assert_not_called()
        self.mes.register.assert_not_called()
        self.assertTrue((directory/'report.png').exists())
        self.queue.step()
        self.assertEqual(self.read(directory)['status'], 'SUCCEEDED')

    def test_completed_receipt_repairs_stale_queue_without_any_network(self):
        directory = self.make_report()
        self.queue.enqueue(directory)
        self.uploader(directory)  # Simulate crash before the queue was notified.
        upload.save_json(directory/'pending'/f'{directory.name}.json', {})
        self.store.put.reset_mock()
        self.mes.register.reset_mock()
        self.new_queue().step()
        self.store.put.assert_not_called()
        self.mes.register.assert_not_called()
        self.assertEqual(self.read(directory)['status'], 'SUCCEEDED')
        self.assertFalse((directory/'pending'/f'{directory.name}.json').exists())

    def test_corrupt_queue_is_retained_and_does_not_starve_other_reports(self):
        bad = self.make_report()
        good = self.make_report('_2')
        (bad/'upload-queue.json').write_bytes(b'broken-json')
        self.queue.step()
        self.assertEqual((bad/'upload-queue.json').read_bytes(), b'broken-json')
        self.assertEqual(self.read(good)['status'], 'SUCCEEDED')
        self.assertEqual(self.notices[-1]['blocked'], 1)

    def test_modified_files_are_blocked_then_rechecked_after_restoration(self):
        directory = self.make_report()
        self.queue.enqueue(directory)
        original = (directory/'report.png').read_bytes()
        (directory/'report.png').write_bytes(b'changed')
        self.queue.step()
        self.assertEqual(self.read(directory)['status'], 'BLOCKED')
        self.store.put.assert_not_called()
        (directory/'report.png').write_bytes(original)
        self.now += BLOCKED_RECHECK_S+1
        self.queue.step()
        self.assertEqual(self.read(directory)['status'], 'SUCCEEDED')

    def test_sn_registration_failure_rechecks_slowly_without_resending_oss(self):
        directory = self.make_report()
        self.mes.register.side_effect = upload.UploadError('SN 未登记')
        self.queue.step()
        self.assertEqual(self.read(directory)['status'], 'BLOCKED')
        self.now += 300
        self.queue.step()
        self.assertEqual(self.mes.register.call_count, 1)
        self.mes.register.side_effect = lambda body: dict(reportId=12, reportKey=body['reportKey'])
        self.now += 301
        self.queue.step()
        self.assertEqual(self.read(directory)['status'], 'SUCCEEDED')
        self.assertEqual(self.store.put.call_count, 3)

    def test_local_endpoint_receipt_never_marks_cloud_report_complete(self):
        directory = self.make_report()
        local = upload.ReportUploader(store=self.store, mes=self.mes,
            endpoint='http://192.168.1.203:8091/open-api/test-report', max_attempts=1)
        local(directory)
        self.queue = UploadQueue([self.root], endpoint=upload.CLOUD_MES_URL, uploader=self.uploader)
        with self.assertRaisesRegex(upload.UploadError, '环境'):
            self.queue.enqueue(directory)
        self.assertEqual(self.mes.register.call_count, 1)

    def test_crash_before_png_manifest_recovers_from_checkpoint_without_retesting_motor(self):
        directory = self.root/f'20260923_162447_{SN}'
        directory.mkdir()
        summary = create_demo(directory)
        summary.update(demo=False, motor_sn=SN, status='PASS', test_status='PASS')
        upload.save_json(directory/'report-input.json', summary)
        with patch('scripts.factory_test.report.render', side_effect=lambda path, _: path.write_bytes(b'png')) as render:
            self.queue.step()
        render.assert_called_once()
        self.assertEqual(self.read(directory)['status'], 'SUCCEEDED')

    def test_missing_csv_is_retained_and_recovers_when_file_becomes_available(self):
        directory = self.make_report()
        path = directory/'tn/curve.csv'
        content = path.read_bytes()
        path.unlink()
        self.queue.step()
        self.store.put.assert_not_called()
        path.write_bytes(content)
        self.now += 31
        self.queue.step()
        self.assertEqual(self.read(directory)['status'], 'SUCCEEDED')

    def test_receipt_disk_commit_failure_can_recover_via_mes_duplicate(self):
        directory = self.make_report()
        original = upload.save_json
        once = True
        def save(path, data):
            nonlocal once
            if once and Path(path).name == 'upload-state.json' and data.get('status') == 'SUCCEEDED':
                once = False
                raise OSError('disk full after remote success')
            return original(path, data)
        with patch('scripts.factory_test.upload.save_json', side_effect=save):
            self.queue.step()
        self.assertEqual(self.read(directory)['status'], 'BLOCKED')
        self.mes.register.side_effect = lambda body: dict(reportId=12, reportKey=body['reportKey'], duplicated=True)
        self.now += 601
        self.new_queue().step()
        self.assertEqual(self.store.put.call_count, 3)
        self.assertTrue(self.read(directory)['receipt']['duplicated'])

    def test_stop_retains_durable_work_for_next_start(self):
        directory = self.make_report()
        self.queue.enqueue(directory)
        self.queue.stop()
        self.queue.step()
        self.store.put.assert_not_called()
        self.new_queue().step()
        self.assertEqual(self.read(directory)['status'], 'SUCCEEDED')

    def test_missing_report_directory_does_not_starve_other_report(self):
        import shutil
        missing = self.make_report()
        good = self.make_report('_2')
        self.queue.enqueue(missing)
        self.queue.enqueue(good)
        shutil.rmtree(missing)  # Test-owned temporary fixture only.
        self.queue.next_scan = self.now+30
        self.queue.step()
        self.assertEqual(self.read(good)['status'], 'SUCCEEDED')
        self.assertEqual(self.notices[-1]['blocked'], 1)

    def test_clock_rollback_cannot_strand_persisted_retry_for_days(self):
        directory = self.make_report()
        self.queue.enqueue(directory)
        item = self.read(directory)
        item['next_attempt'] = self.now+86400
        upload.save_json(directory/'upload-queue.json', item)
        restarted = self.new_queue()
        restarted.step()
        self.mes.register.assert_not_called()
        self.now += BLOCKED_RECHECK_S+1
        restarted.step()
        self.assertEqual(self.read(directory)['status'], 'SUCCEEDED')

    def test_nested_integration_report_is_not_discovered(self):
        import shutil
        directory = self.make_report()
        nested = self.root/'mes-local-validation'
        nested.mkdir()
        shutil.move(str(directory), str(nested/directory.name))
        self.queue.step()
        self.store.put.assert_not_called()

    def test_frozen_exe_also_recovers_legacy_reports_next_to_it(self):
        executable = self.root/'portable/产线测试.exe'
        with patch('sys.frozen', True, create=True), patch('sys.executable', str(executable)):
            roots = default_roots()
        self.assertIn((executable.parent/'data').resolve(), roots)

    def test_production_directory_ids_are_distinct_even_with_same_sn_and_timestamp(self):
        from datetime import datetime
        a = create_output_directory(self.root/'pc1', SN, datetime(2026, 9, 23), unique=True)
        b = create_output_directory(self.root/'pc2', SN, datetime(2026, 9, 23), unique=True)
        self.assertNotEqual(a.name, b.name)
        self.assertLessEqual(len(a.name), 128)


if __name__ == '__main__':
    unittest.main()
