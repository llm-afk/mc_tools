import csv
from contextlib import ExitStack, nullcontext
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import MagicMock, patch

from scripts.torque_impact import app


def sample(at=0, torque=3., velocity=0., temperature=30.):
    return NS(received_at=at, torque=torque, velocity=velocity, speed=abs(velocity),
              motor_temperature=temperature, board_temperature=temperature, error_code=0)


class Rig:
    def __init__(self, pulse=1.):
        self.now, self.command = 100., 0.
        self.pulse = pulse
        self.experiment = None
        self.events = []
        self.fault = False

    def state(self, **kwargs):
        if self.fault:
            raise RuntimeError('feedback timeout')
        phase = self.experiment.commands[-1][1] if self.experiment else ''
        scale = self.pulse if phase.startswith('impact') else 1.
        return sample(self.now, self.command*.1*scale)

    def stats(self):
        return dict(fault=None, recording_dropped=0, dropped_samples=0)
    def drain(self): return [self.state()] if not self.fault else []
    def drain_states(self): return self.drain()
    def start_recording(self): pass
    def set_mit(self, command): self.command = command.torque
    def enable(self): self.events.append('motor_on')
    def disable(self): self.events.append('motor_off'); self.command = 0.
    def receive_state(self, **kwargs): return self.state()
    def sleep(self, duration): self.now += duration


