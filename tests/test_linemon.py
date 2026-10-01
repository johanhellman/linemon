"""Tests for linemon. Run from the repository root: python3 -m unittest discover tests"""
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, 'tests', 'fixtures')
sys.path.insert(0, ROOT)

import analyze  # noqa: E402
import linemon  # noqa: E402


def load_router_module():
    spec = importlib.util.spec_from_file_location('zte_livebox', os.path.join(ROOT, 'routers', 'zte_livebox.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


class PingParsing(unittest.TestCase):
    """iputils ping output, as on Raspberry Pi OS."""

    def check(self, output, expected, ttl=None):
        original = linemon.sh
        linemon.sh = lambda cmd, timeout=5: output
        try:
            self.assertEqual(linemon.ping('1.1.1.1', 'eth0', ttl=ttl), expected)
        finally:
            linemon.sh = original

    def test_echo_reply(self):
        self.check('PING 1.1.1.1 (1.1.1.1) from 192.168.1.50 eth0: 56(84) bytes of data.\n'
                   '64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=4.12 ms\n', (True, 4.12, '1.1.1.1'))

    def test_ttl_exceeded_counts_only_for_hop_probes(self):
        out = 'PING 1.1.1.1 (1.1.1.1) from 192.168.1.50 eth0: 56(84) bytes of data.\n' \
              'From 10.0.0.1 icmp_seq=1 Time to live exceeded\n'
        self.check(out, (True, None, '10.0.0.1'), ttl=4)
        self.check(out, (False, None, None))

    def test_no_reply(self):
        self.check('1 packets transmitted, 0 received, 100% packet loss\n', (False, None, None))


class LiveboxParsing(unittest.TestCase):
    def setUp(self):
        self.mod = load_router_module()

    def summary(self, led, wan):
        return self.mod.summarise(self.mod.parse_xml(fixture(led)), self.mod.parse_xml(fixture(wan)))

    def test_healthy(self):
        s = self.summary('led_ok.xml', 'wan_ok.xml')
        self.assertTrue(s['ok'])
        self.assertEqual(s['summary'], 'fibre O5 operational · signal OK · internet up')
        self.assertEqual(s['uptime_s'], 1060)
        self.assertEqual(s['details']['vlan'], '20')

    def test_fibre_up_but_no_ip(self):
        s = self.summary('led_no_ip.xml', 'wan_no_ip.xml')
        self.assertFalse(s['ok'])
        self.assertEqual(s['summary'], 'fibre O5 operational · signal OK · no IP (ERROR_NO_ANSWER)')
        self.assertIsNone(s['uptime_s'])

    def test_loss_of_signal(self):
        s = self.summary('led_los.xml', 'wan_no_ip.xml')
        self.assertFalse(s['ok'])
        self.assertTrue(s['summary'].startswith('fibre O1 initial · NO SIGNAL (LOS)'))

    def test_router_error_response(self):
        with self.assertRaises(ValueError):
            self.mod.parse_xml('<ajax_response_xml_root><IF_ERRORSTR>SessionTimeout</IF_ERRORSTR></ajax_response_xml_root>')


class FakeLivebox(BaseHTTPRequestHandler):
    """Implements the router's login protocol as its own login page does."""
    password = 'correct horse'
    login_token = '24857193'
    logins = []
    logouts = []
    page = None
    token_counter = 0
    current_token = None

    def log_message(self, *args):
        pass

    def reply(self, body, ctype='text/xml', cookie=None):
        data = body.encode()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def logged_in(self):
        return 'SID=good' in self.headers.get('Cookie', '')

    def page_token(self):
        """Like the real router, every page carries a new session token; only the latest is valid."""
        FakeLivebox.token_counter += 1
        FakeLivebox.current_token = f'T{FakeLivebox.token_counter}'
        escaped = ''.join(f'\\x{ord(c):02x}' for c in FakeLivebox.current_token)
        return f'<script>var _sessionTmpToken = "{escaped}";</script>'

    def session_timeout(self):
        self.reply('<ajax_response_xml_root><IF_ERRORSTR>SessionTimeout</IF_ERRORSTR></ajax_response_xml_root>')

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        tag = q.get('_tag', [''])[0]
        if not q:
            if self.logged_in():
                FakeLivebox.page = 'home'
                self.reply(self.page_token(), 'text/html')
            else:
                self.reply('<script>var _sessionTmpToken = "";</script>', 'text/html', cookie='SID=new; path=/')
        elif tag == 'login_entry':
            self.reply(json.dumps({'sess_token': 'S1', 'lockingTime': 0}), 'application/json')
        elif tag == 'login_token':
            self.reply(f'<ajax_response_xml_root>{self.login_token}</ajax_response_xml_root>')
        elif not self.logged_in():
            self.session_timeout()
        elif q.get('_type') == ['menuView']:
            FakeLivebox.page = tag
            self.reply(self.page_token(), 'text/html')
        # Like the real router, a page's data is only served while the session is on that page.
        elif tag == 'osp_led_status_orange_lua.lua':
            self.reply(fixture('led_ok.xml')) if FakeLivebox.page == 'vmenu-ledstatus' else self.session_timeout()
        elif tag == 'wan_internetstatus_lua.lua':
            self.reply(fixture('wan_ok.xml')) if FakeLivebox.page == 'home' else self.session_timeout()
        else:
            self.session_timeout()

    def do_POST(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
        tag = q['_tag'][0]
        if tag == 'login_entry':
            expected = hashlib.sha256((self.password + self.login_token).encode()).hexdigest()
            ok = (form.get('Password') == [expected] and form.get('Username') == ['admin']
                  and form.get('action') == ['login'] and form.get('_sessionTOKEN') == ['S1'])
            FakeLivebox.logins.append(ok)
            # Like the real router, the reply carries a session token, also when the login fails.
            self.reply(json.dumps({'sess_token': 'SECRET-SESSION-TOKEN', 'login_need_refresh': ok,
                                   'loginErrMsg': '' if ok else 'wrong password', 'lockingTime': 0}),
                       'application/json', cookie='SID=good; path=/' if ok else None)
        elif tag == 'logout_entry':
            ok = form.get('_sessionTOKEN') == [FakeLivebox.current_token]
            FakeLivebox.logouts.append(ok)
            self.reply(json.dumps({'need_refresh': ok}), 'application/json')


class LiveboxCapture(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeLivebox)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.mkdtemp()
        FakeLivebox.logins, FakeLivebox.logouts = [], []

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        shutil.rmtree(self.tmp)

    def capture(self, password, reason='outage-start'):
        conf = os.path.join(self.tmp, 'router.conf')
        with open(conf, 'w') as f:
            f.write(f'[router]\nhost = 127.0.0.1:{self.server.server_port}\nusername = admin\npassword = {password}\n')
        capture_dir = os.path.join(self.tmp, 'capture')
        os.makedirs(capture_dir, exist_ok=True)
        os.environ.update(LINEMON_ROUTER_CONF=conf, LINEMON_DATA=self.tmp,
                          LINEMON_CAPTURE_DIR=capture_dir, LINEMON_REASON=reason)
        return load_router_module().capture(), capture_dir

    def test_login_read_logout(self):
        result, capture_dir = self.capture('correct horse')
        self.assertEqual(FakeLivebox.logins, [True])
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['uptime_s'], 1060)
        self.assertEqual(FakeLivebox.logouts, [True])  # logged out with the latest session token
        self.assertEqual(sorted(os.listdir(capture_dir)), ['led_status.xml', 'wan_status.xml'])

    def test_periodic_capture_keeps_no_raw_files(self):
        _, capture_dir = self.capture('correct horse', reason='periodic')
        self.assertEqual(os.listdir(capture_dir), [])

    def test_wrong_password_backs_off(self):
        result, _ = self.capture('wrong')
        self.assertIn('login rejected', result['error'])
        # the router's reply (with its session token) must not reach anything the web page shows (issue #3)
        self.assertNotIn('SECRET', result['error'])
        with open(os.path.join(self.tmp, 'router-login-failed')) as f:
            self.assertNotIn('SECRET', f.read())
        result, _ = self.capture('correct horse')
        self.assertIn('not retrying until', result['error'])
        self.assertEqual(FakeLivebox.logins, [False])  # no second attempt

    def test_router_unreachable(self):
        self.server.shutdown()
        self.server.server_close()
        result, _ = self.capture('correct horse')
        self.assertIn('not reachable', result['error'])
        self.server = None  # already stopped


class HookLoop(unittest.TestCase):
    """The monitor runs the hook at outage start, during, at the end and periodically."""

    def test_reasons(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        hook = os.path.join(tmp, 'hook.py')
        with open(hook, 'w') as f:
            f.write('import json, os\n'
                    'open(os.path.join(os.environ["LINEMON_CAPTURE_DIR"], "raw.txt"), "w").write("x")\n'
                    'print(json.dumps({"ok": False, "summary": os.environ["LINEMON_REASON"], "uptime_s": 10}))\n')
        args = types.SimpleNamespace(iface='eth0', data=tmp, interval=1, threshold=3,
                                     hook=f'{sys.executable} {hook}', hook_during=2, hook_interval=3, hook_timeout=10)
        original_gateway = linemon.default_gateway
        linemon.default_gateway = lambda iface: '192.168.1.1'
        try:
            mon = linemon.Monitor(args)
        finally:
            linemon.default_gateway = original_gateway
        self.addCleanup(mon.minute_f.close)
        self.addCleanup(mon.events_f.close)
        thread = threading.Thread(target=mon.hook_loop, daemon=True)
        thread.start()
        time.sleep(3.5)                                       # line up: one periodic capture
        mon.down.update({h: True for h in linemon.INTERNET_HOSTS})
        time.sleep(3.5)                                       # outage: start, then ongoing
        mon.down.update({h: False for h in linemon.INTERNET_HOSTS})
        time.sleep(1.5)                                       # recovery: end
        mon.stop.set()
        thread.join()
        captures = analyze.load_captures(tmp)
        reasons = [c['reason'] for c in captures]
        self.assertEqual(reasons[:4], ['periodic', 'outage-start', 'outage-ongoing', 'outage-end'], reasons)
        self.assertEqual(captures[1]['summary'], 'outage-start')
        self.assertTrue(os.path.exists(os.path.join(tmp, captures[1]['files'], 'raw.txt')))


class SafeErrors(unittest.TestCase):
    """Nothing from a hook's stderr or an exception reaches the unauthenticated page (issue #3)."""

    def test_hook_stderr_stays_out_of_captures(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        hook = os.path.join(tmp, 'hook.py')
        with open(hook, 'w') as f:
            f.write('import sys\nprint("password=SECRET-PASSWORD", file=sys.stderr)\nsys.exit(1)\n')
        args = types.SimpleNamespace(iface='eth0', data=tmp, interval=1, threshold=3,
                                     hook=f'{sys.executable} {hook}', hook_during=30, hook_interval=0, hook_timeout=10)
        original = linemon.default_gateway
        linemon.default_gateway = lambda iface: '192.168.1.1'
        try:
            mon = linemon.Monitor(args)
        finally:
            linemon.default_gateway = original
        self.addCleanup(mon.minute_f.close)
        self.addCleanup(mon.events_f.close)
        import contextlib
        import io
        journal = io.StringIO()
        with contextlib.redirect_stderr(journal):
            mon.run_hook('outage-start')
        with open(os.path.join(tmp, 'captures.jsonl')) as f:
            stored = f.read()
        self.assertIn('gave no result', stored)
        self.assertNotIn('SECRET', stored)
        self.assertIn('SECRET', journal.getvalue())  # but the owner can still see it in the journal

    def api(self, data_dir):
        import io
        import contextlib
        import urllib.error
        import urllib.request
        import web
        web.Handler.data_dir = data_dir
        server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/api/status') as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()

    def test_api_errors_are_generic(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.assertEqual(self.api(tmp), (503, '{"error": "no data yet"}'))
        os.mkdir(os.path.join(tmp, 'events.csv'))  # unreadable: the error text names the path
        code, body = self.api(tmp)
        self.assertEqual(code, 500)
        self.assertNotIn(tmp, body)
        self.assertNotIn('events.csv', body)
        self.assertIn('status unavailable', body)


class Analysis(unittest.TestCase):
    def test_session_starts_from_uptime(self):
        t0 = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
        caps = [{'time': t0, 'uptime_s': 600},                               # session started 11:50
                {'time': t0 + dt.timedelta(minutes=5), 'uptime_s': 903},     # same session (3 s drift)
                {'time': t0 + dt.timedelta(minutes=10), 'uptime_s': None},   # no IP
                {'time': t0 + dt.timedelta(minutes=15), 'uptime_s': 120}]    # new session 12:13
        starts = analyze.session_starts(caps)
        self.assertEqual([s['start'].strftime('%H:%M') for s in starts], ['11:50', '12:13'])

    def test_router_during_outage(self):
        t0 = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc)
        caps = [{'time': t0, 'reason': 'periodic', 'summary': 'all fine'},
                {'time': t0 + dt.timedelta(seconds=63), 'reason': 'outage-start', 'summary': 'no IP'},
                {'time': t0 + dt.timedelta(seconds=93), 'reason': 'outage-ongoing', 'summary': 'no IP'},
                {'time': t0 + dt.timedelta(seconds=100), 'reason': 'outage-ongoing', 'error': 'timed out'}]
        text = analyze.router_during(caps, t0 + dt.timedelta(seconds=60), t0 + dt.timedelta(seconds=120))
        self.assertEqual(text, 'no IP; capture failed: timed out')


class OngoingOutages(unittest.TestCase):
    """An internet outage still in progress must be reported, not dropped (issue #2)."""

    T0 = dt.datetime(2026, 10, 1, 20, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))

    def at(self, seconds):
        return self.T0 + dt.timedelta(seconds=seconds)

    def test_intersect_all_with_open_intervals(self):
        lists = [[(self.at(0), None, True)], [(self.at(2), None, True)], [(self.at(1), None, True)]]
        self.assertEqual(analyze.intersect_all(lists), [])  # without an end time, open ones are skipped
        self.assertEqual(analyze.intersect_all(lists, open_end=self.at(60)), [(self.at(2), self.at(60), True)])
        # one host has already recovered: the overlap is a finished outage, not an ongoing one
        lists[1] = [(self.at(2), self.at(30), False)]
        self.assertEqual(analyze.intersect_all(lists, open_end=self.at(60)), [(self.at(2), self.at(30), False)])

    def make_data(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with open(os.path.join(tmp, 'events.csv'), 'w') as f:
            f.write('time,target,event,duration_s,detail\n'
                    f'{self.at(0).isoformat()},monitor,start,,iface=eth0 gateway=192.168.1.1\n'
                    + ''.join(f'{self.at(600).isoformat()},{h},down,,\n' for h in linemon.INTERNET_HOSTS)
                    + f'{self.at(600).isoformat()},isp_hop1,down,,\n')
        with open(os.path.join(tmp, 'minute.csv'), 'w') as f:  # data runs until 20:12
            f.write('minute,target,sent,lost,rtt_avg_ms,rtt_max_ms\n'
                    f'{self.at(660).isoformat()},link,60,0,,\n')
        return tmp

    def test_cli_and_csv_include_the_ongoing_outage(self):
        import csv
        import subprocess
        data = self.make_data()
        out_csv = os.path.join(data, 'out.csv')
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'analyze.py'), data, '--csv', out_csv],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Internet outages (all three hosts down): 1, 2 min 0 s in total (1 still in progress', r.stdout)
        self.assertIn('first ISP hop unreachable (access network)  (still in progress)', r.stdout)
        with open(out_csv) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['end'], rows[0]['ongoing'], rows[0]['duration_s']), ('', 'yes', '120'))

    def test_web_lists_the_ongoing_outage(self):
        import web
        d = web.status(self.make_data())
        self.assertEqual(d['internet']['total'], 1)
        row = d['internet']['outages'][0]
        self.assertTrue(row['ongoing'])
        self.assertIsNone(row['end'])
        self.assertEqual(d['internet_down_since'], row['start'])


class TornLastLine(unittest.TestCase):
    """A power cut can leave the last line of a CSV half written (issue #27)."""

    T0 = '2026-10-01T20:00:00.000+02:00'
    EVENTS_HEADER = 'time,target,event,duration_s,detail\r\n'
    MINUTE_HEADER = 'minute,target,sent,lost,rtt_avg_ms,rtt_max_ms\r\n'

    def data(self, events=None, minute=None):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for name, text in (('events.csv', events), ('minute.csv', minute)):
            if text is not None:
                with open(os.path.join(tmp, name), 'w', newline='') as f:
                    f.write(text)
        return tmp

    def monitor(self, tmp):
        args = types.SimpleNamespace(iface='eth0', data=tmp, interval=1, threshold=3,
                                     hook=None, hook_during=30, hook_interval=300, hook_timeout=30)
        original = linemon.default_gateway
        linemon.default_gateway = lambda iface: '192.168.1.1'
        try:
            mon = linemon.Monitor(args)
        finally:
            linemon.default_gateway = original
        self.addCleanup(mon.minute_f.close)
        self.addCleanup(mon.events_f.close)
        return mon

    def test_next_event_is_not_glued_to_the_torn_line(self):
        tmp = self.data(events=self.EVENTS_HEADER + f'{self.T0},gateway,down,,192.168.1.1\r\n'
                                                    f'2026-10-01T20:00:05.000+02:00,dns_1.')  # cut short, no newline
        mon = self.monitor(tmp)
        mon.event(analyze.parse_time('2026-10-01T20:05:00.000+02:00'), 'monitor', 'start', '', 'eth0')
        rows = analyze.load_events(tmp)
        self.assertEqual([(r['target'], r['event']) for r in rows], [('gateway', 'down'), ('monitor', 'start')])
        with open(os.path.join(tmp, 'events.csv'), newline='') as f:
            self.assertEqual(f.read().count('\r\n'), 4)  # header, down, the torn line, start

    def test_a_clean_file_is_left_alone(self):
        text = self.EVENTS_HEADER + f'{self.T0},monitor,start,,eth0\r\n'
        tmp = self.data(events=text)
        self.monitor(tmp)
        with open(os.path.join(tmp, 'events.csv'), newline='') as f:
            self.assertEqual(f.read(), text)

    def test_torn_lines_do_not_break_the_readers(self):
        import web
        # cut inside the time, inside the target, and a complete row after them
        tmp = self.data(
            events=self.EVENTS_HEADER + f'{self.T0},gateway,down,,\r\n2026-10-01T2\r\n2026-10-01T20:00:09.000+02:00,dns_\r\n'
                                        f'2026-10-01T20:01:00.000+02:00,gateway,up,60,\r\n',
            minute=self.MINUTE_HEADER + '2026-10-01T20:00:00+02:00,gateway,60,0,1.0,2.0\r\n2026-10-01T20:0')  # cut inside the time
        self.assertEqual([(r['target'], r['event']) for r in analyze.load_events(tmp)],
                         [('gateway', 'down'), ('gateway', 'up')])
        self.assertEqual(analyze.last_minute_end(tmp), analyze.parse_time('2026-10-01T20:01:00+02:00'))  # the row before
        since = analyze.parse_time('2026-10-01T19:00:00+02:00')
        self.assertEqual([m['t'] for m in web.minute_series(tmp, since)], ['2026-10-01T20:00:00+02:00'])

    def test_minute_csv_is_repaired_too(self):
        tmp = self.data(minute=self.MINUTE_HEADER + '2026-10-01T20:00:00+02:00,gateway,60,0,1.0,2.0\r\n2026-10-01T20:01:00+02:00,ga')
        mon = self.monitor(tmp)
        mon.minute.writerow(['2026-10-01T20:02:00+02:00', 'gateway', 60, 0, '1.0', '2.0'])
        mon.minute_f.flush()
        with open(os.path.join(tmp, 'minute.csv'), newline='') as f:
            lines = f.read().split('\r\n')
        self.assertEqual(lines[-2], '2026-10-01T20:02:00+02:00,gateway,60,0,1.0,2.0')
        self.assertEqual(analyze.last_minute_end(tmp), analyze.parse_time('2026-10-01T20:03:00+02:00'))


class MinuteTail(unittest.TestCase):
    """minute.csv is read from the end, not scanned whole (issue #28)."""

    def make(self):
        """Two targets, one row per minute, across the autumn clock change (UTC+2 to UTC+1)."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        utc = dt.datetime(2026, 10, 24, 20, 0, tzinfo=dt.timezone.utc)
        change = dt.datetime(2026, 10, 25, 1, 0, tzinfo=dt.timezone.utc)
        with open(os.path.join(tmp, 'minute.csv'), 'w', newline='') as f:
            f.write('minute,target,sent,lost,rtt_avg_ms,rtt_max_ms\r\n')
            while utc < dt.datetime(2026, 10, 26, 4, 0, tzinfo=dt.timezone.utc):
                zone = dt.timezone(dt.timedelta(hours=2 if utc < change else 1))
                for target in ('gateway', '1.1.1.1'):
                    f.write(f'{utc.astimezone(zone).isoformat()},{target},60,0,1.0,2.0\r\n')
                utc += dt.timedelta(minutes=1)
        return tmp

    def full_scan(self, tmp, since):
        import csv
        with open(os.path.join(tmp, 'minute.csv'), newline='') as f:
            return [r for r in csv.DictReader(f) if analyze.parse_time(r['minute']) >= since]

    def test_rows_since_match_a_full_scan_across_the_clock_change(self):
        tmp = self.make()
        self.assertGreater(os.path.getsize(os.path.join(tmp, 'minute.csv')), 100000)  # bigger than the bisection window
        utc = dt.timezone.utc
        for since in (dt.datetime(2026, 10, 24, 10, 0, tzinfo=utc),      # before the data
                      dt.datetime(2026, 10, 24, 22, 0, tzinfo=utc),
                      dt.datetime(2026, 10, 25, 0, 59, tzinfo=utc),      # just before the change
                      dt.datetime(2026, 10, 25, 1, 0, tzinfo=utc),       # the change
                      dt.datetime(2026, 10, 25, 1, 30, tzinfo=utc),
                      dt.datetime(2026, 10, 25, 23, 0, tzinfo=utc),
                      dt.datetime(2026, 10, 26, 3, 59, tzinfo=utc),      # the last minute
                      dt.datetime(2026, 10, 27, 0, 0, tzinfo=utc)):      # after the data
            self.assertEqual(list(analyze.minute_rows(tmp, since)), self.full_scan(tmp, since), since)

    def test_last_minute_end_matches_the_last_row(self):
        tmp = self.make()
        self.assertEqual(analyze.last_minute_end(tmp), dt.datetime(2026, 10, 26, 4, 0, tzinfo=dt.timezone.utc))

    def test_last_minute_end_without_usable_data(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.assertIsNone(analyze.last_minute_end(tmp))                  # no file
        self.assertEqual(list(analyze.minute_rows(tmp, dt.datetime.now().astimezone())), [])
        path = os.path.join(tmp, 'minute.csv')
        for text in ('', 'minute,target,sent,lost,rtt_avg_ms,rtt_max_ms\r\n'):
            with open(path, 'w') as f:
                f.write(text)
            self.assertIsNone(analyze.last_minute_end(tmp), repr(text))
            self.assertEqual(list(analyze.minute_rows(tmp, dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc))), [])

    def test_a_cut_short_last_line_is_skipped(self):
        tmp = self.make()
        with open(os.path.join(tmp, 'minute.csv'), 'a') as f:
            f.write('2026-10-26T05:0')                                   # cut inside the time
        self.assertEqual(analyze.last_minute_end(tmp), dt.datetime(2026, 10, 26, 4, 0, tzinfo=dt.timezone.utc))
        since = dt.datetime(2026, 10, 26, 3, 58, tzinfo=dt.timezone.utc)
        self.assertEqual(len(list(analyze.minute_rows(tmp, since))), 4)  # two minutes, two targets


class CrashGap(unittest.TestCase):
    """Time the monitor wasn't running is not monitored time (issue #19)."""

    TZ = dt.timezone(dt.timedelta(hours=2))

    def at(self, hour, minute=0):
        return dt.datetime(2026, 10, 5, hour, minute, tzinfo=self.TZ)

    def data(self, events, minutes=()):
        """events: [(time, target, event)]; minutes: [(from, to)] with a row for each minute."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with open(os.path.join(tmp, 'events.csv'), 'w', newline='') as f:
            f.write('time,target,event,duration_s,detail\r\n')
            for t, target, kind in events:
                f.write(f'{t.isoformat()},{target},{kind},,\r\n')
        if minutes:
            with open(os.path.join(tmp, 'minute.csv'), 'w', newline='') as f:
                f.write('minute,target,sent,lost,rtt_avg_ms,rtt_max_ms\r\n')
                for start, end in minutes:
                    t = start
                    while t < end:
                        f.write(f'{t.isoformat()},gateway,60,0,1.0,2.0\r\n')
                        t += dt.timedelta(minutes=1)
        return tmp

    def power_cut(self):
        """Runs 00:00-18:00 with an outage open from 17:50, loses power, restarts at 18:20."""
        events = [(self.at(0), 'monitor', 'start')]
        events += [(self.at(17, 50), h, 'down') for h in analyze.INTERNET_HOSTS]
        events += [(self.at(18, 20), 'monitor', 'start')]
        return events, [(self.at(0), self.at(18)), (self.at(18, 20), self.at(18, 30))]

    def test_the_gap_after_a_power_cut_is_not_monitored(self):
        events, minutes = self.power_cut()
        outages, periods = analyze.load_outages(self.data(events, minutes))
        self.assertEqual(periods, [(self.at(0), self.at(18)), (self.at(18, 20), self.at(18, 30))])
        # the outage that was open when the power went is cut there, not carried to the restart
        for host in analyze.INTERNET_HOSTS:
            self.assertEqual(outages[host], [(self.at(17, 50), self.at(18), True)])
        internet = analyze.intersect_all([outages[h] for h in analyze.INTERNET_HOSTS])
        self.assertEqual(internet, [(self.at(17, 50), self.at(18), False)])

    def test_without_minute_rows_the_last_event_is_the_evidence(self):
        events, _ = self.power_cut()
        _, periods = analyze.load_outages(self.data(events))
        self.assertEqual(periods[0], (self.at(0), self.at(17, 50)))

    def test_a_clean_stop_ends_the_run_at_the_stop(self):
        events = [(self.at(0), 'monitor', 'start'), (self.at(12), 'monitor', 'stop'), (self.at(12, 10), 'monitor', 'start')]
        _, periods = analyze.load_outages(self.data(events, [(self.at(0), self.at(12)), (self.at(12, 10), self.at(12, 20))]))
        self.assertEqual(periods, [(self.at(0), self.at(12)), (self.at(12, 10), self.at(12, 20))])

    def test_a_restart_straight_away_loses_nothing(self):
        events = [(self.at(0), 'monitor', 'start'), (self.at(0, 0), 'monitor', 'start')]
        _, periods = analyze.load_outages(self.data(events))
        self.assertEqual(periods[0], (self.at(0), self.at(0)))  # never before it started


class Trim(unittest.TestCase):
    def test_trim_before(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        data = os.path.join(tmp, 'linemon')
        os.makedirs(os.path.join(data, 'captures', '20261001T170604-outage-start'))
        os.makedirs(os.path.join(data, 'captures', '20261001T183000-outage-start'))
        with open(os.path.join(data, 'events.csv'), 'w') as f:
            f.write('time,target,event,duration_s,detail\n'
                    '2026-10-01T16:32:34.500+02:00,monitor,start,,iface=eth0 gateway=None\n'
                    '2026-10-01T16:33:05.256+02:00,monitor,gateway,,None -> 192.168.1.1\n'
                    '2026-10-01T17:00:00.000+02:00,1.1.1.1,down,,\n'
                    '2026-10-01T17:00:30.000+02:00,1.1.1.1,up,30,\n'
                    '2026-10-01T17:59:50.000+02:00,8.8.8.8,down,,\n'      # straddles the cut-off
                    '2026-10-01T18:00:20.000+02:00,8.8.8.8,up,30,\n'
                    '2026-10-01T18:30:00.000+02:00,9.9.9.9,down,,\n'
                    '2026-10-01T18:30:10.000+02:00,9.9.9.9,up,10,\n')
        with open(os.path.join(data, 'minute.csv'), 'w') as f:
            f.write('minute,target,sent,lost,rtt_avg_ms,rtt_max_ms\n'
                    '2026-10-01T17:59:00+02:00,link,60,0,,\n'
                    '2026-10-01T18:00:00+02:00,link,60,0,,\n'
                    '2026-10-01T18:31:00+02:00,link,60,0,,\n')
        with open(os.path.join(data, 'captures.jsonl'), 'w') as f:
            f.write('{"time": "2026-10-01T17:06:04+02:00", "reason": "outage-start"}\n'
                    '{"time": "2026-10-01T18:30:00+02:00", "reason": "outage-start"}\n'
                    '{"time": "2026-10-01T18:3\n')                       # line cut short by a power cut
        with open(os.path.join(data, 'path.log'), 'w') as f:
            f.write('2026-10-01T16:33:46.643+02:00 gateway=192.168.1.1\n'
                    '2026-10-01T18:33:46.643+02:00 gateway=192.168.1.1\n')

        import subprocess
        # capture folders are named in the monitor's local time, so run as the Pi would (CEST)
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'trim.py'), '--before', '2026-10-01T18:00:00+02:00',
                            '--data', data], capture_output=True, text=True,
                           env=dict(os.environ, TZ='Europe/Madrid'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any(n.startswith('linemon-backup-') and n.endswith('.tar.gz') for n in os.listdir(tmp)))

        outages, periods = analyze.load_outages(data)
        self.assertEqual(periods[0][0].isoformat(), '2026-10-01T18:00:00+02:00')  # counted from the cut-off
        self.assertEqual(list(outages), ['9.9.9.9'])  # the straddling outage has no start, so it's dropped
        self.assertEqual(analyze.last_minute_end(data).strftime('%H:%M'), '18:32')
        with open(os.path.join(data, 'minute.csv')) as f:
            self.assertEqual(len(f.readlines()), 3)  # header + 18:00 + 18:31
        self.assertEqual([c['reason'] for c in analyze.load_captures(data)], ['outage-start'])
        with open(os.path.join(data, 'path.log')) as f:
            self.assertEqual(len(f.readlines()), 1)
        self.assertEqual(os.listdir(os.path.join(data, 'captures')), ['20261001T183000-outage-start'])


if __name__ == '__main__':
    unittest.main()
