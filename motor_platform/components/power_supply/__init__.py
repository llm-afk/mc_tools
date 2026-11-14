"""UDP6721 power-supply component, independent of CAN and motor scheduling."""
from .client import PowerSupply
from .protocol import PowerMeasurement, PowerState, PowerSupplyError

__all__ = ['PowerSupply', 'PowerMeasurement', 'PowerState', 'PowerSupplyError']

from .discovery import detect_power_supplies, resolve_power_port

__all__ += ["detect_power_supplies", "resolve_power_port"]
