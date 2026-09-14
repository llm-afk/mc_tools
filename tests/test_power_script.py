import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from motor_platform.components.power_supply.discovery import detect_power_supplies, resolve_power_port
from motor_platform.components.power_supply import PowerSupplyError
from scripts.power_control.app import parse_command, main


class PowerScriptTests(unittest.TestCase):
    def test_parser_validates_before_control(self):
        self.assertEqual(parse_command('voltage 12'), ('voltage', 12))
        self.assertEqual(parse_command('current 1.25'), ('current', 1.25))
        self.assertEqual(parse_command('enable'), ('on', None))
        for text in ('voltage nan', 'current -1', 'rate 0', 'on now', 'unknown'):
            with self.assertRaises(ValueError):
                parse_command(text)

    def test_detection_only_identifies_and_closes(self):
        events = []
        class FakePower:
            def __init__(self, *, port, **kwargs):
                self.port = port
            def __enter__(self):
                events.append(('open', self.port))
                if self.port == 'COM_BAD':
                    raise PowerSupplyError('wrong device')
                self.identity = 'UDP6721'
                return self
            def __exit__(self, *_):
                events.append(('close', self.port))
        with patch('motor_platform.components.power_supply.discovery.PowerSupply', FakePower):
            self.assertEqual(detect_power_supplies(ports=['COM_BAD', 'COM_OK']), {'COM_OK': 'UDP6721'})
        self.assertIn(('close', 'COM_OK'), events)

    def test_auto_requires_exactly_one_device(self):
        with patch('motor_platform.components.power_supply.discovery.detect_power_supplies') as detect:
            detect.return_value = {}
            with self.assertRaises(PowerSupplyError):
                resolve_power_port()
            detect.return_value = {'COM1': 'UDP6721', 'COM2': 'UDP6721'}
            with self.assertRaises(PowerSupplyError):
                resolve_power_port()
            detect.return_value = {'COM26': 'UDP6721'}
            self.assertEqual(resolve_power_port(), 'COM26')
            detect.reset_mock()
            self.assertEqual(resolve_power_port('COM5'), 'COM5')
            detect.assert_not_called()

    def test_scan_does_not_start_control_script(self):
        with patch('scripts.power_control.app.detect_power_supplies', return_value={'COM26': 'UDP6721'}), \
             patch('scripts.power_control.app.PowerSupply') as power, patch('builtins.print'):
            main(['--scan'])
            power.assert_not_called()

    def test_script_start_and_exit_output_policy(self):
        from motor_platform import PowerState
        for keep in (False, True):
            with self.subTest(keep=keep), tempfile.TemporaryDirectory() as folder, \
                 patch('scripts.power_control.app.__file__', str(Path(folder)/'app.py')), \
                 patch('scripts.power_control.app.PowerSupply') as factory, \
                 patch('scripts.power_control.app.TerminalConsole') as terminal, \
                 patch('builtins.print'):
                power = factory.return_value.__enter__.return_value
                power.read_state.return_value = PowerState(12, 0, 1, 12, 1, False, 'CV')
                terminal.return_value.__enter__.return_value.poll.side_effect = [None, 'quit']
                main(['--port', 'COM_TEST'] + (['--keep-output'] if keep else []))
                power.enable.assert_not_called()
                power.set_voltage.assert_not_called()
                self.assertEqual(power.disable.call_count, 0 if keep else 1)
                self.assertEqual(len(list((Path(folder)/'data').glob('*.csv'))), 1)
