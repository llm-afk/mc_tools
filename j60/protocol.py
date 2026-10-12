"""J60 product manual V1.0.7, pages 25-31; all motion values are output-side."""
from dataclasses import dataclass
import math

from motor_platform.core.can import CanFrame

DISABLE = 1
ENABLE = 2
CONTROL = 4
GET_STATUS = 23
GET_CONFIG = 24

FAULT_NAMES = (
    "overvoltage", "undervoltage", "overcurrent", "motor overtemperature",
    "driver overtemperature", "CAN timeout", "temperature sensor disconnected",
)


def can_id(command, motor_id, *, reply=False):
    if type(motor_id) is not int or not 0 <= motor_id <= 15:
        raise ValueError("J60 motor ID must be an integer in 0..15")
    if type(command) is not int or not 0 <= command <= 63:
        raise ValueError("J60 command index must be in 0..63")
    return (command << 5) | (0x10 if reply else 0) | motor_id


def request_frame(command, motor_id, data=b""):
    if len(data) > 8:
        raise ValueError("J60 uses classic CAN with at most 8 data bytes")
    # The shared CanFrame defaults to FD/BRS. Override BOTH for J60.
    return CanFrame(can_id(command, motor_id), data, is_fd=False, brs=False)


def _encode(value, minimum, maximum, bits, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be in [{minimum:g}, {maximum:g}]")
    return int((value - minimum) * ((1 << bits) - 1) / (maximum - minimum))


def _decode(value, minimum, maximum, bits):
    return value * (maximum - minimum) / ((1 << bits) - 1) + minimum


@dataclass(frozen=True)
class MotionCommand:
    # Same argument order as scripts.mit_control: p, v, torque, kp, kd.
    position: float = 0
    velocity: float = 0
    torque: float = 0
    kp: float = 0
    kd: float = 0

    def pack(self):
        p = _encode(self.position, -40, 40, 16, "position (rad)")
        v = _encode(self.velocity, -40, 40, 14, "velocity (rad/s)")
        kp = _encode(self.kp, 0, 1023, 10, "kp")
        kd = _encode(self.kd, 0, 51, 8, "kd")
        torque = _encode(self.torque, -40, 40, 16, "torque (Nm)")
        raw = p | (v << 16) | (kp << 30) | (kd << 40) | (torque << 48)
        return raw.to_bytes(8, "little")


@dataclass(frozen=True)
class MotionFeedback:
    position: float
    velocity: float
    torque: float
    temperature_flag: int
    temperature: float

    @classmethod
    def unpack(cls, data):
        if len(data) != 8:
            raise ValueError(f"J60 motion reply requires DLC=8, got {len(data)}")
        raw = int.from_bytes(data, "little")
        return cls(
            _decode(raw & 0xFFFFF, -40, 40, 20),
            _decode((raw >> 20) & 0xFFFFF, -40, 40, 20),
            _decode((raw >> 40) & 0xFFFF, -40, 40, 16),
            (raw >> 56) & 1,
            _decode((raw >> 57) & 0x7F, -20, 200, 7),
        )


def parse_status(data, byteorder="little"):
    # Manual: 2 bytes. Public SDK constant: 5; its error union occupies 8.
    # Preserve the whole reply in the log; only interpret the first two bytes.
    if len(data) not in (2, 5, 8):
        raise ValueError(f"unexpected J60 status DLC={len(data)}; raw={data.hex(' ')}")
    if byteorder not in ("little", "big"):
        raise ValueError("status byte order must be little or big")
    return int.from_bytes(data[:2], byteorder)


def describe_faults(code):
    names = [name for bit, name in enumerate(FAULT_NAMES) if code & (1 << bit)]
    if code & ~0x7F:
        names.append(f"unknown/reserved bits 0x{code & ~0x7F:04X}")
    return ", ".join(names) or "OK"


def describe_can_error(frame):
    """gs_usb uses Linux CAN error classes, separate from J60 fault words."""
    details = []
    if frame.can_id & 0x20:  # CAN_ERR_ACK
        details.append("未收到 CAN ACK")
    if frame.can_id & 0x40:
        details.append("CAN bus-off")
    if frame.can_id & 0x04 and len(frame.data) >= 2:
        if frame.data[1] & 0x20:  # CAN_ERR_CRTL_TX_PASSIVE
            details.append("发送端处于错误被动状态")
        if frame.data[1] & 0x10:
            details.append("接收端处于错误被动状态")
    if len(frame.data) >= 8:
        details.append(f"TX错误计数={frame.data[6]}, RX错误计数={frame.data[7]}")
    return "; ".join(details) or "CAN controller error"


def parse_config(data):
    if len(data) != 8:
        raise ValueError(f"J60 config reply requires DLC=8, got {len(data)}")
    return dict(motor_id=data[2], can_timeout_ms=data[3],
                current_bandwidth_hz=int.from_bytes(data[4:6], "little"),
                current_limit_a=int.from_bytes(data[6:8], "little") * 40 / 65535,
                internal_raw=data[:2].hex(" "))
