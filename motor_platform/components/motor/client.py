"""Single receive owner; synchronous, serialized SDO transactions."""
import math
from collections import deque
import threading
import time
from typing import Protocol

from .protocol import CanFrame, MitCommand, control_frame, mit_frame, parse_state, validate_node


class CanTransport(Protocol):
    def send(self, frame: CanFrame) -> None: ...
    def recv(self, timeout: float) -> CanFrame | None: ...
    def close(self) -> None: ...


class SdoError(RuntimeError):
    pass


def _timeout(value):
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be finite and positive")
    return value


class MotorBus:
    """Borrow an already open transport. Only this object may receive from it.

    Call poll()/receive_state() to update states; there is no background sender.
    close() releases transport and does not implicitly disable motors.

    USB SN 与适配器选择由上层负责；这里仅接收已打开的 CAN 传输实例。
    同一总线上的电机共用本对象，不同总线分别创建，电机 ID 按总线隔离。
    """
    def __init__(self, transport: CanTransport):
        self.transport = transport
        self._motors = {}
        self._lock = threading.RLock()
        self._closed = False
        self._sdo_uncertain = False
        self._io_owner = None

    def motor(self, node_id: int):
        validate_node(node_id)
        with self._lock:
            if node_id not in self._motors:
                self._motors[node_id] = Motor(self, node_id)
            return self._motors[node_id]

    def _check(self):
        if self._closed:
            raise RuntimeError("MotorBus is closed")
        if self._io_owner is not None and threading.current_thread() is not self._io_owner:
            raise RuntimeError("periodic runtime owns this bus; use its motor instances")

    def _dispatch(self, frame):
        if frame.is_error:
            raise OSError(f"CAN error frame: {frame.can_id:#x} {frame.data.hex()}")
        if frame.is_extended or frame.is_remote:
            return
        for motor in self._motors.values():
            if frame.can_id == 0x190 + motor.node_id:
                motor.last_state = parse_state(frame, motor.node_id, time.perf_counter())
                with motor._samples_lock:
                    if motor._samples is not None:
                        if len(motor._samples) == motor._samples.maxlen:
                            motor._dropped_samples += 1
                        motor._samples.append(motor.last_state)
                return motor.last_state

    def poll(self, timeout=0.1):
        _timeout(timeout)
        with self._lock:
            self._check()
            frame = self.transport.recv(timeout)
            return self._dispatch(frame) if frame is not None else None

    def _wait_sdo(self, node_id, index, deadline):
        key = index.to_bytes(2, "little") + b"\x00"
        while (remaining := deadline - time.perf_counter()) > 0:
            frame = self.transport.recv(remaining)
            if frame is None:
                continue
            self._dispatch(frame)
            if (frame.can_id == 0x580 + node_id and not frame.is_extended
                    and not frame.is_error and not frame.is_remote
                    and len(frame.data) == 8 and frame.data[1:4] == key):
                if frame.data[0] == 0x80:
                    raise SdoError(
                        f"node {node_id}: SDO 0x{index:04X} rejected; raw={frame.data.hex()}"
                    )
                return frame
        raise TimeoutError(f"node {node_id}: SDO 0x{index:04X} timeout")

    def _write_sdo(self, node_id, index, request, deadline):
        self._sdo_uncertain = True
        self.transport.send(request)
        try:
            reply = self._wait_sdo(node_id, index, deadline)
        except SdoError:
            self._sdo_uncertain = False
            raise
        if reply.data[0] != 0x60:
            self._sdo_uncertain = False
            raise SdoError(f"node {node_id}: unexpected SDO write reply {reply.data.hex()}")
        self._sdo_uncertain = False

    def _enable(self, node_id, timeout):
        deadline = time.perf_counter() + _timeout(timeout)
        with self._lock:
            self._check()
            self.transport.send(mit_frame(node_id, MitCommand()))
            while (remaining := deadline - time.perf_counter()) > 0:
                frame = self.transport.recv(remaining)
                if frame is None:
                    continue
                state = self._dispatch(frame)
                if state is not None and state.node_id == node_id:
                    if state.error_code:
                        raise SdoError(
                            f"node {node_id}: RPDO accepted but motor reports 0x{state.error_code:04X}"
                        )
                    self.motor(node_id).enabled = True
                    return state
            raise TimeoutError(f"node {node_id}: no TPDO5 response to enabling RPDO")

    def _disable(self, node_id, timeout):
        deadline = time.perf_counter() + _timeout(timeout)
        with self._lock:
            self._check()
            if self._sdo_uncertain:
                raise SdoError("previous SDO outcome uncertain; do not retry on this session")
            self._write_sdo(node_id, 0x2002, control_frame(node_id, False), deadline)
            self.motor(node_id).enabled = False

    def close(self):
        with self._lock:
            if not self._closed:
                self._check()
                self._closed = True
                self.transport.close()

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, *_):
        self.close()


class Motor:
    def __init__(self, bus: MotorBus, node_id: int):
        self._bus = bus
        self._node_id = node_id
        self.last_state = None
        self.enabled = None
        self._samples_lock = threading.Lock()
        self._samples = None
        self._dropped_samples = 0

    def start_recording(self, capacity=8192):
        """启用逐帧队列，供应用记录；state/last_state 仍只表示最新状态。

        一条反馈只入队一次，包括等待使能/失能期间收到的反馈。
        队列只允许一个消费者；溢出计数可通过 recording_dropped 查询。
        """
        if type(capacity) is not int or capacity <= 0:
            raise ValueError('capacity must be a positive integer')
        with self._samples_lock:
            if self._samples is not None:
                raise RuntimeError('motor recording already started')
            self._samples = deque(maxlen=capacity)
            self._dropped_samples = 0

    def drain_states(self):
        """一次取出尚未消费的全部真实反馈；重复调用不会重复返回。"""
        with self._samples_lock:
            if self._samples is None:
                raise RuntimeError('start_recording before draining motor frames')
            result = list(self._samples)
            self._samples.clear()
            return result

    @property
    def recording_dropped(self):
        with self._samples_lock:
            return self._dropped_samples

    @property
    def node_id(self):
        return self._node_id

    def enable(self, timeout=0.5):
        return self._bus._enable(self.node_id, timeout)

    def disable(self, timeout=0.5, *, wait_ack=True):
        if wait_ack:
            self._bus._disable(self.node_id, timeout)
        else:
            # A timed-out enable may have executed. Permit explicit best-effort
            # disable, but never mistake a delayed enable ACK for disable proof.
            with self._bus._lock:
                self._bus._check()
                self._bus._sdo_uncertain = True
                self._bus.transport.send(control_frame(self.node_id, False))
                self.enabled = None

    def send_mit(self, command: MitCommand):
        frame = mit_frame(self.node_id, command)
        with self._bus._lock:
            self._bus._check()
            self._bus.transport.send(frame)

    def receive_state(self, timeout=0.1):
        """Wait for the next TPDO5; sends nothing and never returns cached state.

        The 0x400 control RPDO and TPDO5 have no sequence number, so a reply
        cannot be correlated strictly to one periodic command.
        """
        deadline = time.perf_counter() + _timeout(timeout)
        with self._bus._lock:
            while (remaining := deadline - time.perf_counter()) > 0:
                state = self._bus.poll(remaining)
                if state is not None and state.node_id == self.node_id:
                    return state
        raise TimeoutError(f"node {self.node_id}: no new TPDO5 received")
