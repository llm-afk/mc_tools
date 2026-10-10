"""One I/O worker per CAN bus; latest-value targets, bounded control queue.

Python/Windows scheduling is best effort. No catch-up bursts or hard real-time guarantee.
"""
from concurrent.futures import Future
from dataclasses import dataclass
import math
import queue
import threading
import time

from .components.motor.protocol import MitCommand


def positive(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("frequency/timeout must be finite and positive")
    return float(value)


@dataclass
class Stats:
    tx_count: int = 0
    rx_count: int = 0
    skipped_periods: int = 0
    feedback_limited_periods: int = 0
    max_lateness_s: float = 0.0
    max_tx_gap_s: float = 0.0
    tx_gaps_over_5ms: int = 0
    max_source_s: float = 0.0
    max_usb_write_s: float = 0.0
    first_tx_at: float = 0.0
    last_tx_at: float = 0.0
    fault: str | None = None


class MotorChannel:
    """One named motor instance. Targets are immutable and replaced atomically."""
    def __init__(self, runtime, name, motor, rate_hz):
        self._runtime, self._motor = runtime, motor
        self.name = name
        self._rate_hz = rate_hz
        self._target = MitCommand()
        self._source = self._on_sent = None
        self._active = False
        self._expires = self._next = 0.0
        self._inflight = 0
        self._last_rx = 0.0
        self._gap_anchor = None
        self._stats = Stats()

    @property
    def node_id(self):
        return self._motor.node_id

    @property
    def rate_hz(self):
        with self._runtime._lock: return self._rate_hz

    def set_rate(self, rate_hz):
        rate_hz = positive(rate_hz)
        with self._runtime._lock:
            self._runtime._check()
            self._rate_hz = rate_hz
            self._next = time.perf_counter()

    def set_mit(self, command: MitCommand):
        command.pack()  # Validate BEFORE replacing any part of the target.
        with self._runtime._lock:
            self._runtime._check()
            self._target = command
            self._source = self._on_sent = None
            self._expires = time.perf_counter() + self._runtime.command_timeout

    def set_mit_source(self, source, *, on_sent=None):
        """Evaluate source(monotonic_time) on each actual periodic send.

        Both callbacks run on the CAN worker: keep them bounded and free of I/O.
        on_sent(sent_at, command) runs only after a successful USB write; this
        is not proof of motor execution. Refresh this call within the normal
        application lease. set_mit() and enable() restore constant targets.
        Callback/command errors stop the worker and trigger its normal cleanup.
        """
        if not callable(source) or (on_sent is not None and not callable(on_sent)):
            raise TypeError("source/on_sent must be callable")
        with self._runtime._lock:
            self._runtime._check()
            self._source, self._on_sent = source, on_sent
            self._expires = time.perf_counter() + self._runtime.command_timeout

    def enable(self, timeout=0.1):
        """Send the neutral enabling RPDO; start periodic TX after TPDO feedback.

        Resets prior target to neutral. Call set_mit after enable, then keep refreshing it.
        """
        return self._runtime._call(self, True, positive(timeout))

    def disable(self, timeout=0.5):
        return self._runtime._call(self, False, positive(timeout))

    def state(self, max_age=0.1):
        positive(max_age)
        with self._runtime._lock:
            self._runtime._check()
            state = self._motor.last_state
            if state is None or time.perf_counter() - state.received_at > max_age:
                raise TimeoutError(f"{self.name}: motor feedback absent or stale")
            return state

    def start_recording(self, capacity=8192):
        """应用可选逐帧记录；必须在使能前调用，不改变 CAN 发送频率。"""
        self._motor.start_recording(capacity)

    def drain_states(self):
        return self._motor.drain_states()

    def stats(self):
        with self._runtime._lock:
            # Scalar fields only: avoid recursive deepcopy while holding the I/O lock.
            data = vars(self._stats).copy()
            data['max_poll_s'] = self._runtime._max_poll_s
            data['max_poll_overrun_s'] = self._runtime._max_poll_overrun_s
            data['recording_dropped'] = self._motor.recording_dropped
            data['can_counter_reports'] = getattr(self._runtime._bus.transport, 'counter_reports', 0)
            data['can_protocol_reports'] = getattr(self._runtime._bus.transport, 'protocol_reports', 0)
            data['can_last_protocol_report'] = getattr(self._runtime._bus.transport, 'last_protocol_report', None)
            data['can_last_counter_report'] = getattr(self._runtime._bus.transport, 'last_counter_report', None)
            span = data["last_tx_at"] - data["first_tx_at"]
            data.update(node_id=self.node_id, name=self.name, target_hz=self._rate_hz,
                        periodic_active=self._active,
                        enabled=self._motor.enabled,
                        actual_tx_hz=(data["tx_count"]-1)/span if span > 0 else 0.0)
            return data


class ControlRuntime:
    def __init__(self, bus, motors, rate_hz=1000, command_timeout=0.25, max_inflight=4):
        self.rate_hz, self.command_timeout = positive(rate_hz), positive(command_timeout)
        if type(max_inflight) is not int or not 1 <= max_inflight <= 32:
            raise ValueError("max_inflight must be in 1..32")
        self.max_inflight = max_inflight
        self._bus = bus
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._queue = queue.Queue(maxsize=32)
        self._error = None
        self._max_poll_s = self._max_poll_overrun_s = 0.0
        self._touched = set()
        self._channels = {name: MotorChannel(self, name, m, self.rate_hz) for name, m in motors.items()}
        self._by_id = {c.node_id: c for c in self._channels.values()}
        self._thread = threading.Thread(target=self._run, name="motor-can-io", daemon=True)

    def start(self):
        # Claim ownership before creating the worker, excluding manual receive/send.
        with self._bus._lock:
            self._bus._check()
            if self._bus._io_owner is not None:
                raise RuntimeError("bus already has a runtime")
            self._bus._io_owner = self._thread
        try:
            self._thread.start()
        except BaseException:
            self._bus._io_owner = None
            raise
        self._ready.wait()
        return self

    def _check(self):
        if self._error is not None:
            raise RuntimeError(f"CAN worker failed: {self._error}") from self._error
        if self._stop.is_set() or not self._thread.is_alive():
            raise RuntimeError("CAN worker is stopped")

    def motor(self, name):
        return self._channels[name]

    def _call(self, channel, enabled, timeout):
        future = Future()
        with self._lock:
            self._check()
            self._queue.put_nowait((channel, enabled, timeout, future))
        # Only worker touches USB. It resolves every queued future, including on failure.
        return future.result()

    def _control(self, channel, enabled, timeout):
        with self._lock:
            channel._active = False
        self._touched.add(channel.node_id)
        try:
            if enabled:
                channel._motor.enable(timeout)
                with self._lock:
                    channel._target = MitCommand()
                    channel._source = channel._on_sent = None
                    channel._expires = time.perf_counter() + self.command_timeout
                    channel._next = time.perf_counter()
                    channel._inflight = 0
                    channel._gap_anchor = None
                    channel._last_rx = time.perf_counter()
                    channel._active = True
                    channel._stats.fault = None
            else:
                channel._motor.disable(timeout)
                self._touched.discard(channel.node_id)
        except BaseException as exc:
            with self._lock: channel._stats.fault = repr(exc)
            channel._motor.disable(wait_ack=False)
            raise

    def _tick(self, now):
        for channel in self._channels.values():
            with self._lock:
                if not channel._active: continue
                if now >= channel._expires:
                    channel._active = False
                    channel._stats.fault = "application command lease expired"
                    expired = True
                else:
                    expired = False
                    if now < channel._next: continue
                    if channel._inflight >= self.max_inflight:
                        channel._stats.feedback_limited_periods += 1
                        channel._next = now + 1/channel._rate_hz
                        if now-channel._last_rx > 0.1:
                            raise TimeoutError(f"{channel.name}: feedback not keeping up for 100 ms")
                        continue
                    target, due, period = channel._target, channel._next, 1/channel._rate_hz
                    source, on_sent = channel._source, channel._on_sent
            if expired:
                # Do not wait for an ACK while other motors have scheduled traffic.
                channel._motor.disable(wait_ack=False)
                continue
            generated = time.perf_counter()
            if source is not None:
                target = source(generated)
                if not isinstance(target, MitCommand):
                    raise TypeError("MIT source must return MitCommand")
            write_started = time.perf_counter()
            channel._motor.send_mit(target)
            sent = time.perf_counter()
            if on_sent is not None:
                on_sent(sent, target)
            with self._lock:
                elapsed_periods = max(1, math.floor((sent-due)/period)+1)
                channel._next = due + elapsed_periods*period
                stats = channel._stats
                if channel._gap_anchor is not None:
                    gap = sent-channel._gap_anchor
                    stats.max_tx_gap_s = max(stats.max_tx_gap_s, gap)
                    stats.tx_gaps_over_5ms += int(gap > 0.005)
                channel._gap_anchor = sent
                stats.max_source_s = max(stats.max_source_s, write_started-generated)
                stats.max_usb_write_s = max(stats.max_usb_write_s, sent-write_started)
                stats.tx_count += 1
                channel._inflight += 1
                stats.skipped_periods += elapsed_periods-1
                stats.max_lateness_s = max(stats.max_lateness_s, sent-due)
                if stats.tx_count == 1: stats.first_tx_at = sent
                stats.last_tx_at = sent
            now = sent

    def _run(self):
        self._ready.set()
        try:
            while not self._stop.is_set():
                try:
                    ch, enabled, timeout, future = self._queue.get_nowait()
                except queue.Empty:
                    pass
                else:
                    try: self._control(ch, enabled, timeout)
                    except Exception as exc:
                        future.set_exception(exc)
                        raise  # Uncertain SDO/USB outcome stops the runtime visibly.
                    else: future.set_result(None)
                self._tick(time.perf_counter())
                with self._lock:
                    deadlines = [min(c._next,c._expires) for c in self._channels.values() if c._active]
                remaining = min(0.005, max(0.000001, min(deadlines)-time.perf_counter())) if deadlines else 0.005
                poll_started = time.perf_counter()
                state = self._bus.poll(remaining)
                poll_elapsed = time.perf_counter()-poll_started
                with self._lock:
                    self._max_poll_s = max(self._max_poll_s, poll_elapsed)
                    self._max_poll_overrun_s = max(self._max_poll_overrun_s, poll_elapsed-remaining)
                if state is not None:
                    with self._lock:
                        channel = self._by_id[state.node_id]
                        channel._stats.rx_count += 1
                        channel._inflight = max(0, channel._inflight-1)
                        channel._last_rx = time.perf_counter()
        except BaseException as exc:
            self._error = exc
        finally:
            self._stop.set()
            for channel in self._channels.values():
                with self._lock: channel._active = False
                if channel.node_id in self._touched:
                    try: channel._motor.disable(timeout=0.1)
                    except BaseException as exc:
                        with self._lock: channel._stats.fault = f"disable unconfirmed: {exc!r}"
                        try: channel._motor.disable(wait_ack=False)
                        except BaseException as send_exc:
                            self._error = self._error or send_exc
            self._bus._io_owner = None
            # Synchronize with producer's check + enqueue, so no future can be stranded.
            with self._lock:
                while True:
                    try: *_, future = self._queue.get_nowait()
                    except queue.Empty: break
                    future.set_exception(RuntimeError("CAN worker stopped"))

    def close(self):
        self._stop.set()
        self._thread.join()
        if self._error is not None:
            raise RuntimeError(f"CAN runtime stopped with error: {self._error}") from self._error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except Exception as cleanup:
            if exc is None:
                raise
            exc.add_note(f'Cleanup failed: {cleanup}')
