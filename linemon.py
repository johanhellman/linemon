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
MAX_PENDING_MINUTES = 100000  # about a week of per-minute rows kept in memory if minute.csv can't be written
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


def ends_with_newline(path):
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            return True
        f.seek(-1, os.SEEK_END)
        return f.read(1) == b'\n'


class Monitor:
    def __init__(self, args):
        self.args = args
        self.iface = args.iface
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.gateway = default_gateway(self.iface)
        self.hops = {}  # 'isp_hop1' -> (ttl, responder)
        self.stats = {}  # (minute, target) -> [sent, lost, rtt_sum, rtt_n, rtt_max]
        self.down = {}  # target -> currently declared down
        self.target_state = {}  # target -> consecutive failures, first failure, down?, last responder
        self.probe_errors = {}  # target -> probes that raised an error on this machine
        self.pending_events = []   # rows that couldn't be written yet: kept and retried, never dropped
        self.pending_minutes = []
        self.warned = {}
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
                result = {'error': f'expected a JSON object, got: {out[:200]}'}
        except subprocess.TimeoutExpired:
            result = {'error': f'timed out after {self.args.hook_timeout} s'}
        except json.JSONDecodeError:
            print(f'hook {reason}: output is not JSON: {out[:500]}', file=sys.stderr, flush=True)
            result = {'error': 'capture script gave unreadable output; see journalctl -u linemon'}
        except Exception as e:
            print(f'hook {reason}: {e!r}', file=sys.stderr, flush=True)
            result = {'error': 'capture script could not be run; see journalctl -u linemon'}
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
        next_t = time.monotonic() + random.random()
        while not self.stop.is_set():
            self.stop.wait(max(0.0, next_t - time.monotonic()))
            if self.stop.is_set():
                break
            next_t = max(next_t + self.args.interval, time.monotonic())
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
        for th in threads:
            th.start()
        while not self.stop.wait(10):  # the main thread watches the others
            self.restart_dead(threads, specs)
        for th in threads:
            th.join(timeout=5)
        self.flush_minutes(everything=True)
        self.event(now(), 'monitor', 'stop')
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
]
MINIMUM = {'interval': 0.001, 'threshold': 1, 'hook_during': 1, 'hook_interval': 0, 'hook_timeout': 1}


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
        args, path, found = parse_settings(argv + (read_env_args(mode.env_file) if mode.check_config else []))
    except ConfigError as e:
        print(f'linemon: configuration error: {e}', file=sys.stderr)
        sys.exit(EX_CONFIG)
    if args.check_config:
        print('configuration OK')
        print(f'  config file: {path if found else "none (defaults)"}')
        print(f'  hook: {args.hook or "off"}')
        return

    mon = Monitor(args)
    signal.signal(signal.SIGTERM, lambda *_: mon.stop.set())
    signal.signal(signal.SIGINT, lambda *_: mon.stop.set())
    mon.run()


if __name__ == '__main__':
    main()