class ImpactTests(unittest.TestCase):
    def test_tail_uses_shorter_stable_window_without_extending_pulse(self):
        rig = Rig()
        e = app.Experiment(rig, rig, NS(), self.args(), {})
        for i in range(70, 101):
            s = sample(i/1000, torque=3.95 if i < 84 else 3.42)
            e.events.extend([(s.received_at, 'motor', s), (s.received_at, 'dyno', s)])
        reading = e.measure_tail(.1)
        self.assertEqual(reading['window_s'], .015)
        self.assertEqual(reading['magnitude_nm'], 3.42)
        self.assertEqual(rig.command, 0)
        for _, _, s in e.events:
            s.torque = 1 if round(s.received_at*1000) % 2 else 4
        self.assertGreater(e.measure_tail(.1)['spread_nm'], 0)

    def args(self): return NS(id=1, serial='test', torque=30.)

    def fast(self, rig):
        stack = ExitStack()
        for name, value in dict(MEASURE_HOLD_S=.05, MEASURE_WINDOW_S=.012,
                                MIN_MEASURE_WINDOW_S=.008, PULSE_S=.02,
                                REST_S=.02, RECOVER_S=.02,
                                STOP_RAMP_S=.02, CYCLES=2).items():
            stack.enter_context(patch.object(app, name, value))
        stack.enter_context(patch.object(app.time, 'perf_counter', lambda: rig.now))
        stack.enter_context(patch.object(app.time, 'sleep', rig.sleep))
        return stack

    def test_args_reject_before_hardware(self):
        for torque in ('0', '-30', 'nan', 'inf'):
            with self.subTest(torque=torque), patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                app.parse_args(['--serial', 'test', '--torque', torque])
        self.assertEqual(app.parse_args(['--serial', 'test', '--torque', '30']).torque, 30)

    def test_short_default_sequence(self):
        rig, result = Rig(), {}
        with patch.object(app.time, 'perf_counter', lambda: rig.now), \
             patch.object(app.time, 'sleep', rig.sleep):
            e = app.Experiment(rig, rig, NS(poll=lambda: None, app=NS(invalidate=lambda: None)), self.args(), result)
            rig.experiment = e
            with patch.object(e, 'segment', wraps=e.segment) as segments:
                e.execute()
            schedule = [(call.args[0], call.args[1]) for call in segments.call_args_list]
        expected = [('before_positive_ramp', .05), ('before_positive_hold', .1), ('zero_rest', .9),
                    ('before_negative_ramp', .05), ('before_negative_hold', .1), ('zero_rest', .9), ('zero_rest', 1.)]
        expected += [('impact_positive', .1), ('zero_rest', .9),
                     ('impact_negative', .1), ('zero_rest', .9)]*5
        self.assertEqual(schedule, expected)
        self.assertEqual(result['completed_pulses'], 10)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(rig.command, 0)
        self.assertLess(rig.now-100, 13.16)

    def test_measurement_rejects_noise_and_missing_samples_but_allows_dynamic_speed(self):
        good = [sample() for _ in range(20)]
        self.assertEqual(app.summarize(good, good)['magnitude_nm'], 3.)
        dynamic = [sample(velocity=30) for _ in good]
        self.assertEqual(app.summarize(dynamic, dynamic)['magnitude_nm'], 3.)
        for dynos, motors in ((good[:1], good),):
            with self.assertRaises(app.InvalidTest): app.summarize(dynos, motors)

    def test_rest_uses_motor_speed_not_stale_dyno_speed(self):
        rig = Rig()
        ui = NS(poll=lambda: None, app=NS(invalidate=lambda: None))
        with self.fast(rig):
            e = app.Experiment(rig, rig, ui, self.args(), {})
            rig.experiment = e
            # 同一次 check 的对象在模拟中共用字段；单独验证实际分路判断。
            with patch.object(e, 'segment'), patch.object(e, 'check', return_value=(sample(velocity=.02), sample(velocity=30))):
                e.rest(.9)
            with patch.object(e, 'segment'), patch.object(e, 'check', return_value=(sample(velocity=.6), sample(velocity=0))):
                e.rest(.9)

    def test_retention_threshold_and_zero_output(self):
        def reading(t):
            return dict(torque_nm=t, magnitude_nm=abs(t), motor_temperature_c=30, driver_temperature_c=30)
        before = dict(positive=reading(3), negative=reading(-3))
        for value, status in ((2.7, 'PASS'), (2.69, 'FAIL'), (0, 'FAIL')):
            pulses = [dict(reading(value), direction='positive'),
                      dict(reading(-value), direction='negative')]
            self.assertEqual(app.judge(before, pulses)['status'], status)
        with self.assertRaises(app.InvalidTest):
            app.judge(before, [])

    def test_complete_sequence_and_raw_csv(self):
        for after, expected in ((1., 'PASS'), (.75, 'FAIL'), (0., 'FAIL')):
            rig, result = Rig(pulse=after), {}
            with self.fast(rig):
                e = app.Experiment(rig, rig, NS(poll=lambda: None, app=NS(invalidate=lambda: None)), self.args(), result)
                rig.experiment = e
                e.execute()
            self.assertEqual(result['status'], expected)
            self.assertEqual(result['completed_pulses'], 4)
            self.assertEqual([p['direction'] for p in result['pulses']], ['positive', 'negative']*2)
            self.assertEqual(all(p['valid'] for p in result['pulses']), expected == 'PASS')
            self.assertNotIn('after', result)
            with tempfile.TemporaryDirectory() as d:
                path = Path(d)/'process.csv'
                e.save_csv(path)
                with path.open() as f: rows = list(csv.DictReader(f))
                self.assertEqual(len(rows), len(e.events))
                self.assertTrue(all(not(r['dyno_torque_nm'] and r['motor_torque']) for r in rows))
                self.assertEqual(list(rows[0]), ['time_ms', 'motor_torque', 'motor_temperature_c',
                    'driver_temperature_c', 'motor_application_target_torque',
                    'dyno_torque_nm', 'dyno_speed_rad_s'])
                times = [float(r['time_ms']) for r in rows]
                self.assertEqual(times, sorted(times))

    def test_ineffective_pulse_and_delayed_pulse_are_invalid(self):
        rig, result = Rig(pulse=.1), {}
        with self.fast(rig):
            e = app.Experiment(rig, rig, NS(poll=lambda: None, app=NS(invalidate=lambda: None)), self.args(), result)
            rig.experiment = e
            e.execute()
            self.assertEqual(result['status'], 'FAIL')
            self.assertEqual(rig.command, 0)
            self.assertEqual(result['completed_pulses'], 4)
            # 休息段可以有Windows调度停顿；后台CAN会继续发零目标。
            with patch.object(app.time, 'sleep', lambda dt: rig.sleep(.1)):
                e.rest(.2)
    def test_main_shutdown_and_reports_on_success_and_fault(self):
        for fault in (False, True):
            rig = Rig()
            power = MagicMock()
            power.__enter__.return_value = power
            power.enable.side_effect = lambda: rig.events.append('power_on')
            power.disable.side_effect = lambda **kw: rig.events.append('power_off')
            bench = MagicMock()
            bench.__enter__.return_value = bench
            bench.start.return_value = nullcontext(NS(motor=lambda _: rig))
            real_execute = app.Experiment.execute
            def execute(e):
                rig.experiment = e
                if fault: rig.fault = True
                return real_execute(e)
            with tempfile.TemporaryDirectory() as d, self.fast(rig), \
                 patch.object(app, '__file__', str(Path(d)/'app.py')), \
                 patch.object(app, 'detect_dynamometers', return_value=['fake']), \
                 patch.object(app, 'Dynamometer', return_value=nullcontext(rig)), \
                 patch.object(app, 'resolve_power_port', return_value='fake'), \
                 patch.object(app, 'PowerSupply', return_value=power), \
                 patch.object(app, 'Canable'), patch.object(app, 'Bench', return_value=bench), \
                 patch.object(app, 'TerminalConsole', return_value=nullcontext(NS(poll=lambda: None, app=NS(invalidate=lambda: None)))), \
                 patch.object(app.Experiment, 'execute', execute), patch('sys.stdout', io.StringIO()):
                result = app.main(['--serial', 'test', '--torque', '30'])
                self.assertEqual(result['status'], 'INVALID' if fault else 'PASS')
                self.assertEqual(rig.events, ['power_off', 'power_on', 'motor_on', 'motor_off', 'power_off'])
                files = [p for p in Path(d).rglob('*') if p.is_file()]
                self.assertEqual(sorted(p.name for p in files), ['process.csv', 'result.json'])
                with next(p for p in files if p.name == 'process.csv').open() as source:
                    self.assertEqual(next(csv.reader(source)), [
                        'time_ms', 'motor_torque', 'motor_temperature_c',
                        'driver_temperature_c', 'motor_application_target_torque',
                        'dyno_torque_nm', 'dyno_speed_rad_s'])
                saved = json.loads(next(p for p in files if p.name == 'result.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['status'], result['status'])


if __name__ == '__main__': unittest.main()
