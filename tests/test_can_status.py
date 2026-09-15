import unittest
from motor_platform.core.can import CanFrame
from motor_platform.transports.canable import is_counter_report, Canable


class CounterReportTests(unittest.TestCase):
    def test_transient_budget_and_real_faults(self):
        can = Canable('test')
        frame = CanFrame(8, bytes.fromhex('0000040000000001'), is_fd=False, is_error=True)
        self.assertTrue(can._record_transient(frame, 0))
        self.assertTrue(can._record_transient(frame, .2))
        self.assertFalse(can._record_transient(frame, .9))
        self.assertTrue(can._record_transient(frame, 2))
        for cid, data in [(0x48, '000008000000f800'), (0x204, '003000000000867f'),
                          (8, '0000040000000009'), (8, '0000040000010001')]:
            self.assertFalse(can._record_transient(CanFrame(cid, bytes.fromhex(data),
                             is_fd=False, is_error=True), 3))
    def test_only_counter_changes_are_nonfatal(self):
        def report(can_id, data):
            return CanFrame(can_id, bytes.fromhex(data), is_fd=False, brs=False, is_error=True)
        self.assertTrue(is_counter_report(report(0, '0000000000000001')))
        for can_id, data in [(0xc, '0020080000008800'), (0x48, '000008000000f800'),
                             (0, '0000000000010001'), (0, '0000000000000060'),
                             (0, '000000')]:
            self.assertFalse(is_counter_report(report(can_id, data)))
        self.assertFalse(is_counter_report(CanFrame(0, bytes(8), is_fd=False)))
