"""上位机内部测量内核、采集记录与离线数据处理。"""
from contextlib import contextmanager
import sys


@contextmanager
def record_shutdown_failure(action):
    """Attach an unconfirmed stop to the error that will actually reach the station."""
    original = sys.exception()
    try:
        yield
    except Exception as cleanup:
        target = original if original is not None else cleanup
        target.add_note(f'工装停机未确认：{action}失败，请切断工装电源。原因：{cleanup}')
        if original is None:
            raise
        for note in getattr(cleanup, '__notes__', ()):
            target.add_note(note)


def power_off(power, summary=None):
    with record_shutdown_failure('负载电源关闭'):
        try:
            power.disable()
            if summary is not None:
                summary['load_output_off_confirmed'] = True
        except Exception as exc:
            if summary is not None:
                summary.update(load_output_off_confirmed=False, load_shutdown_error=str(exc))
            try:
                power.disable(verify=False)
            except Exception as fallback:
                exc.add_note(f'Best-effort OFF failed: {fallback}')
            raise
