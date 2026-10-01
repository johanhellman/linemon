#!/usr/bin/env python3
"""Spike #5 benchmark (not product code). Run on the Pi 4 for the real numbers.

  python3 docs/spikes/005-bench/gen.py /tmp/year
  python3 docs/spikes/005-bench/bench.py /tmp/year

Times the current code paths and the prototypes in variants.py on the same data,
and checks that the prototypes give the same answers.
"""
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', '..'))
sys.path.insert(0, HERE)
import analyze   # noqa: E402
import variants  # noqa: E402
import web       # noqa: E402


def timed(label, fn, repeat=3):
    best, result = None, None
    for _ in range(repeat):
        t = time.perf_counter()
        result = fn()
        dt_ = time.perf_counter() - t
        best = dt_ if best is None else min(best, dt_)
    print(f'  {label:<58} {best * 1000:9.1f} ms')
    return result


def main():
    d = sys.argv[1]
    csv_path = os.path.join(d, 'minute.csv')
    print(f'minute.csv {os.path.getsize(csv_path) / 1e6:.0f} MB, python {sys.version.split()[0]}, sqlite {sqlite3.sqlite_version}')

    print('\n(a) current CSV code')
    end_a = timed('analyze.last_minute_end (full scan)', lambda: analyze.last_minute_end(d), 1)
    timed('web.status() (what the page runs every 10 s)', lambda: web.status(d), 1)

    print('\n(b) CSV, read only the tail')
    end_b = timed('last_minute_end_tail', lambda: variants.last_minute_end_tail(csv_path))
    since = end_a - __import__('datetime').timedelta(hours=24)
    old = web.minute_series(d, since)
    timed('rows of the last 24 h (bisect, then read)', lambda: sum(1 for _ in variants.minute_rows_since(csv_path, since)))
    assert end_a == end_b, (end_a, end_b)
    n_new = sum(1 for _ in variants.minute_rows_since(csv_path, since))
    print(f'  check: same end of data; {n_new} rows in the last 24 h ({len(old)} minutes in the page series)')

    print('\n(c) SQLite, WAL')
    db_path = os.path.join(d, 'bench.sqlite')
    for suffix in ('', '-wal', '-shm'):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)
    db = variants.sqlite_open(db_path)
    t = time.perf_counter()
    variants.sqlite_import_minute(db, csv_path)
    print(f'  {"one-off import of minute.csv":<58} {(time.perf_counter() - t) * 1000:9.1f} ms')
    print(f'  database size {os.path.getsize(db_path) / 1e6:.0f} MB (CSV is {os.path.getsize(csv_path) / 1e6:.0f} MB)')
    timed('last_minute_end (indexed)', lambda: variants.sqlite_last_minute_end(db))
    timed('rows of the last 24 h (indexed)', lambda: sum(1 for _ in variants.sqlite_rows_since(db, since)))
    assert variants.sqlite_last_minute_end(db) == end_a
    db.close()


if __name__ == '__main__':
    main()
