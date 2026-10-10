import csv
import io
from pathlib import Path
import queue
import struct
import tempfile
import unittest
from unittest.mock import patch

from motor_platform import CanFrame, MitCommand
from motor_platform.components.motor.protocol import MotorState
from scripts.position_sweep import app
from scripts.position_sweep.recording import COLUMNS, Recorder
from scripts.position_sweep.trajectory import Chirp, Home, Reference


def state(at, position=0, velocity=0, error=0):
    return MotorState(1, position, velocity, 0.2, 26, 31, error, at)


class SimTransport:
    """Immediate ideal tracking, optional faults; real MotorBus/runtime code."""
    def __init__(self, *, fault_after=None, silent_after=None, stuck=False, hot=False,
                 disable_fails=False):
        self.rx = queue.Queue()
        self.sent = []
        self.enabled = self.closed = False
        self.position = 0.4
        self.feedback_count = self.mit_count = 0
        self.fault_after, self.silent_after = fault_after, silent_after
        self.stuck, self.hot, self.disable_fails = stuck, hot, disable_fails

    def open(self, *args):
        pass

    def feedback(self, error=0):
        self.feedback_count += 1
        self.rx.put(CanFrame(0x191, struct.pack("<fffhhH2x", self.position, 0, 0.2,
                                             850 if self.hot else 260, 310, error)))

    def send(self, frame):
        self.sent.append(frame)
        if frame.can_id == 0x601:
            if self.enabled:
                if self.disable_fails:
                    raise OSError("test disable failed")
                # A final real feedback arriving while awaiting disable ACK.
                self.feedback()
            self.enabled = False
            self.rx.put(CanFrame(0x581, b"\x60" + frame.data[1:4] + b"\0"*4))
        else:
            self.enabled = True
            self.mit_count += 1
            p, v, torque, kp, kd = struct.unpack("<fffHH", frame.data)
            if kp and not self.stuck:
                self.position = p
            if self.silent_after is None or self.mit_count < self.silent_after:
                self.feedback(1 if self.mit_count == self.fault_after else 0)

    def recv(self, timeout):
        try:
            return self.rx.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self.closed = True


class TrajectoryTests(unittest.TestCase):
    def test_homing_starts_at_measured_position_and_bounds_slope(self):
        for p in (-3.2, 0, 2.1):
            home = Home(p, 0.2, 20, 1, "derivative")
            self.assertEqual(home.sample(0).command.position, p)
            self.assertEqual(home.sample(0).command.velocity, 0)
            end = home.sample(home.duration).command
            self.assertEqual((end.position, end.velocity), (0, 0))
            for i in range(1001):
                point = home.sample(i*home.duration/1000).command
                self.assertLessEqual(abs(point.velocity), 0.2+1e-12)
                self.assertLessEqual(abs(point.position), abs(p)+1e-12)

    def test_chirp_frequency_endpoints_and_constant_amplitude(self):
        plan = Chirp(0.5, 10, 12, 1, 2, 20, 1)
        self.assertEqual(plan.sample(2).frequency_hz, 0.5)
        self.assertAlmostEqual(plan.sample(8).frequency_hz, 5.25)
        self.assertEqual(plan.sample(14).frequency_hz, 10)
        for i in range(1601):
            point = plan.sample(i/100)
            self.assertLessEqual(abs(point.command.position), 1)
            self.assertEqual(point.command.velocity, 0)
            self.assertEqual(point.command.torque, 0)

    def test_derivative_matches_position_including_amplitude_ramps(self):
        plan = Chirp(0.3, 4, 3, 0.8, 0.4, 20, 1, "derivative")
        for t in (0.03, 0.22, 0.39, 0.7, 1.9, 3.41, 3.6, 3.78):
            step = 1e-6
            numerical = (plan.sample(t+step).command.position-plan.sample(t-step).command.position)/(2*step)
            self.assertAlmostEqual(plan.sample(t).command.velocity, numerical, places=6)
        for boundary in (0.4, 3.4, 3.8):
            left, right = plan.sample(boundary-1e-9), plan.sample(boundary+1e-9)
            self.assertAlmostEqual(left.command.position, right.command.position, places=6)
            self.assertAlmostEqual(left.command.velocity, right.command.velocity, places=5)
        self.assertEqual(plan.sample(0).command.position, 0)
        self.assertEqual(plan.sample(0).command.velocity, 0)
        self.assertEqual(plan.sample(10).command.position, 0)
        self.assertEqual(plan.sample(10).command.velocity, 0)

    def test_zero_wait_requires_continuous_real_feedback(self):
        args = app.parse_args(["--preview", "--kp", "20", "--kd", "1", "--settle", ".1"])
        wait = app.ZeroWait(1, args)
        wait.update([state(1), state(1.05), state(1.11, position=0.06)])
        self.assertFalse(wait.ready)
        wait.update([state(1.15), state(1.21), state(1.27)])
        self.assertTrue(wait.ready)
        wait.update([state(1.28, velocity=.2)])
        self.assertFalse(wait.ready)
        wait.update([state(1.3), state(1.6)])  # A gap is not proof of settling.
        self.assertFalse(wait.ready)


