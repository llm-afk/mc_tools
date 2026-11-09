import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.factory_test.storage import atomic_replace
from scripts.factory_test.station_config import atomic_json


def denied(code):
    error = PermissionError('simulated file access denial')
    error.winerror = code
    return error


class AtomicStorageTests(unittest.TestCase):
    def test_transient_windows_denial_retries_same_commit_and_keeps_old_file_until_success(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'state.json'
            target.write_text('{"old":true}', encoding='utf-8')
            import os
            original = os.replace
            calls=[]
            def replace(source, destination):
                calls.append((source,destination))
                if len(calls) < 3:
                    self.assertEqual(json.loads(target.read_text(encoding='utf-8')),dict(old=True))
                    raise denied(5 if len(calls)==1 else 32)
                return original(source,destination)
            with patch('scripts.factory_test.storage.os.replace', side_effect=replace), \
                 patch('scripts.factory_test.storage.time.sleep') as sleep:
                atomic_json(target,dict(new=True))
            self.assertEqual(len(set(str(source) for source,destination in calls)),1)
            self.assertEqual(sleep.call_count,2)
            self.assertEqual(json.loads(target.read_text(encoding='utf-8')),dict(new=True))
            self.assertEqual(list(Path(folder).glob('*.tmp')),[])

    def test_permanent_windows_denial_is_bounded_and_preserves_old_data(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'state.json'
            target.write_bytes(b'original')
            with patch('scripts.factory_test.storage.os.replace',side_effect=denied(5)) as replace, \
                 patch('scripts.factory_test.storage.time.sleep') as sleep:
                with self.assertRaises(PermissionError): atomic_json(target,dict(new=True))
            self.assertEqual(replace.call_count,6)
            self.assertEqual(sleep.call_count,5)
            self.assertEqual(target.read_bytes(),b'original')
            self.assertEqual(list(Path(folder).glob('*.tmp')),[])

    def test_other_errors_are_not_retried(self):
        for error in (denied(1314), OSError('disk full')):
            with self.subTest(error=error), patch('scripts.factory_test.storage.os.replace',side_effect=error) as replace, \
                 patch('scripts.factory_test.storage.time.sleep') as sleep:
                with self.assertRaises(type(error)): atomic_replace('source','destination')
            replace.assert_called_once()
            sleep.assert_not_called()
