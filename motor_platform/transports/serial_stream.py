"""Read-only bounded serial byte stream for actively reporting instruments."""
import math


class SerialStream:
    def __init__(self, port, *, baudrate=115200, timeout=0.01, serial_factory=None):
        if not isinstance(port, str) or not port.strip() or port.lower() in ('auto', 'scan'):
            raise ValueError('specify an explicit serial port')
        if type(baudrate) is not int or baudrate <= 0:
            raise ValueError('baudrate must be a positive integer')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 0.1:
            raise ValueError('read timeout must be in (0, 0.1] seconds')
        self.port, self.baudrate, self.timeout = port, baudrate, timeout
        self._factory = serial_factory
        self._serial = None

    def open(self):
        if self._serial is not None:
            raise RuntimeError('serial stream already open')
        factory = self._factory
        if factory is None:
            from serial import Serial
            factory = Serial
        self._serial = factory(port=self.port, baudrate=self.baudrate,
                               bytesize=8, parity='N', stopbits=1, timeout=self.timeout,
                               xonxoff=False, rtscts=False, dsrdtr=False)
        return self

    def read(self):
        if self._serial is None:
            raise RuntimeError('serial stream closed')
        # On Windows some USB-serial drivers wait for a large requested byte
        # count instead of returning promptly with a partial buffer. Read the
        # bytes already queued; when empty, block for exactly one byte using
        # the configured short timeout.
        waiting = getattr(self._serial, 'in_waiting', 0)
        return self._serial.read(min(4096, max(1, waiting)))

    def close(self):
        stream, self._serial = self._serial, None
        if stream is not None:
            stream.close()
