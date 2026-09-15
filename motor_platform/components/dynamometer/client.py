"""One receive worker per instrument, fresh latest state and bounded sample FIFO."""
from collections import deque
import threading
import time

from ...transports.serial_stream import SerialStream
from .protocol import FrameParser, parse_frame, positive


class Dynamometer:
    def __init__(self, *, port, baudrate=115200, torque_scale=100.0,
                 speed_scale=100.0, capacity=4096, transport=None):
        self.torque_scale, self.speed_scale = positive(torque_scale), positive(speed_scale)
        if type(capacity) is not int or capacity <= 0:
            raise ValueError('capacity must be a positive integer')
        self._transport = transport if transport is not None else SerialStream(port, baudrate=baudrate)
        self._samples = deque(maxlen=capacity)
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread = None
        self._latest = None
        self._error = None
        self._count = self._dropped = self._bytes = self._discarded = 0
        self._started = self._ended = 0.0

    def open(self):
        with self._condition:
            if self._thread is not None:
                raise RuntimeError('close the existing session before opening')
            self._transport.open()
            self._stop.clear()
            self._samples.clear()
            self._latest = self._error = None
            self._count = self._dropped = self._bytes = self._discarded = 0
            self._started, self._ended = time.perf_counter(), 0.0
            self._thread = threading.Thread(target=self._run, daemon=True, name='dynamometer-rx')
            try:
                self._thread.start()
            except BaseException:
                self._thread = None
                self._transport.close()
                raise
        return self

    def _run(self):
        parser = FrameParser()
        try:
            while not self._stop.is_set():
                data = self._transport.read()
                received = time.perf_counter()
                frames = parser.feed(data)
                states = [parse_frame(frame, torque_scale=self.torque_scale,
                                      speed_scale=self.speed_scale, received_at=received) for frame in frames]
                with self._condition:
                    self._bytes += len(data)
                    self._discarded = parser.discarded_bytes
                    for state in states:
                        if len(self._samples) == self._samples.maxlen:
                            self._dropped += 1
                        self._samples.append(state)
                        self._latest = state
                        self._count += 1
                    self._condition.notify_all()
                if not data:
                    self._stop.wait(0.001)
        except BaseException as exc:
            with self._condition:
                self._error = exc
        finally:
            try:
                self._transport.close()  # Reader owns close: no concurrent read/free.
            except BaseException as exc:
                with self._condition:
                    self._error = self._error or exc
            with self._condition:
                self._ended = time.perf_counter()
                self._stop.set()
                self._condition.notify_all()

    def _check(self):
        if self._error is not None:
            raise RuntimeError(f'dynamometer receive failed: {self._error}') from self._error
        if self._thread is None or self._stop.is_set():
            raise RuntimeError('dynamometer session is closed')

    def state(self, max_age=0.1):
        positive(max_age)
        with self._condition:
            self._check()
            if self._latest is None or time.perf_counter()-self._latest.received_at > max_age:
                raise TimeoutError('dynamometer feedback absent or stale')
            return self._latest

    def receive_state(self, timeout=0.5):
        """Wait for a new frame after this call; never reuse cached data."""
        deadline = time.perf_counter()+positive(timeout)
        with self._condition:
            self._check()
            previous = self._count
            while self._count == previous:
                remaining = deadline-time.perf_counter()
                if remaining <= 0:
                    raise TimeoutError('no new dynamometer frame')
                self._condition.wait(remaining)
                self._check()
            return self._latest

    def drain(self):
        """Return all queued frames once, in order (also available after close)."""
        with self._condition:
            result = list(self._samples)
            self._samples.clear()
            return result

    def stats(self):
        with self._condition:
            elapsed = (self._ended or time.perf_counter())-self._started if self._started else 0
            return dict(rx_count=self._count, rx_bytes=self._bytes,
                        discarded_bytes=self._discarded, dropped_samples=self._dropped,
                        buffered_samples=len(self._samples),
                        actual_rx_hz=self._count/elapsed if elapsed > 0 else 0,
                        fault=str(self._error) if self._error is not None else None)

    def close(self):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)
            if thread.is_alive():
                raise RuntimeError('serial reader did not stop; transport not closed concurrently')
            self._thread = None
        if self._error is not None:
            raise RuntimeError(f'dynamometer closed after receive failure: {self._error}') from self._error

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except Exception as cleanup:
            if exc is None:
                raise
            exc.add_note(str(cleanup))
