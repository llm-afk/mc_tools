"""短时实验逐帧保存，停机后排序写入；不拿缓存值补齐另一路。"""
from bisect import bisect_right

OUTPUT_COLUMNS = ['time_ms', 'dyno_torque_nm', 'motor_torque', 'motor_temperature_c',
                  'driver_temperature_c', 'motor_torque_smooth', 'dyno_torque_smooth_nm']

COLUMNS = ['time_ms', 'dyno_torque_nm', 'dyno_speed_rad_s', 'dyno_power_w',
           'motor_speed_rad_s', 'motor_torque', 'motor_temperature_c',
           'driver_temperature_c', 'application_target_torque', 'motor_id',
           'canable_serial', 'load_voltage_set_v', 'load_current_limit_a', 'result']


class Recorder:
    def __init__(self, origin, duration):
        self.origin, self.cutoff = origin, origin + duration
        self.events = []
        self.commands = [(origin, 0.0)]

    def command(self, at, torque):
        # 应用更新目标的时间，不是 CAN 发出或电机执行确认时间。
        self.commands.append((at, torque))

    def add(self, source, states):
        for state in states:
            if self.origin <= state.received_at <= self.cutoff:
                if len(self.events) >= 30000:
                    raise RuntimeError('experiment recording capacity exceeded')
                self.events.append((state.received_at, source, state))

    def write(self, writer, args, result):
        writer.writerow(COLUMNS)
        times = [at for at, _ in self.commands]
        for at, source, state in sorted(self.events, key=lambda event: event[0]):
            if at > self.cutoff:
                continue
            if source == 'motor':
                values = ['', '', '', state.velocity, state.torque,
                          state.motor_temperature, state.board_temperature]
            else:
                values = [state.torque, state.speed, state.power, '', '', '', '']
            command = self.commands[bisect_right(times, at)-1][1]
            writer.writerow([f'{(at-self.origin)*1000:.6f}', *values, command,
                             args.id, args.serial, args.voltage, args.current, result])