class RecordingTests(unittest.TestCase):
    def test_deferred_csv_preserves_targets_and_flushes_on_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"deferred.csv"
            with self.assertRaisesRegex(RuntimeError, "simulated stop"):
                with Recorder(path, 0, defer_csv=True) as recorder:
                    with patch.object(recorder, '_write_row', side_effect=AssertionError('disk during control')):
                        for i in range(3):
                            recorder.command(i, i, Reference('sweep', MitCommand(position=i, kp=20)))
                            recorder.write(state(i))
                    self.assertEqual(recorder.rows, 3)
                    raise RuntimeError('simulated stop')
            with path.open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(tuple(rows[0]), COLUMNS)
            self.assertEqual([float(r['target_position_rad']) for r in rows], [0, 1, 2])

    def test_deferred_buffer_limit_stops_and_saves_existing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'bounded.csv'
            with self.assertRaisesRegex(RuntimeError, 'buffer full'):
                with Recorder(path, 0, defer_csv=True, max_buffered_rows=2) as recorder:
                    for i in range(3):
                        recorder.write(state(i))
            with path.open(encoding='utf-8-sig', newline='') as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 2)

    def test_each_packet_kept_and_historical_sent_target_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"feedback.csv"
            with Recorder(path, 0) as recorder:
                recorder.command(.9, 1, Reference("home", MitCommand(position=1, kp=20.123)))
                recorder.command(1.9, 2, Reference("sweep", MitCommand(position=2), 3, .1))
                for at in (.5, 1.5, 1.6, 2.5):
                    recorder.write(state(at, error=1 if at == 2.5 else 0))
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 4)
            self.assertEqual([float(row['target_position_rad']) for row in rows], [0, 1, 1, 2])
            self.assertEqual(tuple(rows[0]), COLUMNS)
            self.assertEqual(len(COLUMNS), 9)
            self.assertEqual([float(row['frequency_hz']) for row in rows], [0, 0, 0, 3])
            self.assertEqual(rows[1]['kp'], '20.12')

    def test_capture_starts_after_zero_and_discards_delayed_homing_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"feedback.csv"
            with Recorder(path, 0, capacity=2, capture_enabled=False) as recorder:
                for i in range(20):
                    recorder.command(i, i, Reference("home", MitCommand(position=3)))
                    recorder.write(state(i))
                self.assertEqual(recorder.rows, 0)
                recorder.begin_capture(20)
                recorder.write(state(19.5))
                recorder.command(20, 20, Reference("ramp_in", MitCommand(kp=20, kd=1)))
                recorder.write(state(20))
                recorder.write(state(20.01))
                self.assertEqual(recorder.rows, 2)
                self.assertEqual(recorder.first_rx, 20)
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(float(row['target_position_rad']) == 0 for row in rows))

    def test_command_overflow_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            with Recorder(Path(directory)/"out.csv", 0, capacity=1) as recorder:
                recorder.command(0, .1, Reference("home", MitCommand()))
                with self.assertRaisesRegex(RuntimeError, "overflow"):
                    recorder.command(.1, .2, Reference("home", MitCommand()))


