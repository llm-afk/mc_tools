import queue
import struct
import threading
import time
import unittest
import ctypes as C
from types import SimpleNamespace
from unittest.mock import patch

from motor_platform import MotorBus, MitCommand, CanFrame
from motor_platform.runtime import ControlRuntime
from motor_platform.transports.canable import _Usb, Overlapped


class Transport:
    def __init__(self):
        self.sent = []
        self.rx = queue.Queue()
        self.owners = set()
        self.lock = threading.Lock()
        self.enabled = {}

    def send(self, frame):
        with self.lock:
            self.sent.append(frame)
            self.owners.add(threading.get_ident())
        if frame.can_id >= 0x600:
            node = frame.can_id - 0x600
            if frame.data[0] == 0x40:
                word = 1 if self.enabled.get(node, False) else 0
                self.rx.put(CanFrame(frame.can_id-0x80, b"\x4b"+frame.data[1:4]
                                     + word.to_bytes(2, "little")+b"\0\0"))
            else:
                if frame.data[1:3] == b"\x02\x20":
                    self.enabled[node] = frame.data[4] == 1
                self.rx.put(CanFrame(frame.can_id-0x80, b"\x60"+frame.data[1:4]+b"\0"*4))
        else:
            node = frame.can_id-0x400
            self.enabled[node] = True
            self.rx.put(CanFrame(0x190+node, struct.pack("<fffhh",node,0,0,250,300)))

    def recv(self, timeout):
        self.owners.add(threading.get_ident())
        try: return self.rx.get(timeout=timeout)
        except queue.Empty: return None

    def close(self): pass


