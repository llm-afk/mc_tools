"""Build/load the native single-owner sweep engine; Python never clocks MIT TX."""
import ctypes as C
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

from scripts.position_sweep.trajectory import Home


class Config(C.Structure):
    _fields_ = [(name, C.c_double) for name in (
        'rate f0 f1 duration amplitude ramp home_position home_duration settle '
        'home_timeout position_tolerance velocity_tolerance motor_temperature '
        'driver_temperature feedback_timeout lease').split()] + [
        (name, C.c_uint32) for name in ('id', 'kp', 'kd', 'derivative')] + [
        ('max_travel', C.c_double), ('max_velocity', C.c_double), ('feedforward', C.c_uint32)]


class Row(C.Structure):
    _fields_ = [(name, C.c_double) for name in
                'target kp kd position velocity torque mt bt at frequency'.split()]


class Stats(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in 'tx rx rows skipped gaps limited counters'.split()] + [
        (name, C.c_double) for name in 'first_tx last_tx max_gap max_write first_rx last_rx max_submit motion_origin'.split()] + [
        (name, C.c_uint32) for name in ('code', 'win_error', 'disable_ack', 'can_error_id', 'status_word')] + [
        ('can_error_data', C.c_ubyte*8)]


class Control(C.Structure):
    _fields_ = [('stop', C.c_int32), ('heartbeat', C.c_int64), ('frequency_bits', C.c_int64)]


def load_engine():
    if sys.platform != 'win32' or C.sizeof(C.c_void_p) != 8:
        raise RuntimeError('native sweep requires 64-bit Windows Python')
    source = Path(__file__).with_name('native_engine.c')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    folder = source.parents[2]/'build'/'j60_position_sweep'
    folder.mkdir(parents=True, exist_ok=True)
    binary = folder/f'sweep-{digest}.dll'
    if not binary.exists():
        compiler = shutil.which('gcc')
        if compiler is None:
            raise RuntimeError('native sweep needs 64-bit MinGW gcc on PATH; no Python timing fallback')
        import tempfile
        with tempfile.TemporaryDirectory(dir=folder) as temporary:
            if not Path(temporary).resolve().is_relative_to(folder.resolve()):
                raise RuntimeError('native build temporary directory escaped its build folder')
            output = Path(temporary)/'engine.dll'
            subprocess.run([compiler, '-shared', '-O2', '-Wall', '-Wextra', '-Werror',
                            '-o', str(output), str(source), '-lwinusb', '-static-libgcc'],
                           check=True, capture_output=True, timeout=60,
                           creationflags=0x08000000)
            output.replace(binary)
    dll = C.CDLL(str(binary))
    dll.sweep_size.argtypes = [C.c_uint32]
    dll.sweep_size.restype = C.c_uint32
    for i, structure in enumerate((Config, Row, Stats, Control)):
        if dll.sweep_size(i) != C.sizeof(structure):
            raise RuntimeError('native sweep ABI mismatch')
    dll.sweep_run.argtypes = [C.c_void_p, C.c_void_p, C.c_uint, C.c_uint,
                              C.POINTER(Config), C.POINTER(Row), C.c_uint64,
                              C.POINTER(Stats), C.POINTER(Control)]
    dll.sweep_run.restype = C.c_int
    for name in ('sweep_heartbeat', 'sweep_stop'):
        getattr(dll, name).argtypes = [C.POINTER(Control)]
        getattr(dll, name).restype = None
    dll.sweep_sample.argtypes = [C.POINTER(Config), C.c_double,
                                 C.POINTER(C.c_double), C.POINTER(C.c_double)]
    dll.sweep_sample.restype = None
    dll.sweep_current_frequency.argtypes = [C.POINTER(Control)]
    dll.sweep_current_frequency.restype = C.c_double
    dll.sweep_frequency.argtypes = [C.POINTER(Config), C.c_double]
    dll.sweep_frequency.restype = C.c_double
    dll.j60_pack.argtypes = [C.c_double]*5 + [C.POINTER(C.c_ubyte)]
    dll.j60_pack.restype = None
    return dll


def config_from_args(args, position):
    kp = args.home_kp if getattr(args, 'feedforward', False) else args.kp
    kd = args.home_kd if getattr(args, 'feedforward', False) else args.kd
    home = Home(position, args.home_speed, args.kp, args.kd, args.velocity_mode)
    return Config(args.rate, args.start_hz, args.stop_hz, args.duration, args.amplitude,
                  args.ramp, position, home.duration, args.settle, args.home_timeout,
                  args.zero_tolerance, args.zero_velocity, args.max_motor_temp,
                  args.max_driver_temp, .1, .25, args.id, int(kp)*100,
                  int(kd*5)*20, int(args.velocity_mode=='derivative'),
                  getattr(args, 'max_travel', 0), getattr(args, 'max_velocity', 0),
                  int(getattr(args, 'feedforward', False)))


ERRORS = {1: 'cancelled', 2: 'USB transfer failed', 3: 'invalid feedback or CAN error',
          4: 'motor fault', 5: 'temperature limit', 6: 'feedback timeout',
          7: 'application lease expired', 8: 'motor did not settle at zero',
          9: 'CSV memory buffer full', 10: 'disable ACK not received',
          11: 'feedforward travel or velocity limit', 12: 'J60 status reply timeout'}


