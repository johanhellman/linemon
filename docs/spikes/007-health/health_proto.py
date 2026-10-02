#!/usr/bin/env python3
"""Spike #7 prototype (not product code): the pieces of a self-health check, standard library only.

Run it to see each piece checked: python3 docs/spikes/007-health/health_proto.py
"""
import os
import shutil
import socket
import tempfile
import time

# ---- systemd watchdog without python-systemd -------------------------------------------

def sd_notify(message, path=None):
    """Send a datagram to systemd's notify socket ($NOTIFY_SOCKET). A leading '@' is an abstract socket."""
    path = path or os.environ.get('NOTIFY_SOCKET')
    if not path:
        return False                       # not started by systemd, or no NotifyAccess: do nothing
    if path.startswith('@'):
        path = '\0' + path[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.settimeout(1)
        try:
            s.sendto(message.encode(), path)
        except OSError:
            return False
    return True


# ---- Raspberry Pi power and temperature flags (vcgencmd get_throttled) ----------------

THROTTLE_BITS = {  # per the Raspberry Pi documentation; to be confirmed on the Pi
    0: ('under-voltage now', 'unhealthy'), 1: ('arm frequency capped now', 'degraded'),
    2: ('throttled now', 'degraded'), 3: ('soft temperature limit now', 'degraded'),
    16: ('under-voltage has occurred', 'note'), 17: ('frequency capping has occurred', 'note'),
    18: ('throttling has occurred', 'note'), 19: ('soft temperature limit has occurred', 'note'),
}

def decode_throttled(text):
    """'throttled=0x50005' -> [(description, severity), ...]. Unparseable output is a 'degraded' unknown."""
    try:
        value = int(text.strip().split('=')[1], 16)
    except (IndexError, ValueError):
        return [('power status unreadable', 'degraded')] if text.strip() else []   # empty = not a Pi
    return [info for bit, info in THROTTLE_BITS.items() if value >> bit & 1]


# ---- clock steps: wall clock against the monotonic clock -------------------------------

class ClockWatch:
    def __init__(self, wall=time.time, mono=time.monotonic, tolerance=2.0):
        self.wall, self.mono, self.tolerance = wall, mono, tolerance
        self.last = (wall(), mono())

    def step(self):
        """Seconds the wall clock jumped since the last call (0 if it only advanced normally)."""
        w, m = self.wall(), self.mono()
        jump = (w - self.last[0]) - (m - self.last[1])
        self.last = (w, m)
        return jump if abs(jump) > self.tolerance else 0.0


# ---- states with hysteresis, so one bad sample doesn't flap -----------------------------

class State:
    """healthy -> unhealthy after `bad` consecutive bad samples; back after `good` good ones."""
    def __init__(self, bad=2, good=4):
        self.bad, self.good, self.state, self.run = bad, good, 'healthy', 0

    def update(self, is_bad):
        self.run = self.run + 1 if (is_bad == (self.state == 'healthy')) else 0
        if self.state == 'healthy' and is_bad and self.run >= self.bad:
            self.state, self.run = 'unhealthy', 0
        elif self.state == 'unhealthy' and not is_bad and self.run >= self.good:
            self.state, self.run = 'healthy', 0
        return self.state


if __name__ == '__main__':
    # sd_notify against a fake notify socket
    d = tempfile.mkdtemp()
    path = os.path.join(d, 'notify')
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(path)
    server.settimeout(1)
    assert sd_notify('READY=1', path) and server.recv(64) == b'READY=1'
    assert sd_notify('WATCHDOG=1', path) and server.recv(64) == b'WATCHDOG=1'
    assert sd_notify('WATCHDOG=1', path + 'x') is False            # nobody listening: no exception
    saved = os.environ.pop('NOTIFY_SOCKET', None)                  # run under systemd? don't use its socket here
    try:
        assert sd_notify('WATCHDOG=1', None) is False               # no NOTIFY_SOCKET: nothing to do
    finally:
        if saved is not None:
            os.environ['NOTIFY_SOCKET'] = saved
    t = time.perf_counter()
    for _ in range(1000):
        sd_notify('WATCHDOG=1', path)
    print(f'sd_notify: protocol ok against a fake socket; {1000 / 1:.0f} sends took {(time.perf_counter() - t) * 1000:.1f} ms')
    shutil.rmtree(d)

    assert decode_throttled('throttled=0x0') == []
    assert [d for d, _ in decode_throttled('throttled=0x50005')] == [
        'under-voltage now', 'throttled now', 'under-voltage has occurred', 'throttling has occurred']
    assert decode_throttled('') == [] and decode_throttled('garbage')[0][1] == 'degraded'
    print('throttle flags: decoded 0x0, 0x50005, empty (not a Pi) and garbage')

    clock = [1000.0, 50.0]
    watch = ClockWatch(wall=lambda: clock[0], mono=lambda: clock[1])
    clock[0] += 15; clock[1] += 15
    assert watch.step() == 0                                         # a normal 15 s cycle
    clock[0] += 3600 + 15; clock[1] += 15                           # NTP steps the clock forward an hour
    assert abs(watch.step() - 3600) < 1e-6
    clock[0] += 14; clock[1] += 15                                  # 1 s of slew is within tolerance
    assert watch.step() == 0
    print('clock steps: an hour forward is seen, a normal cycle and 1 s of slew are not')

    s = State(bad=2, good=4)
    seq = [True, False, True, True, False, False, False, True, False, False, False, False]
    print('state after each sample:', ' '.join(s.update(x)[0] for x in seq), '(h=healthy u=unhealthy)')
