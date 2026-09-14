"""Terminal dashboard and editor on a separate event loop; no CAN access."""
import queue
import threading
import time


class ConsoleDashboard:
    def __init__(self, *, input=None, output=None):
        # Lazy import: --no-console never needs the UI dependency.
        from prompt_toolkit.application import Application
        from prompt_toolkit.cursor_shapes import CursorShape
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import TextArea

        self.commands = queue.Queue(maxsize=32)
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.quit_requested = threading.Event()
        self.error = None
        self.dirty = False
        self._panel = '正在连接电机…'
        self._notice = '输入命令后按 Enter 下发'
        self._history = []
        self._history_index = 0
        self._drafts = {}
        self.editor = TextArea(
            multiline=False, prompt='mit> ',
            accept_handler=self._submit, wrap_lines=False, height=1,
        )
        keys = KeyBindings()

        @keys.add('up')
        def previous(event):
            self._move_history(-1)

        @keys.add('down')
        def following(event):
            self._move_history(1)

        @keys.add('c-c')
        def quit_now(event):
            self.quit_requested.set()
            self._notice = '正在失能并退出…'

        @keys.add('escape')
        def clear(event):
            event.current_buffer.reset()
            self._history_index = len(self._history)
            self._drafts.clear()

        content = HSplit([
            Window(FormattedTextControl('MIT 电机控制与反馈'), height=1, style='class:title'),
            Window(FormattedTextControl(lambda: self._panel), wrap_lines=True),
            Window(height=1, char='─'),
            Window(FormattedTextControl('set p v tau kp kd | stop | rate hz | enable | disable | status | quit'), height=1),
            Window(FormattedTextControl('↑↓ 历史  ←→ 编辑  Home/End 首尾  Esc 清空  Ctrl+C 失能退出'), height=1),
            Window(FormattedTextControl(lambda: self._notice), height=2, wrap_lines=True),
            self.editor,
        ])
        self.app = Application(
            layout=Layout(content, focused_element=self.editor),
            key_bindings=keys, full_screen=True, mouse_support=False,
            style=Style.from_dict({'title': 'bold ansicyan'}),
            cursor=CursorShape.BLINKING_BEAM,
            min_redraw_interval=0.016, input=input, output=output,
        )

    def _move_history(self, direction):
        from prompt_toolkit.document import Document
        if not self._history:
            return
        # Keep history local and synchronous. Toolkit history is loaded after
        # a redraw, which can lag behind a rapid Up immediately after Enter.
        self._drafts[self._history_index] = self.editor.text
        self._history_index = max(0, min(len(self._history), self._history_index + direction))
        text = self._drafts.get(self._history_index)
        if text is None:
            text = self._history[self._history_index] if self._history_index < len(self._history) else ''
        self.editor.buffer.document = Document(text, len(text))

    def _submit(self, buffer):
        line = buffer.text.strip()
        if not line:
            return False
        if line.lower() in ('quit', 'q', 'exit'):
            self.quit_requested.set()
            return False
        try:
            self.commands.put_nowait(line)
        except queue.Full:
            self._notice = '命令队列已满，请稍后按回车重试'
            return True  # Preserve the unsent command for editing/retry.
        if not self._history or self._history[-1] != line:
            self._history.append(line)
            del self._history[:-100]
        self._history_index = len(self._history)
        self._drafts.clear()
        self._notice = f'已提交：{line}'
        return False

    def __enter__(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name='mit-terminal-ui')
        self.thread.start()
        if not self.ready.wait(5):
            self.__exit__()
            raise RuntimeError('terminal UI startup timed out')
        if self.error:
            raise RuntimeError(f'terminal UI failed: {self.error}') from self.error
        return self

    def _run(self):
        try:
            self.app.run(pre_run=self.ready.set, set_exception_handler=False)
        except BaseException as exc:
            self.error = exc
        finally:
            self.ready.set()
            self.stopped.set()

    def __exit__(self, *_):
        def finish():
            if self.app.is_running:
                self.app.exit()
        loop = self.app.loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(finish)
        if hasattr(self, 'thread'):
            self.thread.join(timeout=2)

    def poll(self):
        if self.error is not None:
            raise RuntimeError(f'terminal UI failed: {self.error}') from self.error
        if self.quit_requested.is_set() or self.stopped.is_set():
            return 'quit'
        try:
            line = self.commands.get_nowait()
        except queue.Empty:
            return None
        self.dirty = True
        return line

    def render(self, *, node_id, enabled, desired, state, stats, notice, started):
        # Only immutable strings are shared. Never touch the editor buffer or
        # wait for rendering from the control/lease-refresh thread.
        mode = {True: '已使能', False: '已失能', None: '未知'}.get(stats.get('enabled'), '未知')
        self._panel = (
            f"ID {node_id}   {mode}   周期发送：{'运行' if stats.get('periodic_active') else '停止'}   "
            f"运行 {time.perf_counter()-started:.1f} s\n\n"
            f"下发目标  p={desired.position:g} rad  v={desired.velocity:g} rad/s\n"
            f"          tau={desired.torque:g}  kp={desired.kp:g}  kd={desired.kd:g}\n\n"
            f"反馈位置  {state.position:+.5f} rad\n"
            f"反馈速度  {state.velocity:+.5f} rad/s    力矩 {state.torque:+.5f}\n"
            f"温度      电机 {state.motor_temperature:.1f} / 驱动板 {state.board_temperature:.1f} C\n"
            f"错误码    0x{state.error_code:04X}    回传龄期 {(time.perf_counter()-state.received_at)*1000:.1f} ms\n\n"
            f"频率      {stats['actual_tx_hz']:.1f} / {stats['target_hz']:g} Hz\n"
            f"TX/RX     {stats['tx_count']}/{stats['rx_count']}    跳周期 {stats['skipped_periods']}\n"
            f"最大延迟  {stats['max_lateness_s']*1000:.2f} ms    链路 {stats['fault'] or 'OK'}"
        )
        self._notice = notice
        self.app.invalidate()
        self.dirty = False
