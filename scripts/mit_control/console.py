"""Motor-specific view; terminal editing is shared by application scripts."""
import time
from utils.terminal import TerminalConsole


class ConsoleDashboard(TerminalConsole):
    def __init__(self, *, input=None, output=None):
        super().__init__(title='MIT 电机控制与反馈', prompt='mit> ',
                         commands='set p v tau kp kd | stop | rate hz | enable | disable | status | quit',
                         input=input, output=output)

    def render(self, *, node_id, enabled, desired, state, stats, notice, started):
        # Only immutable strings are shared. Never touch the editor buffer or
        # wait for rendering from the control/lease-refresh thread.
        mode = {True: '已使能', False: '已失能', None: '未知'}.get(stats.get('enabled'), '未知')
        self._panel = (
            f"ID {node_id}   {mode}   周期发送：{'运行' if stats.get('periodic_active') else '停止'}   "
            f"运行 {time.perf_counter()-started:.1f} s\n\n"
            f"下发目标  p={desired.position:g} rad  v={desired.velocity:g} rad/s\n"
            f"          tau={desired.torque:g}  kp={desired.kp:g}  kd={desired.kd:g}\n\n"
            f"反馈位置  {state.position:+.5f} rad\n"
            f"反馈速度  {state.velocity:+.5f} rad/s    力矩 {state.torque:+.5f}\n"
            f"温度      电机 {state.motor_temperature:.1f} / 驱动板 {state.board_temperature:.1f} C\n"
            f"错误码    0x{state.error_code:04X}    回传龄期 {(time.perf_counter()-state.received_at)*1000:.1f} ms\n\n"
            f"频率      {stats['actual_tx_hz']:.1f} / {stats['target_hz']:g} Hz\n"
            f"TX/RX     {stats['tx_count']}/{stats['rx_count']}    跳周期 {stats['skipped_periods']}\n"
            f"最大延迟  {stats['max_lateness_s']*1000:.2f} ms    链路 {stats['fault'] or 'OK'}"
        )
        self._notice = notice
        self.app.invalidate()
        self.dirty = False
