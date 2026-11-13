"""Own one station data directory until the application's workers have exited."""
import errno
import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


class StationInstance:
    """OS-backed lock, independent of stale files or a recycled process ID."""
    def __init__(self, root, *, lock_name='station-instance.lock'):
        self.path = Path(root)/lock_name
        self.stream = None

    def __enter__(self):
        if self.stream is not None:
            raise AlreadyRunning('当前工位实例已持有运行锁')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open('a+b')
        try:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or getattr(exc,'winerror',None) in (32,33):
                raise AlreadyRunning('电机产测软件已在运行，请返回已有窗口。\n如正在退出，请等待设备连接释放后再打开。') from exc
            raise
        self.stream = stream
        return self

    def __exit__(self, *_):
        if self.stream is None:
            return
        stream, self.stream = self.stream, None
        try:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
        # Do not unlink: another process may have opened the same lock file.
