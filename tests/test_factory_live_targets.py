import time
import unittest
from unittest.mock import Mock

from motor_platform import MitCommand
from scripts.factory_test.manual_control import ControlPage,ManualSession
from scripts.factory_test.station_config import StationConfig


def wait_for(predicate):
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        if predicate(): return
        time.sleep(.005)
    raise AssertionError('Live target update did not complete')


class LiveTargetTests(unittest.TestCase):
    def test_presets_only_drive_after_enable_and_survive_disable(self):
        samples=[]
        session=ManualSession(StationConfig(simulation=True),1,
            lambda kind,value:samples.append(value) if kind=='control_sample' else None)
        session.set_target(MitCommand(torque=2,position=.3))
        session.start()
        try:
            wait_for(lambda:bool(samples))
            self.assertFalse(session.enabled)
            self.assertEqual(samples[-1]['target']['torque'],2)
            self.assertEqual(samples[-1]['sent_target']['torque'],0)
            session.command('enable')
            wait_for(lambda:any(s['enabled'] and s['sent_target']['torque']==2 for s in samples))
            session.set_target(MitCommand(torque=-1,position=.4))
            wait_for(lambda:samples[-1]['sent_target']['torque']==-1)
            session.command('disable')
            wait_for(lambda:not samples[-1]['enabled'])
            self.assertEqual(session.target,MitCommand())
            self.assertEqual(session.pending_target.torque,-1)
            self.assertEqual(samples[-1]['sent_target']['torque'],0)
            session.command('enable')
            wait_for(lambda:samples[-1]['enabled'] and samples[-1]['sent_target']['torque']==-1)
        finally:
            session.disconnect();session.thread.join(2)
        self.assertFalse(session.active)
        self.assertIsNone(session.failure)

    def test_fast_target_changes_use_a_latest_value_mailbox_not_the_command_queue(self):
        session=ManualSession(StationConfig(),1,lambda *_:None)
        for value in range(1000):
            session.command('target',MitCommand(position=value*.1))
        self.assertEqual(session.pending_target.position,99.9)
        self.assertTrue(session.commands.empty())
        self.assertFalse(session.enabled)
        self.assertEqual(session.target,MitCommand())
        with self.assertRaises(ValueError):session.set_target(MitCommand(kp=-1))
        self.assertEqual(session.pending_target.position,99.9)

    def make_page(self):
        page=ControlPage.__new__(ControlPage)
        page.frame=Mock();page.frame.after.return_value='target-timer'
        page.send_after=None;page._syncing_targets=False
        values=dict(position='1.2',velocity='0',torque='-2',kp='0',kd='0')
        page.targets={name:Mock(get=Mock(return_value=value)) for name,value in values.items()}
        page.target_sliders={name:Mock() for name in values}
        page.session=ManualSession(StationConfig(),1,lambda *_:None)
        page.host=Mock();page.message=Mock()
        return page

    def test_continuous_editing_does_not_postpone_the_existing_update_timer(self):
        page=self.make_page()
        for _ in range(100):page.target_changed('position')
        page.frame.after.assert_called_once_with(20,page.apply)
        page.frame.after_cancel.assert_not_called()
        self.assertTrue(page.apply())
        self.assertIsNone(page.send_after)
        self.assertEqual(page.session.pending_target,MitCommand(position=1.2,torque=-2))
        page.target_changed('torque')
        self.assertEqual(page.frame.after.call_count,2)

    def test_incomplete_text_keeps_the_last_valid_pending_value(self):
        page=self.make_page();self.assertTrue(page.apply())
        old=page.session.pending_target
        page.targets['position'].get.return_value='-'
        self.assertFalse(page.apply())
        self.assertEqual(page.session.pending_target,old)
        page.message.set.assert_not_called()
        self.assertFalse(page.apply(report_errors=True))
        page.message.set.assert_called_once()

    def test_enable_captures_current_fields_before_queueing_the_enable(self):
        page=self.make_page();page.session.connected=True
        page.target_changed('position')
        page.send('enable')
        page.frame.after_cancel.assert_called_once_with('target-timer')
        self.assertEqual(page.session.pending_target,MitCommand(position=1.2,torque=-2))
        self.assertEqual(page.session.commands.get_nowait(),('enable',None))
        self.assertFalse(page.session.enabled)

    def test_position_drag_uses_tenth_radian_steps(self):
        page=self.make_page()
        page.slider('position',2.04)
        page.targets['position'].set.assert_called_with('2.000')
        page.slider('position',2.06)
        page.targets['position'].set.assert_called_with('2.100')

    def test_disconnect_cancels_delayed_ui_updates_before_releasing_session(self):
        page=self.make_page();page.target_changed('position')
        page.disconnect()
        page.frame.after_cancel.assert_called_once_with('target-timer')
        self.assertIsNone(page.send_after)
        self.assertTrue(page.session.stop.is_set())


if __name__=='__main__':unittest.main()