class ScriptTests(unittest.TestCase):
    def test_scheduling_restored_on_failure(self):
        import sys
        import gc
        previous = sys.getswitchinterval()
        previous_gc = gc.isenabled()
        with self.assertRaisesRegex(RuntimeError, 'test'):
            with app.control_scheduling():
                self.assertLessEqual(sys.getswitchinterval(), .0002)
                self.assertFalse(gc.isenabled())
                raise RuntimeError('test')
        self.assertEqual(sys.getswitchinterval(), previous)
        self.assertEqual(gc.isenabled(), previous_gc)

    def arguments(self, output):
        return ["--serial", "SIMULATED", "--kp", "20", "--kd", "1", "--rate", "200",
                "--start-hz", "1", "--stop-hz", "4", "--duration", ".10", "--ramp", ".06",
                "--settle", ".03", "--home-timeout", ".3", "--home-speed", "1",
                "--output", str(output)]

    def execute(self, transport, extra=(), sleep_error=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"out.csv"
            with patch.object(app, "Canable", return_value=transport) as hardware, patch("sys.stdout", new_callable=io.StringIO), \
                 patch("sys.stderr", new_callable=io.StringIO) as err:
                hardware.detect.return_value = [{"serial_number": "SIMULATED"}]
                if sleep_error is None:
                    result = app.main(self.arguments(path)+list(extra))
                else:
                    with patch.object(app.time, "sleep", side_effect=sleep_error):
                        result = app.main(self.arguments(path)+list(extra))
                errors = err.getvalue()
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            return result, rows, errors

    def test_complete_real_runtime_flow_keeps_all_feedback_including_disable(self):
        transport = SimTransport()
        result, rows, _ = self.execute(transport)
        self.assertEqual(result, 0)
        self.assertFalse(transport.enabled)
        self.assertTrue(transport.closed)
        self.assertGreater(len(rows), 10)
        self.assertLess(len(rows), transport.feedback_count)
        self.assertEqual(tuple(rows[0]), COLUMNS)
        # Do not infer a fixed first-point delay from Windows thread timing.
        # The capture cutoff itself is checked with exact timestamps above.
        self.assertTrue(all(float(row['kp']) == 20 and float(row['kd']) == 1 for row in rows))
        self.assertEqual(float(rows[-1]['target_position_rad']), 0)
        self.assertGreater(len({row['target_position_rad'] for row in rows}), 10)

    def test_transient_fault_is_saved_and_disables(self):
        transport = SimTransport(fault_after=8)
        result, rows, errors = self.execute(transport)
        self.assertEqual(result, 1)
        self.assertIn('motor fault', errors)
        self.assertEqual(rows, [])  # Fault during homing: nothing is captured yet.
        self.assertFalse(transport.enabled)

    def test_temperature_fault_does_not_start_homing(self):
        transport = SimTransport(hot=True)
        result, rows, errors = self.execute(transport)
        self.assertEqual(result, 1)
        self.assertIn('temperature', errors)
        self.assertEqual(rows, [])
        self.assertFalse(transport.enabled)

    def test_no_feedback_aborts_and_closes_transport(self):
        transport = SimTransport(silent_after=5)
        result, rows, errors = self.execute(transport)
        self.assertEqual(result, 1)
        self.assertTrue('feedback' in errors or 'stale' in errors)
        self.assertTrue(transport.closed)
        self.assertFalse(transport.enabled)
        self.assertEqual(rows, [])

    def test_zero_not_reached_never_enters_sweep(self):
        transport = SimTransport(stuck=True)
        result, rows, errors = self.execute(transport)
        self.assertEqual(result, 1)
        self.assertIn('settle at zero', errors)
        self.assertEqual(rows, [])
        self.assertFalse(transport.enabled)

    def test_ctrl_c_disables_and_keeps_tail(self):
        transport = SimTransport()
        result, rows, _ = self.execute(transport, sleep_error=KeyboardInterrupt())
        self.assertEqual(result, 130)
        self.assertFalse(transport.enabled)
        self.assertEqual(rows, [])

    def test_disable_failure_is_visible_even_on_ctrl_c(self):
        result, rows, errors = self.execute(SimTransport(disable_fails=True), sleep_error=KeyboardInterrupt())
        self.assertEqual(result, 130)
        self.assertIn('Disable unconfirmed', errors)
        self.assertEqual(rows, [])

    def test_preview_has_no_fake_feedback_and_never_opens_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"preview.csv"
            with patch.object(app, 'Canable') as hardware, patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(app.main(self.arguments(path)+['--preview']), 0)
                hardware.assert_not_called()
                hardware.detect.assert_not_called()
            with path.open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertGreater(len(rows), 10)
            self.assertEqual(tuple(rows[0]), COLUMNS)
            self.assertEqual(float(rows[0]['target_position_rad']), 0)
            self.assertTrue(all(row['position_rad'] == row['velocity_rad_s'] == '' for row in rows))

    def test_existing_csv_refused_before_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'exists.csv'
            path.write_text('keep', encoding='utf-8')
            with patch.object(app, 'Canable') as hardware, patch('sys.stdout'), patch('sys.stderr'):
                self.assertEqual(app.main(self.arguments(path)), 1)
                hardware.assert_not_called()
            self.assertEqual(path.read_text(encoding='utf-8'), 'keep')

    def test_bad_parameters_rejected_before_hardware(self):
        for option, value in (('--start-hz', '0'), ('--stop-hz', '.2'), ('--stop-hz', '100'),
                              ('--amplitude', 'nan'), ('--kp', '0'), ('--kd', '-1'),
                              ('--rate', '10'), ('--duration', '-1'), ('--ramp', '0'),
                              ('--home-speed', 'inf'), ('--home-timeout', '.01')):
            with self.subTest(option=option, value=value), patch.object(app, 'Canable') as hardware, patch('sys.stderr'):
                with self.assertRaises(SystemExit):
                    app.parse_args(self.arguments('unused.csv')+[option, value])
                hardware.assert_not_called()

    def test_adapter_selection_prefers_match_and_only_falls_back_to_singleton(self):
        for preferred, serials, expected in (
            ('A', ['A', 'B'], 'A'), ('wrong', ['B'], 'B'),
            (None, ['B'], 'B'), ('A', ['A'], 'A'),
            ('wrong', [], None), (None, [], None),
            ('wrong', ['A', 'B'], None), (None, ['A', 'B'], None),
            ('A', ['A', 'A'], None),
        ):
            with self.subTest(preferred=preferred, serials=serials), \
                 patch.object(app.Canable, 'detect', return_value=[{'serial_number': s} for s in serials]), \
                 patch('sys.stdout'):
                if expected is None:
                    with self.assertRaises(OSError):
                        app.resolve_serial(preferred)
                else:
                    self.assertEqual(app.resolve_serial(preferred), expected)

    def test_missing_adapter_fails_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'empty.csv'
            with patch.object(app, 'Canable') as hardware, patch('sys.stdout'), patch('sys.stderr'):
                hardware.detect.return_value = []
                self.assertEqual(app.main(self.arguments(path)), 1)
                hardware.assert_not_called()

    def test_empty_sn_is_allowed_and_normalized_without_discovery_during_parse(self):
        with patch.object(app.Canable, 'detect') as detect:
            args = app.parse_args(['--kp', '20', '--kd', '1', '--serial', '  '])
            self.assertIsNone(args.serial)
            detect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
