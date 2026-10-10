import threading
import unittest
from unittest.mock import patch

from motor_platform import MitCommand, MotorBus
from motor_platform.runtime import ControlRuntime
from test_runtime import Transport


class RuntimeSourceTests(unittest.TestCase):
    def prepare(self):
        transport = Transport()
        bus = MotorBus(transport)
        runtime = ControlRuntime(bus, {'J1': bus.motor(1)}, rate_hz=1000)
        channel = runtime.motor('J1')
        channel._active, channel._expires, channel._next = True, 10, 1
        return transport, runtime, channel

    def test_source_uses_current_time_and_observer_only_follows_successful_write(self):
        tr, rt, channel = self.prepare()
        generated, sent = [], []
        def source(now):
            generated.append(now)
            return MitCommand(position=now, kp=20)
        def observer(at, command):
            self.assertEqual(len(tr.sent), len(sent)+1)
            sent.append((at, command.position))
        channel._source, channel._on_sent = source, observer
        with patch('motor_platform.runtime.time.perf_counter', return_value=1.0105):
            rt._tick(1.0105)
        self.assertEqual(generated, [1.0105])
        self.assertEqual(sent, [(1.0105, 1.0105)])
        self.assertEqual(channel.stats()['skipped_periods'], 10)
        tr.send = lambda frame: (_ for _ in ()).throw(OSError('failed USB write'))
        with self.assertRaises(OSError):
            rt._tick(1.012)
        self.assertEqual(len(sent), 1)

    def test_invalid_source_cannot_send_a_malformed_command(self):
        for result in (None, MitCommand(position=float('nan')), MitCommand(kp=999)):
            tr, rt, channel = self.prepare()
            channel._source = lambda now: result
            with self.assertRaises((ValueError, TypeError)):
                rt._tick(1)
            self.assertEqual(tr.sent, [])

    def test_gap_statistics_measure_successive_writes_without_catchup(self):
        tr, rt, channel = self.prepare()
        channel._source = lambda now: MitCommand(position=now)
        for at in (1.0, 1.001, 1.021):
            with patch('motor_platform.runtime.time.perf_counter', return_value=at):
                rt._tick(at)
        stats = channel.stats()
        self.assertAlmostEqual(stats['max_tx_gap_s'], .020)
        self.assertEqual(stats['tx_gaps_over_5ms'], 1)
        self.assertEqual(stats['max_usb_write_s'], 0)
        self.assertEqual(len(tr.sent), 3)

    def test_source_is_not_evaluated_when_inactive_expired_or_backpressured(self):
        for mode in ('inactive', 'expired', 'backpressure'):
            tr, rt, channel = self.prepare()
            channel._source = lambda now: self.fail('source should not run')
            if mode == 'inactive':
                channel._active = False
            elif mode == 'expired':
                channel._expires = .9
            else:
                channel._inflight, channel._last_rx = 4, 1
            rt._tick(1)
            self.assertFalse(any(frame.can_id == 0x401 for frame in tr.sent))

    def test_source_exception_stops_worker_and_disables_motor(self):
        transport = Transport()
        bus = MotorBus(transport)
        runtime = ControlRuntime(bus, {'J1': bus.motor(1)}, rate_hz=200).start()
        try:
            channel = runtime.motor('J1')
            channel.enable()
            def broken(now):
                raise ValueError('trajectory failure')
            channel.set_mit_source(broken)
            runtime._thread.join(timeout=2)
            self.assertFalse(runtime._thread.is_alive())
            self.assertFalse(transport.enabled[1])
            with self.assertRaisesRegex(RuntimeError, 'trajectory failure'):
                runtime.close()
        finally:
            if runtime._thread.is_alive():
                runtime.close()

    def test_set_mit_and_reenable_remove_source_and_observer(self):
        transport = Transport()
        bus = MotorBus(transport)
        with ControlRuntime(bus, {'J1': bus.motor(1)}, rate_hz=200).start() as runtime:
            channel = runtime.motor('J1')
            callback_threads = []
            channel.enable()
            done = threading.Event()
            def observer(at, command):
                callback_threads.append(threading.get_ident())
                done.set()
            channel.set_mit_source(lambda now: MitCommand(position=now), on_sent=observer)
            self.assertTrue(done.wait(1))
            self.assertEqual(set(callback_threads), {runtime._thread.ident})
            channel.set_mit(MitCommand(position=2))
            self.assertIsNone(channel._source)
            self.assertIsNone(channel._on_sent)
            channel.disable()
            channel.set_mit_source(lambda now: MitCommand(position=99))
            channel.enable()
            self.assertIsNone(channel._source)
            self.assertEqual(channel._target, MitCommand())


if __name__ == '__main__':
    unittest.main()
