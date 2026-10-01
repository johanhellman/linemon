#!/usr/bin/env python3
"""Summarise linemon outages and compare them with a UniFi (UDM) support export.

  analyze.py DATA_DIR                       summary of what the monitor saw
  analyze.py DATA_DIR --udm EXPORT_DIR ...  also match against the UDM's WAN log
  analyze.py DATA_DIR --csv out.csv         write the internet outages as CSV

An internet outage is a period where all of 1.1.1.1, 8.8.8.8 and 9.9.9.9 were
down at once. Each one is classified by the first layer that also failed:
link (cable), gateway (ISP router), isp_hop1, isp_hop2, or beyond.

If the monitor runs with a router capture hook, each outage also gets what the
router itself reported during it, and the router's reported connection uptime
shows every time its internet session was (re)established.
"""
import argparse
import bisect
import csv
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import threading
from collections import defaultdict

INTERNET_HOSTS = ['1.1.1.1', '8.8.8.8', '9.9.9.9']
LAYERS = [
    ('link', 'monitor cable/port down'),
    ('gateway', 'ISP router not responding'),
    ('isp_hop1', 'first ISP hop unreachable (access network)'),
    ('isp_hop2', 'second ISP hop unreachable'),
]
UDM_RE = re.compile(r'^(\S+) .*wan-failover-group-base: wf-group-1-single is (up|down)')


def parse_time(s):
    return dt.datetime.fromisoformat(s)


def fmt_dur(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f'{seconds} s'
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f'{h} h {m} min {s} s' if h else f'{m} min {s} s'


_captures_cache = {}  # path -> {'ino', 'offset', 'items'}; guarded by _captures_lock
_captures_lock = threading.Lock()


def _parse_captures(data):
    captures = []
    for line in data.splitlines():
        try:
            c = json.loads(line)
            c['time'] = parse_time(c['time'])
            captures.append(c)
        except (ValueError, KeyError, TypeError):
            continue  # e.g. a line cut short by a power cut
    return captures


def load_captures(data_dir):
    """Router captures from captures.jsonl, oldest first, with 'time' parsed.

    The web page asks every few seconds and the file only grows at the end, so the
    parsed captures are kept and only the new complete lines are parsed. The file is
    expected to be appended to or replaced by a new file (as trim.py does); the cache is
    dropped if it is replaced or gets shorter. Rewriting it in place at the same size
    isn't noticed. The result is a copy of the list; don't modify the capture dicts in it.
    """
    path = os.path.join(data_dir, 'captures.jsonl')
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return []
    with _captures_lock:
        entry = _captures_cache.get(path)
        if entry is None or entry['ino'] != st.st_ino or st.st_size < entry['offset']:
            entry = _captures_cache[path] = {'ino': st.st_ino, 'offset': 0, 'items': []}
        if st.st_size > entry['offset']:
            with open(path, 'rb') as f:
                f.seek(entry['offset'])
                data = f.read(st.st_size - entry['offset'])
            whole = data[:data.rfind(b'\n') + 1]  # a line still being written waits for its newline
            new = _parse_captures(whole.decode(errors='replace'))
            items = entry['items']
            out_of_order = bool(new and items and min(c['time'] for c in new) < items[-1]['time'])
            items.extend(new)
            if out_of_order or any(a['time'] > b['time'] for a, b in zip(new, new[1:])):
                items.sort(key=lambda c: c['time'])
            entry['offset'] += len(whole)
        return list(entry['items'])


def captures_since(captures, since):
    """The captures at or after `since`, from a list sorted by time."""
    return captures[bisect.bisect_left(captures, since, key=lambda c: c['time']):]


def session_starts(captures, tolerance=30):
    """When the router's internet session (re)started, from its reported uptime.

    Every capture with uptime_s dates the current session's start (capture time
    minus uptime). Starts within `tolerance` seconds are the same session.
    """
    sessions = []
    for c in captures:
        if c.get('uptime_s') is None:
            continue
        start = c['time'] - dt.timedelta(seconds=c['uptime_s'])
        if sessions and abs((start - sessions[-1]['start']).total_seconds()) <= tolerance:
            sessions[-1]['last_seen'] = c['time']
        else:
            sessions.append({'start': start, 'first_seen': c['time'], 'last_seen': c['time']})
    return sessions


def router_during(captures, start, end, slack=5):
    """What the router reported in captures taken during an outage (`captures` sorted by time)."""
    lo, hi = start - dt.timedelta(seconds=slack), end + dt.timedelta(seconds=slack)
    first = bisect.bisect_left(captures, lo, key=lambda c: c['time'])
    last = bisect.bisect_right(captures, hi, key=lambda c: c['time'])
    found = [c for c in captures[first:last] if c.get('reason') in ('outage-start', 'outage-ongoing')]
    summaries = []
    for c in found:
        s = c.get('summary') or ('capture failed: ' + c['error'] if c.get('error') else '')
        if s and s not in summaries:
            summaries.append(s)
    return '; '.join(summaries)


MINUTE_FIELDS = ['minute', 'target', 'sent', 'lost', 'rtt_avg_ms', 'rtt_max_ms']
TAIL_BYTES = 4096


def last_minute_end(data_dir):
    """End of the most recent minute in minute.csv, or None. Reads only the end of the file."""
    try:
        with open(os.path.join(data_dir, 'minute.csv'), 'rb') as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - TAIL_BYTES))
            lines = f.read().decode(errors='replace').splitlines()
    except FileNotFoundError:
        return None
    # The very last line can be cut short by a power cut: fall back to the one before.
    for line in reversed(lines):
        try:
            return parse_time(line.split(',', 1)[0]) + dt.timedelta(minutes=1)
        except ValueError:
            continue  # the header, a partial line, or a line cut short
    return None


