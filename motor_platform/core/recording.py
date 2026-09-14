"""CSV recording utility for scripts, independent of hardware transport."""
import csv
from dataclasses import asdict, fields
from pathlib import Path
from ..components.motor.protocol import MotorState


class StateRecorder:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("x", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, [f.name for f in fields(MotorState)])
        self._writer.writeheader()

    def write(self, state: MotorState):
        self._writer.writerow(asdict(state))
        self._file.flush()

    def close(self):
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
