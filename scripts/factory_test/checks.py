"""Run workstation and component tests with all physical I/O blocked."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
TRANSPORT_TESTS = {
    'test_motor', 'test_can_status', 'test_usb_discovery',
    'test_runtime', 'test_runtime_source', 'test_platform',
}


def run():
    from .build_release import source_files, digest
    from .version import VERSION

    with tempfile.TemporaryDirectory(prefix='pmt-checks-') as scratch, ExitStack() as settings:
        settings.enter_context(patch.dict(os.environ, TEMP=scratch, TMP=scratch, PYTHONDONTWRITEBYTECODE='1'))
        settings.enter_context(patch.object(tempfile, 'tempdir', scratch))
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(ROOT/'tests'))
        names = {p.stem for p in (ROOT/'tests').glob('test_*.py')}
        boundaries = (
            'motor_platform.Canable.detect', 'motor_platform.transports.canable._Usb.__init__',
            'motor_platform.transports.canable._Usb', 'serial.Serial',
            'urllib.request.OpenerDirector.open', 'requests.sessions.Session.request', 'socket.socket.connect',
            'scripts.factory_test.printer_service.spawn_process', 'scripts.factory_test.network_status.spawn_snapshot', 'scripts.factory_test.driver_setup.spawn_driver_process',
        )
        count = 0
        for group, blocked in (
                (names-TRANSPORT_TESTS, (*boundaries, 'motor_platform.Canable.open')),
                (names & TRANSPORT_TESTS, boundaries)):
            suite = unittest.defaultTestLoader.loadTestsFromNames(['tests.'+name for name in sorted(group)])
            with ExitStack() as stack:
                guards = [stack.enter_context(patch(name, side_effect=AssertionError('Physical I/O is blocked')))
                          for name in blocked]
                result = unittest.TextTestRunner(verbosity=1).run(suite)
                for guard in guards:
                    guard.assert_not_called()
            if not result.wasSuccessful():
                raise SystemExit(1)
            count += result.testsRun
    return dict(ok=True, version=VERSION, guarded_tests=count, hardware_or_network_used=False,
                sources={p.relative_to(ROOT).as_posix(): digest(p) for p in source_files()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = run()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(f"{result['guarded_tests']} guarded tests passed; physical USB/serial/network calls: 0.")


if __name__ == '__main__':
    main()
