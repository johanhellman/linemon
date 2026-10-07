#!/usr/bin/env python3
"""linemon - log internet outages layer by layer from a host wired to the ISP router.

Once per second it probes, each in its own thread:

  link         carrier state of --iface (rules out the Pi's own cable)
  gateway      the ISP router (default gateway on --iface)
  isp_hop1/2   the first two routers beyond it, probed with ordinary pings if they answer
               them reliably, otherwise with TTL-limited probes
               towards 1.1.1.1 (re-discovered hourly)
  1.1.1.1, 8.8.8.8, 9.9.9.9   internet hosts (ICMP echo)
  dns_gateway, dns_1.1.1.1    cache-busting DNS lookup via the router and directly

A target is declared down after --threshold consecutive failed probes. The down
event is timestamped at the first failed probe and written immediately, so an
outage survives a crash or power cut. Output in --data:

  events.csv   one row per down/up transition, plus monitor start/stop
  minute.csv   per target and minute: probes sent, lost, average and max RTT
  path.log     discovered hops, NTP sync and power status (start and hourly)
  health.json  whether the monitor trusts its own measurements right now (every 15 s)
  captures.jsonl  output of the --hook command, if one is set (see below)

With --hook, a command (e.g. routers/zte_livebox.py) is run to capture what the
ISP router itself reports: when all internet hosts go down (outage-start), every
--hook-during seconds while they stay down (outage-ongoing), once when they
come back (outage-end), and every --hook-interval seconds otherwise (periodic).
It gets LINEMON_REASON, LINEMON_TIME, LINEMON_DATA and LINEMON_CAPTURE_DIR (a
fresh directory for raw files) in its environment and must print one JSON
object on its last line of output, ideally with "ok", "summary" and "uptime_s".

Settings come from /etc/linemon/linemon.conf (see linemon.conf.example), overridden
by command-line arguments such as LINEMON_ARGS. --check-config validates them and
--migrate prints a linemon.conf equivalent to an existing LINEMON_ARGS.

Needs root (or CAP_NET_RAW) to bind probes to --iface.
"""
import argparse
import configparser
import csv
import datetime as dt
import json
import os
import random
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback

INTERNET_HOSTS = ['1.1.1.1', '8.8.8.8', '9.9.9.9']
EVENTS_HEADER = ['time', 'target', 'event', 'duration_s', 'detail']
MINUTE_HEADER = ['minute', 'target', 'sent', 'lost', 'rtt_avg_ms', 'rtt_max_ms']
HEALTH_FILE = 'health.json'
MAX_PENDING_MINUTES = 100000  # about a week of per-minute rows kept in memory if minute.csv can't be written
TRACE_TARGET = '1.1.1.1'
ECHO_CHECKS = 5  # pings a hop must answer, all of them, to be probed with ordinary pings
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


def sd_notify(message):
    """Tell systemd something (READY=1, WATCHDOG=1, STOPPING=1) through $NOTIFY_SOCKET. A leading '@'
    is an abstract socket. Without systemd, or if the socket is gone, it does nothing."""
    path = os.environ.get('NOTIFY_SOCKET')
    if not path:
        return False
    if path.startswith('@'):
        path = '\0' + path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.settimeout(1)
            s.sendto(message.encode(), path)
    except OSError:
        return False
    return True


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


# A hook's `error` and `summary` are shown on the web page, which has no login. The hook contract
# asks for short fixed messages, but a hook may not follow it, so linemon keeps only what looks
# like one (analyze.py applies the same rule to captures already on disk).
CAPTURE_FAILED = 'capture failed; see journalctl -u linemon'
PLAIN_MESSAGE = re.compile(r"[\w ,.;:()'?+-]{1,100}")
SUMMARY_MAX = 200


def plain_error(text):
    """A capture error as the page may show it: the part before the first ': ' or '. ' (what
    follows is usually exception text or a path), if that is a short plain message; else None."""
    if not isinstance(text, str):
        return None
    text = re.split(r': |\. ', text, maxsplit=1)[0].strip()
    return text if PLAIN_MESSAGE.fullmatch(text) else None


def plain_summary(text):
    """A capture summary as the page may show it: one line of text, cut to SUMMARY_MAX; else None."""
    if not isinstance(text, str):
        return None
    text = ' '.join(text.split())
    return (text if len(text) <= SUMMARY_MAX else text[:SUMMARY_MAX - 1] + '\u2026') or None