class RuntimeTests(unittest.TestCase):
    def test_usb_receive_does_not_sleep_near_send_deadline(self):
        usb = _Usb.__new__(_Usb)
        usb._event = 1
        usb._issue_read = lambda: None
        usb.k = SimpleNamespace(WaitForSingleObject=lambda *args: 258)
        for timeout, clock, expected_sleeps in (
            (.001, [0, .0007, .0011], []),
            (.01, [0, .001, .011], [.001]),
        ):
            with self.subTest(timeout=timeout), \
                 patch('motor_platform.transports.canable.time.perf_counter', side_effect=clock), \
                 patch('motor_platform.transports.canable.time.sleep') as sleep:
                self.assertIsNone(usb._read_async(timeout))
                self.assertEqual([call.args[0] for call in sleep.call_args_list], expected_sleeps)

    def test_usb_write_timeout_cached_and_short_write_still_rejected(self):
        usb = _Usb.__new__(_Usb)
        usb.usb = 2
        usb._write_timeouts = {}
        policies = []
        def policy(h, pipe, kind, size, value):
            policies.append((pipe, value._obj.value))
            return True
        def write(h, pipe, buf, size, n, overlapped):
            n._obj.value = size
            return True
        usb.w = SimpleNamespace(WinUsb_SetPipePolicy=policy, WinUsb_WritePipe=write)
        for _ in range(10):
            self.assertEqual(usb.transfer(2, b'abc'), b'abc')
        usb.transfer(2, b'abc', timeout=.2)
        self.assertEqual(policies, [(2, 100), (2, 200)])
        usb.w.WinUsb_WritePipe = lambda *args: True
        with self.assertRaisesRegex(OSError, 'short USB write'):
            usb.transfer(2, b'abc', timeout=.2)

    def test_usb_poll_timeout_preserves_pending_read(self):
        usb=_Usb.__new__(_Usb)
        calls=[]
        usb._event=1;usb.usb=2;usb.input=0x81;usb.read_size=128
        usb._pending=False;usb._read_buffer=C.create_string_buffer(b'abc',128)
        def read(*args):calls.append('read');return True
        def result(h,overlapped,n,wait):n._obj.value=3;return True
        usb.k=SimpleNamespace(ResetEvent=lambda h:True,WaitForSingleObject=lambda *a:258)
        usb.w=SimpleNamespace(WinUsb_ReadPipe=read,WinUsb_GetOverlappedResult=result)
        with patch('motor_platform.transports.canable.C.get_last_error',return_value=997), patch('motor_platform.transports.canable.time.perf_counter',side_effect=[0,1]):
            self.assertIsNone(usb._read_async(0.001))
        self.assertTrue(usb._pending)
        usb.k.WaitForSingleObject=lambda *a:0
        self.assertEqual(usb._read_async(0.001),b'abc')
        self.assertTrue(usb._pending)  # Next read was immediately rearmed.
        self.assertEqual(calls,['read','read'])

    def test_usb_close_waits_for_cancel_before_free(self):
        usb=_Usb.__new__(_Usb)
        usb._pending=True;usb._event=1;usb.handle=3;usb.usb=2;usb._overlapped=Overlapped()
        calls=[]
        usb.k=SimpleNamespace(CancelIoEx=lambda *a:calls.append('cancel'),CloseHandle=lambda h:calls.append(f'close{h}'))
        usb.w=SimpleNamespace(WinUsb_GetOverlappedResult=lambda *a:calls.append('complete'),WinUsb_Free=lambda *a:calls.append('free'))
        usb.close()
        self.assertEqual(calls,['cancel','complete','close1','free','close3'])

    def make(self, lease=1):
        tr = Transport()
        bus = MotorBus(tr)
        runtime = ControlRuntime(bus, {"J1":bus.motor(1), "J2":bus.motor(2)}, 200, lease)
        return tr, bus, runtime

    def test_multiple_motors_one_io_owner(self):
        tr, bus, rt = self.make()
        rt.start()
        try:
            self.assertIs(rt.motor("J1"), rt.motor("J1"))
            one,two=rt.motor("J1"),rt.motor("J2")
            one.enable();two.enable()
            one.set_mit(MitCommand(position=1))
            two.set_mit(MitCommand(position=2))
            two.set_rate(100)
            time.sleep(0.12)
            self.assertEqual(one.state().position,1)
            self.assertEqual(two.state().position,2)
            # OS scheduling is not the unit-test oracle for requested rate.
            self.assertGreater(one.stats()["tx_count"],0)
            self.assertGreater(two.stats()["tx_count"],0)
            self.assertEqual(two.stats()["target_hz"],100)
            self.assertTrue(one.stats()["enabled"])
            with self.assertRaises(RuntimeError): bus.poll()
            with self.assertRaises(RuntimeError): bus.motor(1).send_mit(MitCommand())
            self.assertEqual(len(tr.owners),1)
            self.assertNotIn(threading.get_ident(),tr.owners)
            one.disable()
            n=len([f for f in tr.sent if f.can_id==0x401])
            time.sleep(0.02)
            self.assertEqual(n,len([f for f in tr.sent if f.can_id==0x401]))
        finally: rt.close()
        self.assertTrue(any(f.can_id==0x602 and f.data[1:3]==b"\x02\x20" and f.data[4]==2 for f in tr.sent))

    def test_no_traffic_before_explicit_enable(self):
        tr, bus, rt=self.make()
        rt.start()
        try:
            rt.motor("J1").set_mit(MitCommand(position=2))
            time.sleep(0.02)
            self.assertFalse(tr.sent)
        finally:rt.close()

    def test_target_lease_expires(self):
        tr,bus,rt=self.make(lease=0.025)
        rt.start()
        try:
            one=rt.motor("J1")
            one.enable()
            time.sleep(0.08)
            self.assertFalse(one.stats()["periodic_active"])
            self.assertIn("lease expired",one.stats()["fault"])
            self.assertTrue(any(f.can_id==0x601 and f.data[4]==2 for f in tr.sent))
        finally:rt.close()

    def test_atomic_validation_and_rate(self):
        tr,bus,rt=self.make()
        rt.start()
        try:
            one=rt.motor("J1")
            good=MitCommand(position=1,kp=2)
            one.set_mit(good)
            with self.assertRaises(ValueError):one.set_mit(MitCommand(position=2,kp=-1))
            self.assertEqual(one._target,good)
            for bad in (0,-1,float('nan'),True):
                with self.assertRaises(ValueError):one.set_rate(bad)
            one.set_rate(1000)
            self.assertEqual(one.stats()['target_hz'],1000)
            with self.assertRaises(TimeoutError):one.state()
        finally:rt.close()

    def test_no_catchup_burst(self):
        tr,bus,rt=self.make()
        one=rt.motor('J1')
        one._active=True;one._expires=10;one._next=1;one._rate_hz=1000
        with patch('motor_platform.runtime.time.perf_counter',return_value=1.0105):rt._tick(1.0105)
        self.assertEqual(len(tr.sent),1)
        self.assertEqual(one.stats()['skipped_periods'],10)
        self.assertGreater(one._next,1.0105)

    def test_feedback_limits_outstanding_requests(self):
        tr,bus,rt=self.make()
        one=rt.motor('J1')
        one._active=True;one._expires=10;one._next=1;one._last_rx=1;one._inflight=4
        rt._tick(1.01)
        self.assertEqual(tr.sent,[])
        self.assertEqual(one.stats()['feedback_limited_periods'],1)
        with self.assertRaises(TimeoutError):rt._tick(1.2)

    def test_worker_failure_is_reported_and_closed(self):
        tr,bus,rt=self.make()
        rt.start()
        one=rt.motor('J1')
        one.enable()
        original=tr.send
        def broken(frame):
            if frame.can_id<0x600:raise OSError('USB unplugged')
            return original(frame)
        tr.send=broken
        rt._thread.join(timeout=1)
        self.assertFalse(rt._thread.is_alive())
        with self.assertRaises(RuntimeError):one.set_mit(MitCommand())
        with self.assertRaises(RuntimeError):rt.close()
        self.assertIsNone(bus._io_owner)


if __name__=='__main__':unittest.main()
