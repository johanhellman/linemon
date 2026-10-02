#!/usr/bin/env python3
"""Spike #9 prototype (not product code): a notifier that runs apart from the monitor.

It tails events.csv (the monitor's fsynced record), turns outages into notices, merges
bursts, keeps them in a durable spool while sending fails, and sends them through a channel.
Standard library only. Run docs/spikes/009-notify/test_scenarios.py to see it work.
"""
import csv
import datetime as dt
import email.message
import json
import os
import smtplib
import urllib.request

INTERNET_HOSTS = ['1.1.1.1', '8.8.8.8', '9.9.9.9']


# ---- reading events.csv from where we stopped, whole lines only -------------------------

class EventTail:
    def __init__(self, events_path, state_path):
        self.path, self.state_path = events_path, state_path
        try:
            with open(state_path) as f:
                self.offset = json.load(f)['offset']
        except (FileNotFoundError, ValueError, KeyError):
            self.offset = None            # first run: start at the end, don't announce history

    def poll(self):
        size = os.path.getsize(self.path)
        if self.offset is None or size < self.offset:    # first run, or the file was replaced (trim.py)
            self.offset = size
            return []
        with open(self.path, 'rb') as f:
            f.seek(self.offset)
            data = f.read()
        whole = data[:data.rfind(b'\n') + 1]             # a line still being written waits
        self.pending_offset = self.offset + len(whole)
        rows = []
        for r in csv.reader(whole.decode(errors='replace').splitlines()):
            if len(r) >= 3 and r[0] != 'time':
                rows.append(r)
        return rows

    def commit(self):
        """Call after the notices from poll() are safely in the spool."""
        if self.offset is not None and hasattr(self, 'pending_offset'):
            self.offset = self.pending_offset
        tmp = self.state_path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'offset': self.offset}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)


# ---- outages from events: all three internet hosts down -> one outage --------------------

class OutageTracker:
    def __init__(self):
        self.down, self.since = set(), None

    def feed(self, row):
        """Yield ('down', time) or ('up', time, seconds) when the internet goes down or comes back."""
        t, target, kind = row[0], row[1], row[2]
        if target == 'monitor' and kind in ('start', 'stop'):
            self.down.clear()
            return
        if target not in INTERNET_HOSTS:
            return
        was = len(self.down) == 3
        (self.down.add if kind == 'down' else self.down.discard)(target)
        if len(self.down) == 3 and not was:
            self.since = dt.datetime.fromisoformat(t)
            yield ('down', self.since)
        elif was and len(self.down) < 3:
            yield ('up', dt.datetime.fromisoformat(t), (dt.datetime.fromisoformat(t) - self.since).total_seconds())


# ---- durable spool: at-least-once, torn-line safe ---------------------------------------

class Spool:
    def __init__(self, path):
        self.path = path

    def add(self, notice):
        with open(self.path, 'ab') as f:
            f.write((json.dumps(notice) + '\n').encode())
            f.flush()
            os.fsync(f.fileno())

    def pending(self):
        try:
            with open(self.path, 'rb') as f:
                lines = f.read().decode(errors='replace').splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue                  # cut short by a power cut
        return out

    def replace(self, notices):
        tmp = self.path + '.tmp'
        with open(tmp, 'wb') as f:
            for n in notices:
                f.write((json.dumps(n) + '\n').encode())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)


# ---- merging bursts ---------------------------------------------------------------------

def summarise(outages):
    """outages: [(start_iso, seconds)] -> one message."""
    total = sum(s for _, s in outages)
    span = f'{total / 60:.0f} min' if total >= 90 else f'{total:.0f} s'
    if len(outages) == 1:
        return f'Internet was down at {outages[0][0][11:16]} for {span}.'
    first, last = outages[0][0], outages[-1][0]
    return f'{len(outages)} internet outages between {first[11:16]} and {last[11:16]}, {span} down in total.'


def merge_burst(notices, quiet_s, now):
    """Notices of completed outages that are `quiet_s` old with nothing newer nearby become one message.

    Returns (messages_to_send, notices_still_waiting)."""
    if not notices:
        return [], []
    newest = max(n['at'] for n in notices)
    if now - newest < quiet_s:
        return [], notices                # still inside a burst: wait for it to settle
    return [summarise([(n['start'], n['seconds']) for n in notices])], []


# ---- quiet hours ------------------------------------------------------------------------

def in_quiet_hours(local_time, start='23:00', end='07:00'):
    now = local_time.strftime('%H:%M')
    return (start <= now or now < end) if start > end else (start <= now < end)


# ---- channels: each builds a request we can check against a fake server -----------------

def send_ntfy(url, topic, title, body, token=None, timeout=10):
    req = urllib.request.Request(f'{url.rstrip("/")}/{topic}', data=body.encode(), method='POST',
                                 headers={'Title': title, 'Tags': 'warning'})
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    return urllib.request.urlopen(req, timeout=timeout).status


def send_telegram(api_base, token, chat_id, text, timeout=10):
    req = urllib.request.Request(f'{api_base.rstrip("/")}/bot{token}/sendMessage', method='POST',
                                 data=json.dumps({'chat_id': chat_id, 'text': text}).encode(),
                                 headers={'Content-Type': 'application/json'})
    return urllib.request.urlopen(req, timeout=timeout).status


def send_email(host, port, sender, to, subject, body, user=None, password=None, starttls=False, timeout=10):
    msg = email.message.EmailMessage()
    msg['From'], msg['To'], msg['Subject'] = sender, to, subject
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=timeout) as s:
        if starttls:
            s.starttls()
        if user:
            s.login(user, password)
        s.send_message(msg)
