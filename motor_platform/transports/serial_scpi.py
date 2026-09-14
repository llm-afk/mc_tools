"""Bounded, serialized ASCII line transactions over RS-232 (pyserial optional)."""
import math
import threading
import time


class SerialScpi:
    def __init__(self, port, *, baudrate=9600, timeout=0.3, serial_factory=None):
        if not isinstance(port, str) or not port.strip() or port.lower() in ('auto', 'scan'):
            raise ValueError('script must specify a serial port')
        if type(baudrate) is not int or baudrate <= 0:
            raise ValueError('baudrate must be positive')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be finite and positive')
        self.port, self.baudrate, self.timeout = port, baudrate, timeout
        self._factory = serial_factory
        self._serial = None
        self._uncertain = False
        self._lock = threading.RLock()

    def open(self):
        with self._lock:
            if self._serial is not None:
                raise RuntimeError('serial session already open')
            factory = self._factory
            if factory is None:
                from serial import Serial
                factory = Serial
            stream = factory(port=self.port, baudrate=self.baudrate,
                             bytesize=8, parity='N', stopbits=1,
                             timeout=self.timeout, write_timeout=self.timeout,
                             xonxoff=False, rtscts=False, dsrdtr=False)
            try:
                time.sleep(0.3)  # Preserve the known-working device startup delay.
                stream.reset_input_buffer()
            except BaseException:
                stream.close()
                raise
            self._serial = stream
            self._uncertain = False
        return self

    @staticmethod
    def _encode(command):
        if not isinstance(command, str) or not command or any(c in command for c in '\r\n;'):
            raise ValueError('one SCPI command per transaction required')
        return command.encode('ascii') + b'\n'

    def _check(self, allow_uncertain=False):
        if self._serial is None:
            raise RuntimeError('serial session is closed')
        if self._uncertain and not allow_uncertain:
            raise RuntimeError('SCPI response alignment uncertain; close and reopen the session')

    def write(self, command, *, allow_uncertain=False):
        data = self._encode(command)
        with self._lock:
            self._check(allow_uncertain)
            try:
                if self._serial.write(data) != len(data):
                    raise OSError('incomplete serial write')
            except BaseException:
                self._uncertain = True
                raise

    def query(self, command):
        with self._lock:
            self._check()
            self.write(command)
            try:
                raw = self._serial.read_until(b'\n', 1025)
                if len(raw) > 1024:
                    raise ValueError('SCPI response exceeds 1024 bytes')
                if not raw.endswith(b'\n'):
                    raise TimeoutError(f'no complete SCPI reply to {command}')
                reply = raw.decode('ascii').strip()
                if not reply:
                    raise ValueError(f'empty SCPI reply to {command}')
                return reply
            except BaseException:
                # Never assign a late, untagged reply to the next query.
                self._uncertain = True
                raise

    def close(self):
        with self._lock:
            stream, self._serial = self._serial, None
            if stream is not None:
                stream.close()
