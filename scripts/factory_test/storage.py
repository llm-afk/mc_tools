"""Portable report artifacts and separate durable workflow records.

New report folders contain only the PNG and two CSVs. Legacy directories retain
their original metadata and locks so older jobs are never silently resealed.
Copy the entire release folder, including .factory-state, when moving machines.
"""
import os
from pathlib import Path
import shutil
import tempfile
import time


def atomic_replace(source, target):
    """Bounded retry of a Windows sharing/permission denial; never rewrite in place."""
    delays = (.01, .025, .05, .1, .2)
    for attempt in range(len(delays)+1):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == len(delays):
                raise
            try:
                from .diagnostics import log_event
                log_event('storage.replace.retry', '本地文件替换暂时被拒绝', level='WARNING',
                          path=str(target), attempt=attempt+1, winerror=exc.winerror, delay_s=delays[attempt])
            except Exception:
                pass  # Diagnostic failure must not prevent a durable file commit.
            time.sleep(delays[attempt])


def external_directory(directory):
    directory = Path(directory).resolve()
    return directory.parent.parent / '.factory-state' / directory.parent.name / directory.name


def initialize(directory):
    external_directory(directory).mkdir(parents=True, exist_ok=False)


def records(directory):
    directory = Path(directory)
    external = external_directory(directory)
    return external if external.is_dir() else directory


def metadata(directory, name):
    return records(directory) / name


def stage_directory(directory, name):
    directory = Path(directory)
    state = records(directory)
    return state / 'work' / name if state != directory else directory / name


def csv_relative(directory, stage, filename):
    return filename if records(directory) != Path(directory) else f'{stage}/{filename}'


def publish_csvs(directory):
    """Publish closed CSVs atomically; keep raw evidence for storage-error recovery."""
    directory = Path(directory)
    if records(directory) == directory:
        return
    for stage, filename in (('kt', 'kt_curve.csv'), ('tn', 'curve.csv')):
        source = stage_directory(directory, stage) / filename
        if not source.is_file():
            continue
        # Stage on the same volume, outside the operator's report directory.
        fd, name = tempfile.mkstemp(prefix=filename, suffix='.tmp', dir=records(directory))
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'wb') as target, source.open('rb') as stream:
                shutil.copyfileobj(stream, target)
                target.flush()
                os.fsync(target.fileno())
            atomic_replace(temporary, directory / filename)
        finally:
            temporary.unlink(missing_ok=True)
