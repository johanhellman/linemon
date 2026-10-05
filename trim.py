#!/usr/bin/env python3
"""Delete linemon data from before a given time, e.g. after a setup period.

  sudo systemctl stop linemon
  sudo python3 /opt/linemon/trim.py --before 2026-10-01T18:00
  sudo systemctl start linemon

Trims events.csv, minute.csv, captures.jsonl, path.log and the raw captures/
directories, and inserts a monitor 'start' event at the cut-off so the analyzer
and web page count the monitoring period from there. A time without a UTC offset
is taken as the Pi's local time.

A backup of the whole data directory is written first, next to it
(e.g. /var/lib/linemon-backup-20261001T183000.tar.gz) or in --backup-dir, unless
--no-backup is given. If the archive ends up on the same disk as the data, as it does
by default on a Pi's SD card, it protects against a mistaken trim but not against the
card failing, so trim.py says so: copy it off the card, or point --backup-dir at a
USB stick or network share.
It refuses to run while the linemon service is active, because the monitor keeps
its files open and would carry on writing to the deleted copies.
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile


def parse_time(s):
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.astimezone()  # naive = local time


def rewrite(path, keep_lines):
    """Atomically replace a file with the given lines, keeping its permissions."""
    tmp = path + '.trim'
    with open(tmp, 'w', newline='') as f:
        f.writelines(keep_lines)
    shutil.copymode(path, tmp)
    os.replace(tmp, path)


def trim_csv(path, column, cutoff, first_row=None):
    if not os.path.exists(path):
        return 0, 0
    with open(path, newline='') as f:
        rows = list(csv.reader(f))
    if not rows:
        return 0, 0
    header, body = rows[0], rows[1:]
    i = header.index(column)
    kept = [r for r in body if r and parse_time(r[i]) >= cutoff]
    buf = io.StringIO()
    csv.writer(buf).writerows([header] + ([first_row] if first_row else []) + kept)
    rewrite(path, buf.getvalue().splitlines(keepends=True))
    return len(body) - len(kept), len(kept)


def trim_lines(path, cutoff, time_of):
    if not os.path.exists(path):
        return 0, 0
    with open(path) as f:
        lines = f.readlines()
    kept = []
    for line in lines:
        try:
            if time_of(line) >= cutoff:
                kept.append(line)
        except (ValueError, KeyError, IndexError, TypeError):
            continue  # unreadable line, e.g. cut short by a power cut
    rewrite(path, kept)
    return len(lines) - len(kept), len(kept)


def trim_capture_dirs(data, cutoff):
    root = os.path.join(data, 'captures')
    removed = 0
    if os.path.isdir(root):
        for name in os.listdir(root):
            try:
                t = dt.datetime.strptime(name[:15], '%Y%m%dT%H%M%S').astimezone()
            except ValueError:
                continue
            if t < cutoff:
                shutil.rmtree(os.path.join(root, name))
                removed += 1
    return removed


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--before', required=True, help='delete everything before this time, e.g. 2026-10-01T18:00')
    p.add_argument('--data', default='/var/lib/linemon')
    p.add_argument('--backup-dir', help='where to write the backup archive (default: next to the data directory)')
    p.add_argument('--no-backup', action='store_true')
    p.add_argument('--force', action='store_true', help='run even if the linemon service is active')
    args = p.parse_args()

    cutoff = parse_time(args.before)
    data = os.path.abspath(args.data)
    active = subprocess.run(['systemctl', 'is-active', '-q', 'linemon'], capture_output=True).returncode == 0 \
        if shutil.which('systemctl') else False
    if active and not args.force:
        sys.exit('The linemon service is running. Stop it first: sudo systemctl stop linemon')
    backup_dir = os.path.abspath(args.backup_dir) if args.backup_dir else os.path.dirname(data.rstrip('/'))
    if not args.no_backup and not os.path.isdir(backup_dir):
        sys.exit(f'The backup directory {backup_dir} does not exist. Nothing was changed.')

    if not args.no_backup:
        name = f"{os.path.basename(data.rstrip('/'))}-backup-{dt.datetime.now():%Y%m%dT%H%M%S}.tar.gz"
        backup = os.path.join(backup_dir, name)
        with tarfile.open(backup, 'w:gz') as tar:
            tar.add(data, arcname=os.path.basename(data))
        print(f'Backup: {backup}')
        if os.stat(backup).st_dev == os.stat(data).st_dev:
            print('  It is on the same disk as the data, so it is lost too if the card fails.\n'
                  '  Copy it off, e.g. from your computer:\n'
                  f'    scp <user>@<monitor-address>:{backup} .\n'
                  '  or choose another disk next time with --backup-dir.')

    print(f'Deleting data from before {cutoff:%Y-%m-%d %H:%M:%S %z}')
    marker = [cutoff.isoformat(timespec='milliseconds'), 'monitor', 'start', '',
              f'data before {cutoff:%Y-%m-%d %H:%M} was deleted with trim.py']
    results = {
        'events.csv': trim_csv(os.path.join(data, 'events.csv'), 'time', cutoff, first_row=marker),
        'minute.csv': trim_csv(os.path.join(data, 'minute.csv'), 'minute', cutoff),
        'captures.jsonl': trim_lines(os.path.join(data, 'captures.jsonl'), cutoff,
                                     lambda line: parse_time(json.loads(line)['time'])),
        'path.log': trim_lines(os.path.join(data, 'path.log'), cutoff, lambda line: parse_time(line.split()[0])),
    }
    for name, (removed, kept) in results.items():
        print(f'  {name:<15} removed {removed:>6}, kept {kept:>6}')
    print(f'  {"captures/":<15} removed {trim_capture_dirs(data, cutoff):>6} raw capture folders')
    print('Done. Start the monitor again: sudo systemctl start linemon')


if __name__ == '__main__':
    main()
