#!/usr/bin/env python3
"""Spike #9: run the prototype against fake servers. Nothing leaves this machine."""
import datetime as dt
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notify_proto as n  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=2))
T0 = dt.datetime(2026, 10, 2, 14, 0, tzinfo=TZ)


# ---- fake servers -----------------------------------------------------------------------

class Fake(BaseHTTPRequestHandler):
    log, line_up = [], True
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0))).decode()
        if not Fake.line_up:                       # the line is down: the request never gets through
            self.send_response(503); self.end_headers(); return
        Fake.log.append((self.path, dict(self.headers), body))
        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
    def log_message(self, *a): pass


class SMTP(socketserver.StreamRequestHandler):
    mails = []
    def handle(self):
        w = lambda s: self.wfile.write(s.encode() + b'\r\n')
        w('220 fake'); data = False; text = []
        for raw in self.rfile:
            line = raw.decode().rstrip('\r\n')
            if data:
                if line == '.': data = False; SMTP.mails.append('\n'.join(text)); w('250 queued')
                else: text.append(line)
            elif line.upper().startswith('EHLO'): w('250 fake')
            elif line.upper().startswith(('MAIL', 'RCPT')): w('250 ok')
            elif line.upper() == 'DATA': data = True; w('354 go')
            elif line.upper() == 'QUIT': w('221 bye'); return


def serve(handler, base=ThreadingHTTPServer):
    srv = base(('127.0.0.1', 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


http, smtp = serve(Fake), serve(SMTP, socketserver.ThreadingTCPServer)
base = f'http://127.0.0.1:{http.server_port}'

# ---- 1. the three channels: what each request looks like --------------------------------
assert n.send_ntfy(base, 'linemon-demo', 'Internet down', 'Down since 14:02', token='tk_demo') == 200
assert n.send_telegram(base, '123:DEMO', '42', 'Down since 14:02') == 200
n.send_email('127.0.0.1', smtp.server_address[1], 'linemon@example.invalid', 'me@example.invalid',
             'Internet down', 'Down since 14:02')
path, headers, body = Fake.log[0]
assert path == '/linemon-demo' and headers['Title'] == 'Internet down' and headers['Authorization'] == 'Bearer tk_demo'
path, headers, body = Fake.log[1]
assert path == '/bot123:DEMO/sendMessage' and json.loads(body) == {'chat_id': '42', 'text': 'Down since 14:02'}
assert 'Subject: Internet down' in SMTP.mails[0] and 'Down since 14:02' in SMTP.mails[0]
print('1. ntfy, Telegram and email requests are built correctly with the standard library')
Fake.log.clear()

# ---- 2. a timeout on an unreachable address costs what we set, not more ------------------
t = time.perf_counter()
try:
    n.send_ntfy('http://192.0.2.1', 'x', 't', 'b', timeout=2)
except (urllib.error.URLError, OSError, TimeoutError) as e:
    pass
elapsed = time.perf_counter() - t
print(f'2. send to an unreachable address with timeout=2 gave up after {elapsed:.1f} s')
assert 1.5 < elapsed < 4

# ---- 3. five outages in 20 minutes while the line is down; a restart; recovery -----------
d = tempfile.mkdtemp()
events, state, spool_path = (os.path.join(d, x) for x in ('events.csv', 'tail.json', 'spool.jsonl'))
open(events, 'w').write('time,target,event,duration_s,detail\r\n')
tail, spool, tracker = n.EventTail(events, state), n.Spool(spool_path), n.OutageTracker()
assert tail.poll() == []                           # first run starts at the end
tail.commit()

def write(offset_s, kind, hosts=n.INTERNET_HOSTS):
    with open(events, 'a', newline='') as f:
        for h in hosts:
            f.write(f'{(T0 + dt.timedelta(seconds=offset_s)).isoformat(timespec="milliseconds")},{h},{kind},,\r\n')

def pump():
    """One cycle of the notifier: read new events, spool completed outages."""
    global tail, tracker
    for row in tail.poll():
        for ev in tracker.feed(row):
            if ev[0] == 'up':
                spool.add({'start': (ev[1] - dt.timedelta(seconds=ev[2])).isoformat(), 'at': ev[1].timestamp(), 'seconds': ev[2]})
    tail.commit()

for i in range(5):                                  # outages of 40 s, every 4 minutes
    write(i * 240, 'down'); write(i * 240 + 40, 'up')
pump()
assert len(spool.pending()) == 5
# restart in the middle: new objects, state from disk
tail, tracker = n.EventTail(events, state), n.OutageTracker()
write(1300, 'down')                                 # a sixth outage starts and is still going
pump()
assert len(spool.pending()) == 5, 'an outage in progress must not be announced as finished'
print('3a. 5 outages spooled; a restart in the middle lost and repeated nothing')

last = max(x['at'] for x in spool.pending())
Fake.line_up = False
for when in (last + 10, last + 120, last + 300):    # quiet period passes, but the line is still down
    msgs, rest = n.merge_burst(spool.pending(), quiet_s=60, now=when) if when > last + 60 else ([], spool.pending())
    if msgs:
        try:
            n.send_ntfy(base, 'demo', 'Internet', msgs[0])
        except urllib.error.HTTPError:
            pass                                    # failed: the spool is not touched
assert len(spool.pending()) == 5 and not Fake.log
Fake.line_up = True                                 # the line is back
msgs, rest = n.merge_burst(spool.pending(), quiet_s=60, now=last + 400)
n.send_ntfy(base, 'demo', 'Internet', msgs[0]); spool.replace(rest)
print('3b. while sending failed nothing was lost; once it worked: ', repr(Fake.log[0][2]))
assert len(Fake.log) == 1 and spool.pending() == [] and '5 internet outages' in Fake.log[0][2]

# ---- 4. quiet hours, including past midnight ---------------------------------------------
at = lambda h, m: T0.replace(hour=h, minute=m)
assert n.in_quiet_hours(at(23, 30)) and n.in_quiet_hours(at(2, 0)) and not n.in_quiet_hours(at(7, 0)) and not n.in_quiet_hours(at(14, 0))
assert n.in_quiet_hours(at(13, 0), '12:00', '14:00') and not n.in_quiet_hours(at(15, 0), '12:00', '14:00')
print('4. quiet hours work across midnight and inside a day')
print('all scenarios passed')
