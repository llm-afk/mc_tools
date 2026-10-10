"""Bounded TX history and unsmoothed, per-TPDO CSV output."""
from collections import deque
import csv
from dataclasses import dataclass
from pathlib import Path
import struct
import threading

from motor_platform import MitCommand
from .trajectory import Reference


COLUMNS = (
    "target_position_rad", "kp", "kd", "position_rad", "velocity_rad_s", "torque",
    "motor_temperature_c", "driver_temperature_c", "frequency_hz",
)


@dataclass(frozen=True)
class Sent:
    index: int
    generated_at: float | None
    sent_at: float | None
    reference: Reference


class Recorder:
    def __init__(self, path, origin, *, capacity=8192, capture_enabled=True,
                 defer_csv=False, max_buffered_rows=500_000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("x", encoding="utf-8-sig", newline="")
        self._writer = csv.DictWriter(self._file, COLUMNS)
        self._writer.writeheader()
        self._file.flush()
        self.origin, self.capacity = origin, capacity
        self.capture_after = origin if capture_enabled else None
        self.rows = self.commands = 0
        self.first_rx = self.last_rx = None
        self._lock = threading.Lock()
        self._pending = deque()
        self._defer_csv = defer_csv
        self._max_buffered_rows = max_buffered_rows
        self._buffered = deque()
        # Enabling and pre-source periodic commands are neutral by contract.
        # Their individual USB write timestamps are not observed here.
        self._current = Sent(0, None, None, Reference("enable", MitCommand()))

    def begin_capture(self, started):
        """Only the application thread calls this, after zero is confirmed."""
        if self.rows:
            raise RuntimeError("capture already contains feedback")
        self.origin = self.capture_after = started

    def command(self, generated_at, sent_at, reference):
        # Worker callback: append only, never touch disk here.
        with self._lock:
            if len(self._pending) >= self.capacity:
                raise RuntimeError("sweep command recording queue overflow")
            self.commands += 1
            self._pending.append(Sent(self.commands, generated_at, sent_at, reference))

    @staticmethod
    def _target(reference):
        # Save the float32 / 0.01 gain values that were actually encoded.
        p, v, torque, kp, kd = struct.unpack("<fffHH", reference.command.pack())
        return dict(target_position_rad=p, kp=kp/100, kd=kd/100,
                    frequency_hz=reference.frequency_hz)

    def write(self, state):
        if self.last_rx is not None and state.received_at < self.last_rx:
            raise RuntimeError("motor feedback timestamps went backwards")
        with self._lock:
            while self._pending and self._pending[0].sent_at <= state.received_at:
                self._current = self._pending.popleft()
            command = self._current
        # Consume TX history even during homing, so the queue stays bounded.
        if self.capture_after is None or state.received_at < self.capture_after:
            return
        if self._defer_csv and len(self._buffered) >= self._max_buffered_rows:
            raise RuntimeError("sweep CSV memory buffer full; stopping before dropping more feedback")
        self.rows += 1
        self.first_rx = state.received_at if self.first_rx is None else self.first_rx
        self.last_rx = state.received_at
        if self._defer_csv:
            # Keep the historical command with each packet; no packing/formatting/I/O.
            self._buffered.append((command, state))
        else:
            self._write_row(command, state)

    def _write_row(self, command, state):
        self._writer.writerow(dict(
            **self._target(command.reference), position_rad=state.position,
            velocity_rad_s=state.velocity, torque=state.torque,
            motor_temperature_c=state.motor_temperature,
            driver_temperature_c=state.board_temperature,
        ))

    def preview(self, reference):
        self.rows += 1
        self._writer.writerow(self._target(reference))

    def write_native_rows(self, rows, count):
        """Native owner has stopped; serialize its exact, ordered packet records."""
        for i in range(count):
            row = rows[i]
            self._writer.writerow(dict(zip(COLUMNS, (row.target, row.kp, row.kd,
                row.position, row.velocity, row.torque, row.mt, row.bt, row.frequency))))
            self.rows += 1
            if self.first_rx is None:
                self.first_rx = row.at
            self.last_rx = row.at

    @property
    def actual_rx_hz(self):
        span = (self.last_rx-self.first_rx) if self.rows > 1 and self.last_rx is not None else 0
        return (self.rows-1)/span if span > 0 else 0.0

    def flush(self):
        # Deferred mode: only call after the motor worker has stopped.
        while self._buffered:
            command, state = self._buffered[0]
            self._write_row(command, state)
            self._buffered.popleft()
        self._file.flush()

    def close(self):
        if not self._file.closed:
            try:
                self.flush()
            finally:
                self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
