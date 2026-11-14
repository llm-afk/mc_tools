"""DYN-200 actively reporting HEX instrument over USB-to-RS485, 115200 8N1."""
from .client import Dynamometer
from .protocol import DynamometerState, FrameParser, parse_frame

__all__ = ['Dynamometer', 'DynamometerState', 'FrameParser', 'parse_frame']

from .discovery import detect_dynamometers

__all__ += ["detect_dynamometers"]
