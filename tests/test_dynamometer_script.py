import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from motor_platform import DynamometerState
from scripts.dynamometer_test.app import main, parse_args, resolve_port, panel_text


class DynamometerScriptTests(unittest.TestCase):
    def test_defaults_and_argument_validation(self):
        args = parse_args([])
        self.assertEqual((args.port, args.baudrate, args.duration), ('auto', 115200, 0))
        for argv in (['--duration', '-1'], ['--display-hz', 'nan'], ['--torque-scale', '0']):
            with patch('sys.stderr'), self.assertRaises(SystemExit):
                parse_args(argv)

    def test_auto_selection_and_scan_do_not_start_recording(self):
        with patch('scripts.dynamometer_test.app.detect_dynamometers', return_value=['COM22']), \
             patch('scripts.dynamometer_test.app.Dynamometer') as factory, patch('builtins.print'):
            self.assertEqual(resolve_port('auto', 115200), 'COM22')
            main(['--scan'])
            factory.assert_not_called()
        with patch('scripts.dynamometer_test.app.detect_dynamometers', return_value=['COM1', 'COM2']):
            with self.assertRaises(RuntimeError):
                resolve_port('auto', 115200)

    def test_csv_records_batches_and_final_drain_not_display_snapshots(self):
        s = DynamometerState(1, 2, 3, 100, 300, 1)
        class Device:
            def __enter__(self):
                self.closed = self.sent = self.final_sent = False
                return self
            def __exit__(self, *_):
                self.closed = True
            def drain(self):
                if self.closed and not self.final_sent:
                    self.final_sent = True
                    return [s]
                if not self.sent:
                    self.sent = True
                    return [s, s]
                return []
            def stats(self):
                return dict(fault=None, actual_rx_hz=1000, rx_count=3, rx_bytes=18,
                            discarded_bytes=0, buffered_samples=0, dropped_samples=0)
        device = Device()
        with tempfile.TemporaryDirectory() as folder, \
             patch('scripts.dynamometer_test.app.__file__', str(Path(folder)/'app.py')), \
             patch('scripts.dynamometer_test.app.Dynamometer', return_value=device), patch('builtins.print'):
            main(['--port', 'COM_TEST', '--no-console', '--duration', '0.02'])
            paths = list((Path(folder)/'data').glob('*.csv'))
            self.assertEqual(len(paths), 1)
            with paths[0].open(encoding='utf-8') as log:
                rows = list(csv.DictReader(log))
            self.assertEqual(len(rows), 3)
            self.assertEqual([r['sample_index'] for r in rows], ['1', '2', '3'])
            self.assertTrue(device.closed)

    def test_stale_feedback_is_marked_explicitly(self):
        s = DynamometerState(1, 2, 3, 100, 300, 1)
        stats = dict(actual_rx_hz=0, rx_count=1, rx_bytes=6, discarded_bytes=0,
                     buffered_samples=0, dropped_samples=0)
        self.assertIn('反馈过期', panel_text('COM_TEST', s, stats, 1, 2))
