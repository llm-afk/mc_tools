import csv
from collections import deque
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from motor_platform.core.can import CanFrame
from motor_platform.transports.canable import encode_usb
from j60.mit_control import app
from j60.canable import J60Canable
from j60.client import J60Client
from j60.protocol import (
    CONTROL, DISABLE, ENABLE, GET_CONFIG, GET_STATUS, MotionCommand, MotionFeedback,
    can_id, describe_can_error, describe_faults, parse_config, parse_status, request_frame,
)


MOTION_RAW = bytes.fromhex("FF FF F7 FF 7F FF 7F 41")


@contextmanager
def temporary_folder():
    # Keep all generated test files in this directory, including sandbox runs.
    base = Path(__file__).resolve().parent
    folder = tempfile.TemporaryDirectory(prefix=".j60-test-", dir=base)
    resolved = Path(folder.name).resolve()
    if not resolved.is_relative_to(base) or resolved == base:
        raise RuntimeError("test temporary folder escaped its intended directory")
    with folder:
        yield resolved


class FakeTransport:
    """Reply-producing transport; it never imports or opens native USB handles."""
    def __init__(self, *, fault=0, running_fault=0, drop_motion=False,
                 drop_second_status=False, reject_enable=False, uncertain_enable=False,
                 reject_final_disable=False):
        self.frames = deque()
        self.sent = []
        self.events = []
        self.info = {"firmware": "0x261001"}
        self.fault, self.running_fault = fault, running_fault
        self.drop_motion, self.drop_second_status = drop_motion, drop_second_status
        self.reject_enable, self.uncertain_enable = reject_enable, uncertain_enable
        self.reject_final_disable = reject_final_disable
        self.status_count = self.disable_count = 0
        self.closed = False

    def open(self):
        return self

    def close(self):
        self.closed = True

    def send(self, frame):
        self.sent.append(frame)
        command = frame.can_id >> 5
        self.events.append(("TX", command, frame.data))
        motor = frame.can_id & 0xF
        data = b"\0"
        if command == GET_CONFIG:
            data = bytes((0, 0, motor, 50)) + struct.pack("<HH", 1000, 32767)
        elif command == GET_STATUS:
            self.status_count += 1
            if self.drop_second_status and self.status_count > 1:
                return
            code = self.fault if self.status_count == 1 else self.running_fault
            data = code.to_bytes(2, "little")
        elif command == ENABLE:
            if self.uncertain_enable:
                raise OSError("short USB write; outcome uncertain")
            data = bytes((int(self.reject_enable),))
        elif command == DISABLE:
            self.disable_count += 1
            data = bytes((int(self.reject_final_disable and self.disable_count > 1),))
        elif command == CONTROL:
            if self.drop_motion:
                return
            data = MOTION_RAW
        self.frames.append(CanFrame(can_id(command, motor, reply=True), data,
                                    is_fd=False, brs=False))

    def recv(self, timeout):
        if self.frames:
            frame = self.frames.popleft()
            self.events.append(("RX", frame.can_id >> 5, frame.data))
            return frame
        time.sleep(min(timeout, 0.001))
        return None


