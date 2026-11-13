"""Explicit SCPI identification of serial ports; never changes power output."""
import threading
import time
from .client import PowerSupply
from .protocol import PowerSupplyError


def detect_power_supplies(*, ports=None, baudrate=9600, timeout=0.3, observations=None):
    if ports is None:
        from serial.tools.list_ports import comports
        ports = [item.device for item in comports()]
    matches = {}
    for port in dict.fromkeys(ports):
        started = time.perf_counter()
        result = {}
        def probe(port=port, result=result):
            try:
                with PowerSupply(port=port, baudrate=baudrate, timeout=timeout) as power:
                    result['identity'] = power.identity
            except Exception as exc:
                result.update(error_type=type(exc).__name__, error=str(exc))
        worker = threading.Thread(target=probe, daemon=True, name='power-port-probe')
        worker.start()
        worker.join(timeout=max(2.0, timeout + 1))
        stalled = worker.is_alive()
        if observations is not None:
            observations.append(dict(port=port, baudrate=baudrate, elapsed_s=round(time.perf_counter()-started, 4),
                                     **({'stalled': True} if stalled else result)))
        if stalled:
            # The worker owns cleanup if a driver eventually returns from open.
            raise TimeoutError(f'port probe stalled on {port}; specify --port explicitly')
        if 'identity' in result:
            matches[port] = result['identity']
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
