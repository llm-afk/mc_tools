import threading
import time
import unittest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from scripts.mit_control.console import ConsoleDashboard
from motor_platform import MitCommand, MotorState


def wait_for(predicate, timeout=2):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError('terminal event not received')


def refresh(ui, count):
    ui.render(node_id=1, enabled=True, desired=MitCommand(),
        state=MotorState(1, count, 0, 0, 25, 30, 0, time.perf_counter()),
        stats=dict(enabled=True, periodic_active=True, tx_count=count, rx_count=count,
                   actual_tx_hz=999, target_hz=1000, skipped_periods=0,
                   max_lateness_s=0, fault=None), notice='OK', started=time.perf_counter())


class ConsoleTests(unittest.TestCase):
    def test_live_refresh_preserves_editor_and_history(self):
        with create_pipe_input() as keys:
            with ConsoleDashboard(input=keys, output=DummyOutput()) as ui:
                keys.send_text('status\r')
                wait_for(lambda: not ui.commands.empty())
                self.assertEqual(ui.poll(), 'status')
                keys.send_text('rate 500\r')
                wait_for(lambda: not ui.commands.empty())
                self.assertEqual(ui.poll(), 'rate 500')
                keys.send_text('draft')
                wait_for(lambda: ui.editor.text == 'draft')
                keys.send_text('\x1b[A')
                wait_for(lambda: ui.editor.text == 'rate 500')
                keys.send_text('\x1b[A')
                wait_for(lambda: ui.editor.text == 'status')
                keys.send_text('\x1b[B\x1b[B')
                wait_for(lambda: ui.editor.text == 'draft')
                keys.send_text('\x1b[D\x1b[D')
                wait_for(lambda: ui.editor.buffer.cursor_position == 3)
                for i in range(30):
                    refresh(ui, i)
                    self.assertIsNone(ui.poll())
                    time.sleep(0.005)
                self.assertIn('TX/RX     29/29', ui._panel)
                self.assertEqual(ui.editor.text, 'draft')
                self.assertEqual(ui.editor.buffer.cursor_position, 3)
                keys.send_text('X\r')
                wait_for(lambda: not ui.commands.empty())
                self.assertEqual(ui.poll(), 'draXft')
                keys.send_text('\x03')
                wait_for(ui.quit_requested.is_set)
                self.assertEqual(ui.poll(), 'quit')
            self.assertFalse(ui.thread.is_alive())

    def test_slow_terminal_output_does_not_block_control(self):
        class SlowOutput(DummyOutput):
            def __init__(self):
                self.block = threading.Event()
                self.entered = threading.Event()
                self.release = threading.Event()
            def flush(self):
                if self.block.is_set():
                    self.entered.set()
                    self.release.wait(3)
        out = SlowOutput()
        with create_pipe_input() as keys:
            with ConsoleDashboard(input=keys, output=out) as ui:
                try:
                    out.block.set()
                    refresh(ui, 1)
                    self.assertTrue(out.entered.wait(1))
                    started = time.perf_counter()
                    count = 0
                    while time.perf_counter() - started < 0.35:
                        refresh(ui, count)
                        self.assertIsNone(ui.poll())
                        count += 1
                        time.sleep(0.005)
                    self.assertGreater(count, 20)
                finally:
                    out.release.set()

    def test_submissions_keep_order(self):
        with create_pipe_input() as keys:
            with ConsoleDashboard(input=keys, output=DummyOutput()) as ui:
                keys.send_text('status\rdisable\r')
                wait_for(lambda: ui.commands.qsize() == 2)
                self.assertEqual(ui.poll(), 'status')
                self.assertEqual(ui.poll(), 'disable')
