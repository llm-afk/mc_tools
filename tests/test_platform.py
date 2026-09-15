import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from motor_platform import Bench, MotorState
from motor_platform.core.config import BenchConfig
from motor_platform.core.recording import StateRecorder


class PlatformTests(unittest.TestCase):
    def test_configuration_and_duplicate_id(self):
        cfg = BenchConfig(motor_ids=[1,2])
        self.assertEqual(cfg.motor_ids,(1,2))
        for ids in ([],[None],[1,1],[True],[-1],[128]):
            with self.assertRaises(ValueError): BenchConfig(motor_ids=ids)

    def test_bench_lifecycle_and_no_auto_enable(self):
        transport = Mock()
        bench = Bench(transport=transport,motor_ids=[1,2])
        transport.open.assert_not_called()
        with bench:
            self.assertEqual(bench.motor(1).node_id, 1)
            self.assertEqual(bench.motor(2).node_id, 2)
            with self.assertRaises(KeyError): bench.motor(3)
            transport.send.assert_not_called()
        transport.close.assert_called_once()
        with self.assertRaises(RuntimeError): bench.motor(1)

    def test_scripts_have_independent_motor_sets(self):
        a, b = Mock(), Mock()
        with Bench(transport=a,motor_ids=[1]) as first, Bench(transport=b,motor_ids=[2,3]) as second:
            self.assertEqual(first.motor(1).node_id,1)
            with self.assertRaises(KeyError):first.motor(2)
            with self.assertRaises(KeyError):second.motor(1)
            self.assertEqual(second.motor(3).node_id,3)
            self.assertIs(first._bus.transport, a)
            self.assertIs(second._bus.transport, b)

    def test_csv_state_recording(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.csv"
            with StateRecorder(path) as recorder:
                recorder.write(MotorState(1, 1.5, -2, 0.5, 25, 30, 0, 123))
            with path.open(newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(rows[0]["node_id"], "1")
            self.assertEqual(rows[0]["position"], "1.5")
            with self.assertRaises(FileExistsError): StateRecorder(path)


if __name__ == "__main__": unittest.main()
