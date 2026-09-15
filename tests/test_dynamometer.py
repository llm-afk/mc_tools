import math
import queue
import time
import unittest

from motor_platform.components.dynamometer import Dynamometer, FrameParser, parse_frame
from motor_platform.components.dynamometer.protocol import crc16
from motor_platform.transports.serial_stream import SerialStream


def frame(torque=1234, speed=123):
    data = abs(torque).to_bytes(2, 'big') + (speed | (0x8000 if torque < 0 else 0)).to_bytes(2, 'big')
    return data + crc16(data).to_bytes(2, 'little')


def wait_for(predicate):
    deadline = time.perf_counter()+1
    while not predicate():
        if time.perf_counter() > deadline:
            raise AssertionError('receive worker timeout')
        time.sleep(0.001)


class Transport:
    def __init__(self):
        self.incoming = queue.Queue()
        self.opened = False
    def open(self):
        self.opened = True
    def read(self):
        try:
            value = self.incoming.get(timeout=0.005)
        except queue.Empty:
            return b''
        if isinstance(value, Exception):
            raise value
        return value
    def close(self):
        self.opened = False


class DynamometerTests(unittest.TestCase):
    def test_crc_and_signed_torque_unsigned_speed(self):
        self.assertEqual(crc16(b'123456789'), 0x4B37)
        s = parse_frame(frame(-1234, 32767), received_at=1)
        self.assertEqual(s.torque, -12.34)
        self.assertEqual(s.rpm, 327.67)
        self.assertAlmostEqual(s.speed, 327.67*math.pi/30)
        self.assertEqual(s.power, s.torque*s.speed)
        self.assertEqual(s.received_at, 1)
        with self.assertRaises(ValueError):
            parse_frame(b'123456')

    def test_all_split_boundaries_and_coalesced_frames(self):
        frames = [frame(i, i*2) for i in range(10)]
        data = b''.join(frames)
        for split in range(len(data)+1):
            p = FrameParser()
            self.assertEqual(p.feed(data[:split])+p.feed(data[split:]), frames)
        p = FrameParser()
        decoded = []
        for byte in data:
            decoded += p.feed(bytes([byte]))
        self.assertEqual(decoded, frames)

    def test_noise_crc_failure_and_resynchronization(self):
        p = FrameParser()
        f = frame()
        self.assertEqual(p.feed(b'noise'*100+f+f), [f, f])
        bad = bytearray(f)
        bad[0] ^= 1
        self.assertEqual(p.feed(bytes(bad)+f+f), [f, f])
        self.assertGreaterEqual(p.discarded_bytes, 506)
        p.feed(b'x'*10000)
        self.assertLessEqual(len(p._buffer), 11)

    def test_single_crc_is_not_sufficient_for_initial_sync(self):
        p = FrameParser()
        self.assertEqual(p.feed(frame()), [])
        self.assertEqual(p.feed(frame()), [frame(), frame()])

    def test_readonly_transport_uses_115200_8n1(self):
        options = {}
        class Serial:
            def __init__(self, **kwargs):
                options.update(kwargs)
                self.in_waiting = 3
            def read(self, size):
                options['read_size'] = size
                return b'abc'
            def close(self):
                pass
        t = SerialStream('COM_TEST', serial_factory=Serial)
        t.open()
        self.assertEqual(t.read(), b'abc')
        t.close()
        self.assertEqual((options['baudrate'], options['bytesize'], options['parity'], options['stopbits']), (115200, 8, 'N', 1))
        self.assertEqual(options['read_size'], 3)

    def test_empty_serial_buffer_waits_for_one_byte(self):
        sizes = []
        class Serial:
            in_waiting = 0
            def __init__(self, **kwargs): pass
            def read(self, size):
                sizes.append(size)
                return b''
            def close(self): pass
        stream = SerialStream('COM_TEST', serial_factory=Serial).open()
        self.assertEqual(stream.read(), b'')
        self.assertEqual(sizes, [1])
        stream.close()

    def test_worker_keeps_every_frame_and_reports_overflow(self):
        transport = Transport()
        with Dynamometer(port='COM_TEST', transport=transport, capacity=3) as d:
            transport.incoming.put(b''.join(frame(i) for i in range(10)))
            wait_for(lambda: d.stats()['rx_count'] == 10)
            self.assertEqual(d.state().torque, 0.09)
            self.assertEqual(d.stats()['dropped_samples'], 7)
            self.assertEqual([s.raw_torque for s in d.drain()], [7, 8, 9])
            self.assertEqual(d.drain(), [])
            self.assertEqual(d.stats()['rx_count'], 10)
            with self.assertRaises(TimeoutError):
                d.receive_state(timeout=0.01)
            with self.assertRaises(TimeoutError):
                d.state(max_age=0.000001)
        self.assertFalse(transport.opened)
        self.assertGreater(d.stats()['actual_rx_hz'], 0)

    def test_identical_hardware_frames_remain_identical(self):
        transport = Transport()
        with Dynamometer(port='COM_TEST', transport=transport) as d:
            transport.incoming.put(frame()*100)
            wait_for(lambda: d.stats()['rx_count'] == 100)
            self.assertEqual(len({s.torque for s in d.drain()}), 1)

    def test_worker_error_is_visible_and_releases_transport(self):
        transport = Transport()
        d = Dynamometer(port='COM_TEST', transport=transport).open()
        transport.incoming.put(OSError('USB disconnected'))
        wait_for(lambda: d.stats()['fault'] is not None)
        with self.assertRaises(RuntimeError):
            d.state()
        with self.assertRaises(RuntimeError):
            d.close()
        self.assertFalse(transport.opened)

    def test_invalid_configuration_and_independent_scales(self):
        with self.assertRaises(ValueError):
            Dynamometer(port='COM_TEST', capacity=0)
        for scale in (0, -1, float('inf'), True):
            with self.assertRaises(ValueError):
                Dynamometer(port='COM_TEST', torque_scale=scale)
        self.assertEqual(parse_frame(frame(1000, 1000), torque_scale=1000, speed_scale=10).rpm, 100)
