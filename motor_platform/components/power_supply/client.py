"""One explicit power-supply instance per serial port; no implicit output changes."""
import threading
import time

from ...transports.serial_scpi import SerialScpi
from .protocol import (PowerMeasurement, PowerState, PowerSupplyError,
                       number, parse_number, parse_output, parse_mode)


class PowerSupply:
    def __init__(self, *, port, baudrate=9600, timeout=0.3, transport=None):
        self._transport = transport if transport is not None else SerialScpi(port, baudrate=baudrate, timeout=timeout)
        self._lock = threading.RLock()
        self._opened = False
        self.identity = None

    def open(self):
        with self._lock:
            if self._opened:
                raise RuntimeError('power supply already open')
            self._transport.open()
            try:
                identity = self._transport.query('*IDN?')
                if 'UDP6721' not in identity.upper():
                    raise PowerSupplyError(f'expected UDP6721, received {identity!r}')
            except BaseException:
                self._transport.close()
                raise
            self.identity = identity
            self._opened = True
        return self

    def _check(self):
        if not self._opened:
            raise RuntimeError('open power supply before use')

    def _set(self, command, value, digits, query):
        text = f'{number(value):.{digits}f}'
        with self._lock:
            self._check()
            self._transport.write(f'{command} {text}')
            actual = parse_number(self._transport.query(query))
            if abs(actual - float(text)) > 0.5 * 10**(-digits) + 1e-9:
                raise PowerSupplyError(f'{query} readback {actual} differs from requested {text}')
            return actual

    def set_voltage(self, voltage):
        return self._set('SOUR:VOLT', voltage, 2, 'VOLT?')

    def set_current(self, current):
        """Set the output current limit (A), with readback."""
        return self._set('SOUR:CURR', current, 3, 'CURR?')

    def set_output(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be bool')
        with self._lock:
            self._check()
            self._transport.write('OUTP ON' if enabled else 'OUTP OFF')
            actual = parse_output(self._transport.query('OUTP?'))
            if actual != enabled:
                raise PowerSupplyError('output readback differs from requested state')
            return actual

    def enable(self):
        return self.set_output(True)

    def disable(self, *, verify=True):
        if verify:
            return self.set_output(False)
        with self._lock:
            self._check()
            self._transport.write('OUTP OFF', allow_uncertain=True)
        # No ACK/readback: completion only means bytes were written.
        return None

    def measure(self):
        """Fresh V/I readings; two sequential queries, power calculated as V*I."""
        with self._lock:
            self._check()
            voltage = parse_number(self._transport.query('MEAS:VOLT?'))
            current = parse_number(self._transport.query('MEAS:CURR?'))
            return PowerMeasurement(voltage, current, time.perf_counter())

    def read_state(self):
        """Fresh measurements, setpoints, output and CV/CC; never a local cache."""
        with self._lock:
            self._check()
            voltage_set = parse_number(self._transport.query('VOLT?'))
            current_set = parse_number(self._transport.query('CURR?'))
            output = parse_output(self._transport.query('OUTP?'))
            mode = parse_mode(self._transport.query('OUTP:CVCC?'))
            measured = self.measure()
            return PowerState(measured.voltage, measured.current, measured.received_at,
                              voltage_set, current_set, output, mode)

    def _write(self, command):
        with self._lock:
            self._check()
            self._transport.write(command)

    def set_ovp(self, voltage):
        """Write OVP only; the source driver has no validated readback query."""
        self._write(f'SOUR:VOLT:PROT {number(voltage):.2f}')

    def set_ocp(self, current):
        """Write OCP only; no readback confirmation."""
        self._write(f'SOUR:CURR:PROT {number(current):.3f}')

    def remote(self):
        self._write('SYST:REM')

    def local(self):
        self._write('SYST:LOC')

    def close(self):
        """Close the serial connection. Does not switch the power output."""
        with self._lock:
            if self._opened:
                self._opened = False
                self.identity = None
                self._transport.close()

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except Exception as cleanup:
            if exc is None:
                raise
            exc.add_note(f'Power supply close failed: {cleanup}')
