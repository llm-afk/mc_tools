import unittest
import io
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch, Mock
from scripts.factory_test.measurements.tn_curve import app


def state(t, v):
    return SimpleNamespace(received_at=t, velocity=v)


class CurveTests(unittest.TestCase):
    def test_fractional_step_accumulates_without_rounding_drift(self):
        with patch.object(app, 'CURRENT_STEP_A', .0125):
            values = [0]+[app.load_target(i) for i in range(1, 81)]
            self.assertEqual(values[-1], 1.0)
            increments = [round(b-a, 3) for a,b in zip(values, values[1:])]
            self.assertEqual(set(increments), {.012, .013})
            self.assertEqual(app.load_target(10000), app.MAX_CURRENT_A)
    def test_load_period_includes_serial_transaction(self):
        power = Mock()
        power.set_current.return_value = .01
        with patch.object(app.time, 'perf_counter', return_value=10):
            current, next_step = app.set_load_current(power, .01)
        self.assertEqual(current, .01)
        self.assertAlmostEqual(next_step, 10+1/15)
        power.set_current.assert_called_once_with(.01)
    def test_torque_argument_overrides_without_mutating_default(self):
        default = app.TORQUE_FF
        self.assertEqual(app.parse_args(['--torque', '-12']).torque, -12)
        self.assertEqual(app.ramp_torque(.5, -12), -6)
        self.assertEqual(app.ramp_torque(2, -12), -12)
        self.assertEqual(app.TORQUE_FF, default)
        self.assertEqual(app.parse_args([]).torque, default)
        with patch('sys.stderr'):
            for value in ('0', 'nan', 'inf', '-inf'):
                with self.subTest(value=value), self.assertRaises(SystemExit):
                    app.parse_args(['--torque='+value])
    def test_linear_torque_ramp(self):
        for target in (30, -30):
            with patch.object(app, 'TORQUE_FF', target), patch.object(app, 'TORQUE_RAMP_S', 1):
                self.assertEqual(app.ramp_torque(0), 0)
                self.assertEqual(app.ramp_torque(.5), target/2)
                self.assertEqual(app.ramp_torque(1), target)
                self.assertEqual(app.ramp_torque(2), target)
    def test_plateau_with_ripple_and_isolated_spike(self):
        p = app.Progress(0)
        for i in range(201):
            speed = 40 if i == 95 else 20 + (.8 if i % 2 else -.8)
            ready = p.update(state(i*.01, speed))
        self.assertTrue(ready)

    def test_continued_acceleration_not_stable(self):
        p = app.Progress(0)
        for i in range(201):
            ready = p.update(state(i*.01, 20+i*.007))
        self.assertTrue(ready)  # 固定等待，不再以加速趋势阻止加载

    def test_stationary_is_not_stable_spin(self):
        p = app.Progress(0)
        self.assertFalse(p.update(state(0, 0)))
        self.assertFalse(p.update(state(.999, 0)))
        self.assertTrue(p.update(state(1, 0)))

    def test_stability_and_continuous_stall(self):
        p = app.Progress(0)
        self.assertFalse(p.update(state(0, -10)))
        self.assertTrue(p.update(state(1, -10.1)))
        self.assertTrue(p.update(state(2, -10)))
        p.loading_at = 2
        self.assertFalse(p.update(state(3, -0.2)))
        self.assertFalse(p.update(state(3.09, -0.2)))
        self.assertFalse(p.update(state(3.10, -1)))
        self.assertFalse(p.update(state(4, 0)))
        self.assertTrue(p.update(state(4.101, 0)))

    def test_cached_feedback_does_not_advance_hold(self):
        p = app.Progress(0)
        p.loading_at = 0
        for _ in range(1000):
            self.assertFalse(p.update(state(1, 0)))

    def test_raw_time_and_eight_columns(self):
        d = SimpleNamespace(received_at=10.0012345, torque=1, speed=2, power=2)
        m = SimpleNamespace(received_at=10.0012345, velocity=3, torque=4, motor_temperature=25, board_temperature=26)
        row = app.event_row('motor', m, 10)
        self.assertEqual(len(row), 8)
        self.assertAlmostEqual(float(row[0]), 1.2345)
        self.assertEqual(row[1:], ['', '', '', 3, 4, 25, 26])
        self.assertEqual(app.event_row('dyno', d, 10)[1:], [1, 2, 2, '', '', '', ''])

    def test_config_requires_explicit_torque(self):
        with patch.object(app, 'TORQUE_FF', None):
            with self.assertRaises(ValueError): app.validate()

    def test_full_loop_stall_disables_motor(self):
        motor = Mock(node_id=1)
        received = []
        def drain_motor():
            result = received[:]
            received.clear()
            return result
        motor.drain_states.side_effect = drain_motor
        motor.stats.return_value = {'fault': None, 'actual_tx_hz': 1000}
        counter = [0]
        def feedback(**kwargs):
            counter[0] += 1
            sample = SimpleNamespace(received_at=app.time.perf_counter(),
                velocity=10 if counter[0] < 8 else 0, torque=.2,
                motor_temperature=25, board_temperature=25, error_code=0)
            received.append(sample)
            return sample
        motor.state.side_effect = feedback
        dyno = Mock()
        dyno.stats.return_value = {'dropped_samples': 0}
        dyno.state.return_value = SimpleNamespace(speed=10, torque=.2, power=2)
        dyno.drain.side_effect = lambda: [SimpleNamespace(received_at=app.time.perf_counter()-.01,
                                                         torque=.1, speed=10, power=1)]
        ui = Mock()
        ui.poll.return_value = None
        power = Mock()
        power.set_current.side_effect = lambda v: v
        summary = {}
        with patch.object(app, 'TORQUE_FF', .2), patch.object(app, 'TORQUE_SETTLE_S', .003), \
             patch.object(app, 'TORQUE_RAMP_S', .001), \
             patch.object(app, 'STOP_TORQUE_RAMP_S', .002), \
             patch.object(app, 'STALL_HOLD_S', .003), ThreadPoolExecutor(max_workers=1) as pool:
            reason = app.run(motor, dyno, power, pool, ui, Mock(), summary)
        self.assertEqual(reason, 'stall_confirmed')
        motor.disable.assert_called_once()
        self.assertEqual(power.set_current.call_args_list[0].args, (app.load_target(1),))
        power.disable.assert_called_once()

    def test_motor_error_preserved_when_disable_fails(self):
        motor = Mock()
        motor.enable.side_effect = RuntimeError('enable failed')
        motor.disable.side_effect = RuntimeError('disable failed')
        with self.assertRaisesRegex(RuntimeError, 'enable failed') as caught:
            app.run(motor, Mock(), Mock(), Mock(), Mock(), Mock(), {})
        self.assertTrue(any('工装停机未确认' in n and 'disable failed' in n for n in caught.exception.__notes__))
        with patch.object(app, 'TORQUE_FF', -0.2):
            app.validate()
        with patch.object(app, 'TORQUE_FF', 0.2), patch.object(app, 'CURRENT_STEP_A', .0001):
            with self.assertRaises(ValueError): app.validate()


if __name__ == '__main__':
    unittest.main()
