#!/usr/bin/env python3
"""browse - generate ordinary client traffic for a supervised line test, and log it.

Some ISPs want an "end user device" generating normal traffic while they watch the
line. This visits well-known web sites over HTTPS at random intervals, using the
system's DNS (normally the ISP router), and now and then downloads a larger file,
like a video or an update would. Every request is logged, so failures can be lined
up against linemon's outages afterwards.

  browse.py --out browse.csv --duration 10800      run for 3 hours
  browse.py --summary browse.csv                   summarise a finished run

Each request's result is one of: ok; dns_failed (the name didn't resolve); timeout;
failed (refused or broken connection); intercepted (something other than the site
answered: its certificate didn't check out, e.g. the ISP router's own "no
connection" page while the line is down). The address each name resolved to is
logged too, so an intercepted request to a private address points at the router.

This is a test tool, not part of the monitor: linemon itself only observes and
never loads the line. Run it only for a test you have agreed with the ISP. With the
defaults it averages under 1 Mbit/s (a 20 MB download every 5 minutes plus pages).
"""
import argparse
import csv
import datetime as dt
import ipaddress
import os
import random
import signal
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SITES = [
    'https://www.google.es/',
    'https://www.elpais.com/',
    'https://www.rtve.es/',
    'https://www.20minutos.es/',
    'https://www.marca.com/',
    'https://es.wikipedia.org/wiki/Especial:Aleatoria',
    'https://www.youtube.com/',
    'https://www.amazon.es/',
    'https://www.bbc.com/',
    'https://github.com/',
    'https://www.microsoft.com/es-es',
    'https://www.apple.com/es/',
]
DOWNLOAD = 'https://speed.cloudflare.com/__down?bytes={bytes}'
USER_AGENT = ('Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/124.0 Safari/537.36')
FIELDS = ['time', 'kind', 'url', 'result', 'http_status', 'address', 'dns_ms', 'first_byte_ms',
          'total_ms', 'bytes', 'mbit_s', 'error']
INCIDENT_GAP_S = 45  # failures further apart than this are separate incidents (pages are 5-30 s apart)


def now():
    return dt.datetime.now().astimezone()


def fetch(url, kind, timeout, max_bytes):
    """One request. Returns a log row; never raises."""
    row = {'time': now().isoformat(timespec='seconds'), 'kind': kind, 'url': url, 'result': 'ok',
           'http_status': '', 'address': '', 'dns_ms': '', 'first_byte_ms': '', 'total_ms': '', 'bytes': 0,
           'mbit_s': '', 'error': ''}
    host = urllib.parse.urlsplit(url).hostname
    t0 = time.monotonic()
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        row['dns_ms'] = round((time.monotonic() - t0) * 1000)
        # the system resolver caches this answer, so urlopen below connects to the same address
        row['address'] = next((i[4][0] for i in infos if i[0] == socket.AF_INET), infos[0][4][0])
    except OSError as e:
        row.update(result='dns_failed', error=str(e)[:200], total_ms=round((time.monotonic() - t0) * 1000))
        return row
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept-Language': 'es-ES,es;q=0.9'})
    t1 = time.monotonic()
    try:
        try:
            resp = urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context())
        except urllib.error.HTTPError as e:  # the site answered: the line works, even if it refuses robots
            resp = e
        with resp:
            row['http_status'] = resp.status if hasattr(resp, 'status') else resp.code
            row['first_byte_ms'] = round((time.monotonic() - t1) * 1000)
            while row['bytes'] < max_bytes:
                chunk = resp.read(65536)
                if not chunk:
                    break
                row['bytes'] += len(chunk)
    except (urllib.error.URLError, OSError, ssl.SSLError) as e:
        reason = getattr(e, 'reason', e)
        if isinstance(reason, ssl.SSLCertVerificationError):
            result = 'intercepted'  # e.g. the router's own page with its self-signed certificate
        elif isinstance(reason, (socket.timeout, TimeoutError)) or 'timed out' in str(reason):
            result = 'timeout'
        else:
            result = 'failed'
        row.update(result=result, error=str(reason)[:200])
    total = time.monotonic() - t1
    row['total_ms'] = round(total * 1000)
    if kind == 'download' and row['bytes'] and total > 0:
        row['mbit_s'] = round(row['bytes'] * 8 / total / 1e6, 1)
    return row


