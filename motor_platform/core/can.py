"""Shared CAN data frame contract."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CanFrame:
    can_id: int
    data: bytes
    is_fd: bool = True
    brs: bool = True
    is_extended: bool = False
    is_error: bool = False
    is_remote: bool = False

    def __post_init__(self):
        object.__setattr__(self, "data", bytes(self.data))
