"""将两路真实帧按主机时间合并；未更新的一路留空，不做前值填充。"""
import heapq


def event_row(source, state, origin):
    timestamp = f'{(state.received_at-origin)*1000:.6f}'
    if source == 'motor':
        return [timestamp, '', '', '', state.velocity, state.torque,
                state.motor_temperature, state.board_temperature]
    return [timestamp, state.torque, state.speed, state.power, '', '', '', '']


class CurveRecorder:
    def __init__(self, writer, progress, max_age):
        self.writer, self.progress, self.max_age = writer, progress, max_age
        self.pending = []
        self.sequence = 0
        self.latest = {'motor': float('-inf'), 'dyno': float('-inf')}
        self.motor_count = self.dyno_count = 0
        self.count = 0
        self.finished = False

    def add_dyno(self, states):
        if not self.finished:
            for state in states:
                self._add('dyno', state)

    def _add(self, source, state):
        self.latest[source] = max(self.latest[source], state.received_at)
        origin = self.progress.loading_at
        if origin is None or state.received_at < origin:
            return
        if len(self.pending) >= 32768:
            raise RuntimeError('CSV merge queue overflow')
        self.sequence += 1
        heapq.heappush(self.pending, (state.received_at, self.sequence, source, state))

    def finish(self, cutoff):
        """输出截至 cutoff 的已接收事件，后续数据不属于本次曲线。"""
        self._flush(cutoff)
        self.pending.clear()
        self.finished = True

    def _flush(self, cutoff):
        while self.pending and self.pending[0][0] <= cutoff:
            _, _, source, state = heapq.heappop(self.pending)
            self.writer.writerow(event_row(source, state, self.progress.loading_at))
            self.count += 1
            if source == 'motor':
                self.motor_count += 1
            else:
                self.dyno_count += 1

    def record(self, motor_states):
        """每路实际帧均保留；等待两路时间进度，防止跨批次排序倒退。"""
        if self.finished:
            return True
        for motor in motor_states:
            self._add('motor', motor)
            origin = self.progress.loading_at
            if origin is None or motor.received_at < origin:
                continue
            if self.progress.update(motor):
                self.finish(motor.received_at)
                return True
        self._flush(min(self.latest.values()))
        return False
