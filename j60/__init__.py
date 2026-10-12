"""J60 CAN communication tests, using mc_tools' CANable transport."""

from .protocol import MotionCommand, MotionFeedback

__all__ = ["MotionCommand", "MotionFeedback"]
