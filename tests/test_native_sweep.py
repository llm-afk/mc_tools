import ctypes as C
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from scripts.position_sweep import native, app
from scripts.position_sweep.trajectory import Chirp


@unittest.skipUnless(sys.platform == 'win32' and shutil.which('gcc'), 'Windows MinGW required')
class NativeSweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dll = native.load_engine()
        root = Path(__file__).resolve().parents[1]
        output = root/'build'/'position_sweep'/'fake-tests.dll'
        subprocess.run(['gcc','-shared','-O2','-Wall','-Wextra','-Werror',
                        '-o',str(output),str(root/'tests'/'native_sweep_fake.c'),
                        '-static-libgcc'],check=True,capture_output=True)
        cls.fake = C.CDLL(str(output))
        cls.fake.simulate.argtypes = [C.c_int,C.POINTER(native.Config),C.POINTER(native.Row),
                                      C.c_uint64,C.POINTER(native.Stats),C.POINTER(native.Control)]
        cls.fake.simulate.restype = C.c_int

    def config(self):
        args = app.parse_args(['--kp','20','--kd','1'])
        return native.config_from_args(args,0)

    def test_native_trajectory_matches_python_including_ramps(self):
        c=self.config()
        for derivative in (0,1):
            c.derivative=derivative
            plan=Chirp(c.f0,c.f1,c.duration,c.amplitude,c.ramp,20,1,
                       'derivative' if derivative else 'zero')
            for t in [0,.1,1.99,2,2.01,15,31.99,32,33.9,34,35]:
                p,v=C.c_double(),C.c_double()
                self.dll.sweep_sample(C.byref(c),t,C.byref(p),C.byref(v))
                target=plan.sample(t).command
                self.assertAlmostEqual(p.value,target.position,places=10)
                self.assertAlmostEqual(v.value,target.velocity,places=9)
                self.assertAlmostEqual(self.dll.sweep_frequency(C.byref(c),t),
                                       plan.sample(t).frequency_hz,places=10)

    def simulate(self,scenario=0,capacity=1000,stop=False,expired=False,feedforward=False,max_travel=.5):
        c=self.config()
        c.home_duration=.002;c.settle=.002;c.duration=.01;c.ramp=.002;c.home_timeout=.03
        if feedforward:
            c.feedforward=1;c.home_position=.2;c.max_travel=max_travel;c.max_velocity=5
        rows=(native.Row*capacity)();s=native.Stats();ctl=native.Control()
        if not expired:self.dll.sweep_heartbeat(C.byref(ctl))
        if stop:self.dll.sweep_stop(C.byref(ctl))
        result=self.fake.simulate(scenario,C.byref(c),rows,capacity,C.byref(s),C.byref(ctl))
        self.assertEqual(s.disable_ack,1)
        return result,rows,s

    def test_complete_capture_only_after_home_and_keep_each_packet(self):
        code,rows,s=self.simulate()
        self.assertEqual(code,0)
        self.assertGreater(s.rows,10)
        self.assertLess(s.rows,s.rx)
        self.assertEqual(rows[s.rows-1].target,0)
        self.assertEqual(rows[s.rows-1].frequency,0)
        self.assertTrue(any(rows[i].frequency>0 for i in range(s.rows)))
        for i in range(s.rows):
            self.assertEqual(rows[i].position,rows[i].target)

    def test_fault_temperature_timeout_overflow_and_cancellation_disable(self):
        for scenario,expected in ((1,8),(2,4),(3,5),(4,8)):
            with self.subTest(scenario=scenario):
                code,_,_=self.simulate(scenario)
                # Silence may reach the zero deadline before feedback timeout.
                self.assertIn(code,(6,8) if scenario==1 else (expected,))
        self.assertEqual(self.simulate(capacity=2)[0],9)
        self.assertEqual(self.simulate(stop=True)[0],1)
        self.assertEqual(self.simulate(expired=True)[0],7)

    def test_pending_write_does_not_prevent_receive_and_short_write_stops(self):
        code,_,stats=self.simulate(6)
        self.assertEqual(code,0)
        self.assertGreater(stats.rx,10)
        self.assertEqual(self.simulate(7)[0],2)

    def test_feedforward_uses_only_torque_field_and_checks_motion_limits(self):
        code,rows,stats=self.simulate(8,feedforward=True)
        self.assertEqual(code,0)
        self.assertGreater(stats.rows,2)
        self.assertLess(stats.rows,stats.rx)  # PD homing feedback is excluded.
        self.assertAlmostEqual(stats.motion_origin,0)
        self.assertTrue(any(rows[i].target!=0 for i in range(stats.rows)))
        for i in range(stats.rows):
            self.assertEqual((rows[i].kp,rows[i].kd,rows[i].position),(0,0,0))
            self.assertEqual(rows[i].torque,rows[i].target)
        for scenario in (10,11):
            self.assertEqual(self.simulate(scenario,feedforward=True)[0],11)
        self.assertEqual(self.simulate(10,feedforward=True,max_travel=0)[0],0)
        self.assertEqual(self.simulate(11,feedforward=True,max_travel=0)[0],11)
        code,_,stats=self.simulate(4,feedforward=True)
        self.assertEqual(code,8)
        self.assertEqual(stats.rows,0)  # Failed homing must not enter the sweep.
