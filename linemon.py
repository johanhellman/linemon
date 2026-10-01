#!/usr/bin/env python3
"""linemon - log internet outages layer by layer from a host wired to the ISP router.

Once per second it probes, each in its own thread:

  link         carrier state of --iface (rules out the Pi's own cable)
  gateway      the ISP router (default gateway on --iface)
  isp_hop1/2   the first two routers beyond it that answer TTL-limited probes
               towards 1.1.1.1 (re-discovered hourly)
  1.1.1.1, 8.8.8.8, 9.9.9.9   internet hosts (ICMP echo)
  dns_gateway, dns_1.1.1.1    cache-busting DNS lookup via the router and directly

A target is declared down after --threshold consecutive failed probes. The down
event is timestamped at the first failed probe and written immediately, so an
outage survives a crash or power cut. Output in --data:

  events.csv   one row per down/up transition, plus monitor start/stop
  minute.csv   per target and minute: probes sent, lost, average and max RTT
  path.log     discovered hops, NTP sync and power status (start and hourly)

Needs root (or CAP_NET_RAW) to bind probes to --iface.
"""
import argparse
import csv
import datetime as dt
import os
import random
import re
import signal
import socket
import struct
import subprocess
import threading
import time

INTERNET_HOSTS = ['1.1.1.1', '8.8.8.8', '9.9.9.9']
TRACE_TARGET = '1.1.1.1'
SO_BINDTODEVICE = getattr(socket, 'SO_BINDTODEVICE', 25)


def now():
    return dt.datetime.now().astimezone()


def iso(t):
    return t.isoformat(timespec='milliseconds')


def sh(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ''


def default_gateway(iface):
    m = re.search(r'default via (\S+)', sh(['ip', '-4', 'route', 'show', 'default', 'dev', iface]))
    return m.group(1) if m else None


def carrier_up(iface):
    try:
        with open(f'/sys/class/net/{iface}/carrier') as f:
            return f.read().strip() == '1'
    except OSError:
        return False


def ping(host, iface, ttl=None, timeout=1):
    """One ICMP echo. Returns (ok, rtt_ms, responder).

    With ttl set, a 'Time to live exceeded' reply also counts as success:
    it proves the router at that hop is reachable and answering.
    """
    cmd = ['ping', '-n', '-c', '1', '-W', str(timeout), '-I', iface]
    if ttl:
        cmd += ['-t', str(ttl)]
    out = sh(cmd + [host], timeout=timeout + 2)
    m = re.search(r'bytes from (\S+?):.*time=([\d.]+) ms', out)
    if m:
        return True, float(m.group(2)), m.group(1)
    if ttl:
        m = re.search(r'From (\S+?):? icmp_seq=\d+ Time to live exceeded', out)
        if m:
            return True, None, m.group(1)
    return False, None, None


def dns_probe(server, iface, timeout=1.0):
    """A-record lookup of a random name, so no cache can answer it.

    NOERROR or NXDOMAIN both prove an upstream resolver answered; SERVFAIL,
    a malformed reply or a timeout count as failure.
    """
    qid = random.getrandbits(16)
    name = f'lm{random.getrandbits(40):x}.google.com'
    query = struct.pack('>HHHHHH', qid, 0x0100, 1, 0, 0, 0)
    query += b''.join(bytes([len(p)]) + p.encode() for p in name.split('.')) + b'\0'
    query += struct.pack('>HH', 1, 1)
    t0 = time.monotonic()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, iface.encode())
        s.settimeout(timeout)
        try:
            s.sendto(query, (server, 53))
            while True:
                data, _ = s.recvfrom(512)
                if len(data) >= 4 and struct.unpack('>H', data[:2])[0] == qid:
                    break
        except OSError:
            return False, None, None
    if data[3] & 0x0F in (0, 3):
        return True, (time.monotonic() - t0) * 1000, server
    return False, None, None


