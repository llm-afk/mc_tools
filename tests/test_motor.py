from collections import deque
import struct
import unittest
from unittest.mock import patch

from motor_platform import CanFrame, MitCommand, MotorBus, SdoError, control_frame, mit_frame, parse_state
from motor_platform.transports.canable import Canable, bit_timing, decode_usb, encode_usb


def status(node=1, error=None):
    data = struct.pack("<fffhh", 1.25, -2.5, 0.125, -123, 456)
    if error is not None:
        data += struct.pack("<H", error) + b"\xDE\xAD"
    return CanFrame(0x190 + node, data)


class FakeTransport:
    def __init__(self, replies=()):
        self.sent, self.queue, self.closed = [], deque(replies), False
        self.on_send = None

    def send(self, frame):
        self.sent.append(frame)
        if self.on_send:
            self.queue.extend(self.on_send(frame))

    def recv(self, timeout):
        return self.queue.popleft() if self.queue else None

    def close(self):
        self.closed = True


class WireTests(unittest.TestCase):
    def test_control_golden_bytes(self):
        self.assertEqual(control_frame(1, True).can_id, 0x601)
        self.assertEqual(control_frame(1, True).data.hex(), "2b02200001000000")
        self.assertEqual(control_frame(127, False).data.hex(), "2b02200002000000")

    def test_mit_golden_bytes(self):
        frame = mit_frame(1, MitCommand(1, -2, 0.5, 20, 1))
        self.assertEqual(frame.can_id, 0x401)
        self.assertEqual(frame.data.hex(), "0000803f000000c00000003fd0076400")
        self.assertTrue(frame.is_fd and frame.brs)

    def test_invalid_mit_never_sent(self):
        transport = FakeTransport()
        motor = MotorBus(transport).motor(1)
        for cmd in (MitCommand(float("nan")), MitCommand(float("inf")),
                    MitCommand(1e100), MitCommand(kp=-1), MitCommand(kd=655.36),
                    MitCommand(kp=True)):
            with self.subTest(cmd=cmd), self.assertRaises(ValueError):
                motor.send_mit(cmd)
        self.assertEqual(transport.sent, [])

    def test_node_bounds(self):
        for node in (-1, 128, 1.2, True):
            with self.assertRaises(ValueError):
                mit_frame(node, MitCommand())
        self.assertEqual(mit_frame(0, MitCommand()).can_id, 0x400)

    def test_state_units_and_signed_temperature(self):
        state = parse_state(status(), 1, 123.0)
        self.assertEqual((state.position, state.velocity, state.torque), (1.25, -2.5, 0.125))
        self.assertEqual((state.motor_temperature, state.board_temperature), (-12.3, 45.6))
        self.assertEqual((state.error_code, state.received_at), (0, 123.0))

    def test_error_extension_ignores_last_two_bytes(self):
        self.assertEqual(parse_state(status(error=0x1234), 1).error_code, 0x1234)

    def test_tpdo_id_is_addition_not_mask(self):
        self.assertEqual(parse_state(status(127), 127).node_id, 127)
        with self.assertRaises(ValueError):
            parse_state(CanFrame(0x180 + 1, status().data), 1)

    def test_bad_feedback_rejected(self):
        for frame in (CanFrame(0x191, b"\0"*18), CanFrame(0x191, status().data, is_fd=False),
                      CanFrame(0x191, status().data, is_error=True),
                      CanFrame(0x191, status().data, is_extended=True),
                      CanFrame(0x191, struct.pack("<f", float("nan")) + status().data[4:])):
            with self.assertRaises(ValueError):
                parse_state(frame, 1)


