"""Configuration explicitly supplied by each script; no file loading."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class BenchConfig:
    motor_ids: tuple[int, ...]
    bitrate: int = 1_000_000
    data_bitrate: int = 4_000_000
    sample_point: float = 75.0
    data_sample_point: float = 64.0
    rate_hz: float = 1000.0
    command_timeout: float = 0.25

    def __post_init__(self):
        object.__setattr__(self, "motor_ids", tuple(self.motor_ids))
        ids = self.motor_ids
        if not ids or any(type(node) is not int or not 0 <= node <= 127 for node in ids):
            raise ValueError("script must specify motor IDs in 0..127")
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate motor IDs")
        for value in (self.bitrate, self.data_bitrate):
            if type(value) is not int or value <= 0:
                raise ValueError("bitrates must be positive integers")
        for value in (self.sample_point, self.data_sample_point):
            if type(value) not in (int,float) or not math.isfinite(value) or not 0 < value < 100:
                raise ValueError("invalid sample point")
        for value in (self.rate_hz, self.command_timeout):
            if type(value) not in (int,float) or not math.isfinite(value) or value <= 0:
                raise ValueError("rate and timeout must be finite and positive")
