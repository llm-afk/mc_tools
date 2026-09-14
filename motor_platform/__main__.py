"""The command-line entrypoint only inspects USB; never starts CAN."""
import argparse
import json
from .transports.canable import Canable

parser = argparse.ArgumentParser(description="Read-only CANable identification")
parser.add_argument("--serial", help="USB serial number")
args = parser.parse_args()
print(json.dumps(Canable(args.serial).inspect(), indent=2))