class BusTests(unittest.TestCase):
    def test_enable_disable_ack_and_shared_routing(self):
        tr = FakeTransport()
        bus = MotorBus(tr)
        one, two = bus.motor(1), bus.motor(2)
        def reply(frame):
            if frame.can_id == 0x401:
                return [status(2), status(1)]
            return [CanFrame(0x581, b"\x60" + frame.data[1:4] + b"\0"*4)]
        tr.on_send = reply
        one.enable()
        one.disable()
        self.assertIs(bus.motor(1), one)
        self.assertEqual(two.last_state.node_id, 2)
        self.assertEqual([f.can_id for f in tr.sent], [0x401, 0x601])
        self.assertFalse(one.enabled)

    def test_sdo_rejection_is_not_standard_abort_code(self):
        tr = FakeTransport()
        tr.on_send = lambda _: [status(error=0x20)]
        with self.assertRaises(SdoError):
            MotorBus(tr).motor(1).enable()

    def test_enable_timeout_allows_retry_then_best_effort_disable(self):
        tr = FakeTransport()
        one = MotorBus(tr).motor(1)
        with self.assertRaises(TimeoutError):
            one.enable(0.001)
        tr.queue.append(status())
        one.enable()
        self.assertTrue(one.enabled)
        self.assertEqual(len(tr.sent), 2)
        one.disable(wait_ack=False)
        self.assertEqual(tr.sent[-1].data[4], 2)
        self.assertEqual(len(tr.sent), 3)
        self.assertIsNone(one.enabled)

    def test_enable_requires_tpdo_response(self):
        tr = FakeTransport()
        with self.assertRaises(TimeoutError):
            MotorBus(tr).motor(1).enable(0.01)

    def test_receive_only_and_no_cached_return(self):
        tr = FakeTransport([status(2), status(1)])
        bus = MotorBus(tr)
        one, two = bus.motor(1), bus.motor(2)
        self.assertEqual(one.receive_state().node_id, 1)
        self.assertEqual(two.last_state.node_id, 2)
        with self.assertRaises(TimeoutError):
            one.receive_state(0.001)
        self.assertFalse(tr.sent)

    def test_close_does_not_send_motor_commands(self):
        tr = FakeTransport()
        with MotorBus(tr) as bus:
            one = bus.motor(1)
        self.assertTrue(tr.closed)
        self.assertFalse(tr.sent)
        with self.assertRaises(RuntimeError):
            one.send_mit(MitCommand())

    def test_error_frame_is_visible(self):
        bus = MotorBus(FakeTransport([CanFrame(0x40, b"\0"*8, is_error=True)]))
        with self.assertRaises(OSError):
            bus.poll()


