"""Spike #5 prototypes (not product code): ways to read minute.csv without a full scan.

(b) tail reading: minute.csv is append-only and chronological, so the newest rows
    are at the end. Read from the end / bisect by byte offset. No format change.
(c) SQLite in WAL mode with the same columns, for comparison.
"""
import csv
import datetime as dt
import io
import os
import sqlite3


def parse_time(s):
    return dt.datetime.fromisoformat(s)


# ---- (b) tail reading ------------------------------------------------------

def last_minute_end_tail(path):
    """Same answer as analyze.last_minute_end, reading only the end of the file."""
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode(errors='ignore').splitlines()
    except FileNotFoundError:
        return None
    for line in reversed(tail):
        first = line.split(',', 1)[0]
        if first and first != 'minute':
            try:
                return parse_time(first) + dt.timedelta(minutes=1)
            except ValueError:
                continue   # a line cut short by a power cut
    return None


def _first_offset_since(f, size, since):
    """Byte offset of the first row at or after `since`, by bisecting on parsed times.

    Parsed, not compared as text: the UTC offset changes at daylight saving time,
    so ISO strings of local times don't sort by instant across the change.
    """
    lo, hi = 0, size
    while hi - lo > 8192:
        mid = (lo + hi) // 2
        f.seek(mid)
        f.readline()                      # skip the (probably partial) line we landed in
        line = f.readline().decode(errors='ignore')
        try:
            t = parse_time(line.split(',', 1)[0])
        except ValueError:
            hi = mid                      # unreadable (e.g. cut short): look earlier
            continue
        if t < since:
            lo = mid
        else:
            hi = mid
    return lo


def minute_rows_since(path, since):
    """Yield minute.csv rows (as dicts) with minute >= since, reading from the right place."""
    try:
        f = open(path, 'rb')
    except FileNotFoundError:
        return
    with f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = _first_offset_since(f, size, since)
        f.seek(start)
        if start:
            f.readline()                  # drop the partial line
        text = io.TextIOWrapper(f, newline='', errors='ignore')
        for r in csv.DictReader(text, fieldnames=['minute', 'target', 'sent', 'lost', 'rtt_avg_ms', 'rtt_max_ms']):
            if r['minute'] == 'minute':
                continue
            try:
                if parse_time(r['minute']) >= since:
                    yield r
            except ValueError:
                continue


# ---- (c) SQLite ------------------------------------------------------------

def sqlite_open(path, synchronous='FULL'):
    db = sqlite3.connect(path, isolation_level=None)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute(f'PRAGMA synchronous={synchronous}')
    db.execute('CREATE TABLE IF NOT EXISTS minute(ts REAL, minute TEXT, target TEXT, sent INT, lost INT, '
               'rtt_avg_ms REAL, rtt_max_ms REAL)')
    db.execute('CREATE INDEX IF NOT EXISTS minute_ts ON minute(ts)')
    db.execute('CREATE TABLE IF NOT EXISTS events(ts REAL, time TEXT, target TEXT, event TEXT, '
               'duration_s TEXT, detail TEXT)')
    return db


def sqlite_import_minute(db, csv_path):
    db.execute('BEGIN')
    with open(csv_path, newline='') as f:
        batch = []
        for r in csv.DictReader(f):
            batch.append((parse_time(r['minute']).timestamp(), r['minute'], r['target'], int(r['sent']), int(r['lost']),
                          float(r['rtt_avg_ms']) if r['rtt_avg_ms'] else None,
                          float(r['rtt_max_ms']) if r['rtt_max_ms'] else None))
            if len(batch) >= 50000:
                db.executemany('INSERT INTO minute VALUES (?,?,?,?,?,?,?)', batch)
                batch = []
        if batch:
            db.executemany('INSERT INTO minute VALUES (?,?,?,?,?,?,?)', batch)
    db.execute('COMMIT')


def sqlite_rows_since(db, since):
    cur = db.execute('SELECT minute, target, sent, lost FROM minute WHERE ts >= ? ORDER BY ts', (since.timestamp(),))
    for minute, target, sent, lost in cur:
        yield {'minute': minute, 'target': target, 'sent': sent, 'lost': lost}


def sqlite_last_minute_end(db):
    row = db.execute('SELECT minute FROM minute ORDER BY ts DESC LIMIT 1').fetchone()
    return parse_time(row[0]) + dt.timedelta(minutes=1) if row else None