def _minute_offset(f, size, t, window=8192):
    """A byte offset in minute.csv, at the start of a line, from which reading forward
    reaches every row at or after `t`.

    The file only grows at the end, so its rows are in time order and a bisection finds
    the place. It compares parsed times, not text: the UTC offset changes at daylight
    saving time, so local ISO strings don't sort by instant across the change.
    """
    lo, hi = 0, size
    while hi - lo > window:
        mid = (lo + hi) // 2
        f.seek(mid)
        f.readline()  # we probably landed inside a line: skip the rest of it
        try:
            row_time = parse_time(f.readline().decode(errors='replace').split(',', 1)[0])
        except ValueError:
            hi = mid  # unreadable (e.g. cut short): look earlier
            continue
        if row_time < t:
            lo = mid
        else:
            hi = mid
    if lo:
        f.seek(lo)
        f.readline()
        return f.tell()
    return 0


def minute_rows(data_dir, since):
    """Rows of minute.csv at or after `since`, as dicts, without reading the whole file."""
    try:
        f = open(os.path.join(data_dir, 'minute.csv'), 'rb')
    except FileNotFoundError:
        return
    with f:
        size = f.seek(0, os.SEEK_END)
        f.seek(_minute_offset(f, size, since))
        text = io.TextIOWrapper(f, newline='', errors='replace')
        for row in csv.DictReader(text, fieldnames=MINUTE_FIELDS):
            try:
                if parse_time(row['minute']) >= since:
                    yield row
            except (ValueError, TypeError):
                continue  # the header, or a line cut short
        text.detach()  # f is closed by the with


def last_minute_before(data_dir, t):
    """Start of the last minute in minute.csv that began before `t`, or None."""
    try:
        f = open(os.path.join(data_dir, 'minute.csv'), 'rb')
    except FileNotFoundError:
        return None
    last = None
    with f:
        size = f.seek(0, os.SEEK_END)
        f.seek(_minute_offset(f, size, t))
        text = io.TextIOWrapper(f, newline='', errors='replace')
        for row in csv.DictReader(text, fieldnames=MINUTE_FIELDS):
            try:
                minute = parse_time(row['minute'])
            except (ValueError, TypeError):
                continue  # the header, or a line cut short
            if minute >= t:
                break
            last = minute
        text.detach()
    return last