class UsbTests(unittest.TestCase):
    def test_fd_dlc_and_echo_filter(self):
        frame = mit_frame(1, MitCommand())
        packet = encode_usb(frame, 7)
        self.assertEqual(len(packet), 76)
        self.assertEqual(packet[:12].hex(), "07000000010400000a000600")
        self.assertIsNone(decode_usb(packet))
        self.assertEqual(decode_usb(b"\xff"*4 + packet[4:]), frame)

    def test_real_dlc_lengths(self):
        for length in (8, 16, 20, 64):
            frame = CanFrame(0x191, b"\x55"*length)
            self.assertEqual(decode_usb(encode_usb(frame, 0xFFFFFFFF)), frame)
        with self.assertRaises(ValueError):
            encode_usb(CanFrame(1, b"\0"*9))

    def test_classic_frame(self):
        frame = CanFrame(0x581, b"\0"*8, is_fd=False, brs=False)
        self.assertEqual(len(encode_usb(frame)), 20)
        self.assertEqual(decode_usb(encode_usb(frame, 0xFFFFFFFF)), frame)

    def test_device_timing_limits(self):
        nominal = (1, 256, 1, 128, 128, 1, 512, 1)
        data = (1, 32, 1, 16, 16, 1, 32, 1)
        for rate, sp, limits in ((1_000_000, 75, nominal), (4_000_000, 64, data)):
            prop, s1, s2, sjw, brp = bit_timing(160_000_000, rate, sp, limits)
            self.assertEqual(160_000_000 / (brp*(1+prop+s1+s2)), rate)
            self.assertLessEqual(s1, limits[1])
            self.assertLessEqual(s2, limits[3])
            self.assertLessEqual(sjw, limits[4])
        with self.assertRaises(ValueError):
            bit_timing(160_000_000, 3_000_001, 64, data)

    def test_receive_split_and_coalesced_frames(self):
        class Usb:
            input = 0x81
            def __init__(self):
                data = encode_usb(status(1), 3) + encode_usb(status(2), 0xFFFFFFFF) + encode_usb(status(1), 0xFFFFFFFF)
                self.chunks = deque([data[:64], data[64:80], data[80:]])
            def transfer(self, *args, **kwargs):
                return self.chunks.popleft() if self.chunks else None
        can = Canable()
        can._usb, can._started = Usb(), True
        self.assertEqual(can.recv().can_id, 0x192)
        self.assertEqual(can.recv().can_id, 0x191)
        self.assertIsNone(can.recv())

    def test_inspect_only_gets_and_closes(self):
        class Usb:
            def __init__(self): self.calls, self.closed = [], False
            def control(self, request, *, length):
                self.calls.append(request)
                if request == 5:
                    return struct.pack("<BBBBII", 1, 2, 5, 0, 0x260803, 0x200)
                return struct.pack("<18I", 0xE53B, 160_000_000, *(1,256,1,128,128,1,512,1), *(1,32,1,16,16,1,32,1))
            def close(self): self.closed = True
        usb = Usb()
        can = Canable()
        with patch.object(can, "_connect", return_value=usb):
            self.assertEqual(can.inspect()["firmware"], "0x260803")
        self.assertEqual(usb.calls, [5, 11])
        self.assertTrue(usb.closed)

    def test_open_sequence_and_failed_start_cleanup(self):
        info = dict(firmware="0x260803", channels=1, features="0xE53B", clock_hz=160_000_000,
                    nominal_limits=(1,256,1,128,128,1,512,1), data_limits=(1,32,1,16,16,1,32,1))
        class Usb:
            def __init__(self, fail): self.calls, self.closed, self.fail = [], False, fail
            def control(self, request, *, length=0, data=None):
                self.calls.append((request, data))
                if self.fail and request == 10: raise OSError("simulated USB failure")
                return b"\x02" if request == 22 else b""
            def close(self): self.closed = True
        for firmware, fail in ((version, failure) for version in ("0x260618", "0x260803", "0x261001")
                               for failure in (False, True)):
            info["firmware"] = firmware
            usb, can = Usb(fail), Canable()
            with patch.object(can, "_connect", return_value=usb), patch.object(can, "_info", return_value=info):
                if fail:
                    with self.assertRaises(OSError): can.open()
                    self.assertIsNone(can._usb)
                else:
                    self.assertIs(can.open(), can)
                    sets = [(r,d) for r,d in usb.calls if d is not None]
                    self.assertEqual([r for r,d in sets], [2,0,1,10,2])
                    self.assertEqual(sets[-1][1], struct.pack("<II", 1, 0x100))
                    can.close()
                self.assertEqual(usb.calls[-1], (2, struct.pack("<II", 0, 0)))
                self.assertTrue(usb.closed)

    def test_new_firmware_does_not_bypass_capability_checks(self):
        baseline = dict(firmware="0x261001", channels=1, features="0xE53B", clock_hz=160_000_000,
                        nominal_limits=(1,256,1,128,128,1,512,1), data_limits=(1,32,1,16,16,1,32,1))
        class Usb:
            def __init__(self): self.closed = False
            def control(self, *args, **kwargs):
                raise AssertionError("unsupported adapter must not be configured")
            def close(self): self.closed = True
        for change in ({"firmware": "0xFFFFFF"}, {"channels": 2}, {"features": "0xE43B"}):
            with self.subTest(change=change):
                usb, can = Usb(), Canable()
                with patch.object(can, "_connect", return_value=usb), \
                     patch.object(can, "_info", return_value=baseline | change):
                    with self.assertRaisesRegex(OSError, "unsupported CANable"):
                        can.open()
                self.assertTrue(usb.closed)
                self.assertIsNone(can._usb)


if __name__ == "__main__":
    unittest.main()
