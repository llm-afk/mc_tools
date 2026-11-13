import csv
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from scripts.factory_test.measurements.kt_curve import app
from scripts.factory_test.measurements.kt_curve.recording import Recorder, COLUMNS, OUTPUT_COLUMNS


def state(at):
    return NS(received_at=at, velocity=2., torque=.3, motor_temperature=30.,
              board_temperature=31., error_code=0, speed=2., power=.6)


class FakeMotor:
    def __init__(self, clock, events):
        self.clock, self.events = clock, events
        self.node_id = 1
        self.commands = []
        self.failure = None
    def start_recording(self): pass
    def enable(self): self.events.append('motor_on')
    def disable(self): self.events.append('motor_off')
    def state(self, **kwargs):
        if self.failure:
            raise self.failure
        return state(self.clock[0])
    def stats(self): return dict(fault=None, actual_tx_hz=1000, recording_dropped=0)
    def drain_states(self): return [state(self.clock[0])]
    def set_mit(self, command): self.commands.append((self.clock[0], command.torque))


class FakeDyno:
    def __init__(self, clock): self.clock = clock
    def state(self, **kwargs): return state(self.clock[0])
    def stats(self): return dict(dropped_samples=0)
    def drain(self): return [state(self.clock[0])]


class ConsistencyTests(unittest.TestCase):
    def args(self, *extra):
        return app.parse_args(['--serial', 'test-sn', '--torque', '30', *extra])

    def test_parameters_and_signed_ramp(self):
        args = self.args('--torque', '-20', '--voltage', '24', '--current', '3')
        self.assertEqual((args.id, args.torque, args.voltage, args.current), (1, -20, 24, 3))
        self.assertEqual([app.ramp_torque(t, -20) for t in (-1, 0, 1.5, 3, 4)], [0, 0, -10, -20, -20])
        self.assertIsNone(app.TORQUE_FF)
        for extra in (['--torque', 'nan'], ['--torque', '0'], ['--id', '128'],
                      ['--current', '-1'], ['--voltage', 'inf'], ['--serial', ''],
                      ['--current', '0.0001']):
            with self.subTest(extra=extra), patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                self.args(*extra)

    def test_sparse_rows_duplicate_usb_time_cutoff_and_command_history(self):
        rec = Recorder(10, 5)
        rec.command(11, 6)
        rec.command(13, 18)
        rec.add('motor', [state(9), state(12), state(15), state(15.001)])
        rec.add('dyno', [state(10.5), state(12), state(12)])
        stream = io.StringIO()
        rec.write(csv.writer(stream), self.args(), 'completed')
        rows = list(csv.DictReader(io.StringIO(stream.getvalue())))
        self.assertEqual(len(rows), 5)
        self.assertEqual([float(r['time_ms']) for r in rows], [500, 2000, 2000, 2000, 5000])
        self.assertEqual([float(r['motor_application_target_torque']) for r in rows], [0, 6, 6, 6, 18])
        self.assertEqual(sum(r['motor_torque'] != '' for r in rows), 2)
        self.assertTrue(all(not (r['motor_torque'] and r['dyno_torque_nm']) for r in rows))

    def exercise(self, operator=False, fail=False):
        clock, events, result = [100.], [], {}
        motor, dyno = FakeMotor(clock, events), FakeDyno(clock)
        ui = NS(app=NS(invalidate=lambda: None))
        ui.poll = lambda: 'quit' if operator and clock[0] >= 100.015 else None
        def sleep(dt):
            clock[0] += dt
            if fail and clock[0] >= 100.015:
                motor.failure = RuntimeError('feedback lost')
        with patch.object(app.time, 'perf_counter', lambda: clock[0]), patch.object(app.time, 'sleep', sleep), \
             patch.object(app, 'RAMP_DURATION_S', .05), patch.object(app, 'STOP_RAMP_S', .01), \
             patch.object(app, 'TARGET_UPDATE_S', .005):
            if fail:
                with self.assertRaisesRegex(RuntimeError, 'feedback lost'):
                    app.run(motor, dyno, ui, self.args(), result)
            else:
                app.run(motor, dyno, ui, self.args(), result)
        return clock, events, result, motor

    def test_complete_ramp_capture_cutoff_and_controlled_stop(self):
        clock, events, result, motor = self.exercise()
        self.assertEqual(events, ['motor_on', 'motor_off'])
        self.assertEqual(result['reason'], 'completed')
        self.assertAlmostEqual(result['recorder'].cutoff, 100.05)
        self.assertTrue(all(at <= 100.05 for at, _, _ in result['recorder'].events))
        self.assertEqual(max(torque for _, torque in motor.commands), 30)
        self.assertEqual(motor.commands[-1][1], 0)
        self.assertLess(clock[0], 100.08)

    def test_operator_stop_ramps_down(self):
        _, _, result, motor = self.exercise(operator=True)
        self.assertEqual(result['reason'], 'operator_stop')
        self.assertLess(result['recorder'].cutoff, 100.05)
        self.assertLess(max(t for _, t in motor.commands), 30)
        self.assertEqual(motor.commands[-1][1], 0)

    def test_fault_skips_ramp_and_disables(self):
        clock, events, result, _ = self.exercise(fail=True)
        self.assertEqual(result['reason'], 'error')
        self.assertEqual(events[-1], 'motor_off')
        self.assertLess(clock[0], 100.025)

    def test_main_power_order_and_single_csv(self):
        from contextlib import nullcontext
        from unittest.mock import MagicMock
        events = []
        power = MagicMock()
        power.__enter__.return_value = power
        power.disable.side_effect = lambda **kwargs: events.append('off')
        power.set_current.side_effect = lambda value: events.append(('current', value))
        power.set_voltage.side_effect = lambda value: events.append(('voltage', value))
        power.enable.side_effect = lambda: events.append('on')
        dyno = MagicMock()
        dyno.__enter__.return_value = dyno
        bench = MagicMock()
        bench.__enter__.return_value = bench
        bench.start.return_value = nullcontext(NS(motor=lambda node: None))
        def run(motor, dyno, ui, args, result):
            events.append('run')
            rec = Recorder(0, 5)
            rec.add('motor', [state(0), state(1)])
            rec.add('dyno', [state(.005)])
            result.update(recorder=rec, reason='completed')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, 'detect_dynamometers', return_value=['fake']), \
             patch.object(app, 'resolve_power_port', return_value='fake'), \
             patch.object(app, 'Dynamometer', return_value=dyno), \
             patch.object(app, 'PowerSupply', return_value=power), \
             patch.object(app, 'Canable'), patch.object(app, 'Bench', return_value=bench), \
             patch.object(app, 'TerminalConsole', return_value=nullcontext(None)), \
             patch.object(app, 'run', side_effect=run), \
             patch.object(app.time, 'sleep', side_effect=lambda seconds: events.append(('wait', seconds))), \
             patch('sys.stdout', io.StringIO()):
            app.main(['--serial', 'test-sn', '--torque', '30'], output_dir=Path(directory)/"result")
            files = [p for p in Path(directory).rglob('*') if p.is_file()]
            self.assertEqual(len(files), 1)
            with files[0].open() as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 2)
            self.assertEqual(list(rows[0]), ['time_ms', 'motor_torque', 'motor_temperature_c',
                'driver_temperature_c', 'motor_application_target_torque', 'motor_torque_smooth', 'dyno_torque_nm',
                'dyno_torque_smooth_nm'])
            self.assertEqual(files[0].name, 'kt_curve.csv')
            self.assertNotEqual(rows[0]['dyno_torque_smooth_nm'], '')
            self.assertEqual(rows[1]['dyno_torque_smooth_nm'], '')
            self.assertNotEqual(rows[0]['motor_torque_smooth'], '')
            self.assertNotIn('motor_speed_smooth_rad_s', rows[0])
            self.assertNotIn('dyno_speed_smooth_rad_s', rows[0])
        self.assertEqual(events, ['off', ('current', 0), ('voltage', 24.), ('current', 3.),
                                  'on', ('wait', .5), 'run', 'off', 'off'])


if __name__ == '__main__':
    unittest.main()
