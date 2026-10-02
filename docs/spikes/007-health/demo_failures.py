#!/usr/bin/env python3
"""Spike #7 (not product code): show how the Monitor behaves when things go wrong locally.

Written against the monitor as it was when the spike ran, where case 1 wrote a false outage and
case 2 ended the thread and lost the outage. Fixed by #39 and #40; run it to see the monitor now.
"""
import errno
import os
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..'))
import linemon  # noqa: E402


def monitor(tmp):
    args = types.SimpleNamespace(iface='eth0', data=tmp, interval=0.05, threshold=3, hook=None,
                                 hook_during=30, hook_interval=300, hook_timeout=30)
    original, linemon.default_gateway = linemon.default_gateway, lambda iface: '192.0.2.1'
    try:
        return linemon.Monitor(args)
    finally:
        linemon.default_gateway = original


def read_events(tmp):
    with open(os.path.join(tmp, 'events.csv')) as f:
        return [line.strip() for line in f][1:]


def run(label, probe, patch=None):
    tmp = tempfile.mkdtemp()
    mon = monitor(tmp)
    if patch:
        patch(mon)
    th = threading.Thread(target=mon.run_target, args=('gateway', probe), daemon=True)
    th.start()
    time.sleep(2.0)
    alive = th.is_alive()
    mon.stop.set()
    th.join(1)
    print(f'{label}\n  thread alive after 2 s: {alive}\n  events.csv: {read_events(tmp) or "(empty)"}\n'
          f'  mon.down: {mon.down}  probe errors: {getattr(mon, "probe_errors", "n/a")}  '
          f'pending rows: {len(getattr(mon, "pending_events", []))}\n')


# 1. the probe itself raises (here EMFILE, "too many open files": a local problem, not the line's)
def broken_probe():
    raise OSError(errno.EMFILE, 'Too many open files')

run('1. probe raises a local error (EMFILE)', broken_probe)


# 2. the probe fails three times (a real outage) but the disk is full when the down row is written
def disk_full(mon):
    def fsync(fd):
        raise OSError(errno.ENOSPC, 'No space left on device')
    os.fsync = fsync

run('2. a real outage, but writing the event fails (ENOSPC)', lambda: (False, None, None), disk_full)
print('(pending rows are kept in memory and written when the disk allows)')
