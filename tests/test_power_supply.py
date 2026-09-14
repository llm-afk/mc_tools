import threading
import unittest
from unittest.mock import patch

from motor_platform.components.power_supply import PowerSupply, PowerSupplyError
from motor_platform.components.power_supply.protocol import parse_number, parse_output, parse_mode
from motor_platform.transports.serial_scpi import SerialScpi


class Stream:
    def __init__(self):
        self.writes = []
        self.closed = False
        self.short_write = False
        self.replies = {
            b'*IDN?\n': b'UNI-TREND,UDP6721,SN123,1.0\r\n',
            b'VOLT?\n': b'12.34\n', b'CURR?\n': b'1.234\n',
            b'OUTP?\n': b'OFF\n', b'OUTP:CVCC?\n': b'CV\n',
            b'MEAS:VOLT?\n': b'12.0\n', b'MEAS:CURR?\n': b'0.5\n',
        }
    def reset_input_buffer(self):
        pass
    def write(self, data):
        self.writes.append(data)
        return len(data)-1 if self.short_write else len(data)
    def read_until(self, terminator, size):
        return self.replies[self.writes[-1]]
    def close(self):
        self.closed = True


class PowerSupplyTests(unittest.TestCase):
    def setUp(self):
        self.stream = Stream()
        self.kwargs = None
        def factory(**kwargs):
            self.kwargs = kwargs
            return self.stream
        self.transport = SerialScpi('COM_TEST', serial_factory=factory)
        self.power = PowerSupply(port='COM_TEST', transport=self.transport)
        self.sleep = patch('motor_platform.transports.serial_scpi.time.sleep')
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.addCleanup(self.power.close)

    def test_open_only_identifies_and_close_does_not_switch_output(self):
        self.assertEqual(self.stream.writes, [])
        with self.power:
            self.assertIn('UDP6721', self.power.identity)
            self.assertEqual(self.kwargs['baudrate'], 9600)
            self.assertEqual((self.kwargs['bytesize'], self.kwargs['parity'], self.kwargs['stopbits']), (8, 'N', 1))
            self.assertEqual(self.kwargs['write_timeout'], 0.3)
        self.assertEqual(self.stream.writes, [b'*IDN?\n'])
        self.assertTrue(self.stream.closed)

    def test_known_command_bytes_and_readbacks(self):
        self.power.open()
        self.assertEqual(self.power.set_voltage(12.34), 12.34)
        self.assertEqual(self.power.set_current(1.234), 1.234)
        self.stream.replies[b'OUTP?\n'] = b'ON\n'
        self.assertTrue(self.power.enable())
        self.stream.replies[b'OUTP?\n'] = b'0\n'
        self.assertFalse(self.power.disable())
        self.power.set_ovp(13)
        self.power.set_ocp(2)
        self.power.remote()
        self.power.local()
        self.assertEqual(self.stream.writes[1:], [
            b'SOUR:VOLT 12.34\n', b'VOLT?\n', b'SOUR:CURR 1.234\n', b'CURR?\n',
            b'OUTP ON\n', b'OUTP?\n', b'OUTP OFF\n', b'OUTP?\n',
            b'SOUR:VOLT:PROT 13.00\n', b'SOUR:CURR:PROT 2.000\n', b'SYST:REM\n', b'SYST:LOC\n'])

    def test_state_is_fresh_and_mode_is_not_cached(self):
        self.power.open()
        state = self.power.read_state()
        self.assertEqual((state.voltage, state.current, state.power), (12, 0.5, 6))
        self.assertEqual((state.voltage_set, state.current_set, state.output, state.mode), (12.34, 1.234, False, 'CV'))
        self.stream.replies[b'OUTP:CVCC?\n'] = b'CC\n'
        self.assertEqual(self.power.read_state().mode, 'CC')
        self.assertGreater(self.power.measure().received_at, state.received_at)

    def test_invalid_setpoints_send_nothing(self):
        self.power.open()
        for method in (self.power.set_voltage, self.power.set_current, self.power.set_ovp, self.power.set_ocp):
            for bad in (-1, float('nan'), float('inf'), True, '12\nOUTP ON'):
                with self.subTest(method=method.__name__, value=bad), self.assertRaises(ValueError):
                    method(bad)
        with self.assertRaises(ValueError):
            self.power.set_output(1)
        self.assertEqual(self.stream.writes, [b'*IDN?\n'])

    def test_no_fake_success_on_mismatched_readback(self):
        self.power.open()
        with self.assertRaises(PowerSupplyError):
            self.power.set_voltage(20)
        with self.assertRaises(PowerSupplyError):
            self.power.enable()

    def test_wrong_device_releases_serial(self):
        self.stream.replies[b'*IDN?\n'] = b'OTHER DEVICE\n'
        with self.assertRaises(PowerSupplyError):
            self.power.open()
        self.assertTrue(self.stream.closed)
        with self.assertRaises(RuntimeError):
            self.power.enable()

    def test_timeout_prevents_late_reply_misassignment(self):
        self.power.open()
        self.stream.replies[b'MEAS:VOLT?\n'] = b'12'
        with self.assertRaises(TimeoutError):
            self.power.measure()
        count = len(self.stream.writes)
        with self.assertRaises(RuntimeError):
            self.power.measure()
        self.assertEqual(len(self.stream.writes), count)
        self.assertIsNone(self.power.disable(verify=False))
        self.assertEqual(self.stream.writes[-1], b'OUTP OFF\n')
        with self.assertRaises(RuntimeError):
            self.power.enable()

    def test_short_write_and_oversized_reply_fail(self):
        self.power.open()
        self.stream.short_write = True
        with self.assertRaises(OSError):
            self.power.set_voltage(12.34)
        self.power.close()
        self.stream.short_write = False
        self.stream.replies[b'*IDN?\n'] = b'x'*1024 + b'\n'
        with self.assertRaises(ValueError):
            self.power.open()
        self.assertTrue(self.stream.closed)

    def test_concurrent_calls_do_not_interleave_transactions(self):
        self.power.open()
        threads = [threading.Thread(target=self.power.measure) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.stream.writes[1:], [b'MEAS:VOLT?\n', b'MEAS:CURR?\n']*10)

    def test_invalid_responses_never_become_zero_or_false(self):
        for raw in ('', 'error', 'nan', 'inf'):
            with self.assertRaises(PowerSupplyError):
                parse_number(raw)
        with self.assertRaises(PowerSupplyError):
            parse_output('unknown')
        with self.assertRaises(PowerSupplyError):
            parse_mode('??')
