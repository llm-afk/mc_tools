"""The command-line entrypoint only inspects USB; never starts CAN."""
import argparse
import json
from .transports.canable import Canable

parser = argparse.ArgumentParser(description="Read-only CANable identification")
parser.add_argument("--serial", help="USB serial number")
parser.add_argument("--list", action="store_true", help="List connected CANable serial numbers without opening CAN")
args = parser.parse_args()
if args.list:
    print(json.dumps([{'serial_number': d['serial_number']} for d in Canable.detect()], indent=2))
else:
    print(json.dumps(Canable(args.serial).inspect(), indent=2))
