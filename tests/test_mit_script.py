import unittest
from unittest.mock import patch

from scripts.mit_control import main, parse_args, parse_console_command
from motor_platform import MotorState, MitCommand


class MitScriptTests(unittest.TestCase):
    def test_defaults_and_requested_command(self):
        args, command = parse_args(["--position","1.5","--velocity","-0.2","--torque","0.1","--kp","20","--kd","1"])
        self.assertEqual(args.id,1)
        self.assertEqual(args.rate,1000)
        self.assertEqual(args.duration,0)
        self.assertEqual((command.position,command.velocity,command.torque,command.kp,command.kd),(1.5,-0.2,0.1,20,1))

    def test_zero_duration_selects_continuous_mode(self):
        args, _ = parse_args(["--duration", "0"])
        self.assertEqual(args.duration, 0)

    def test_console_set_and_operations(self):
        action, command = parse_console_command("set 1.5 -0.2 0.1 20 1")
        self.assertEqual(action, "set")
        self.assertEqual(command, MitCommand(1.5, -0.2, 0.1, 20, 1))
        self.assertEqual(parse_console_command("rate 500"), ("rate", 500.0))
        for text in ("stop", "enable", "disable", "status", "help", "quit", "q", "exit"):
            self.assertIn(parse_console_command(text)[0],
                          {"stop", "enable", "disable", "status", "help", "quit"})

    def test_invalid_console_commands(self):
        for text in ("set 1 2", "set 0 0 nan 0 0", "set 0 0 0 -1 0",
                     "rate 0", "rate nan", "enable now", "unknown"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_console_command(text)

    def test_argument_parsing_does_not_open_devices(self):
        with patch("motor_platform.bench.Canable") as factory:
            parse_args([])
            factory.assert_not_called()

    def test_invalid_command_before_hardware(self):
        with patch("motor_platform.bench.Canable") as factory, patch("sys.stderr"):
            for argv in (["--id","128"],["--kp","-1"],["--torque","nan"],["--rate","0"],["--duration","-1"],["--duration","nan"]):
                with self.assertRaises(SystemExit):parse_args(argv)
            factory.assert_not_called()

    def test_real_run_flow_with_fake_hardware(self):
        state=MotorState(1,0,0,0,25,30,0,1)
        with patch('scripts.mit_control.app.Bench') as factory, patch('scripts.mit_control.app.StateRecorder'), patch('builtins.print'):
            bench=factory.return_value.__enter__.return_value
            bench.motor.return_value.receive_state.return_value=state
            control=bench.start.return_value.__enter__.return_value
            motor=control.motor.return_value
            motor.state.return_value=state
            motor.stats.return_value=dict(fault=None,periodic_active=True,tx_count=1,rx_count=1,actual_tx_hz=1000)
            main(['--duration','0.02','--position','0.1','--kp','2','--no-console'])
            bench.motor.assert_called_once_with(1)
            control.motor.assert_called_once_with(1)
            motor.enable.assert_called_once()
            motor.set_mit.assert_called_with(MitCommand(position=0.1,kp=2))
            motor.disable.assert_called_once()

    def test_console_enable_always_resends_enabling_rpdo(self):
        state = MotorState(1, 0, 0, 0, 25, 30, 0, 1)

        class Dashboard:
            def __init__(self):
                self.commands = iter(("enable", "quit"))
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return None
            def poll(self):
                return next(self.commands)
            def render(self, **_):
                pass

        with patch('scripts.mit_control.app.Bench') as factory, \
             patch('scripts.mit_control.app.StateRecorder'), \
             patch('scripts.mit_control.app.ConsoleDashboard', return_value=Dashboard()), \
             patch('builtins.print'):
            bench = factory.return_value.__enter__.return_value
            bench.motor.return_value.receive_state.return_value = state
            control = bench.start.return_value.__enter__.return_value
            motor = control.motor.return_value
            motor.state.return_value = state
            motor.stats.return_value = dict(
                fault=None, periodic_active=True, tx_count=1, rx_count=1,
                actual_tx_hz=1000, target_hz=1000, skipped_periods=0,
                feedback_limited_periods=0, max_lateness_s=0,
            )
            main([])
            self.assertEqual(motor.enable.call_count, 2)
            motor.disable.assert_called_once()


if __name__ == "__main__":unittest.main()