def safe_hook_result(result, reason):
    """The hook's result with `error` and `summary` made safe to show; anything changed is logged."""
    result = dict(result)
    for key in ('time', 'reason', 'files'):  # linemon's own fields: a hook can't set them
        if key in result:
            print(f'hook {reason}: ignored its {key!r} field', file=sys.stderr, flush=True)
            del result[key]
    for key, clean, fallback in (('error', plain_error, CAPTURE_FAILED), ('summary', plain_summary, None)):
        if key not in result:
            continue
        value = clean(result[key]) or fallback
        if value != result[key]:
            print(f'hook {reason}: {key} not shown as given: {str(result[key])[:500]}', file=sys.stderr, flush=True)
            if value is None:
                del result[key]
            else:
                result[key] = value
    return result


def ends_with_newline(path):
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            return True
        f.seek(-1, os.SEEK_END)
        return f.read(1) == b'\n'


THROTTLE_BITS = {  # `vcgencmd get_throttled`, per the Raspberry Pi documentation
    0: ('under-voltage now', 'unhealthy'), 1: ('arm frequency capped now', 'degraded'),
    2: ('throttled now', 'degraded'), 3: ('soft temperature limit now', 'degraded'),
    16: ('under-voltage has occurred', 'note'), 17: ('frequency capping has occurred', 'note'),
    18: ('throttling has occurred', 'note'), 19: ('soft temperature limit has occurred', 'note'),
}


def decode_throttled(text):
    """'throttled=0x50005' -> [(description, severity)]. Empty output (not a Pi) gives None."""
    text = text.strip()
    if not text:
        return None
    try:
        value = int(text.split('=')[1], 16)
    except (IndexError, ValueError):
        return [('power status unreadable', 'degraded')]
    return [info for bit, info in THROTTLE_BITS.items() if value >> bit & 1]


class Health:
    """Healthy or unhealthy, with hysteresis so one odd sample can't flap it: unhealthy after 2 bad
    samples in a row, healthy again after 4 good ones. update() returns the change, if any, with the
    time it really began: the first bad sample, or the first of the good ones."""
    BAD, GOOD = 2, 4

    def __init__(self):
        self.state, self.run, self.mark, self.since, self.reasons = 'healthy', 0, None, None, set()

    def update(self, reasons, t):
        bad = bool(reasons)
        if self.state == 'healthy':
            self.run = self.run + 1 if bad else 0
            if bad:
                self.mark = t if self.run == 1 else self.mark
                self.reasons |= set(reasons)
            else:
                self.reasons = set()
            if self.run >= self.BAD:
                self.state, self.run, self.since = 'unhealthy', 0, self.mark
                return 'unhealthy', self.since, '', ';'.join(sorted(self.reasons))
        else:
            self.reasons |= set(reasons)
            self.run = 0 if bad else self.run + 1
            if not bad and self.run == 1:
                self.mark = t
            if self.run >= self.GOOD:
                began, self.state, self.run = self.since, 'healthy', 0
                self.since, self.reasons = None, set()
                return 'healthy', self.mark, f'{(self.mark - began).total_seconds():.0f}', ''
        return None


