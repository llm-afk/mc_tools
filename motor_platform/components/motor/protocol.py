"""Common GD/ADM MIT wire contract used by reusable motor components."""
from dataclasses import dataclass
import math
import struct


from ...core.can import CanFrame


def validate_node(node_id: int) -> int:
    if type(node_id) is not int or not 0 <= node_id <= 127:
        raise ValueError("node_id must be an integer in 0..127 (0 is not broadcast)")
    return node_id


@dataclass(frozen=True)
class MitCommand:
    position: float = 0.0  # rad
    velocity: float = 0.0  # rad/s
    torque: float = 0.0    # firmware torque units, nominally Nm
    kp: float = 0.0        # firmware wire value is gain * 100
    kd: float = 0.0

    def pack(self) -> bytes:
        for value in (self.position, self.velocity, self.torque):
            if not math.isfinite(value):
                raise ValueError("MIT values must be finite")
        for gain in (self.kp, self.kd):
            if type(gain) not in (int, float) or not math.isfinite(gain) or not 0 <= gain <= 655.35:
                raise ValueError("kp/kd must be finite values in 0..655.35")
        try:
            data = struct.pack("<fffHH", self.position, self.velocity, self.torque,
                               round(self.kp * 100), round(self.kd * 100))
        except (OverflowError, struct.error) as exc:
            raise ValueError("MIT value cannot be represented on the wire") from exc
        return data


@dataclass(frozen=True)
class MotorState:
    node_id: int
    position: float
    velocity: float
    torque: float
    motor_temperature: float
    board_temperature: float
    error_code: int  # TPDO excludes ERR_HEARTBEAT_TIMEOUT (0x0080)
    received_at: float = 0.0  # host monotonic time, not motor sampling time


def mit_frame(node_id: int, command: MitCommand) -> CanFrame:
    # Common GD/ADM compatibility control entry. Both firmwares auto-enable
    # when this RPDO is accepted, then answer on TPDO5 (0x190 + node).
    return CanFrame(0x400 + validate_node(node_id), command.pack())


def feedback_request_frame(node_id: int) -> CanFrame:
    # GD RPDO5 / ADM RPDO5 updates neutral targets and returns TPDO5, without
    # the automatic enable performed by their 0x400 compatibility entry.
    return CanFrame(0x100 + validate_node(node_id), MitCommand().pack())


def control_frame(node_id: int, enabled: bool) -> CanFrame:
    if type(enabled) is not bool:
        raise ValueError("enabled must be bool")
    return CanFrame(0x600 + validate_node(node_id),
                    struct.pack("<BH B H 2x", 0x2B, 0x2002, 0, 1 if enabled else 2))


def parse_state(frame: CanFrame, node_id: int, received_at: float = 0.0) -> MotorState:
    validate_node(node_id)
    # TPDO base 0x190 is NOT aligned to 0x80: do not mask low 7 bits.
    if (frame.can_id != 0x190 + node_id or not frame.is_fd or frame.is_extended
            or frame.is_error or frame.is_remote):
        raise ValueError("not this motor's standard CAN FD TPDO5")
    if len(frame.data) not in (16, 20):
        raise ValueError("TPDO5 must contain 16 or 20 bytes")
    p, v, t, mt, bt = struct.unpack_from("<fffhh", frame.data)
    if not all(math.isfinite(x) for x in (p, v, t)):
        raise ValueError("non-finite motor feedback")
    error = struct.unpack_from("<H", frame.data, 16)[0] if len(frame.data) == 20 else 0
    # Bytes 18..19 are not initialized by this firmware; deliberately ignore.
    return MotorState(node_id, p, v, t, mt / 10.0, bt / 10.0, error, received_at)
