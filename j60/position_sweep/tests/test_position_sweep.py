import csv
import ctypes as C
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from j60.position_sweep import app, native
from j60.protocol import MotionCommand, MotionFeedback
from scripts.position_sweep.trajectory import Chirp


@contextmanager
def temporary_folder():
    base=Path(__file__).resolve().parent
    folder=tempfile.TemporaryDirectory(prefix='.sweep-test-',dir=base)
    if not Path(folder.name).resolve().is_relative_to(base):
        raise RuntimeError('test directory escaped its intended root')
    with folder:
        yield Path(folder.name)


class ScriptTests(unittest.TestCase):
    def test_preview_without_hardware_matches_wire_quantization(self):
        with temporary_folder() as folder, patch.object(app,'select_canable',side_effect=AssertionError('USB')), redirect_stdout(io.StringIO()):
            path=folder/'preview.csv'
            self.assertEqual(app.main(['--preview','--rate','100','--duration','.1','--output',str(path)]),0)
            with path.open(encoding='utf-8-sig') as stream:
                rows=list(csv.DictReader(stream))
            self.assertEqual(tuple(rows[0]),app.COLUMNS)
            self.assertEqual(len(rows),461)
            self.assertTrue(all(r['position_rad']=='' for r in rows))
            self.assertTrue(all(float(r['kp'])==100 and float(r['kd'])==2 for r in rows))
            self.assertEqual(float(rows[-1]['target_position_rad']),app.encoded_targets(0,100,2)[0])

    def test_defaults_and_j60_limits(self):
        a=app.parse_args([])
        self.assertEqual((a.rate,a.start_hz,a.stop_hz,a.duration,a.amplitude,a.kp,a.kd),(1000,.1,10,30,1,100,2))
        for flags in (['--id','16'],['--kp','.5'],['--kd','52'],['--amplitude','41'],['--rate','1001'],['--amplitude','nan'],['--velocity-mode','derivative']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):app.parse_args(flags)
        a=app.parse_args(['--kp','100.9','--kd','2.39'])
        c=native.config_from_args(a,0)
        self.assertEqual((c.kp,c.kd),(10000,220))

    def test_native_rows_keep_gains_and_missing_temperature(self):
        with temporary_folder() as folder:
            rows=(native.Row*2)()
            for r in rows:r.kp=100;r.kd=2;r.mt=float('nan')
            path=folder/'rows.csv'
            with app.Recorder(path) as recorder:recorder.write_native_rows(rows,2)
            with path.open(encoding='utf-8-sig') as stream:saved=list(csv.DictReader(stream))
            self.assertEqual(len(saved),2)
            self.assertTrue(all(float(r['kp'])==100 and r['motor_temperature_c']=='' for r in saved))

    def test_adapter_selection_failure_creates_no_csv(self):
        with temporary_folder() as folder, patch.object(app,'select_canable',side_effect=OSError('multiple')), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            path=folder/'rows.csv'
            self.assertEqual(app.main(['--output',str(path)]),1)
            self.assertFalse(path.exists())

class OwnershipTests(unittest.TestCase):
    def run_wrapper(self, *, enable_error=None, call_error=None, interrupt_start=False):
        events=[]
        transport=Mock()
        usb=transport._usb
        usb._write_timeouts={}
        transport.open.side_effect=lambda:events.append('open')
        transport.close.side_effect=lambda:events.append('close')
        usb.suspend_read.side_effect=lambda:events.append('suspend')
        client=Mock()
        client.get_config.return_value={'motor_id':1}
        client.feedback=MotionFeedback(0,0,0,0,26)
        if enable_error:client.enable.side_effect=enable_error
        recorder=Mock()
        recorder.write_native_rows.side_effect=lambda *_:events.append('save')
        dll=Mock()
        dll.sweep_current_frequency.return_value=0.0
        def execute(*values):
            events.append('native')
            if call_error:raise call_error
            stats=values[7]._obj
            stats.disable_ack=1
        dll.sweep_run.side_effect=execute
        class InterruptedThread(threading.Thread):
            def start(self):
                super().start()
                raise KeyboardInterrupt
        def build():
            events.append('build')
            return dll
        with patch.object(native,'load_engine',side_effect=build), \
             patch('j60.canable.J60Canable',return_value=transport), \
             patch('j60.client.J60Client',return_value=client), redirect_stdout(io.StringIO()):
            context=patch.object(native.threading,'Thread',InterruptedThread) if interrupt_start else None
            if context:context.start()
            try:
                native.run_native('fake',recorder,app.parse_args([]))
            except BaseException as exc:
                return exc,client,dll,events
            finally:
                if context:context.stop()
        return None,client,dll,events

    def test_build_handoff_save_and_close_order(self):
        error,client,_,events=self.run_wrapper()
        self.assertIsNone(error)
        self.assertEqual(events,['build','open','suspend','native','save','close'])
        client.disable.assert_called_once()
        client.enable.assert_called_once()

    def test_uncertain_enable_is_disabled_without_entering_native(self):
        error,client,dll,events=self.run_wrapper(enable_error=OSError('uncertain enable'))
        self.assertIsInstance(error,OSError)
        self.assertEqual(client.disable.call_count,2)
        dll.sweep_run.assert_not_called()
        self.assertEqual(events[-1],'close')

    def test_ctypes_call_failure_falls_back_to_confirmed_python_disable(self):
        error,client,_,events=self.run_wrapper(call_error=RuntimeError('ctypes call failed'))
        self.assertIsInstance(error,RuntimeError)
        self.assertEqual(client.disable.call_count,2)
        self.assertEqual(events[-2:],['save','close'])

    def test_interrupted_thread_start_does_not_use_closed_usb(self):
        error,client,dll,events=self.run_wrapper(interrupt_start=True)
        self.assertIsInstance(error,KeyboardInterrupt)
        self.assertEqual(client.disable.call_count,2)
        dll.sweep_run.assert_not_called()
        self.assertNotIn('native',events)
        self.assertEqual(events[-1],'close')


