"""Each application script explicitly declares the motor IDs it will use."""
from .core.config import BenchConfig
from .components.motor.client import MotorBus


class Bench:
    """管理一条 CAN 总线及上层指定的电机集合，不选择 USB 设备。

    transport：上层创建、尚未打开的独立传输实例，例如 Canable(sn)。
    本对象负责打开/关闭该实例；不要让多个 Bench 共用同一个实例。
    motor_ids：这条总线上要使用的 ID；不同总线允许有相同 ID。
    rate_hz：周期发送的目标频率；打开 Bench 不会自动使能电机。

    多适配器时，上层为每个 SN 创建一个传输实例和一个 Bench。
    使用 bench.start() 启动运行时后，通过运行时的 motor(id) 操作电机。
    """
    def __init__(self, *, transport, motor_ids, bitrate=1_000_000, data_bitrate=4_000_000,
                 sample_point=75.0, data_sample_point=64.0, rate_hz=1000,
                 command_timeout=0.25):
        self._transport = transport
        self.config = BenchConfig(motor_ids=tuple(motor_ids), bitrate=bitrate,
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
        can = self._transport
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
        """Start one shared I/O worker; opt into monitor() or explicit enable()."""
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