def run_native(serial, recorder, args):
    from j60.canable import J60Canable
    from j60.client import J60Client
    from j60.protocol import CONTROL, MotionCommand

    dll = load_engine()  # Build and allocate before opening USB or enabling.
    capacity = 500_000
    rows = (Row*capacity)()
    stats, control = Stats(), Control()
    transport = J60Canable(serial)
    client = J60Client(transport, args.id)
    entered = needs_disable = False
    failures = []
    finished = threading.Event()
    start_gate = threading.Event()
    cancel_before_start = threading.Event()
    usb = None
    try:
        transport.open()
        client.drain()
        config_info = client.get_config()
        if config_info['motor_id'] != args.id:
            raise RuntimeError('J60 config motor ID does not match requested ID')
        client.get_status()
        client.check_status()
        client.disable()
        needs_disable = True
        client.enable()
        client.send(CONTROL, MotionCommand().pack())
        deadline = time.perf_counter()+.1
        while client.feedback is None and time.perf_counter()<deadline:
            client.receive(max(.000001, deadline-time.perf_counter()))
        if client.feedback is None:
            raise TimeoutError('initial J60 motion feedback missing')
        initial = client.feedback
        if abs(initial.velocity)>args.zero_velocity:
            raise RuntimeError('initial J60 motor is moving')
        limit = args.max_motor_temp if initial.temperature_flag else args.max_driver_temp
        if initial.temperature>=limit:
            raise RuntimeError('initial J60 temperature exceeds limit')
        config = config_from_args(args, initial.position)
        usb = transport._usb
        usb.suspend_read()
        transport._rx.clear()
        dll.sweep_heartbeat(C.byref(control))

        def worker():
            try:
                start_gate.wait()
                if cancel_before_start.is_set():
                    return
                dll.sweep_run(usb.usb, usb.handle, usb.input, usb.output,
                              C.byref(config), rows, capacity, C.byref(stats), C.byref(control))
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=worker, name='j60-native-sweep', daemon=False)
        print('Native engine: PD homing -> settle -> position sweep -> zero settle -> disable; '
              'Ctrl+C stops and saves.', flush=True)
        interrupted = False
        try:
            thread.start()
            entered = True
            needs_disable = False  # The native owner now performs disable on every exit.
            start_gate.set()
            next_display = 0.0
            while not finished.is_set():
                try:
                    dll.sweep_heartbeat(C.byref(control))
                    now = time.perf_counter()
                    if now>=next_display:
                        frequency = dll.sweep_current_frequency(C.byref(control))
                        print(f'Sweep frequency: {frequency:.3f} Hz', flush=True)
                        next_display = now+.5
                    finished.wait(.01)
                except KeyboardInterrupt:
                    interrupted = True
                    dll.sweep_stop(C.byref(control))
        finally:
            # An interruption inside thread.start cannot race USB close: the
            # worker needs an explicit gate before it may touch native handles.
            if not entered:
                cancel_before_start.set()
            else:
                needs_disable = False
            start_gate.set()
            dll.sweep_stop(C.byref(control))
            while entered and not finished.is_set():
                try:
                    finished.wait(.01)
                except KeyboardInterrupt:
                    interrupted = True
            if thread.ident is not None:
                thread.join()
            usb._write_timeouts.clear()
            if failures:
                needs_disable = True  # A ctypes invocation may fail before C takes ownership.
        if failures:
            raise failures[0]
        if interrupted or stats.code==1:
            raise KeyboardInterrupt
        if stats.code:
            detail = ''
            if stats.code==11 and stats.rows:
                last = rows[stats.rows-1]
                detail = (f'; travel={abs(last.position-stats.motion_origin):.6g} rad, '
                          f'limit={args.max_travel:g}; speed={abs(last.velocity):.6g}/{args.max_velocity:g} rad/s')
            if stats.can_error_id:
                detail += f'; CAN error ID=0x{stats.can_error_id:X}, raw={bytes(stats.can_error_data).hex(" ")}'
            if stats.status_word:
                detail += f'; J60 status=0x{stats.status_word:04X}'
            raise RuntimeError(f'native J60 sweep: {ERRORS.get(stats.code, stats.code)}; '
                               f'Win32={stats.win_error}; disable_ack={bool(stats.disable_ack)}{detail}')
    finally:
        original = sys.exception()
        try:
            if needs_disable:
                try:
                    client.disable()
                except Exception as exc:
                    if original is None:
                        raise
                    original.add_note(f'Disable unconfirmed: {exc}')
            if entered:
                recorder.write_native_rows(rows, stats.rows)
                span = stats.last_tx-stats.first_tx
                print('Native statistics:', dict(
                    tx_count=stats.tx, rx_count=stats.rx, rows=stats.rows,
                    actual_tx_hz=(stats.tx-1)/span if span>0 else 0,
                    max_tx_gap_s=stats.max_gap, tx_gaps_over_5ms=stats.gaps,
                    skipped_periods=stats.skipped, feedback_limited_periods=stats.limited,
                    max_usb_write_s=stats.max_write, max_usb_submit_s=stats.max_submit,
                    can_counter_reports=stats.counters, disable_ack=bool(stats.disable_ack), code=stats.code),
                    flush=True)
                if not stats.disable_ack and original is not None:
                    original.add_note('Native J60 disable ACK not confirmed')
        finally:
            try:
                transport.close()
            except Exception as exc:
                if original is None:
                    raise
                original.add_note(f'CANable close failed: {exc}')
