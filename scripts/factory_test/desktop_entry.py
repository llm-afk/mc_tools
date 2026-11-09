"""Windows EXE entry point, including a hardware-free release check."""
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-check', type=Path, metavar='RESULT_JSON')
    args = parser.parse_args()
    if args.self_check:
        from scripts.factory_test.self_check import run
        raise SystemExit(0 if run(args.self_check)['ok'] else 1)
    try:
        from scripts.factory_test.gui import main as gui_main
        gui_main()
    except Exception as exc:
        # Import/Tk initialization failures happen before the normal window logger exists.
        try:
            from scripts.factory_test.diagnostics import DiagnosticLogs
            from scripts.factory_test.station_config import state_root
            logs = DiagnosticLogs(state_root(), simulation=False)
            logs.station.event('application.startup.failed', '窗口启动或主线程退出异常',level='ERROR',error=exc)
            exc.add_note('启动失败日志：'+str(logs.station.path) if not logs.last_error else logs.last_error)
        except Exception as log_exc:
            exc.add_note('启动失败日志也未能保存：'+str(log_exc))
        raise


if __name__ == '__main__':
    main()