def run(args):
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    new = not os.path.exists(args.out) or os.path.getsize(args.out) == 0
    if not new:
        with open(args.out, newline='') as f:
            header = next(csv.reader(f), [])
        if header != FIELDS:  # written by an older version: keep it, start a new file next to it
            base, ext = os.path.splitext(args.out)
            args.out = f'{base}-{now():%Y%m%dT%H%M%S}{ext or ".csv"}'
            new = True
            print(f'The existing log has an older format; logging to {args.out} instead', flush=True)
    stop = {'now': False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    end = time.monotonic() + args.duration
    next_download = time.monotonic() + random.uniform(30, 90)
    counts = {'ok': 0, 'other': 0}
    with open(args.out, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        print(f'{now():%H:%M:%S} browsing until {now() + dt.timedelta(seconds=args.duration):%H:%M:%S}, '
              f'logging to {args.out}', flush=True)
        while not stop['now'] and time.monotonic() < end:
            if time.monotonic() >= next_download:
                row = fetch(DOWNLOAD.format(bytes=args.download_bytes), 'download', args.timeout * 4,
                            args.download_bytes)
                next_download = time.monotonic() + args.download_every
            else:
                row = fetch(random.choice(SITES), 'page', args.timeout, args.page_bytes)
            w.writerow(row)
            f.flush()
            counts['ok' if row['result'] == 'ok' else 'other'] += 1
            if row['result'] != 'ok' or args.verbose:
                print(f"{row['time'][11:19]} {row['kind']:<8} {row['result']:<11} {row['url'][:50]} "
                      f"{row['address']} {row['error']}", flush=True)
            wait = random.uniform(args.min_wait, args.max_wait)
            while wait > 0 and not stop['now'] and time.monotonic() < end:
                time.sleep(min(1, wait))
                wait -= 1
    print(f'{now():%H:%M:%S} done: {counts["ok"]} requests ok, {counts["other"]} failed', flush=True)


def summary(path):
    with open(path, newline='') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print('No requests logged.')
        return
    fails = [r for r in rows if r['result'] != 'ok']
    pages = [r for r in rows if r['kind'] == 'page' and r['result'] == 'ok' and r['first_byte_ms']]
    downloads = [float(r['mbit_s']) for r in rows if r['kind'] == 'download' and r['mbit_s']]
    print(f"{rows[0]['time']} to {rows[-1]['time']}: {len(rows)} requests, {len(fails)} failed")
    if pages:
        ttfb = sorted(int(r['first_byte_ms']) for r in pages)
        print(f'  page response: median {ttfb[len(ttfb) // 2]} ms, slowest {ttfb[-1]} ms')
    if downloads:
        print(f'  downloads: {len(downloads)}, median {sorted(downloads)[len(downloads) // 2]} Mbit/s, '
              f'slowest {min(downloads)} Mbit/s')
    # Group failures into incidents. A request that succeeds, or a gap longer than the
    # 5-30 s between requests allows for, means the line came back in between.
    incidents, current = [], None
    for r in rows:
        if r['result'] == 'ok':
            current = None
            continue
        t = dt.datetime.fromisoformat(r['time'])
        if current is None or (t - current['last']).total_seconds() > INCIDENT_GAP_S:
            current = {'first': t, 'last': t, 'rows': []}
            incidents.append(current)
        current['last'] = t
        current['rows'].append(r)
    for i in incidents:
        kinds = sorted({r['result'] for r in i['rows']})
        addresses = sorted({r.get('address') or '' for r in i['rows']} - {''})
        local = [a for a in addresses if is_private(a)]
        note = f"; answered by local address {', '.join(local)}" if local else ''
        print(f"  failures {i['first']:%d/%m %H:%M:%S} - {i['last']:%H:%M:%S}: {len(i['rows'])} requests "
              f"({', '.join(kinds)}){note}")


def is_private(address):
    try:
        return ipaddress.ip_address(address).is_private
    except ValueError:
        return False


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--out', default='browse.csv', help='CSV log to append to')
    p.add_argument('--duration', type=int, default=3 * 3600, help='seconds to run')
    p.add_argument('--min-wait', type=float, default=5, help='minimum seconds between page visits')
    p.add_argument('--max-wait', type=float, default=30, help='maximum seconds between page visits')
    p.add_argument('--download-every', type=int, default=300, help='seconds between larger downloads')
    p.add_argument('--download-bytes', type=int, default=20_000_000, help='size of each larger download')
    p.add_argument('--page-bytes', type=int, default=2_000_000, help='read at most this much of a page')
    p.add_argument('--timeout', type=float, default=15, help='seconds before a page counts as timed out')
    p.add_argument('--verbose', action='store_true', help='print every request, not only failures')
    p.add_argument('--summary', metavar='CSV', help='summarise a finished run instead of browsing')
    args = p.parse_args()
    if args.summary:
        summary(args.summary)
    else:
        run(args)


if __name__ == '__main__':
    main()
