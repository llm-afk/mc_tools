"""One caller owns CAN I/O; requests match motor ID, direction and command."""
import time

from .protocol import (
    CONTROL, DISABLE, ENABLE, GET_CONFIG, GET_STATUS, MotionFeedback,
    can_id, describe_can_error, describe_faults, parse_config, parse_status, request_frame,
)


class J60Client:
    def __init__(self, transport, motor_id, *, timeout=0.2,
                 status_endian="little", on_frame=None):
        can_id(CONTROL, motor_id)
        self.transport, self.motor_id = transport, motor_id
        self.timeout, self.status_endian = timeout, status_endian
        self.on_frame = on_frame
        self.feedback = None
        self.feedback_at = 0.0
        self.motor_temperature = self.driver_temperature = None
        self.motor_temperature_at = self.driver_temperature_at = 0.0
        self.status_code = None
        self.status_at = 0.0
        self.status_raw = b""
        self.config = None
        self.tx_count = self.rx_count = self.control_tx_count = self.motion_rx_count = 0
        self.ignored_count = 0
        self.last_motion_raw = b""

    def send(self, command, data=b""):
        frame = request_frame(command, self.motor_id, data)
        # No automatic retries: an unsuccessful USB write can have reached CAN.
        self.transport.send(frame)
        self.tx_count += 1
        if command == CONTROL:
            self.control_tx_count += 1
        if self.on_frame:
            self.on_frame("TX", frame, time.perf_counter())

    def receive(self, timeout):
        frame = self.transport.recv(timeout=timeout)
        if frame is None:
            return None
        if frame.is_error:
            if self.on_frame:
                self.on_frame("ERROR", frame, time.perf_counter())
            raise OSError(f"CAN error frame ID=0x{frame.can_id:X}, raw={frame.data.hex(' ')}; "
                          f"{describe_can_error(frame)}")
        if (frame.is_fd or frame.brs or frame.is_extended or frame.is_remote
                or frame.can_id > 0x7FF or not frame.can_id & 0x10
                or frame.can_id & 0x0F != self.motor_id):
            self.ignored_count += 1
            return None
        now = time.perf_counter()
        self.rx_count += 1
        if self.on_frame:
            self.on_frame("RX", frame, now)
        command = frame.can_id >> 5
        if command == CONTROL:
            self.feedback = MotionFeedback.unpack(frame.data)
            self.feedback_at = now
            self.motion_rx_count += 1
            self.last_motion_raw = frame.data
            if self.feedback.temperature_flag:
                self.motor_temperature = self.feedback.temperature
                self.motor_temperature_at = now
            else:
                self.driver_temperature = self.feedback.temperature
                self.driver_temperature_at = now
        elif command == GET_STATUS:
            self.status_code = parse_status(frame.data, self.status_endian)
            self.status_at, self.status_raw = now, frame.data
        elif command == GET_CONFIG:
            self.config = parse_config(frame.data)
        return frame

    def drain(self, duration=0.02):
        """Bound startup draining even when other CAN nodes keep sending."""
        deadline = time.perf_counter() + duration
        while time.perf_counter() < deadline:
            wait = max(0.000001, min(0.001, deadline-time.perf_counter()))
            if self.transport.recv(timeout=wait) is None:
                return

    def request(self, command):
        self.send(command)
        deadline = time.perf_counter() + self.timeout
        expected = can_id(command, self.motor_id, reply=True)
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError(f"J60 reply timeout: expected ID=0x{expected:03X}")
            frame = self.receive(remaining)
            if frame is not None and frame.can_id == expected:
                return frame

    def _ack(self, command):
        frame = self.request(command)
        if len(frame.data) != 1 or frame.data[0] not in (0, 1):
            raise ValueError(f"malformed J60 ACK: ID=0x{frame.can_id:03X}, raw={frame.data.hex(' ')}")
        if frame.data[0] != 0:
            raise RuntimeError(f"J60 rejected command {command}: ACK=01")

    def enable(self):
        self._ack(ENABLE)

    def disable(self):
        self._ack(DISABLE)

    def get_status(self):
        self.request(GET_STATUS)
        return self.status_code

    def get_config(self):
        self.request(GET_CONFIG)
        return self.config

    def check_status(self):
        if self.status_code:
            raise RuntimeError(f"J60 fault 0x{self.status_code:04X}: {describe_faults(self.status_code)}; "
                               f"raw={self.status_raw.hex(' ')}")
