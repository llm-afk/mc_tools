"""Passive port detection; requires multiple CRC-valid frames, sends no bytes."""
import time
from .client import Dynamometer
from .protocol import positive


def detect_dynamometers(*, ports=None, baudrate=115200, duration=0.3):
    positive(duration)
    if ports is None:
        from serial.tools.list_ports import comports
        ports = [item.device for item in comports()]
    matches = []
    for port in dict.fromkeys(ports):
        try:
            with Dynamometer(port=port, baudrate=baudrate) as device:
                deadline = time.perf_counter()+duration
                while time.perf_counter() < deadline:
                    if device.stats()['rx_count'] >= 3:
                        matches.append(port)
                        break
                    time.sleep(0.01)
        except (OSError, RuntimeError):
            continue
    return matches