def crash_end(data_dir, started, last_seen, restart):
    """When a run that ended in a restart with no 'stop' row (a crash or power cut) was last
    known to be running: its latest event or minute row, never before it started or
    after the restart. The time in between is not monitored, so it is not up."""
    minute = last_minute_before(data_dir, restart)
    evidence = [last_seen or started]
    if minute:
        evidence.append(minute + dt.timedelta(minutes=1))
    return max(started, min(restart, max(evidence)))


def readable_event(row):
    """False for a line cut short by a power cut, which has no usable time or event."""
    try:
        parse_time(row['time'])
    except (ValueError, TypeError):
        return False
    return bool(row['target'] and row['event'])


def load_events(data_dir):
    with open(os.path.join(data_dir, 'events.csv'), newline='') as f:
        rows = [r for r in csv.DictReader(f) if readable_event(r)]
    return sorted(rows, key=lambda r: parse_time(r['time']))


def router_change(rows):
    """The last 'monitor,gateway' event, or None."""
    changes = [r for r in rows if r['target'] == 'monitor' and r['event'] == 'gateway']
    return changes[-1] if changes else None


def load_outages(data_dir, include_all=False):
    """Return ({target: [(start, end, truncated)]}, monitoring periods).

    Unless include_all is set, only data since the last router change counts:
    anything before was measured somewhere else (e.g. behind the UDM during install).
    """
    rows = load_events(data_dir)
    change = router_change(rows)
    if change and not include_all:
        rows = [dict(change, event='start')] + rows[rows.index(change) + 1:]
    outages = defaultdict(list)
    open_down = {}
    periods, started, last_seen = [], None, None
    for r in rows:
        t, target, kind = parse_time(r['time']), r['target'], r['event']
        if target == 'monitor':
            if kind not in ('start', 'stop'):
                last_seen = t
                continue
            # A start with no 'stop' before it means the run crashed or lost power: it ended
            # when it was last seen, not when the monitor came back.
            run_end = crash_end(data_dir, started, last_seen, t) if kind == 'start' and started else t
            # a restart ends every outage that was still open
            for tgt, start in open_down.items():
                outages[tgt].append((start, run_end, True))
            open_down.clear()
            if kind == 'start':
                if started:
                    periods.append((started, run_end))
                started = t
            elif started:
                periods.append((started, run_end))
                started = None
        elif kind == 'down':
            open_down[target] = t
        elif kind == 'up' and target in open_down:
            outages[target].append((open_down.pop(target), t, False))
        last_seen = t
    # While the line is stable no events are written, so the latest per-minute
    # measurement is the best evidence of how long the monitor has been running.
    end = parse_time(rows[-1]['time']) if rows else None
    last_minute = last_minute_end(data_dir)
    if end and last_minute and last_minute > end:
        end = last_minute
    for tgt, start in open_down.items():
        outages[tgt].append((start, None, True))  # still down at end of data
    if started:
        periods.append((started, end))
    return outages, periods


def intersect_all(interval_lists, open_end=None):
    """Periods where every list has an interval covering the time: [(start, end, ongoing)].

    Intervals still open (end None) run until `open_end`, e.g. the end of the data
    or now; a period where every list is still open is returned as ongoing, with
    `open_end` as its provisional end. Without `open_end`, open intervals are skipped.
    """
    points = []
    for intervals in interval_lists:
        for s, e, _ in intervals:
            if e is None:
                if open_end is None or open_end <= s:
                    continue
                e = open_end
            points.append((s, 1))
            points.append((e, -1))
    points.sort(key=lambda p: (p[0], p[1]))
    need, level, start, result = len(interval_lists), 0, None, []
    for t, d in points:
        level += d
        if level == need and start is None:
            start = t
        elif level < need and start is not None:
            if t > start:
                result.append((start, t))
            start = None

    def still_open(s):
        return all(any(e is None and st <= s for st, e, _ in intervals) for intervals in interval_lists)
    return [(s, e, open_end is not None and e == open_end and still_open(s)) for s, e in result]


