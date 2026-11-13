import csv
import io
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

from motor_platform import MotorBus, MotorState
from scripts.factory_test.measurements.tn_curve.recording import CurveRecorder
from scripts.factory_test.measurements.tn_curve import app


def feedback(t, torque):
    return MotorState(1, 0, 0, torque, 25, 26, 0, t)


class RecordingTests(unittest.TestCase):
    def test_main_creates_only_one_csv(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(app, 'detect_dynamometers', return_value=['COM_TEST']), \
             patch.object(app, 'resolve_power_port', return_value='COM_POWER'), \
             patch.object(app, 'Dynamometer'), patch.object(app, 'PowerSupply'), \
             patch.object(app, 'TerminalConsole'), patch.object(app, 'Bench'), \
             patch.object(app, 'run') as run, patch('builtins.print'):
            app.main(['--id', '1', '--serial', 'test', '--torque', '12'], output_dir=Path(folder)/"result")
            self.assertEqual(run.call_args.kwargs['torque'], 12)
            files = [p for p in Path(folder).rglob('*') if p.is_file()]
            self.assertEqual([p.name for p in files], ['curve.csv'])
            with files[0].open(encoding='utf-8') as f:
                self.assertEqual(next(csv.reader(f)), [
                    'time_ms', 'motor_speed_rad_s', 'motor_torque',
                    'motor_temperature_c', 'driver_temperature_c',
                    'motor_speed_smooth_rad_s', 'motor_torque_smooth',
                    'dyno_torque_nm', 'dyno_speed_rad_s', 'dyno_power_w',
                    'dyno_speed_smooth_rad_s', 'dyno_torque_smooth_nm'])
    def test_every_motor_frame_is_one_csv_row(self):
        output = io.StringIO()
        progress = app.Progress(0)
        progress.loading_at = 10
        frames = CurveRecorder(csv.writer(output), progress, .1)
        frames.add_dyno([SimpleNamespace(received_at=10, torque=1, speed=2, power=2)])
        frames.record([feedback(10.001, 1), feedback(10.002, 2), feedback(10.003, 3)])
        frames.add_dyno([SimpleNamespace(received_at=10.004, torque=4, speed=2, power=8)])
        frames.record([])
        frames.finish(10.004)
        rows = list(csv.reader(io.StringIO(output.getvalue())))
        self.assertEqual([float(row[0]) for row in rows], [0, 1, 2, 3, 4])
        self.assertEqual([float(row[5]) for row in rows if row[5]], [1, 2, 3])
        self.assertEqual([float(row[1]) for row in rows if row[1]], [1, 4])
        self.assertEqual(frames.count, 5)

    def test_start_and_stall_boundary_within_one_batch(self):
        writer = Mock()
        progress = app.Progress(0)
        progress.loading_at = 10
        frames = CurveRecorder(writer, progress, .1)
        states = [feedback(t, i) for i, t in enumerate([9.9, 10.01, 10.05, 10.111, 10.12])]
        frames.add_dyno([SimpleNamespace(received_at=s.received_at-.001, torque=0, speed=0, power=0)
                        for s in states])
        self.assertTrue(frames.record(states))
        self.assertEqual(frames.count, 6)
        self.assertAlmostEqual(float(writer.writerow.call_args.args[0][0]), 111)
        frames.record([feedback(10.2, 99)])
        self.assertEqual(frames.count, 6)

    def test_usb_batch_preserves_all_dyno_points_at_same_time(self):
        writer = Mock()
        progress = app.Progress(0)
        progress.loading_at = 10
        frames = CurveRecorder(writer, progress, .1)
        frames.add_dyno([SimpleNamespace(received_at=10.01, torque=i, speed=2, power=2*i)
                        for i in range(5)])
        frames.record([feedback(10.02, 20)])
        frames.finish(10.02)
        rows = [call.args[0] for call in writer.writerow.call_args_list]
        self.assertEqual([row[1] for row in rows[:5]], list(range(5)))
        self.assertEqual(frames.dyno_count, 5)
        self.assertEqual(frames.motor_count, 1)

    def test_receive_queue_preserves_each_dispatch_once(self):
        bus = MotorBus(Mock())
        motor = bus.motor(1)
        motor.start_recording(capacity=2)
        frame = Mock(is_error=False, is_extended=False, is_remote=False, can_id=0x191)
        with patch('motor_platform.components.motor.client.parse_state', side_effect=[
                feedback(1, 1), feedback(2, 2), feedback(3, 3)]):
            for _ in range(3): bus._dispatch(frame)
        self.assertEqual(motor.recording_dropped, 1)
        self.assertEqual([s.torque for s in motor.drain_states()], [2, 3])
        self.assertEqual(motor.drain_states(), [])

    def test_stop_ramps_torque_before_disabling_and_unloading(self):
        events = []
        motor, power, ui = Mock(), Mock(), Mock()
        motor.state.return_value = feedback(0, 0)
        motor.stats.return_value = {'fault': None}
        motor.set_mit.side_effect = lambda cmd: events.append(('torque', cmd.torque))
        motor.disable.side_effect = lambda: events.append(('disable', 0))
        def set_current(v):
            events.append(('current', v))
            return v
        power.set_current.side_effect = set_current
        power.disable.side_effect = lambda: events.append(('power_off', 0))
        pending = Mock()
        pending.result.return_value = .10
        with patch.object(app, 'STOP_TORQUE_RAMP_S', .01):
            app.graceful_stop(motor, power, pending, -30, ui, Mock())
        off = events.index(('disable', 0))
        torques = [v for k, v in events[:off] if k == 'torque']
        self.assertEqual(torques[-1], 0)
        self.assertEqual(torques, sorted(torques))
        self.assertEqual(events[off+1:], [('power_off', 0)])
        power.set_current.assert_not_called()

    def test_stop_fault_does_not_delay_disable_for_unload(self):
        motor, power = Mock(), Mock()
        motor.state.side_effect = TimeoutError('feedback stale')
        with self.assertRaises(TimeoutError):
            app.graceful_stop(motor, power, None, 30, Mock(), Mock())
        motor.disable.assert_called_once()
        power.set_current.assert_not_called()