class ProtocolTests(unittest.TestCase):
    def test_documented_ids_and_four_bit_motor_limit(self):
        self.assertEqual(can_id(4, 1), 0x081)
        self.assertEqual(can_id(4, 1, reply=True), 0x091)
        self.assertEqual(can_id(23, 1, reply=True), 0x2F1)
        self.assertEqual(can_id(2, 0), 0x040)
        self.assertEqual(can_id(2, 15), 0x04F)
        for invalid in (16, -1, True, 1.0):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                can_id(4, invalid)

    def test_manual_position_example_and_physical_zero_payload(self):
        self.assertEqual(MotionCommand(3.14, 0, 0, 50, 5).pack(),
                         bytes.fromhex("0B 8A FF 9F 0C 19 FF 7F"))
        self.assertEqual(MotionCommand().pack(), bytes.fromhex("FF 7F FF 1F 00 00 FF 7F"))

    def test_mapping_endpoints(self):
        self.assertEqual(MotionCommand(-40, -40, -40, 0, 0).pack(), bytes(8))
        self.assertEqual(MotionCommand(40, 40, 40, 1023, 51).pack(), bytes([255])*8)

    def test_commands_reject_out_of_range_and_nonfinite(self):
        for kwargs in ({"position": 40.01}, {"velocity": -40.01}, {"torque": 41},
                       {"kp": -1}, {"kp": 1024}, {"kd": 51.01},
                       {"torque": float("nan")}, {"kd": float("inf")}, {"kp": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MotionCommand(**kwargs).pack()

    def test_feedback_20bit_fields_and_one_temperature_flag(self):
        state = MotionFeedback.unpack(MOTION_RAW)
        self.assertAlmostEqual(state.position, -40/1048575)
        self.assertAlmostEqual(state.velocity, -40/1048575)
        self.assertAlmostEqual(state.torque, -40/65535)
        self.assertEqual(state.temperature_flag, 1)
        self.assertAlmostEqual(state.temperature, 32*220/127-20)
        with self.assertRaises(ValueError):
            MotionFeedback.unpack(MOTION_RAW[:-1])

    def test_only_classic_standard_can_without_brs_is_encoded(self):
        frame = request_frame(CONTROL, 1, MotionCommand().pack())
        self.assertFalse(frame.is_fd or frame.brs or frame.is_extended or frame.is_remote)
        packet = encode_usb(frame)
        self.assertEqual(len(packet), 20)
        self.assertEqual(packet[8:12], bytes((8, 0, 0, 0)))
        self.assertEqual(packet[12:], MotionCommand().pack())

    def test_configuration_request_usb_bytes_match_classic_can(self):
        packet = encode_usb(request_frame(GET_CONFIG, 1), echo_id=1)
        self.assertEqual(packet, bytes.fromhex(
            "01 00 00 00 01 03 00 00 00 00 00 00 00 00 00 00 00 00 00 00"))
        self.assertEqual(can_id(GET_CONFIG, 1, reply=True), 0x311)

    def test_observed_no_ack_error_is_decoded_separately_from_j60_status(self):
        frame = CanFrame(0x24, bytes.fromhex("00 20 00 00 00 00 80 00"),
                         is_fd=False, brs=False, is_error=True)
        text = describe_can_error(frame)
        self.assertIn("未收到 CAN ACK", text)
        self.assertIn("错误被动", text)
        self.assertIn("TX错误计数=128", text)
        self.assertIn("RX错误计数=0", text)

    def test_status_versions_preserve_explicit_byteorder(self):
        for length in (2, 5, 8):
            data = b"\x01\0" + b"\xA5"*(length-2)
            self.assertEqual(parse_status(data), 1)
            self.assertEqual(parse_status(data, "big"), 0x100)
        for length in (0, 1, 3, 4, 6, 7):
            with self.assertRaises(ValueError):
                parse_status(bytes(length))
        self.assertIn("sensor", describe_faults(0x40))
        self.assertIn("unknown", describe_faults(0x100))

    def test_config_fields_and_length(self):
        config = parse_config(bytes.fromhex("AA BB 01 32 E8 03 FF 7F"))
        self.assertEqual(config["motor_id"], 1)
        self.assertEqual(config["can_timeout_ms"], 50)
        self.assertEqual(config["current_bandwidth_hz"], 1000)
        self.assertAlmostEqual(config["current_limit_a"], 32767*40/65535)
        with self.assertRaises(ValueError):
            parse_config(bytes(7))


class TransportAndClientTests(unittest.TestCase):
    def test_classic_mode_startup_reuses_transport_and_closes(self):
        usb = Mock()
        usb.control.side_effect = lambda request, **kw: b"\x02" if request == 22 else b""
        transport = J60Canable("test")
        info = dict(channels=1, firmware="0x261001", features="0x4500", clock_hz=160_000_000,
                    nominal_limits=(1, 256, 1, 128, 128, 1, 512, 1))
        with patch.object(transport, "_connect", return_value=usb), \
             patch.object(transport, "_info", return_value=info):
            transport.open()
        writes = [(call.args[0], call.kwargs["data"]) for call in usb.control.call_args_list if "data" in call.kwargs]
        self.assertEqual([request for request, _ in writes], [2, 0, 1, 2])
        self.assertEqual(writes[-1][1], struct.pack("<II", 1, 0))
        self.assertEqual(transport.nominal_timing, (0, 119, 40, 40, 1))
        prop, seg1, seg2, sjw, brp = transport.nominal_timing
        self.assertEqual(info["clock_hz"]/(brp*(1+prop+seg1+seg2)), 1_000_000)
        self.assertEqual((1+prop+seg1)/(1+prop+seg1+seg2), 0.75)
        self.assertTrue(transport._started)
        transport.close()
        usb.close.assert_called_once()

    def test_request_matches_direction_motor_command_and_frame_type(self):
        transport = FakeTransport()
        invalid = [CanFrame(0x052, b"\0", False, False),
                   CanFrame(0x041, b"\0", False, False),
                   CanFrame(0x031, b"\0", False, False),
                   CanFrame(0x051, b"\0", True, False),
                   CanFrame(0x051, b"\0", False, False, True),
                   CanFrame(0x051, b"\0", False, False, False, False, True)]
        transport.frames.extend(invalid)
        client = J60Client(transport, 1)
        client.enable()
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(client.ignored_count, 5)

    def test_wrong_reply_does_not_complete_request_or_trigger_retry(self):
        transport = FakeTransport(drop_motion=True)
        client = J60Client(transport, 1, timeout=0.004)
        with self.assertRaises(TimeoutError):
            client.request(CONTROL)
        self.assertEqual(len(transport.sent), 1)

    def test_bus_error_is_not_motion_feedback(self):
        transport = FakeTransport()
        transport.frames.append(CanFrame(0x40, bytes(8), False, False, False, True))
        with self.assertRaises(OSError):
            J60Client(transport, 1).receive(0.01)

    def test_two_temperature_caches_update_separately(self):
        transport = FakeTransport()
        transport.frames.append(CanFrame(0x091, MOTION_RAW, False, False))
        transport.frames.append(CanFrame(0x091, MOTION_RAW[:-1]+b"\x80", False, False))
        client = J60Client(transport, 1)
        client.receive(0.01)
        motor_temperature = client.motor_temperature
        self.assertIsNone(client.driver_temperature)
        client.receive(0.01)
        self.assertEqual(client.motor_temperature, motor_temperature)
        self.assertAlmostEqual(client.driver_temperature, 64*220/127-20)

    def test_failed_and_malformed_enable_acks_are_not_success(self):
        for data in (b"\x01", b"", b"\x02", b"\0\0"):
            transport = FakeTransport()
            transport.frames.append(CanFrame(0x051, data, False, False))
            with self.subTest(data=data), self.assertRaises((ValueError, RuntimeError)):
                J60Client(transport, 1).enable()


class ApplicationTests(unittest.TestCase):
    def run_fake(self, transport, *argv):
        with temporary_folder() as folder:
            output = Path(folder)/"j60.csv"
            text = io.StringIO()
            with patch.object(app, "J60Canable", return_value=transport) as factory, \
                 patch.object(app, "select_canable", return_value="fake-canable"), \
                 redirect_stdout(text), redirect_stderr(text):
                factory.detect.return_value = [{"serial_number": "fake-canable"}]
                result = app.main(["--no-console", "--duration", "0.018", "--output", str(output), *argv])
            with output.open(newline="", encoding="utf-8") as source:
                rows = list(csv.DictReader(source))
        return result, rows, text.getvalue()

    def test_argument_and_console_validation_before_hardware(self):
        with patch.object(app, "J60Canable") as factory:
            args, command = app.parse_args([])
            self.assertEqual((args.id, args.rate, args.duration), (1, 1000, 0))
            self.assertIsNone(args.serial)
            self.assertEqual(command, MotionCommand())
            factory.assert_not_called()
            for argv in (["--id", "16"], ["--rate", "1001"], ["--torque", "nan"],
                         ["--duration", "-1"], ["--kp", "-1"], ["--status-interval", "0"]):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    app.parse_args(argv)
            factory.assert_not_called()
        self.assertEqual(app.parse_console_command("set 0 0 0.1 0 0"), ("set", MotionCommand(torque=0.1)))
        for line in ("set 1 2", "set 0 0 0 -1 0", "rate nan", "enable now", "unknown"):
            with self.subTest(line=line), self.assertRaises(ValueError):
                app.parse_console_command(line)

    def test_single_adapter_sn_is_selected_automatically(self):
        with patch("j60.canable.J60Canable") as factory:
            factory.detect.return_value = [{"serial_number": "20923378374350012"}]
            self.assertEqual(app.select_canable(), "20923378374350012")
            self.assertEqual(app.select_canable("20923378374350012"), "20923378374350012")
            with self.assertRaisesRegex(OSError, "不一致"):
                app.select_canable("old-sn")
            factory.assert_not_called()

    def test_zero_or_multiple_adapters_exit_before_can_ui_and_csv(self):
        for devices, message in (([], "未检测到"),
                                 ([{"serial_number": "one"}, {"serial_number": "two"}], "检测到 2 只")):
            text = io.StringIO()
            with patch("j60.canable.J60Canable") as factory, \
                 patch.object(app, "FrameRecorder") as recorder, \
                 patch.object(app, "ConsoleDashboard") as console, \
                 redirect_stdout(text), redirect_stderr(text):
                factory.detect.return_value = devices
                result = app.main(["--probe"])
                self.assertEqual(result, 1)
                self.assertIn(message, text.getvalue())
                factory.assert_not_called()
                recorder.assert_not_called()
                console.assert_not_called()
        with patch("j60.canable.J60Canable") as factory:
            factory.detect.return_value = [{"serial_number": "one"}, {"serial_number": "two"}]
            with self.assertRaisesRegex(OSError, "检测到 2 只"):
                app.select_canable("one")

    def test_probe_never_enables_or_disables(self):
        transport = FakeTransport(fault=1)
        result, rows, _ = self.run_fake(transport, "--probe")
        self.assertEqual(result, 0)
        self.assertEqual([frame.can_id >> 5 for frame in transport.sent], [GET_CONFIG, GET_STATUS])
        self.assertTrue(transport.closed)
        self.assertEqual({row["direction"] for row in rows}, {"TX", "RX"})

    def test_complete_zero_command_flow_records_real_replies_and_disables(self):
        transport = FakeTransport()
        result, rows, _ = self.run_fake(transport)
        self.assertEqual(result, 0)
        commands = [frame.can_id >> 5 for frame in transport.sent]
        self.assertEqual(commands[:4], [GET_CONFIG, GET_STATUS, DISABLE, ENABLE])
        self.assertEqual(commands[-1], DISABLE)
        self.assertIn(CONTROL, commands)
        self.assertTrue(transport.closed)
        motions = [row for row in rows if row["direction"] == "RX" and row["can_id"] == "0x091"]
        self.assertTrue(motions)
        self.assertTrue(all(row["raw_hex"] == MOTION_RAW.hex(" ").upper() for row in motions))
        self.assertTrue(all(row["is_fd"] == row["brs"] == "0" for row in rows))

    def test_initial_fault_prevents_enable_and_motion(self):
        transport = FakeTransport(fault=0x40)
        result, _, text = self.run_fake(transport)
        self.assertEqual(result, 1)
        self.assertNotIn(ENABLE, [frame.can_id >> 5 for frame in transport.sent])
        self.assertNotIn(CONTROL, [frame.can_id >> 5 for frame in transport.sent])
        self.assertIn("sensor", text)
        self.assertTrue(transport.closed)

    def test_failed_or_uncertain_enable_attempts_disable_without_retrying_enable(self):
        for kwargs in ({"reject_enable": True}, {"uncertain_enable": True}):
            transport = FakeTransport(**kwargs)
            result, _, _ = self.run_fake(transport)
            commands = [frame.can_id >> 5 for frame in transport.sent]
            self.assertEqual(result, 1)
            self.assertEqual(commands.count(ENABLE), 1)
            self.assertEqual(commands.count(DISABLE), 2)
            self.assertNotIn(CONTROL, commands)

    def test_missing_feedback_aborts_and_disables(self):
        transport = FakeTransport(drop_motion=True)
        result, _, text = self.run_fake(transport, "--feedback-timeout", "0.004")
        self.assertEqual(result, 1)
        self.assertIn("feedback missing", text)
        self.assertEqual(transport.sent[-1].can_id >> 5, DISABLE)

    def test_pending_status_does_not_pause_motion_and_times_out(self):
        transport = FakeTransport(drop_second_status=True)
        result, _, text = self.run_fake(transport, "--duration", "0.1", "--status-interval", "0.003",
                                        "--reply-timeout", "0.012")
        self.assertEqual(result, 1)
        self.assertIn("status reply timed out", text)
        commands = [frame.can_id >> 5 for frame in transport.sent]
        query_index = commands.index(GET_STATUS, 2)
        self.assertGreaterEqual(commands[query_index+1:-1].count(CONTROL), 2)
        self.assertEqual(commands.count(GET_STATUS), 2)
        self.assertEqual(commands[-1], DISABLE)

    def test_running_fault_outranks_next_motion_frame(self):
        transport = FakeTransport(running_fault=1)
        result, _, _ = self.run_fake(transport, "--status-interval", "0.003")
        self.assertEqual(result, 1)
        fault_index = transport.events.index(("RX", GET_STATUS, b"\x01\0"))
        later_tx = [event[1] for event in transport.events[fault_index+1:] if event[0] == "TX"]
        self.assertEqual(later_tx, [DISABLE])

    def test_unconfirmed_disable_returns_failure(self):
        transport = FakeTransport(reject_final_disable=True)
        result, _, text = self.run_fake(transport)
        self.assertEqual(result, 1)
        self.assertIn("Disable UNCONFIRMED", text)
        self.assertTrue(transport.closed)

    def test_interactive_set_stop_rate_disable_enable_and_quit(self):
        class Dashboard:
            dirty = False
            def __init__(self):
                self.commands = iter(("set 0.3 0 0 10 0.2", "stop", "rate 500",
                                      "disable", "enable", "quit"))
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return None
            def poll(self):
                return next(self.commands, None)
            def render(self, *_, **__):
                pass

        transport = FakeTransport()
        with temporary_folder() as folder, patch.object(app, "J60Canable", return_value=transport) as factory, \
             patch.object(app, "select_canable", return_value="fake-canable"), \
             patch.object(app, "ConsoleDashboard", return_value=Dashboard()), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            factory.detect.return_value = [{"serial_number": "fake-canable"}]
            result = app.main(["--duration", "0.2", "--output", str(folder/"interactive.csv")])
        self.assertEqual(result, 0)
        commands = [frame.can_id >> 5 for frame in transport.sent]
        self.assertEqual(commands.count(ENABLE), 2)
        self.assertEqual(commands.count(DISABLE), 3)
        motions = [frame.data for frame in transport.sent if frame.can_id >> 5 == CONTROL]
        self.assertIn(MotionCommand(0.3, 0, 0, 10, 0.2).pack(), motions)
        self.assertIn(MotionCommand().pack(), motions)
        self.assertTrue(transport.closed)

    def test_interactive_disable_failure_aborts_without_another_enable(self):
        dashboard = Mock()
        dashboard.__enter__ = Mock(return_value=dashboard)
        dashboard.__exit__ = Mock(return_value=None)
        dashboard.poll.return_value = "disable"
        dashboard.dirty = False
        transport = FakeTransport(reject_final_disable=True)
        with temporary_folder() as folder, patch.object(app, "J60Canable", return_value=transport) as factory, \
             patch.object(app, "select_canable", return_value="fake-canable"), \
             patch.object(app, "ConsoleDashboard", return_value=dashboard), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            factory.detect.return_value = [{"serial_number": "fake-canable"}]
            result = app.main(["--duration", "0.1", "--output", str(folder/"interactive.csv")])
        self.assertEqual(result, 1)
        self.assertEqual([frame.can_id >> 5 for frame in transport.sent].count(ENABLE), 1)
        self.assertEqual([frame.can_id >> 5 for frame in transport.sent].count(DISABLE), 2)
        self.assertTrue(transport.closed)

    def test_csv_is_never_overwritten(self):
        with temporary_folder() as folder:
            path = Path(folder)/"j60.csv"
            path.write_text("original", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                app.FrameRecorder(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "original")


if __name__ == "__main__":
    unittest.main()