def overlaps(a_start, a_end, b_start, b_end, tolerance=0):
    tol = dt.timedelta(seconds=tolerance)
    return a_start <= b_end + tol and b_start <= a_end + tol


def classify(start, end, outages):
    for target, label in LAYERS:
        for s, e, _ in outages.get(target, []):
            if overlaps(start, end, s, e or end):
                return label
    return 'beyond the ISP hops (router and first hops answered)'


def load_udm(paths, window_start, window_end):
    events = set()
    for path in paths:
        files = [path] if os.path.isfile(path) else [
            os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs if re.fullmatch(r'messages(\.\d+)?(\.zst)?', f)]
        for fp in files:
            if fp.endswith('.zst'):
                if not shutil.which('zstd'):
                    continue
                text = subprocess.run(['zstd', '-dc', fp], capture_output=True, text=True, errors='ignore').stdout
            else:
                with open(fp, errors='ignore') as f:
                    text = f.read()
            for line in text.splitlines():
                m = UDM_RE.match(line)
                if m:
                    events.add((parse_time(m.group(1)), m.group(2)))
    events = sorted(events)
    result = []
    for (t1, k1), (t2, k2) in zip(events, events[1:]):
        if k1 == 'down' and k2 == 'up' and window_start <= t1 <= window_end:
            result.append((t1, t2))
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('data', help='linemon data directory (with events.csv)')
    p.add_argument('--udm', nargs='+', default=[], help='UniFi support export folder(s) or messages files')
    p.add_argument('--tolerance', type=int, default=15, help='seconds of slack when matching UDM outages')
    p.add_argument('--csv', help='write internet outages to this CSV file')
    p.add_argument('--all', action='store_true', help='include data from before the last router change')
    args = p.parse_args()

    change = router_change(load_events(args.data))
    if change and not args.all:
        print(f"Using data since the router changed at {parse_time(change['time']):%d/%m %H:%M} "
              f"({change['detail']}). Use --all to include earlier data.")
    outages, periods = load_outages(args.data, args.all)
    if not periods:
        print('No monitoring data yet.')
        return
    first, last = periods[0][0], periods[-1][1]
    monitored = sum(((e or last) - s).total_seconds() for s, e in periods)
    print(f'Monitoring: {first:%d/%m %H:%M} to {last:%d/%m %H:%M} '
          f'({fmt_dur(monitored)} in {len(periods)} run(s))\n')

    # Outages still in progress count up to the end of the data, and are marked as such.
    print('Per target (outages of at least 3 s; ongoing ones counted up to the end of the data):')
    names = ['link', 'gateway', 'isp_hop1', 'isp_hop2'] + INTERNET_HOSTS + ['dns_gateway', 'dns_1.1.1.1']
    for name in names:
        ivs = [(s, e or last) for s, e, _ in outages.get(name, []) if (e or last) > s]
        ongoing = sum(1 for _, e, _ in outages.get(name, []) if e is None)
        total = sum((e - s).total_seconds() for s, e in ivs)
        longest = max(((e - s).total_seconds() for s, e in ivs), default=0)
        print(f'  {name:<12} {len(ivs):>4} outages  {fmt_dur(total):>16} total  longest {fmt_dur(longest)}'
              + ('  (still down)' if ongoing else ''))

    internet = intersect_all([outages.get(h, []) for h in INTERNET_HOSTS], open_end=last)
    total = sum((e - s).total_seconds() for s, e, _ in internet)
    still = sum(1 for *_, ongoing in internet if ongoing)
    print(f'\nInternet outages (all three hosts down): {len(internet)}, {fmt_dur(total)} in total'
          + (f' ({still} still in progress at the end of the data)' if still else ''))

    captures = captures_since(load_captures(args.data), first)
    rows = []
    by_layer = defaultdict(lambda: [0, 0.0])
    by_day = defaultdict(lambda: [0, 0.0])
    for s, e, ongoing in internet:
        layer = classify(s, e, outages)
        secs = (e - s).total_seconds()
        by_layer[layer][0] += 1
        by_layer[layer][1] += secs
        by_day[s.strftime('%d/%m')][0] += 1
        by_day[s.strftime('%d/%m')][1] += secs
        rows.append({'start': s.isoformat(timespec='seconds'),
                     'end': '' if ongoing else e.isoformat(timespec='seconds'),
                     'duration_s': round(secs), 'ongoing': 'yes' if ongoing else '',
                     'layer': layer, 'router': router_during(captures, s, e), 'udm_match': ''})
    if internet:
        print('\n  Where the path broke:')
        for layer, (n, secs) in sorted(by_layer.items(), key=lambda x: -x[1][0]):
            print(f'    {n:>4} x  {fmt_dur(secs):>16}  {layer}')
        print('\n  Per day:')
        for day, (n, secs) in by_day.items():
            print(f'    {day}  {n:>4} outages  {fmt_dur(secs)}')
        print('\n  Longest:')
        for r in sorted(rows, key=lambda r: -r['duration_s'])[:5]:
            print(f"    {r['start'][:19].replace('T', ' ')}  {fmt_dur(r['duration_s']):>14}  {r['layer']}"
                  + ('  (still in progress)' if r['ongoing'] else ''))
            if r['router']:
                print(f"    {'':19}  {'':>14}  router said: {r['router']}")

    if captures:
        failed = [c for c in captures if c.get('error')]
        during = [c for c in captures if c.get('reason') in ('outage-start', 'outage-ongoing')]
        print(f'\nRouter captures: {len(captures)} ({len(during)} during outages, {len(failed)} failed)')
        if failed:
            print(f"  Last failure: {failed[-1]['time']:%d/%m %H:%M:%S}  {failed[-1]['error']}")
        sessions = session_starts(captures)
        if sessions:
            print('  Internet session (re)started at, according to the router:')
            for s in sessions:
                print(f"    {s['start']:%d/%m %H:%M:%S}  (seen until {s['last_seen']:%d/%m %H:%M})")

    if args.udm:
        udm = load_udm(args.udm, first, last)
        tol = args.tolerance
        udm_matched = [u for u in udm if any(overlaps(u[0], u[1], s, e, tol) for s, e, _ in internet)]
        for r, (s, e, _) in zip(rows, internet):
            r['udm_match'] = 'yes' if any(overlaps(s, e, u[0], u[1], tol) for u in udm) else 'no'
        pi_matched = sum(r['udm_match'] == 'yes' for r in rows)
        print(f'\nUDM comparison (same period, {tol} s tolerance):')
        print(f'  UDM outages: {len(udm)}, also seen by the monitor: {len(udm_matched)}')
        print(f'  Monitor internet outages: {len(internet)}, also seen by the UDM: {pi_matched}')
        missed = [u for u in udm if u not in udm_matched]
        if missed:
            print('  UDM outages the monitor did NOT see:')
            for s, e in missed:
                print(f'    {s:%d/%m %H:%M:%S}  {fmt_dur((e - s).total_seconds())}')
        print('  (The monitor needs 3 s and the UDM about 5 s to call an outage, so it'
              ' normally sees extra short ones.)')

    if args.csv:
        with open(args.csv, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['start', 'end', 'duration_s', 'ongoing', 'layer', 'router', 'udm_match'])
            w.writeheader()
            w.writerows(rows)
        print(f'\nWrote {len(rows)} outages to {args.csv}')


if __name__ == '__main__':
    main()
