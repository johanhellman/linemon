#!/usr/bin/env python3
"""pull - copy linemon's data from the Pi to this computer or a NAS, keeping what a trim removed.

Run it on your own computer, by hand or daily from cron, so the evidence survives an SD
card failure. The Pi needs SSH and nothing else: it holds no credentials for your machine.

  pull.py pi@linemon.local:/var/lib/linemon ~/linemon-backup
  pull.py /mnt/pi-data ~/linemon-backup                       a local or mounted copy works too

It uses rsync over SSH. The data files only grow, so they are copied with
--append-verify where this computer's rsync has it (only what was added is sent, and the
whole file is checked); otherwise the files are compared and only changes are sent.

trim.py makes files smaller. A plain mirror would then replace the older backup copy with
the trimmed one and lose the deleted data from the backup too. So before copying, any file
that is smaller on the Pi than in the backup is moved to a dated folder next to the backup
(e.g. ~/linemon-backup-kept-20261005T090000), and the script says so. Files that are gone
from the Pi are never deleted from the backup.

Afterwards the copy is checked by running analyze.py on it (--no-check skips that).
rsync and ssh are not part of Python; without rsync the script stops and says so.
"""
import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GROWING = ['events.csv', 'minute.csv', 'captures.jsonl', 'path.log']  # only ever appended to, except by trim.py
TEMPORARY = ['*.tmp', '*.new', '*.trim']  # half-written files the monitor or trim.py are about to rename

# Run by python3 on the source machine (the Pi has it): every file's size, as JSON.
SIZES = r'''
import json, os, sys
root = sys.argv[1]
sizes = {}
for folder, _, files in os.walk(root):
    for name in files:
        path = os.path.join(folder, name)
        try:
            sizes[os.path.relpath(path, root)] = os.path.getsize(path)
        except OSError:
            pass  # replaced while we looked, e.g. health.json
print(json.dumps(sizes))
'''


def split_source(source):
    """'pi@host:/var/lib/linemon' -> ('pi@host', '/var/lib/linemon'); a local path -> (None, path)."""
    m = re.match(r'^([^/:]+):(.+)$', source)
    return (m.group(1), m.group(2)) if m else (None, source)


def source_sizes(host, path):
    cmd = ['ssh', host, 'python3', '-', shlex.quote(path)] if host else [sys.executable, '-', path]
    r = subprocess.run(cmd, input=SIZES, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f'Could not list {path}{" on " + host if host else ""}:\n{r.stderr.strip()}')
    return json.loads(r.stdout)


def local_sizes(root):
    sizes = {}
    for folder, _, files in os.walk(root):
        for name in files:
            path = os.path.join(folder, name)
            sizes[os.path.relpath(path, root)] = os.path.getsize(path)
    return sizes


def keep_shrunk(dest, theirs, stamp):
    """Move every backup file that is bigger than its source to a dated folder. Returns that folder or None."""
    ours = local_sizes(dest)
    shrunk = sorted(name for name, size in theirs.items() if name in ours and size < ours[name])
    if not shrunk:
        return None
    kept = f"{dest.rstrip('/')}-kept-{stamp}"
    for name in shrunk:
        os.makedirs(os.path.dirname(os.path.join(kept, name)), exist_ok=True)
        shutil.move(os.path.join(dest, name), os.path.join(kept, name))
        print(f'{name} got smaller on the source (trimmed?): kept the old copy in {kept}')
    return kept


def has_append_verify():
    r = subprocess.run(['rsync', '--help'], capture_output=True, text=True)
    return '--append-verify' in r.stdout + r.stderr


def rsync(src, dest, *options):
    r = subprocess.run(['rsync', '-a', *options, src, dest])
    if r.returncode not in (0, 24):  # 24: a file vanished while copying, e.g. health.json being replaced
        sys.exit(f'rsync failed (exit {r.returncode}). The backup may be incomplete; run the script again.')


def pull(source, dest, stamp):
    host, path = split_source(source)
    src = source.rstrip('/') + '/'
    os.makedirs(dest, exist_ok=True)
    kept = keep_shrunk(dest, source_sizes(host, path), stamp)
    excludes = [f'--exclude={p}' for p in TEMPORARY]
    if has_append_verify():
        rsync(src, dest, *excludes, *[f'--exclude=/{name}' for name in GROWING])
        rsync(src, dest, '--append-verify', *[f'--include=/{name}' for name in GROWING], '--exclude=*')
    else:
        rsync(src, dest, *excludes)  # never plain --append: it doesn't check the part already copied
    return kept


def check(dest):
    analyzer = os.path.join(ROOT, 'analyze.py')
    if not os.path.exists(analyzer):
        print('analyze.py not found next to tools/, so the copy was not checked.')
        return True
    r = subprocess.run([sys.executable, analyzer, dest], capture_output=True, text=True)
    if r.returncode != 0:
        print(f'analyze.py could not read the copy:\n{r.stderr.strip()}', file=sys.stderr)
        return False
    print('Checked: analyze.py reads the copy.')
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('source', help='user@host:/var/lib/linemon, or a local directory')
    p.add_argument('dest', help='the backup directory on this computer')
    p.add_argument('--no-check', action='store_true', help="don't run analyze.py on the copy")
    args = p.parse_args()
    if not shutil.which('rsync'):
        sys.exit('rsync is not installed on this computer. Install it (e.g. sudo apt install rsync) and run again.')
    dest = os.path.abspath(os.path.expanduser(args.dest))
    pull(args.source, dest, f'{dt.datetime.now():%Y%m%dT%H%M%S}')
    print(f'Pulled {args.source} to {dest}')
    if not args.no_check and not check(dest):
        sys.exit(1)


if __name__ == '__main__':
    main()
