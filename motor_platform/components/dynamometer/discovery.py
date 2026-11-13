"""Passive port detection; requires multiple CRC-valid frames, sends no bytes."""
import time
from .client import Dynamometer
from .protocol import positive


def detect_dynamometers(*, ports=None, baudrate=115200, duration=0.3, observations=None):
    positive(duration)
    if ports is None:
        from serial.tools.list_ports import comports
        ports = [item.device for item in comports()]
    matches = []
    for port in dict.fromkeys(ports):
        started = time.perf_counter()
        evidence = dict(port=port, baudrate=baudrate, opened=False, matched=False)
        try:
            with Dynamometer(port=port, baudrate=baudrate) as device:
                evidence['opened'] = True
                deadline = time.perf_counter()+duration
                while True:
                    stats = device.stats()
                    if stats['rx_count'] >= 3 or stats.get('fault') or time.perf_counter() >= deadline:
                        break
                    time.sleep(0.01)
                evidence.update(stats)
            if stats['rx_count'] >= 3 and not stats.get('fault'):
                matches.append(port)
                evidence['matched'] = True
        except (OSError, RuntimeError) as exc:
            evidence.update(error_type=type(exc).__name__, error=str(exc))
        if observations is not None:
            observations.append(dict(evidence, elapsed_s=round(time.perf_counter()-started, 4)))
    return matches