class Monitor:
    def __init__(self, args):
        self.args = args
        self.iface = args.iface
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.gateway = default_gateway(self.iface)
        self.hops = {}  # 'isp_hop1' -> (ttl, responder, 'echo' or 'ttl')
        self.stats = {}  # (minute, target) -> [sent, lost, rtt_sum, rtt_n, rtt_max]
        self.down = {}  # target -> currently declared down
        self.target_state = {}  # target -> consecutive failures, first failure, down?, last responder
        self.probe_errors = {}  # target -> probes that raised an error on this machine
        self.pending_events = []   # rows that couldn't be written yet: kept and retried, never dropped
        self.pending_minutes = []
        self.warned = {}
        # health: what the monitor can see of itself (see sample_health)
        self.health = Health()
        self.supervise_every, self.health_every = 5.0, 15.0  # seconds; shortened by tests
        self.limits = {k: getattr(args, f'health_{k}', d) for k, d in HEALTH_DEFAULTS.items()}
        self.wall, self.mono = time.time, time.monotonic  # replaced by tests
        self.notify = sd_notify  # replaced by tests
        self.beats = {}  # thread name -> monotonic time of its last pass through its loop
        self.probe_error_times = []
        self.hook_failures = 0  # consecutive router captures that reported an error
        self.threads, self.hook_index = [], None
        self.clock_ref, self.clock_bad_until, self.clock_note = None, 0.0, ''
        self.slow_at, self.slow = None, {}  # readings that cost a process: refreshed once a minute
        self.unsynced_since = None
        os.makedirs(args.data, exist_ok=True)
        self.events_f, self.events = self._open_csv('events.csv', EVENTS_HEADER)
        self.minute_f, self.minute = self._open_csv('minute.csv', MINUTE_HEADER)

    def _open_csv(self, name, header):
        path = os.path.join(self.args.data, name)
        f = open(path, 'a', newline='')
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow(header)
            f.flush()
        elif not ends_with_newline(path):
            # A power cut can leave a half-written last line. End it, so the next
            # row isn't glued on to it and lost with it.
            f.write(w.dialect.lineterminator)
            f.flush()
            os.fsync(f.fileno())
        return f, w

    def warn(self, key, message, every=60):
        """Say something on stderr (the systemd journal), at most once per `every` seconds per key."""
        t = time.monotonic()
        if t - self.warned.get(key, -every) >= every:
            self.warned[key] = t
            print(f'linemon: {message}', file=sys.stderr, flush=True)

    def _drop_file(self, f):
        try:
            f.close()
        except (OSError, ValueError):
            pass

    def event(self, t, target, kind, duration='', detail=''):
        """Write a row to events.csv, flushed and fsynced. If that fails (disk full, I/O error) the
        row stays queued and is retried, so an outage isn't lost if the disk recovers."""
        with self.lock:
            self.pending_events.append([iso(t), target, kind, duration, detail])
            self._write_events()

    def retry_events(self):
        with self.lock:
            if self.pending_events:
                self._write_events()

    def _write_events(self):  # called with the lock held
        try:
            if self.events_f is None:
                self.events_f, self.events = self._open_csv('events.csv', EVENTS_HEADER)
            while self.pending_events:
                self.events.writerow(self.pending_events[0])
                self.events_f.flush()
                os.fsync(self.events_f.fileno())
                self.pending_events.pop(0)
        except OSError as e:
            # Don't trust the file object after an error, and don't retry only the fsync: after a failed
            # fsync the kernel may have dropped the data. Reopen and write the row again; at worst it is
            # in the file twice, which the analyzer ignores.
            self.warn('events', f'cannot write events.csv ({e}); {len(self.pending_events)} row(s) kept, will retry')
            self._drop_file(self.events_f)
            self.events_f = None

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
                self.pending_minutes.append([key[0], key[1], sent, lost, avg, mx])
            if len(self.pending_minutes) > MAX_PENDING_MINUTES:  # a disk that stays broken must not eat the memory
                dropped = len(self.pending_minutes) - MAX_PENDING_MINUTES
                del self.pending_minutes[:dropped]
                self.warn('minute-drop', f'minute.csv still not writable: dropped the {dropped} oldest per-minute row(s)')
            try:
                if self.minute_f is None:
                    self.minute_f, self.minute = self._open_csv('minute.csv', MINUTE_HEADER)
                rows = list(self.pending_minutes)
                for row in rows:
                    self.minute.writerow(row)
                self.minute_f.flush()
                del self.pending_minutes[:len(rows)]
            except OSError as e:
                self.warn('minute', f'cannot write minute.csv ({e}); {len(self.pending_minutes)} row(s) kept, will retry')
                self._drop_file(self.minute_f)
                self.minute_f = None

    def run_hook(self, reason):
        """Run --hook and append its JSON result to captures.jsonl."""
        t = now()
        capture_dir = os.path.join(self.args.data, 'captures', f"{t:%Y%m%dT%H%M%S}-{reason}")
        os.makedirs(capture_dir, exist_ok=True)
        env = dict(os.environ, LINEMON_REASON=reason, LINEMON_TIME=iso(t),
                   LINEMON_DATA=self.args.data, LINEMON_CAPTURE_DIR=capture_dir)
        out = ''
        try:
            r = subprocess.run(shlex.split(self.args.hook), capture_output=True, text=True,
                               timeout=self.args.hook_timeout, env=env)
            if r.stderr.strip():  # details for the journal only; captures.jsonl is shown on the web page
                print(f'hook {reason}: {r.stderr.strip()[-2000:]}', file=sys.stderr, flush=True)
            out = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ''
            result = json.loads(out) if out else {
                'error': f'capture script gave no result (exit code {r.returncode}); see journalctl -u linemon'}
            if not isinstance(result, dict):
                print(f'hook {reason}: output is not a JSON object: {out[:500]}', file=sys.stderr, flush=True)
                result = {'error': 'capture script gave something other than a JSON object; see journalctl -u linemon'}
            result = safe_hook_result(result, reason)
        except subprocess.TimeoutExpired:
            result = {'error': f'timed out after {self.args.hook_timeout} s'}
        except json.JSONDecodeError:
            print(f'hook {reason}: output is not JSON: {out[:500]}', file=sys.stderr, flush=True)
            result = {'error': 'capture script gave unreadable output; see journalctl -u linemon'}
        except Exception as e:
            print(f'hook {reason}: {e!r}', file=sys.stderr, flush=True)
            result = {'error': 'capture script could not be run; see journalctl -u linemon'}
        self.hook_failures = self.hook_failures + 1 if 'error' in result else 0
        if os.listdir(capture_dir):
            result['files'] = os.path.relpath(capture_dir, self.args.data)
        else:
            os.rmdir(capture_dir)
        line = json.dumps({'time': iso(t), 'reason': reason, **result}, ensure_ascii=False)
        with self.lock, open(os.path.join(self.args.data, 'captures.jsonl'), 'a') as f:
            f.write(line + '\n')
            f.flush()
            os.fsync(f.fileno())

    def hook_loop(self):
        """Capture the router's own status around internet outages, and periodically."""
        was_down = False
        last_run = time.monotonic()  # the first periodic capture comes one interval after start
        while not self.stop.wait(1):
            self.beats['hook'] = self.mono()
            down = all(self.down.get(h) for h in INTERNET_HOSTS)
            elapsed = time.monotonic() - last_run
            if down and not was_down:
                reason = 'outage-start'
            elif down and elapsed >= self.args.hook_during:
                reason = 'outage-ongoing'
            elif was_down and not down:
                reason = 'outage-end'
            elif not down and self.args.hook_interval and elapsed >= self.args.hook_interval:
                reason = 'periodic'
            else:
                continue
            was_down = down
            try:
                self.run_hook(reason)
            except Exception:
                self.warn('hook', f'router capture failed unexpectedly:\n{traceback.format_exc()}')
            last_run = time.monotonic()

    def discover_hops(self):
        """Find the first two routers beyond the gateway, and how to probe each one.

        Routers differ in what they answer reliably: some throttle the "time exceeded"
        replies that TTL-limited probes rely on, but answer ordinary pings every time
        (on 05/10/2026 the first ISP hop answered 42/60 TTL-limited probes and 60/60
        pings), while others never answer pings. A hop that answers every one of
        ECHO_CHECKS pings is probed with ordinary pings; otherwise with TTL-limited ones.
        """
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
                answers = sum(ping(responder, self.iface)[0] for _ in range(ECHO_CHECKS))
                found.append((ttl, responder, 'echo' if answers == ECHO_CHECKS else 'ttl'))
            if len(found) == 2:
                break
        if found:  # keep the previous path if discovery ran during an outage
            self.hops = {f'isp_hop{i + 1}': hop for i, hop in enumerate(found)}
            return True
        return False

    def log_path(self):
        ntp = sh(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value']).strip() or '?'
        throttled = sh(['vcgencmd', 'get_throttled']).strip() or 'n/a'
        hops = ', '.join(f'{name}=ttl{ttl}:{ip}/{method}'
                         for name, (ttl, ip, method) in sorted(self.hops.items())) or 'none found'
        line = f'{iso(now())} gateway={self.gateway} {hops} ntp_synced={ntp} {throttled}\n'
        with open(os.path.join(self.args.data, 'path.log'), 'a') as f:
            f.write(line)

    def probes(self):
        iface = self.iface

        def hop(name):
            def probe():
                if name not in self.hops:
                    return None  # not discovered (yet): skip rather than count as failure
                ttl, address, method = self.hops[name]
                if method == 'echo':
                    return ping(address, iface)
                return ping(TRACE_TARGET, iface, ttl=ttl)
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
        next_t = time.monotonic() + random.random()
        while not self.stop.is_set():
            self.stop.wait(max(0.0, next_t - time.monotonic()))
            if self.stop.is_set():
                break
            next_t = max(next_t + self.args.interval, time.monotonic())
            self.beats[name] = self.mono()
            try:
                self.probe_once(name, probe)
            except Exception:  # whatever goes wrong, this target keeps being probed
                self.warn(('target', name), f'{name}: unexpected error:\n{traceback.format_exc()}')

    def probe_once(self, name, probe):
        st = self.target_state.setdefault(name, {'fails': 0, 'first_fail': None, 'down': False, 'last_ok': ''})
        t = now()
        try:
            result = probe()
        except Exception:
            # An error on this machine (out of file descriptors, a bug) says nothing about the line:
            # it is not a failed probe. Skip it, like a hop that isn't discovered yet, and count it.
            self.probe_errors[name] = self.probe_errors.get(name, 0) + 1
            self.probe_error_times.append(self.mono())
            self.warn(('probe', name), f'{name}: the probe raised an error; not counted as a failure:\n{traceback.format_exc()}')
            return
        if result is None:
            return
        ok, rtt, who = result
        self.record(t, name, ok, rtt)
        if ok:
            if st['down']:
                self.event(t, name, 'up', f'{(t - st["first_fail"]).total_seconds():.0f}', who or '')
            st.update(fails=0, first_fail=None, down=False, last_ok=who or '')
        else:
            st['fails'] += 1
            if st['fails'] == 1:
                st['first_fail'] = t
            if st['fails'] == self.args.threshold:
                st['down'] = True
                self.event(st['first_fail'], name, 'down', '', st['last_ok'])
        self.down[name] = st['down']

    # ---- health ---------------------------------------------------------------------------------

    def read_ntp(self):
        return sh(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value']).strip()

    def read_throttled(self):
        return sh(['vcgencmd', 'get_throttled']).strip()

    def read_disk(self):
        usage = shutil.disk_usage(self.args.data)
        return usage.free, usage.total

    def hook_alive(self):
        return self.hook_index is None or self.threads[self.hook_index].is_alive()

    def stalled_threads(self, mono):
        """Probe and maintenance threads that have stopped making progress (a probe takes about a second,
        even in an outage). The router capture is left out: it is an extra. Returns (limit, all, stalled)."""
        stall = max(self.limits['stall'], 3 * self.args.interval + 5)
        workers = {n: b for n, b in self.beats.items() if n != 'hook'}
        return stall, workers, sorted(n for n, b in workers.items() if mono - b > stall)

    def collect_signals(self, wall, mono):
        """Every signal as name -> (severity, value, reason). Severity is 'ok', 'degraded' (worth
        knowing) or 'unhealthy' (measurements can't be trusted); reason is from a fixed list."""
        lim, sig = self.limits, {}

        stall, workers, stalled = self.stalled_threads(mono)
        sig['threads'] = (('unhealthy', f'no progress for {stall:g} s: {", ".join(stalled)}', 'probe-stalled') if stalled
                          else ('ok', f'{len(workers)} threads running', None))

        # errors on this machine while probing (not failed probes)
        self.probe_error_times = [t for t in self.probe_error_times if mono - t < 60]
        n = len(self.probe_error_times)
        sig['probe_errors'] = (('unhealthy', f'{n} in the last minute', 'probe-error') if n
                               else ('ok', 'none in the last minute', None))

        # files that can't be written
        waiting = len(self.pending_events) + len(self.pending_minutes)
        sig['writes'] = (('unhealthy', f'{waiting} row(s) waiting to be written', 'write-failing') if waiting
                         else ('ok', 'ok', None))

        # the clock: a jump of the wall clock against the monotonic one, and NTP
        if self.clock_ref is not None:
            jump = (wall - self.clock_ref[0]) - (mono - self.clock_ref[1])
            if abs(jump) > lim['clock_step']:
                self.clock_bad_until, self.clock_note = mono + 60, f'stepped by {jump:+.0f} s'
                self.warn('clock', f'the clock {self.clock_note}: timestamps around it are unreliable')
        self.clock_ref = (wall, mono)
        if self.slow_at is None or mono - self.slow_at >= 60:
            self.slow_at = mono
            self.slow = {'ntp': self.read_ntp(), 'throttled': self.read_throttled()}
            try:
                self.slow['disk'] = self.read_disk()
            except OSError:
                self.slow['disk'] = None
        ntp = self.slow['ntp']
        self.unsynced_since = (self.unsynced_since if self.unsynced_since is not None else mono) if ntp == 'no' else None
        if mono < self.clock_bad_until:
            sig['clock'] = ('unhealthy', f'{self.clock_note}', 'clock')
        elif self.unsynced_since is not None and mono - self.unsynced_since > lim['ntp_grace']:
            sig['clock'] = ('unhealthy', f'not synchronised for {(mono - self.unsynced_since) / 60:.0f} min', 'clock')
        elif ntp == 'no':
            sig['clock'] = ('degraded', 'not synchronised yet', None)
        elif ntp == 'yes':
            sig['clock'] = ('ok', 'synchronised', None)
        else:
            sig['clock'] = ('degraded', 'synchronisation state unreadable', None)

        # power and temperature on a Raspberry Pi (nothing to report elsewhere)
        flags = decode_throttled(self.slow['throttled'])
        if flags is not None:
            severities = {sev for _, sev in flags}
            text = ', '.join(d for d, _ in flags) or 'ok'
            sig['power'] = (('unhealthy', text, 'power') if 'unhealthy' in severities
                            else ('degraded', text, None) if 'degraded' in severities else ('ok', text, None))

        # free space where the data is written
        if self.slow['disk']:
            free, total = self.slow['disk']
            mb = free / 1e6
            text = f'{mb:,.0f} MB free'
            sig['disk'] = (('unhealthy', text, 'disk') if mb < lim['disk_critical']
                           else ('degraded', text, None) if mb < lim['disk_warn'] or free < total / 10 else ('ok', text, None))

        # the router capture is an extra: a problem with it is worth knowing, not a reason to distrust the probes
        if self.args.hook:
            if not self.hook_alive():
                sig['router_capture'] = ('degraded', 'the capture thread has stopped', None)
            elif self.hook_failures >= 3:
                sig['router_capture'] = ('degraded', f'{self.hook_failures} captures in a row reported an error', None)
            else:
                sig['router_capture'] = ('ok', 'ok', None)
        return sig

    def health_step(self, t=None):
        """One 15 second sample: collect the signals, record a change to or from unhealthy in
        events.csv, and rewrite health.json."""
        t = t or now()
        signals = self.collect_signals(self.wall(), self.mono())
        bad = sorted({reason for severity, _, reason in signals.values() if severity == 'unhealthy'})
        change = self.health.update(bad, t)
        if change:
            kind, when, duration, detail = change
            self.event(when, 'monitor', kind, duration, detail)
        self.write_health(t, signals)

    def write_health(self, t, signals, stopped=False):
        """health.json: written atomically, readable by the web page's unprivileged user, no secrets."""
        unhealthy = self.health.state == 'unhealthy'
        degraded = any(sev == 'degraded' for sev, _, _ in signals.values())
        doc = {
            'time': iso(t),
            'state': 'stopped' if stopped else 'unhealthy' if unhealthy else 'degraded' if degraded else 'healthy',
            'unhealthy_since': iso(self.health.since) if unhealthy else None,
            'reasons': sorted(self.health.reasons) if unhealthy else [],
            'signals': {name: {'state': sev, 'value': value} for name, (sev, value, _) in signals.items()},
        }
        path = os.path.join(self.args.data, HEALTH_FILE)
        try:
            with open(path + '.tmp', 'w') as f:
                json.dump(doc, f, ensure_ascii=False)
                f.write('\n')
            os.chmod(path + '.tmp', 0o644)
            os.replace(path + '.tmp', path)
        except OSError as e:
            self.warn('health-file', f'cannot write {HEALTH_FILE}: {e}')

    def restart_dead(self, threads, specs):
        """Start again any thread that has ended. The loops catch their own errors, so this is a safety
        net: a thread that is gone would leave a target silently unmonitored. Returns how many."""
        restarted = 0
        for i, th in enumerate(threads):
            if not th.is_alive() and not self.stop.is_set():
                fn, args = specs[i]
                self.warn(('dead', i), f'thread for {args[0] if args else fn.__name__} ended unexpectedly; restarting it')
                threads[i] = threading.Thread(target=fn, args=args, daemon=True)
                threads[i].start()
                restarted += 1
        return restarted

    def maintenance(self):
        last_path = time.monotonic()
        while not self.stop.wait(15):
            self.beats['maintenance'] = self.mono()
            try:
                last_path = self.maintain(last_path)
            except Exception:  # the writer of minute.csv must not stop because one cycle went wrong
                self.warn('maintenance', f'maintenance cycle failed:\n{traceback.format_exc()}')

    def maintain(self, last_path):
        """One 15 second cycle; returns when the path was last logged."""
        self.retry_events()
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
                return time.monotonic()
        elif time.monotonic() - last_path >= 3600:
            self.discover_hops()
            self.log_path()
            return time.monotonic()
        return last_path

    def run(self):
        self.discover_hops()
        self.log_path()
        self.event(now(), 'monitor', 'start', '', f'iface={self.iface} gateway={self.gateway}')
        specs = [(self.run_target, (n, p)) for n, p in self.probes().items()] + [(self.maintenance, ())]
        if self.args.hook:
            specs.append((self.hook_loop, ()))
        threads = [threading.Thread(target=fn, args=args, daemon=True) for fn, args in specs]
        self.threads, self.hook_index = threads, (len(threads) - 1 if self.args.hook else None)
        started = self.mono()
        self.beats.update({name: started for name in [n for n, _ in self.probes().items()] + ['maintenance']})
        for th in threads:
            th.start()
        self.notify('READY=1')
        next_sample = time.monotonic() + self.health_every
        while not self.stop.wait(self.supervise_every):  # the main thread watches the others, and the health
            self.restart_dead(threads, specs)
            # systemd's watchdog (WatchdogSec in linemon.service) restarts the service if these stop
            # coming: when this thread hangs, or a probe or the maintenance thread stays stalled.
            if not self.stalled_threads(self.mono())[2]:
                self.notify('WATCHDOG=1')
            if time.monotonic() >= next_sample:
                next_sample += self.health_every
                try:
                    self.health_step()
                except Exception:
                    self.warn('health', f'health check failed:\n{traceback.format_exc()}')
        self.notify('STOPPING=1')
        for th in threads:
            th.join(timeout=5)
        self.flush_minutes(everything=True)
        self.event(now(), 'monitor', 'stop')
        self.write_health(now(), {}, stopped=True)
        self.report_unwritten()

    def report_unwritten(self):
        """Say what was lost, if the monitor stops while a file still can't be written."""
        with self.lock:
            if self.pending_events or self.pending_minutes:
                print(f'linemon: stopping with {len(self.pending_events)} event row(s) and {len(self.pending_minutes)} '
                      f'minute row(s) that could not be written; they are lost', file=sys.stderr, flush=True)


CONFIG_PATH = '/etc/linemon/linemon.conf'
ENV_PATH = '/etc/default/linemon'
EX_CONFIG = 78  # the service doesn't restart on this: a typo must not become a restart loop

# (section, key, flag, type, default, help). One table drives the command line, the
# config file and the checks, so they can't drift apart; linemon.conf.example mirrors it.
OPTIONS = [
    ('monitor', 'iface', '--iface', str, 'eth0', 'wired interface connected to the ISP router'),
    ('monitor', 'data', '--data', str, '/var/lib/linemon', 'output directory'),
    ('monitor', 'interval', '--interval', float, 1.0, 'seconds between probes per target'),
    ('monitor', 'threshold', '--threshold', int, 3, 'consecutive failures before a target is down'),
    ('hook', 'command', '--hook', str, None, "command that captures the ISP router's own status (see above)"),
    ('hook', 'during', '--hook-during', int, 30, 'seconds between captures during an outage'),
    ('hook', 'interval', '--hook-interval', int, 300,
     'seconds between periodic captures when the line is up (0 = only around outages)'),
    ('hook', 'timeout', '--hook-timeout', int, 30, 'seconds before a capture is abandoned'),
    ('health', 'stall', '--health-stall', int, 30, 'seconds without progress before a thread counts as stalled'),
    ('health', 'clock_step', '--health-clock-step', float, 2.0, 'seconds the clock may jump before measurements are unreliable'),
    ('health', 'ntp_grace', '--health-ntp-grace', int, 600, 'seconds the clock may stay unsynchronised before measurements are unreliable'),
    ('health', 'disk_warn', '--health-disk-warn', int, 1000, 'free megabytes in the data directory below which health is degraded'),
    ('health', 'disk_critical', '--health-disk-critical', int, 100, 'free megabytes below which measurements are unreliable'),
]
MINIMUM = {'interval': 0.001, 'threshold': 1, 'hook_during': 1, 'hook_interval': 0, 'hook_timeout': 1,
           'health_stall': 5, 'health_clock_step': 0.5, 'health_ntp_grace': 0, 'health_disk_warn': 0,
           'health_disk_critical': 0}
HEALTH_DEFAULTS = {key: default for section, key, _, _, default, _ in OPTIONS if section == 'health'}


class ConfigError(Exception):
    """A problem with the settings. The message says what and where."""


def dest(flag):
    return flag.lstrip('-').replace('-', '_')


def read_config(path, required=False):
    """Values from a linemon.conf as {argparse dest: value}. Unknown settings are errors."""
    cp = configparser.ConfigParser(interpolation=None)  # '%' is an ordinary character
    try:
        with open(path) as f:
            cp.read_file(f)
    except FileNotFoundError:
        if required:
            raise ConfigError(f'{path}: no such file')
        return {}
    except (configparser.Error, OSError) as e:
        raise ConfigError(f'{path}: {e}')
    known = {(sec, key): (flag, typ) for sec, key, flag, typ, *_ in OPTIONS}
    values = {}
    for section in cp.sections():
        for key, raw in cp.items(section):
            if (section, key) not in known:
                raise ConfigError(f'{path}: unknown setting "{key}" in [{section}]')
            flag, typ = known[(section, key)]
            try:
                values[dest(flag)] = typ(raw) if raw != '' else None
            except ValueError:
                raise ConfigError(f'{path}: [{section}] {key} = {raw!r} is not a valid {typ.__name__}')
    return values


def read_env_args(path):
    """The LINEMON_ARGS of a systemd EnvironmentFile such as /etc/default/linemon, as a list."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    args = []
    for line in lines:
        name, _, value = line.strip().partition('=')
        if name == 'LINEMON_ARGS':
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                value = value[1:-1]  # as systemd does: the quotes wrap the whole value
            try:
                args = shlex.split(value)
            except ValueError as e:
                raise ConfigError(f'{path}: LINEMON_ARGS: {e}')
    return args


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ConfigError(message)  # not argparse's exit status 2, which would make the service restart in a loop


def build_parser(file_values, config_path=CONFIG_PATH):
    p = Parser(description=__doc__.split('\n')[0])
    for _, _, flag, typ, default, text in OPTIONS:
        p.add_argument(flag, type=typ, default=default, help=text)
    p.add_argument('--config', default=config_path, help=f'settings file (default {config_path})')
    p.add_argument('--env-file', default=ENV_PATH, help=f'systemd environment file with LINEMON_ARGS (default {ENV_PATH})')
    p.add_argument('--check-config', action='store_true', help='check the settings the service would use, then exit')
    p.add_argument('--migrate', action='store_true',
                   help='print a linemon.conf equivalent to LINEMON_ARGS in --env-file, then exit; changes nothing')
    p.set_defaults(**file_values)  # the file overrides the defaults; real arguments override the file
    return p


def parse_settings(argv):
    """The effective settings: defaults < config file < command line. Raises ConfigError."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument('--config')
    known, _ = pre.parse_known_args(argv)
    path = known.config or CONFIG_PATH
    file_values = read_config(path, required=bool(known.config))
    args = build_parser(file_values, path).parse_args(argv)
    for name, low in MINIMUM.items():
        if getattr(args, name) < low:
            flag = next(f for _, _, f, *_ in OPTIONS if dest(f) == name)
            raise ConfigError(f'{flag} must be at least {low:g}, not {getattr(args, name):g}')
    if args.health_disk_warn < args.health_disk_critical:
        raise ConfigError('--health-disk-warn must not be below --health-disk-critical')
    return args, path, bool(file_values) or os.path.exists(path)


def render_config(args, source):
    """A linemon.conf with the settings in `args` that differ from the defaults."""
    lines = [f'# Generated by linemon.py --migrate from {source}. Review it, then save it as {CONFIG_PATH}.']
    section = None
    for sec, key, flag, _, default, _ in OPTIONS:
        value = getattr(args, dest(flag))
        if value == default:
            continue
        if sec != section:
            lines += ['', f'[{sec}]']
            section = sec
        lines.append(f'{key} = {value}')
    if section is None:
        lines.append('# Nothing to migrate: every setting is the default.')
    return '\n'.join(lines) + '\n'


def main():
    argv = sys.argv[1:]
    try:
        # The environment file is what the service passes as extra arguments, so a check
        # or a migration looks at the same settings the monitor would start with.
        pre = argparse.ArgumentParser(add_help=False)
        pre.add_argument('--env-file', default=ENV_PATH)
        pre.add_argument('--check-config', action='store_true')
        pre.add_argument('--migrate', action='store_true')
        mode, _ = pre.parse_known_args(argv)
        if mode.migrate:
            args = build_parser({}).parse_args(read_env_args(mode.env_file))
            sys.stdout.write(render_config(args, mode.env_file))
            return
        env_args = read_env_args(mode.env_file) if mode.check_config else []
        args, path, found = parse_settings(argv + env_args)
    except ConfigError as e:
        print(f'linemon: configuration error: {e}', file=sys.stderr)
        sys.exit(EX_CONFIG)
    if args.check_config:
        print('configuration OK')
        print(f'  config file: {path if found else "none (defaults)"}')
        sources = ([path] if found else []) + ([f'LINEMON_ARGS in {mode.env_file}'] if env_args else [])
        print(f'  settings: {" + ".join(sources) or "none (defaults)"}')
        print(f'  hook: {args.hook or "off"}')
        return

    mon = Monitor(args)
    signal.signal(signal.SIGTERM, lambda *_: mon.stop.set())
    signal.signal(signal.SIGINT, lambda *_: mon.stop.set())
    mon.run()


if __name__ == '__main__':
    main()
