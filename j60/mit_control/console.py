"""J60 view on mc_tools' shared terminal dashboard/editor."""
import time

from utils.terminal import TerminalConsole

COMMAND_HELP = "set p v tau kp kd | stop | rate hz | enable | disable | status | quit"


def _temperature(value, received_at, now):
    if value is None:
        return "待回传"
    return f"{value:.1f} C ({now-received_at:.2f}s 前)"


def panel_text(client, *, enabled, desired, rate, started, skipped):
    now = time.perf_counter()
    state = client.feedback
    feedback = "等待运动反馈"
    if state:
        feedback = (f"反馈      p={state.position:+.5f} rad   v={state.velocity:+.5f} rad/s\n"
                    f"          tau={state.torque:+.5f} Nm   龄期 {(now-client.feedback_at)*1000:.1f} ms")
    status = "待查询" if client.status_code is None else f"0x{client.status_code:04X}"
    actual = client.control_tx_count / max(now-started, 1e-6)
    return (
        f"J60 ID {client.motor_id}   {'已使能' if enabled else '已失能'}   CAN 1 Mbps / 标准帧\n\n"
        f"下发      p={desired.position:g} rad   v={desired.velocity:g} rad/s\n"
        f"          tau={desired.torque:g} Nm   kp={desired.kp:g}   kd={desired.kd:g}\n\n"
        f"{feedback}\n"
        f"温度      电机 {_temperature(client.motor_temperature, client.motor_temperature_at, now)}\n"
        f"          驱动 {_temperature(client.driver_temperature, client.driver_temperature_at, now)}\n"
        f"故障码    {status}   原始状态 {client.status_raw.hex(' ').upper()}\n\n"
        f"控制频率  平均 {actual:.1f} / 目标 {rate:g} Hz   跳周期 {skipped}\n"
        f"控制 TX / 反馈 RX   {client.control_tx_count} / {client.motion_rx_count}\n"
        f"全部 TX/RX {client.tx_count}/{client.rx_count}   忽略其他帧 {client.ignored_count}\n"
        f"控制原始  {desired.pack().hex(' ').upper()}\n"
        f"反馈原始  {client.last_motion_raw.hex(' ').upper()}"
    )


class ConsoleDashboard(TerminalConsole):
    def __init__(self, **kwargs):
        super().__init__(title="J60 基础 CAN 控制与反馈", prompt="j60> ",
                         commands=COMMAND_HELP, **kwargs)

    def render(self, client, *, notice, **kwargs):
        self._panel = panel_text(client, **kwargs)
        self._notice = notice
        self.app.invalidate()
        self.dirty = False
