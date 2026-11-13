"""测量内核的界面事件与取消适配；不创建终端或设备连接。"""
import json
import threading
import time


class WindowConsole:
    """通过标准输出传递面板事件，并共享产测取消信号。"""
    cancelled = threading.Event()

    def __init__(self, **kwargs):
        self._panel = '正在连接设备…'
        self._notice = ''
        self.app = self
        self.last_update = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.invalidate(force=True)

    def invalidate(self, force=False):
        now = time.monotonic()
        if force or now - self.last_update >= .2:
            print('@panel ' + json.dumps(self._panel + '\n' + self._notice, ensure_ascii=False), flush=True)
            self.last_update = now

    def poll(self):
        return 'quit' if self.cancelled.is_set() else None