@unittest.skipUnless(sys.platform=='win32' and shutil.which('gcc'),'Windows MinGW required')
class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dll=native.load_engine()
        source=Path(__file__).with_name('native_fake.c')
        folder=Path(native.__file__).resolve().parents[2]/'build'/'j60_position_sweep'
        output=folder/'fake-tests.dll'
        subprocess.run(['gcc','-shared','-O2','-Wall','-Wextra','-Werror',
                        '-o',str(output),str(source),'-static-libgcc'],
                       check=True,capture_output=True,timeout=60,creationflags=0x08000000)
        cls.fake=C.CDLL(str(output))
        cls.fake.simulate.argtypes=[C.c_int,C.POINTER(native.Config),C.POINTER(native.Row),
                                   C.c_uint64,C.POINTER(native.Stats),C.POINTER(native.Control)]
        cls.fake.simulate.restype=C.c_int

    def config(self):
        args=app.parse_args([])
        config=native.config_from_args(args,.2)
        config.home_duration=.002;config.settle=.003;config.duration=.05;config.ramp=.002
        config.home_timeout=.05;config.max_travel=.5;config.max_velocity=5
        return config

    def simulate(self,scenario=0,capacity=2000,stop=False,expired=False,max_travel=0):
        config=self.config();config.max_travel=max_travel
        if scenario==1:
            config.duration=.05;config.feedback_timeout=.006
        if scenario in (20,21):
            config.duration=1.3;config.lease=3
        rows=(native.Row*capacity)();stats=native.Stats();control=native.Control()
        if not expired:self.dll.sweep_heartbeat(C.byref(control))
        if stop:self.dll.sweep_stop(C.byref(control))
        result=self.fake.simulate(scenario,C.byref(config),rows,capacity,C.byref(stats),C.byref(control))
        self.assertEqual(stats.disable_ack,0 if scenario==12 else 1)
        return result,rows,stats

    def test_native_chirp_matches_reference_including_both_ramps(self):
        config=self.config()
        plan=Chirp(config.f0,config.f1,config.duration,config.amplitude,config.ramp,0,0)
        for t in (0,.001,.002,.003,.009,.012,.013,.014,.02):
            target,velocity=C.c_double(),C.c_double()
            self.dll.sweep_sample(C.byref(config),t,C.byref(target),C.byref(velocity))
            self.assertAlmostEqual(target.value,plan.sample(t).command.position,places=10)
            self.assertEqual(velocity.value,0)
            self.assertAlmostEqual(self.dll.sweep_frequency(C.byref(config),t),
                                   plan.sample(t).frequency_hz,places=10)

    def test_native_command_bytes_match_python_j60_codec(self):
        for command in (MotionCommand(),MotionCommand(3.14,0,0,50,5),
                        MotionCommand(-40,-40,-40,0,0),MotionCommand(40,40,40,1023,51),
                        MotionCommand(torque=.1)):
            packed=(C.c_ubyte*8)()
            self.dll.j60_pack(command.position,command.velocity,command.torque,command.kp,command.kd,packed)
            self.assertEqual(bytes(packed),command.pack())

    def test_position_wire_and_return_zero_settling(self):
        code,rows,stats=self.simulate(23)
        self.assertEqual(code,0)
        self.assertGreater(stats.rows,3)
        self.assertLess(stats.rows,stats.rx)
        for i in range(stats.rows):
            self.assertEqual((rows[i].kp,rows[i].kd),(100,2))
            self.assertLess(abs(rows[i].torque),.001)
            self.assertLess(abs(rows[i].position-rows[i].target),.0001)
        end=stats.rows-1
        self.assertAlmostEqual(rows[end].target,app.encoded_targets(0,100,2)[0])
        self.assertGreaterEqual(rows[end].at-stats.first_rx,.05+.004+.003-.002)

    def test_failure_to_settle_after_sweep_is_not_reported_as_success(self):
        code,_,stats=self.simulate(24)
        self.assertEqual(code,8)
        self.assertGreater(stats.rows,0)

    def test_fault_timeout_cancel_and_usb_cleanup(self):
        for scenario,expected in ((1,6),(3,5),(4,8),(7,2),(12,10),(20,4),(21,12),(22,3)):
            with self.subTest(scenario=scenario):self.assertEqual(self.simulate(scenario)[0],expected)
        self.assertEqual(self.simulate(stop=True)[0],1)
        self.assertEqual(self.simulate(expired=True)[0],7)
        self.assertEqual(self.simulate(capacity=2)[0],9)
        self.assertEqual(self.simulate(6)[0],0)

    def test_derivative_native_trajectory_matches_reference(self):
        c=self.config();c.derivative=1
        plan=Chirp(c.f0,c.f1,c.duration,c.amplitude,c.ramp,100,2,'derivative')
        for t in (.001,.002,.01,.052,.053,.054):
            p,v=C.c_double(),C.c_double()
            self.dll.sweep_sample(C.byref(c),t,C.byref(p),C.byref(v))
            self.assertAlmostEqual(v.value,plan.sample(t).command.velocity,places=8)

if __name__=='__main__':unittest.main()
