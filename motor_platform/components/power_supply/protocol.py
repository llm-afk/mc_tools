"""UNI-T UDP6721 SCPI commands used by the previously validated RS-232 driver."""
from dataclasses import dataclass
import math


class PowerSupplyError(RuntimeError):
    pass


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('setpoint must be a finite nonnegative number')
    return value


def parse_number(raw):
    try:
        result = float(raw.strip())
    except (ValueError, AttributeError) as exc:
        raise PowerSupplyError(f'invalid numeric reply: {raw!r}') from exc
    if not math.isfinite(result):
        raise PowerSupplyError(f'non-finite reply: {raw!r}')
    return result


def parse_output(raw):
    value = raw.strip().upper()
    if value in ('ON', '1', 'YES'):
        return True
    if value in ('OFF', '0', 'NO'):
        return False
    raise PowerSupplyError(f'invalid output reply: {raw!r}')


def parse_mode(raw):
    value = raw.strip().upper()
    if value not in ('CV', 'CC'):
        raise PowerSupplyError(f'invalid CV/CC reply: {raw!r}')
    return value


@dataclass(frozen=True)
class PowerMeasurement:
    voltage: float
    current: float
    received_at: float

    @property
    def power(self):
        return self.voltage * self.current


@dataclass(frozen=True)
class PowerState(PowerMeasurement):
    voltage_set: float
    current_set: float
    output: bool
    mode: str
