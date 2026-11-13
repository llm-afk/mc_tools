import csv
import math
from pathlib import Path
import tempfile
import unittest
from scripts.factory_test.measurements.smoothing import smooth, append_smoothed_columns, SMOOTH_COLUMNS
from scripts.factory_test.measurements.tn_curve.app import COLUMNS


class SmoothingTests(unittest.TestCase):
    def test_dense_ramp_and_sparse_spikes(self):
        times = [i*.001 for i in range(1001)]
        ideal = [30-20*t for t in times]
        values = ideal[:]
        values[200], values[500] = 100, -100
        fitted = smooth(times, values, .3)
        self.assertEqual(len(fitted), len(values))
        self.assertLess(max(abs(a-b) for a, b in zip(fitted, ideal)), .03)
    def test_isolated_positive_and_negative_spikes_on_plateau(self):
        times = [i*.01 for i in range(101)]
        values = [30.0]*101
        values[30], values[70] = 60, -20
        result = smooth(times, values, .3)
        self.assertLess(max(abs(v-30) for v in result), 1e-8)

    def test_real_signed_ramp_preserved_with_irregular_timing(self):
        times = [i*.01+(i%3)*.001 for i in range(101)]
        values = [5-10*t for t in times]
        result = smooth(times, values, .3)
        self.assertLess(max(abs(a-b) for a, b in zip(values, result)), 1e-8)

    def test_sustained_change_survives_and_no_overshoot(self):
        times = [i*.01 for i in range(101)]
        values = [30 if i < 50 else 10 for i in range(101)]
        result = smooth(times, values, .3)
        self.assertAlmostEqual(result[20], 30)
        self.assertAlmostEqual(result[80], 10)
        self.assertTrue(all(10 <= value <= 30 for value in result))

    def test_gap_and_short_segments(self):
        self.assertEqual(smooth([], [], .3), [])
        self.assertEqual(smooth([0, .01, 1, 1.01], [30, 30, -2, -2], .3), [30, 30, -2, -2])
        self.assertTrue(all(math.isfinite(v) for v in smooth([0]*5, [1, 1, 100, 1, 1], .3)))

    def test_single_file_raw_values_and_sparse_rows_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'curve.csv'
            original = [['0.001', '', '', '', '2', '30', '25', '26'],
                        ['0.002', '3', '4', '12', '', '', '', '']]
            with path.open('w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(COLUMNS)
                writer.writerows(original)
            append_smoothed_columns(path)
            with path.open(encoding='utf-8', newline='') as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], COLUMNS+SMOOTH_COLUMNS)
            self.assertEqual([row[:8] for row in rows[1:]], original)
            self.assertEqual(rows[1][8:], ['2', '30', '', ''])
            self.assertEqual(rows[2][8:], ['', '', '4', '3'])
            self.assertEqual([p.name for p in Path(directory).iterdir()], ['curve.csv'])
