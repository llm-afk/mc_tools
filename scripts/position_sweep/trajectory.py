"""Time-based trajectories; no hardware or file access."""
from dataclasses import dataclass
import math

from motor_platform import MitCommand


@dataclass(frozen=True)
class Reference:
    phase: str
    command: MitCommand
    frequency_hz: float = 0.0
    sweep_time_s: float | None = None


def smoothstep(u):
    """Quintic envelope and its derivative with respect to u."""
    u = max(0.0, min(1.0, u))
    return u**3 * (10 + u * (-15 + 6*u)), 30 * u*u * (1-u)**2


@dataclass(frozen=True)
class Home:
    position: float
    speed: float
    kp: float
    kd: float
    velocity_mode: str = "zero"

    @property
    def duration(self):
        # max(d smoothstep / du) = 1.875; speed bounds the target slope.
        return max(2.0, 1.875 * abs(self.position) / self.speed)

    def sample(self, elapsed):
        g, dg = smoothstep(elapsed / self.duration)
        velocity = -self.position * dg / self.duration
        command = MitCommand(self.position * (1-g),
                             velocity if self.velocity_mode == "derivative" else 0.0,
                             0.0, self.kp, self.kd)
        return Reference("home" if elapsed < self.duration else "zero_hold", command)


@dataclass(frozen=True)
class Chirp:
    start_hz: float
    stop_hz: float
    duration: float
    amplitude: float
    ramp: float
    kp: float
    kd: float
    velocity_mode: str = "zero"

    @property
    def total_duration(self):
        return self.duration + 2*self.ramp

    def sample(self, elapsed):
        t = max(0.0, elapsed)
        if t >= self.total_duration:
            return Reference("finished", MitCommand(kp=self.kp, kd=self.kd),
                             0.0, t-self.ramp)
        slope = (self.stop_hz-self.start_hz) / self.duration
        if t < self.ramp:
            phase = "ramp_in"
            g, dg = smoothstep(t / self.ramp)
            dg /= self.ramp
            frequency = self.start_hz
            cycles = self.start_hz*t
            sweep_time = t-self.ramp
        elif t < self.ramp+self.duration:
            phase = "sweep"
            sweep_time = t-self.ramp
            g, dg = 1.0, 0.0
            frequency = self.start_hz+slope*sweep_time
            cycles = (self.start_hz*self.ramp + self.start_hz*sweep_time
                      + 0.5*slope*sweep_time*sweep_time)
        else:
            phase = "ramp_out"
            tail = t-self.ramp-self.duration
            g, dg = smoothstep(tail / self.ramp)
            g, dg = 1-g, -dg/self.ramp
            frequency = self.stop_hz
            sweep_time = self.duration+tail
            cycles = (self.start_hz*self.ramp
                      + 0.5*(self.start_hz+self.stop_hz)*self.duration
                      + self.stop_hz*tail)
        angle = math.tau * math.remainder(cycles, 1.0)
        p = self.amplitude*g*math.sin(angle)
        v = self.amplitude*(dg*math.sin(angle) + g*math.tau*frequency*math.cos(angle))
        return Reference(phase, MitCommand(p, v if self.velocity_mode == "derivative" else 0.0,
                                          0.0, self.kp, self.kd), frequency, sweep_time)


class Source:
    """Owned by the I/O worker; the application only renews its registration."""
    def __init__(self, plan, started, recorder):
        self.plan, self.started, self.recorder = plan, started, recorder
        self.pending = None

    def __call__(self, now):
        reference = self.plan.sample(now-self.started)
        self.pending = now, reference
        return reference.command

    def on_sent(self, sent_at, command):
        generated_at, reference = self.pending
        self.recorder.command(generated_at, sent_at, reference)