class Monitor:
    def __init__(self, args):
        self.args = args
        self.iface = args.iface
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.gateway = default_gateway(self.iface)
        self.hops = {}  # 'isp_hop1' -> (ttl, responder)
        self.stats = {}  # (minute, target) -> [sent, lost, rtt_sum, rtt_n, rtt_max]
        os.makedirs(args.data, exist_ok=True)
        self.events_f, self.events = self._open_csv('events.csv', ['time', 'target', 'event', 'duration_s', 'detail'])
        self.minute_f, self.minute = self._open_csv(
            'minute.csv', ['minute', 'target', 'sent', 'lost', 'rtt_avg_ms', 'rtt_max_ms'])

    def _open_csv(self, name, header):
        f = open(os.path.join(self.args.data, name), 'a', newline='')
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow(header)
            f.flush()
        return f, w

    def event(self, t, target, kind, duration='', detail=''):
        with self.lock:
            self.events.writerow([iso(t), target, kind, duration, detail])
            self.events_f.flush()
            os.fsync(self.events_f.fileno())

    def record(self, t, target, ok, rtt):
        key = (t.replace(second=0, microsecond=0).isoformat(), target)
        with self.lock:
            s = self.stats.setdefault(key, [0, 0, 0.0, 0, 0.0])
            s[0] += 1
            if not ok:
                s[1] += 1
            elif rtt is not None:
                s[2] += rtt
                s[3] += 1
                s[4] = max(s[4], rtt)

    def flush_minutes(self, everything=False):
        current = now().replace(second=0, microsecond=0).isoformat()
        with self.lock:
            done = sorted(k for k in self.stats if everything or k[0] < current)
            for key in done:
                sent, lost, rtt_sum, rtt_n, rtt_max = self.stats.pop(key)
                avg = f'{rtt_sum / rtt_n:.1f}' if rtt_n else ''
                mx = f'{rtt_max:.1f}' if rtt_n else ''
                self.minute.writerow([key[0], key[1], sent, lost, avg, mx])
            self.minute_f.flush()

    def discover_hops(self):
        """Find the first two routers beyond the gateway that answer TTL-limited probes."""
        found = []
        for ttl in range(2, 9):
            responder = None
            for _ in range(3):
                ok, _, who = ping(TRACE_TARGET, self.iface, ttl=ttl)
                if ok:
                    responder = who
                    break
            if responder == TRACE_TARGET:
                break
            if responder:
                found.append((ttl, responder))
            if len(found) == 2:
                break
        if found:  # keep the previous path if discovery ran during an outage
            self.hops = {f'isp_hop{i + 1}': hop for i, hop in enumerate(found)}
            return True
        return False

    def log_path(self):
        ntp = sh(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value']).strip() or '?'
        throttled = sh(['vcgencmd', 'get_throttled']).strip() or 'n/a'
        hops = ', '.join(f'{name}=ttl{ttl}:{ip}' for name, (ttl, ip) in sorted(self.hops.items())) or 'none found'
        line = f'{iso(now())} gateway={self.gateway} {hops} ntp_synced={ntp} {throttled}\n'
        with open(os.path.join(self.args.data, 'path.log'), 'a') as f:
            f.write(line)

    def probes(self):
        iface = self.iface

        def hop(name):
            def probe():
                if name not in self.hops:
                    return None  # not discovered (yet): skip rather than count as failure
                return ping(TRACE_TARGET, iface, ttl=self.hops[name][0])
            return probe

        targets = {
            'link': lambda: (carrier_up(iface), None, None),
            'gateway': lambda: ping(self.gateway, iface) if self.gateway else (False, None, None),
            'isp_hop1': hop('isp_hop1'),
            'isp_hop2': hop('isp_hop2'),
            'dns_gateway': lambda: dns_probe(self.gateway, iface) if self.gateway else (False, None, None),
            'dns_1.1.1.1': lambda: dns_probe('1.1.1.1', iface),
        }
        for host in INTERNET_HOSTS:
            targets[host] = lambda host=host: ping(host, iface)
        return targets

    def run_target(self, name, probe):
        fails, first_fail, down, last_ok = 0, None, False, ''
        next_t = time.monotonic() + random.random()
        while not self.stop.is_set():
            self.stop.wait(max(0.0, next_t - time.monotonic()))
            if self.stop.is_set():
                break
            next_t = max(next_t + self.args.interval, time.monotonic())
            t = now()
            try:
                result = probe()
            except Exception:
                result = (False, None, None)
            if result is None:
                continue
            ok, rtt, who = result
            self.record(t, name, ok, rtt)
            if ok:
                if down:
                    self.event(t, name, 'up', f'{(t - first_fail).total_seconds():.0f}', who or '')
                fails, first_fail, down, last_ok = 0, None, False, who or ''
            else:
                fails += 1
                if fails == 1:
                    first_fail = t
                if fails == self.args.threshold:
                    down = True
                    self.event(first_fail, name, 'down', '', last_ok)

    def maintenance(self):
        last_path = time.monotonic()
        while not self.stop.wait(15):
            self.flush_minutes()
            gateway = default_gateway(self.iface)
            if gateway and gateway != self.gateway:
                # Pi moved to another router (e.g. installed behind the UDM, then
                # plugged into the Livebox): the old hops no longer apply.
                self.event(now(), 'monitor', 'gateway', '', f'{self.gateway} -> {gateway}')
                self.gateway, self.hops = gateway, {}
            if not self.hops:
                if self.discover_hops():  # retried every 15 s until the path is known
                    self.log_path()
                    last_path = time.monotonic()
            elif time.monotonic() - last_path >= 3600:
                self.discover_hops()
                self.log_path()
                last_path = time.monotonic()

    def run(self):
        self.discover_hops()
        self.log_path()
        self.event(now(), 'monitor', 'start', '', f'iface={self.iface} gateway={self.gateway}')
        threads = [threading.Thread(target=self.run_target, args=(n, p), daemon=True)
                   for n, p in self.probes().items()]
        threads.append(threading.Thread(target=self.maintenance, daemon=True))
        for th in threads:
            th.start()
        self.stop.wait()
        for th in threads:
            th.join(timeout=5)
        self.flush_minutes(everything=True)
        self.event(now(), 'monitor', 'stop')


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--iface', default='eth0', help='wired interface connected to the ISP router')
    p.add_argument('--data', default='/var/lib/linemon', help='output directory')
    p.add_argument('--interval', type=float, default=1.0, help='seconds between probes per target')
    p.add_argument('--threshold', type=int, default=3, help='consecutive failures before a target is down')
    args = p.parse_args()

    mon = Monitor(args)
    signal.signal(signal.SIGTERM, lambda *_: mon.stop.set())
    signal.signal(signal.SIGINT, lambda *_: mon.stop.set())
    mon.run()


if __name__ == '__main__':
    main()
