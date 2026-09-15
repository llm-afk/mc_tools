"""DYN-200 mode 0: 4 data bytes plus CRC16/Modbus, low CRC byte first."""
from dataclasses import dataclass
import math


def positive(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('scale/timeout must be finite and positive')
    return value


def crc16(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def valid_frame(data):
    return len(data) == 6 and crc16(data[:4]) == int.from_bytes(data[4:6], 'little')


@dataclass(frozen=True)
class DynamometerState:
    torque: float
    speed: float
    rpm: float
    raw_torque: int
    raw_speed: int
    received_at: float

    @property
    def power(self):
        """Signed torque * angular speed; speed direction is absent on the wire."""
        return self.torque * self.speed


def parse_frame(data, *, torque_scale=100.0, speed_scale=100.0, received_at=0.0):
    positive(torque_scale)
    positive(speed_scale)
    if not valid_frame(data):
        raise ValueError('invalid DYN-200 frame length or CRC')
    magnitude = int.from_bytes(data[:2], 'big')
    raw_torque = -magnitude if data[2] & 0x80 else magnitude
    raw_speed = ((data[2] & 0x7F) << 8) | data[3]
    rpm = raw_speed / speed_scale
    return DynamometerState(raw_torque/torque_scale, rpm*math.pi/30, rpm,
                            raw_torque, raw_speed, received_at)


class FrameParser:
    """Handles arbitrary read boundaries; two consecutive CRCs acquire sync."""
    def __init__(self):
        self._buffer = bytearray()
        self._synced = False
        self.discarded_bytes = 0

    def feed(self, data):
        frames = []
        # Slice large inputs so parser pending memory stays bounded.
        for offset in range(0, len(data), 4096):
            self._buffer.extend(data[offset:offset+4096])
            pos = 0
            while len(self._buffer)-pos >= (6 if self._synced else 12):
                frame = bytes(self._buffer[pos:pos+6])
                good = valid_frame(frame)
                if good and (self._synced or valid_frame(self._buffer[pos+6:pos+12])):
                    self._synced = True
                    frames.append(frame)
                    pos += 6
                else:
                    self._synced = False
                    pos += 1
                    self.discarded_bytes += 1
            del self._buffer[:pos]
        return frames
