"""Each application script explicitly declares the motor IDs it will use."""
from .core.config import BenchConfig
from .components.motor.client import MotorBus
from .transports.canable import Canable


class Bench:
    def __init__(self, *, motor_ids, serial, bitrate=1_000_000, data_bitrate=4_000_000,
                 sample_point=75.0, data_sample_point=64.0, rate_hz=1000,
                 command_timeout=0.25):
        self.config = BenchConfig(serial=serial, motor_ids=tuple(motor_ids), bitrate=bitrate,
                                  data_bitrate=data_bitrate, sample_point=sample_point,
                                  data_sample_point=data_sample_point, rate_hz=rate_hz,
                                  command_timeout=command_timeout)
        self._bus = None
        self._motors = {}
        self._runtime = None

    def open(self):
        """Start CAN only; never enable a motor or start a periodic command loop."""
        if self._bus is not None:
            raise RuntimeError("bench is already open")
        cfg = self.config
        can = Canable(serial_number=cfg.serial)
        try:
            can.open(cfg.bitrate, cfg.data_bitrate, cfg.sample_point, cfg.data_sample_point)
            bus = MotorBus(can)
            motors = {node: bus.motor(node) for node in cfg.motor_ids}
        except BaseException:
            can.close()
            raise
        self._bus, self._motors = bus, motors
        return self

    def motor(self, node_id):
        if self._bus is None:
            raise RuntimeError("open bench before accessing motor")
        return self._motors[node_id]

    def poll(self, timeout=0.1):
        if self._bus is None:
            raise RuntimeError("bench is closed")
        return self._bus.poll(timeout)

    def start(self, rate_hz=None, command_timeout=None):
        """Start one shared I/O worker. Motors remain inactive until explicitly enabled."""
        from .runtime import ControlRuntime
        if self._bus is None:
            raise RuntimeError("open bench first")
        if self._runtime is not None:
            raise RuntimeError("this bench already created a runtime")
        runtime = ControlRuntime(self._bus, self._motors,
                                 self.config.rate_hz if rate_hz is None else rate_hz,
                                 self.config.command_timeout if command_timeout is None else command_timeout)
        self._runtime = runtime.start()
        return self._runtime

    def close(self):
        try:
            if self._runtime is not None:
                self._runtime.close()
        finally:
            self._runtime = None
            bus, self._bus = self._bus, None
            self._motors = {}
            if bus is not None:
                bus.close()

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except Exception as cleanup:
            if exc is None:
                raise
            exc.add_note(f'Cleanup failed: {cleanup}')
