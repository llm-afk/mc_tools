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

from j60.feedforward_sweep import app, native
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
    def test_preview_matches_reference_interaction_and_uses_own_data(self):
        with temporary_folder() as folder:
            path=folder/'preview.csv'
            with patch.object(app,'select_canable',side_effect=AssertionError('USB discovery')), \
                 patch.object(app,'run_native',side_effect=AssertionError('hardware')), \
                 redirect_stdout(io.StringIO()):
                result=app.main(['--preview','--amplitude','.1','--duration','.1',
                                 '--rate','100','--output',str(path)])
            self.assertEqual(result,0)
            with path.open(encoding='utf-8-sig',newline='') as source:
                rows=list(csv.DictReader(source))
            self.assertEqual(tuple(rows[0]),app.COLUMNS)
            self.assertTrue(all(row['kp']=='0' and row['kd']=='0' for row in rows))
            self.assertTrue(all(row['position_rad']=='' for row in rows))
            values=[float(row['target_feedforward_torque']) for row in rows]
            self.assertEqual((values[0],values[-1]),(0,0))
            self.assertGreater(max(values),0)
            self.assertLess(min(values),0)
            self.assertLessEqual(max(map(abs,values)),.100001)

    def test_j60_config_validation_and_reference_defaults(self):
        args=app.parse_args([])
        self.assertEqual((args.id,args.rate,args.start_hz,args.stop_hz,args.duration,args.amplitude),
                         (1,1000,.1,20,30,.1))
        self.assertEqual((args.kp,args.kd,args.home_kp,args.home_kd),(0,0,100,2))
        self.assertIsNone(args.serial)
        for argv in (['--id','16'],['--amplitude','41'],['--amplitude','nan'],
                     ['--rate','1001'],['--max-travel','-1'],['--amplitude','0']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                app.parse_args(argv)

    def test_csv_preserves_every_packet_and_missing_temperature_is_blank(self):
        with temporary_folder() as folder:
            path=folder/'sample.csv'
            rows=(native.Row*2)()
            for row in rows:
                row.target=.1;row.position=1;row.at=123;row.mt=float('nan');row.bt=26
            with app.Recorder(path) as recorder:
                recorder.write_native_rows(rows,2)
            with path.open(encoding='utf-8-sig',newline='') as stream:
                saved=list(csv.DictReader(stream))
            self.assertEqual(len(saved),2)
            self.assertTrue(all(row['motor_temperature_c']=='' for row in saved))

    def test_live_entry_uses_auto_selected_adapter_and_native_engine(self):
        with temporary_folder() as folder, patch.object(app,'select_canable',return_value='one') as detect, \
             patch.object(app,'run_native') as run, redirect_stdout(io.StringIO()):
            result=app.main(['--output',str(folder/'result.csv')])
            self.assertEqual(result,0)
            detect.assert_called_once_with(None)
            self.assertEqual(run.call_args.args[0],'one')
            self.assertEqual(run.call_args.args[2].amplitude,.1)


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
        folder=Path(native.__file__).resolve().parents[2]/'build'/'j60_feedforward_sweep'
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
        config.home_duration=.002;config.settle=.002;config.duration=.01;config.ramp=.002
        config.home_timeout=.03;config.max_travel=.5;config.max_velocity=5
        return config

    def simulate(self,scenario=0,capacity=2000,stop=False,expired=False,max_travel=.5):
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

    def test_homing_then_only_zero_gain_feedforward_is_captured(self):
        code,rows,stats=self.simulate(8)
        self.assertEqual(code,0)
        self.assertGreater(stats.rows,2)
        self.assertLess(stats.rows,stats.rx)
        self.assertTrue(any(rows[i].target!=0 for i in range(stats.rows)))
        for i in range(stats.rows):
            self.assertEqual((rows[i].kp,rows[i].kd),(0,0))
            self.assertLess(abs(rows[i].position),.001)
            self.assertLess(abs(rows[i].torque-rows[i].target),.0013)

    def test_feedback_timeout_temperature_homing_buffer_and_cancellation_stop(self):
        self.assertIn(self.simulate(1)[0],(6,8))
        self.assertEqual(self.simulate(3)[0],5)
        code,_,stats=self.simulate(4)
        self.assertEqual((code,stats.rows),(8,0))
        self.assertEqual(self.simulate(capacity=2)[0],9)
        self.assertEqual(self.simulate(stop=True)[0],1)
        self.assertEqual(self.simulate(expired=True)[0],7)

    def test_async_write_short_write_and_each_packet_in_batched_read(self):
        self.assertEqual(self.simulate(6)[0],0)
        self.assertEqual(self.simulate(7)[0],2)
        code,_,stats=self.simulate(13)
        self.assertEqual(code,0)
        self.assertEqual(stats.rows%2,0)
        self.assertGreater(stats.rx,stats.tx)

    def test_travel_and_velocity_limits_and_explicit_disable_ack_failure(self):
        self.assertEqual(self.simulate(10)[0],11)
        self.assertEqual(self.simulate(11)[0],11)
        self.assertEqual(self.simulate(10,max_travel=0)[0],0)
        self.assertEqual(self.simulate(11,max_travel=0)[0],11)
        self.assertEqual(self.simulate(12)[0],10)

    def test_separate_status_queries_detect_fault_and_do_not_pause_control(self):
        code,_,stats=self.simulate(20)
        self.assertEqual((code,stats.status_word),(4,1))
        self.assertGreaterEqual(self.fake.queries_count(),1)
        code,_,_=self.simulate(21)
        self.assertEqual(code,12)
        self.assertGreater(self.fake.controls_after_query(),10)

    def test_can_error_is_preserved_and_not_interpreted_as_j60_fault(self):
        code,_,stats=self.simulate(22)
        self.assertEqual((code,stats.can_error_id),(3,0x24))
        self.assertEqual(bytes(stats.can_error_data)[6],128)


if __name__=='__main__':
    unittest.main()
