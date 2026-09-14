"""Explicit SCPI identification of serial ports; never changes power output."""
import threading
from .client import PowerSupply
from .protocol import PowerSupplyError


def detect_power_supplies(*, ports=None, baudrate=9600, timeout=0.3):
    if ports is None:
        from serial.tools.list_ports import comports
        ports = [item.device for item in comports()]
    matches = {}
    for port in dict.fromkeys(ports):
        result = []
        def probe(port=port, result=result):
            try:
                with PowerSupply(port=port, baudrate=baudrate, timeout=timeout) as power:
                    result.append(power.identity)
            except Exception:
                pass  # Not a responding UDP6721, or the port is unavailable.
        worker = threading.Thread(target=probe, daemon=True, name='power-port-probe')
        worker.start()
        worker.join(timeout=max(2.0, timeout + 1))
        if worker.is_alive():
            # The worker owns cleanup if a driver eventually returns from open.
            raise TimeoutError(f'port probe stalled on {port}; specify --port explicitly')
        if result:
            matches[port] = result[0]
    return matches


def resolve_power_port(port='auto', *, baudrate=9600):
    if port != 'auto':
        return port
    matches = detect_power_supplies(baudrate=baudrate)
    if not matches:
        raise PowerSupplyError('no responding UDP6721 found; check cable or specify --port COMxx')
    if len(matches) != 1:
        raise PowerSupplyError(f'multiple UDP6721 supplies found: {", ".join(matches)}; specify --port')
    return next(iter(matches))
