#!/usr/bin/env python3
"""Summarise linemon outages and compare them with a UniFi (UDM) support export.

  analyze.py DATA_DIR                       summary of what the monitor saw
  analyze.py DATA_DIR --udm EXPORT_DIR ...  also match against the UDM's WAN log
  analyze.py DATA_DIR --csv out.csv         write the internet outages as CSV

An internet outage is a period where all of 1.1.1.1, 8.8.8.8 and 9.9.9.9 were
down at once. Each one is classified by the first layer that also failed:
link (cable), gateway (ISP router), isp_hop1, isp_hop2, or beyond.
"""
import argparse
import csv
import datetime as dt
import os
import re
import shutil
import subprocess
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


def last_minute_end(data_dir):
    """End of the most recent minute in minute.csv, or None."""
    try:
        with open(os.path.join(data_dir, 'minute.csv'), newline='') as f:
            last = None
            for last in csv.reader(f):
                pass
    except FileNotFoundError:
        return None
    if not last or last[0] == 'minute':
        return None
    return parse_time(last[0]) + dt.timedelta(minutes=1)


def load_events(data_dir):
    with open(os.path.join(data_dir, 'events.csv'), newline='') as f:
        return sorted(csv.DictReader(f), key=lambda r: parse_time(r['time']))


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
    periods, started = [], None
    for r in rows:
        t, target, kind = parse_time(r['time']), r['target'], r['event']
        if target == 'monitor':
            if kind not in ('start', 'stop'):
                continue
            # a restart ends every outage that was still open
            for tgt, start in open_down.items():
                outages[tgt].append((start, t, True))
            open_down.clear()
            if kind == 'start':
                if started:
                    periods.append((started, t))
                started = t
            elif started:
                periods.append((started, t))
                started = None
        elif kind == 'down':
            open_down[target] = t
        elif kind == 'up' and target in open_down:
            outages[target].append((open_down.pop(target), t, False))
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


def intersect_all(interval_lists):
    """Periods where every list has an interval covering the time."""
    points = []
    for intervals in interval_lists:
        for s, e, _ in intervals:
            if e is None:
                continue
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
    return result


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

    print('Per target (outages of at least 3 s):')
    names = ['link', 'gateway', 'isp_hop1', 'isp_hop2'] + INTERNET_HOSTS + ['dns_gateway', 'dns_1.1.1.1']
    for name in names:
        ivs = [(s, e) for s, e, _ in outages.get(name, []) if e]
        total = sum((e - s).total_seconds() for s, e in ivs)
        longest = max(((e - s).total_seconds() for s, e in ivs), default=0)
        print(f'  {name:<12} {len(ivs):>4} outages  {fmt_dur(total):>16} total  longest {fmt_dur(longest)}')

    internet = intersect_all([outages.get(h, []) for h in INTERNET_HOSTS])
    total = sum((e - s).total_seconds() for s, e in internet)
    print(f'\nInternet outages (all three hosts down): {len(internet)}, {fmt_dur(total)} in total')

    rows = []
    by_layer = defaultdict(lambda: [0, 0.0])
    by_day = defaultdict(lambda: [0, 0.0])
    for s, e in internet:
        layer = classify(s, e, outages)
        secs = (e - s).total_seconds()
        by_layer[layer][0] += 1
        by_layer[layer][1] += secs
        by_day[s.strftime('%d/%m')][0] += 1
        by_day[s.strftime('%d/%m')][1] += secs
        rows.append({'start': s.isoformat(timespec='seconds'), 'end': e.isoformat(timespec='seconds'),
                     'duration_s': round(secs), 'layer': layer, 'udm_match': ''})
    if internet:
        print('\n  Where the path broke:')
        for layer, (n, secs) in sorted(by_layer.items(), key=lambda x: -x[1][0]):
            print(f'    {n:>4} x  {fmt_dur(secs):>16}  {layer}')
        print('\n  Per day:')
        for day, (n, secs) in by_day.items():
            print(f'    {day}  {n:>4} outages  {fmt_dur(secs)}')
        print('\n  Longest:')
        for r in sorted(rows, key=lambda r: -r['duration_s'])[:5]:
            print(f"    {r['start'][:19].replace('T', ' ')}  {fmt_dur(r['duration_s']):>14}  {r['layer']}")

    if args.udm:
        udm = load_udm(args.udm, first, last)
        tol = args.tolerance
        udm_matched = [u for u in udm if any(overlaps(u[0], u[1], s, e, tol) for s, e in internet)]
        for r, (s, e) in zip(rows, internet):
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
            w = csv.DictWriter(f, fieldnames=['start', 'end', 'duration_s', 'layer', 'udm_match'])
            w.writeheader()
            w.writerows(rows)
        print(f'\nWrote {len(rows)} outages to {args.csv}')


if __name__ == '__main__':
    main()
