import csv
from pathlib import Path
import tempfile
import unittest
from utils.csv_streams import independent_rows, pack_streams


class StreamColumnsTests(unittest.TestCase):
    def test_alignment_never_reuses_a_batched_frame(self):
        from utils.csv_streams import independent_rows
        fields = ['time_ms', 'motor_torque', 'dyno_torque_nm']
        rows = [dict(time_ms=str(i), motor_torque=str(i), dyno_torque_nm='') for i in range(10)]
        rows += [dict(time_ms='5', motor_torque='', dyno_torque_nm=str(100+i)) for i in range(6)]
        _, packed = independent_rows(fields, rows, align_motor=True)
        values = [r[-1] for r in packed if r[-1] != '']
        self.assertEqual(values, [str(100+i) for i in range(6)])

    def test_alignment_uses_timestamps_and_drops_extra_dyno_frames(self):
        fields = ['time_ms', 'motor_torque', 'dyno_torque_nm']
        source = [dict(zip(fields, row)) for row in (
            ['10', '1', ''], ['20', '2', ''], ['100', '3', ''],
            ['0', '', '0'], ['9', '', '9'], ['11', '', '11'], ['20', '', '20'], ['22', '', '22'])]
        output, rows = independent_rows(fields, source, align_motor=True)
        self.assertEqual(output, fields)
        self.assertEqual(list(rows), [['10', '1', '9'], ['20', '2', '20'], ['100', '3', '']])

    def test_each_device_is_dense_and_preserves_all_samples(self):
        fields = ['time_ms', 'motor_torque', 'dyno_torque_nm', 'motor_torque_smooth',
                  'dyno_torque_smooth_nm', 'phase', 'cycle', 'application_target_torque']
        values = [
            ['0.5', '1', '', '1.25', '', 'ramp', '0', '3'],
            ['0.6', '', '0', '', '0.2', 'ramp', '0', '4'],
            ['0.6', '', '2', '', '2.2', 'ramp', '0', '4'],
            ['0.6', '', '2', '', '2.2', 'ramp', '0', '4'],
            ['1.2', '5', '', '5.25', '', 'hold', '1', '6']]
        output, rows = independent_rows(fields, [dict(zip(fields, row)) for row in values])
        rows = [dict(zip(output, row)) for row in rows]
        self.assertEqual(len(rows), 3)
        self.assertEqual([r['motor_torque'] for r in rows], ['1', '5', ''])
        self.assertEqual([r['dyno_torque_nm'] for r in rows], ['0', '2', '2'])
        self.assertEqual([r['dyno_time_ms'] for r in rows], ['0.6']*3)
        self.assertEqual([r['motor_phase'] for r in rows], ['ramp', 'hold', ''])
        self.assertEqual([r['dyno_phase'] for r in rows], ['ramp']*3)
        self.assertEqual(rows[1]['motor_application_target_torque'], '6')
        self.assertEqual(rows[1]['dyno_application_target_torque'], '4')

    def test_one_stream_and_empty_file(self):
        fields = ['time_ms', 'motor_torque', 'dyno_torque_nm']
        output, rows = independent_rows(fields, [dict(zip(fields, ['1', '', '0']))])
        self.assertEqual(list(rows), [['', '', '1', '0']])
        _, rows = independent_rows(fields, [])
        self.assertEqual(list(rows), [])

    def test_single_global_time_prefers_motor_then_dyno_tail(self):
        fields = ['time_ms', 'motor_torque', 'dyno_torque_nm']
        source = [dict(zip(fields, row)) for row in (
            ['1', '10', ''], ['2', '11', ''], ['1.5', '', '20'],
            ['2.5', '', '21'], ['3.5', '', '22'])]
        output, rows = independent_rows(fields, source, single_time=True)
        self.assertEqual(output, ['time_ms', 'motor_torque', 'dyno_torque_nm'])
        self.assertEqual(list(rows), [
            ['1', '10', '20'], ['2', '11', '21'], ['3.5', '', '22']])

    def test_atomic_conversion_copy_and_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'raw.csv'
            with path.open('w', newline='') as f:
                w = csv.writer(f)
                w.writerow(['time_ms', 'motor_torque', 'dyno_torque_nm'])
                w.writerows([['2', '2.000', ''], ['1', '', '-1.0'], ['0', '0', '']])
            original = path.read_bytes()
            copy = Path(directory)/'continuous.csv'
            pack_streams(path, copy)
            self.assertEqual(path.read_bytes(), original)
            with self.assertRaises(FileExistsError): pack_streams(path, copy)
            pack_streams(path)
            self.assertEqual(path.read_bytes(), copy.read_bytes())
            packed = path.read_bytes()
            pack_streams(path)
            self.assertEqual(path.read_bytes(), packed)
            with path.open() as f: rows = list(csv.DictReader(f))
            self.assertEqual([row['motor_torque'] for row in rows], ['0', '2.000'])
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ['continuous.csv', 'raw.csv'])

    def test_ambiguous_merged_row_does_not_overwrite_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'raw.csv'
            with path.open('w', newline='') as f:
                w = csv.writer(f)
                w.writerow(['time_ms', 'motor_torque', 'dyno_torque_nm'])
                w.writerow(['0', '1', '1'])
            original = path.read_bytes()
            with self.assertRaises(ValueError): pack_streams(path)
            self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__': unittest.main()
