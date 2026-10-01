#!/usr/bin/env python3
"""Spike #5 prototype (not product code): write a synthetic year of linemon data.

  gen.py OUT_DIR [--days 365]

Same files and formats as the monitor writes (events.csv, minute.csv,
captures.jsonl). All addresses are documentation addresses; the data is random.
"""
import argparse
import csv
import datetime as dt
import json
import os
import random

TARGETS = ['link', 'gateway', 'isp_hop1', 'isp_hop2', '1.1.1.1', '8.8.8.8', '9.9.9.9',
           'dns_gateway', 'dns_1.1.1.1']
HOSTS = ['1.1.1.1', '8.8.8.8', '9.9.9.9']


def main():
    p = argparse.ArgumentParser()
    p.add_argument('out')
    p.add_argument('--days', type=int, default=365)
    args = p.parse_args()
    os.makedirs(args.out, exist_ok=True)
    rnd = random.Random(5)
    tz = dt.timezone(dt.timedelta(hours=2))
    start = dt.datetime(2025, 10, 1, 0, 0, tzinfo=tz)
    end = start + dt.timedelta(days=args.days)

    # outages: about one a week on the internet hosts (most of them short), plus
    # hop/DNS blips and a few monitor restarts
    outages = []   # (start, seconds, targets)
    t = start + dt.timedelta(hours=3)
    while t < end:
        t += dt.timedelta(hours=rnd.expovariate(1 / 150))
        secs = max(4, int(rnd.lognormvariate(4.5, 1.6)))
        layer = rnd.choice([HOSTS, HOSTS + ['isp_hop1'], HOSTS + ['isp_hop1', 'gateway'], ['dns_1.1.1.1']])
        outages.append((t, secs, layer))
    blips = []
    for _ in range(args.days * 2):
        s = start + dt.timedelta(seconds=rnd.randrange(args.days * 86400))
        blips.append((s, rnd.randint(3, 20), [rnd.choice(TARGETS[1:4] + TARGETS[7:])]))
    restarts = sorted(start + dt.timedelta(days=rnd.randrange(args.days), hours=rnd.randrange(24)) for _ in range(6))

    iso = lambda x: x.isoformat(timespec='milliseconds')
    rows = [(start, 'monitor', 'start', '', 'eth0 gateway 192.0.2.1')]
    for r in restarts:
        rows.append((r - dt.timedelta(seconds=1), 'monitor', 'stop', '', ''))
        rows.append((r + dt.timedelta(seconds=9), 'monitor', 'start', '', 'eth0 gateway 192.0.2.1'))
    loss = {}   # (minute iso, target) -> lost probes
    for s, secs, targets in outages + blips:
        for tg in targets:
            rows.append((s, tg, 'down', '', '192.0.2.1'))
            rows.append((s + dt.timedelta(seconds=secs), tg, 'up', secs, '192.0.2.1'))
            cur = s
            while cur < s + dt.timedelta(seconds=secs):
                m = cur.replace(second=0, microsecond=0)
                nxt = min(m + dt.timedelta(minutes=1), s + dt.timedelta(seconds=secs))
                key = (m.isoformat(), tg)
                loss[key] = min(60, loss.get(key, 0) + int((nxt - cur).total_seconds()))
                cur = nxt
    rows.sort(key=lambda r: r[0])
    with open(os.path.join(args.out, 'events.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['time', 'target', 'event', 'duration_s', 'detail'])
        for t, tg, kind, dur, detail in rows:
            w.writerow([iso(t), tg, kind, dur, detail])

    with open(os.path.join(args.out, 'minute.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['minute', 'target', 'sent', 'lost', 'rtt_avg_ms', 'rtt_max_ms'])
        m = start
        while m < end:
            key = m.isoformat()
            for tg in TARGETS:
                lost = loss.get((key, tg), 0)
                rtt = '' if tg in ('link', 'isp_hop1', 'isp_hop2') else f'{rnd.uniform(1, 25):.1f}'
                w.writerow([key, tg, 60, lost, rtt, '' if not rtt else f'{float(rtt) + rnd.uniform(0, 20):.1f}'])
            m += dt.timedelta(minutes=1)

    with open(os.path.join(args.out, 'captures.jsonl'), 'w') as f:
        c = start
        while c < end:
            f.write(json.dumps({
                'time': iso(c), 'reason': 'periodic', 'ok': True,
                'summary': 'fibre O5 operational · signal OK · internet up', 'uptime_s': 1000000,
                'details': {'gpon_state': 5, 'los': False, 'conn_status': 'Connected', 'ip': '192.0.2.50',
                            'conn_error': 'ERROR_NONE'}}) + '\n')
            c += dt.timedelta(seconds=300)
    for name in ('events.csv', 'minute.csv', 'captures.jsonl'):
        print(f'{name:<15} {os.path.getsize(os.path.join(args.out, name)) / 1e6:8.1f} MB')


if __name__ == '__main__':
    main()
