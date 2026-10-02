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

    def capture(self, password, reason='outage-start', mode=0o600):
        conf = os.path.join(self.tmp, 'router.conf')
        if os.path.exists(conf):
            os.chmod(conf, 0o600)  # a previous call may have made it read-only
        with open(conf, 'w') as f:
            f.write(f'[router]\nhost = 127.0.0.1:{self.server.server_port}\nusername = admin\npassword = {password}\n')
        os.chmod(conf, mode)  # as the README says
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

    def test_warns_when_the_password_file_is_readable_by_others(self):
        import contextlib
        import io
        for mode, warns in ((0o600, False), (0o400, False), (0o640, True), (0o644, True)):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                result, _ = self.capture('correct horse', mode=mode)
            self.assertTrue(result['ok'], result)  # a warning, not a refusal: existing setups keep working
            self.assertEqual('is readable by other users' in err.getvalue(), warns, oct(mode))
            self.assertNotIn('correct horse', err.getvalue())
            self.assertNotIn('chmod', json.dumps(result))  # nothing about it reaches captures.jsonl or the page
            if warns:
                self.assertIn(f'(mode {mode:03o})', err.getvalue())

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


class CapturesCache(unittest.TestCase):
    """captures.jsonl is parsed once and only new lines after that (issue #29)."""

    TZ = dt.timezone(dt.timedelta(hours=2))

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, 'captures.jsonl')

    def line(self, minute, reason='periodic', summary=None):
        t = dt.datetime(2026, 10, 1, 12, minute // 60, minute % 60, tzinfo=self.TZ)
        return json.dumps({'time': t.isoformat(), 'reason': reason, 'ok': True,
                           'summary': summary or f'ok {minute}', 'uptime_s': 1000 + minute}) + '\n'

    def append(self, text):
        with open(self.path, 'a') as f:
            f.write(text)

    def minutes(self):
        return [c['summary'] for c in analyze.load_captures(self.tmp)]

    def test_new_lines_are_picked_up(self):
        self.assertEqual(analyze.load_captures(self.tmp), [])            # no file yet
        self.append(self.line(1) + self.line(2))
        self.assertEqual(self.minutes(), ['ok 1', 'ok 2'])
        self.append(self.line(3))
        self.assertEqual(self.minutes(), ['ok 1', 'ok 2', 'ok 3'])

    def test_a_half_written_line_waits_for_its_newline(self):
        self.append(self.line(1) + self.line(2)[:30])
        self.assertEqual(self.minutes(), ['ok 1'])
        self.append(self.line(2)[30:])
        self.assertEqual(self.minutes(), ['ok 1', 'ok 2'])

    def test_bad_lines_are_skipped_and_order_is_kept(self):
        self.append(self.line(5) + 'not json\n' + '{"time": "garbage"}\n' + self.line(3))
        self.assertEqual(self.minutes(), ['ok 3', 'ok 5'])
        self.append(self.line(4))                                          # arrives late: still sorted
        self.assertEqual(self.minutes(), ['ok 3', 'ok 4', 'ok 5'])

    def test_a_replaced_or_shortened_file_is_read_again(self):
        self.append(self.line(1) + self.line(2) + self.line(3))
        self.assertEqual(len(analyze.load_captures(self.tmp)), 3)
        replacement = self.path + '.new'                                   # as trim.py does: write, then swap
        with open(replacement, 'w') as f:
            f.write(self.line(3))
        os.replace(replacement, self.path)
        self.assertEqual(self.minutes(), ['ok 3'])
        with open(self.path, 'w'):                                         # emptied in place
            pass
        self.assertEqual(self.minutes(), [])
        self.append(self.line(9))
        self.assertEqual(self.minutes(), ['ok 9'])

    def test_the_caller_cannot_corrupt_the_cache(self):
        self.append(self.line(1) + self.line(2))
        analyze.load_captures(self.tmp).clear()
        self.assertEqual(self.minutes(), ['ok 1', 'ok 2'])

    def test_router_during_matches_a_linear_scan(self):
        lines = ''.join(self.line(m, reason=('outage-start' if m % 7 == 0 else 'periodic'), summary=f'summary {m // 7}')
                        for m in range(0, 180))
        self.append(lines)
        captures = analyze.load_captures(self.tmp)

        def linear(start, end, slack=5):
            lo, hi = start - dt.timedelta(seconds=slack), end + dt.timedelta(seconds=slack)
            out = []
            for c in captures:
                if c['reason'] in ('outage-start', 'outage-ongoing') and lo <= c['time'] <= hi and c['summary'] not in out:
                    out.append(c['summary'])
            return '; '.join(out)
        base = dt.datetime(2026, 10, 1, 12, 0, tzinfo=self.TZ)
        for start_min, length_min in ((0, 1), (6, 2), (13, 30), (50, 0), (170, 20), (400, 5)):
            s, e = base + dt.timedelta(minutes=start_min), base + dt.timedelta(minutes=start_min + length_min)
            self.assertEqual(analyze.router_during(captures, s, e), linear(s, e), (start_min, length_min))
        since = base + dt.timedelta(minutes=100)
        self.assertEqual([c['summary'] for c in analyze.captures_since(captures, since)],
                         [c['summary'] for c in captures if c['time'] >= since])


class Config(unittest.TestCase):
    """Settings come from defaults < linemon.conf < command line (issue #23)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.conf = os.path.join(self.tmp, 'linemon.conf')
        self.env = os.path.join(self.tmp, 'default-linemon')

    def write(self, text, path=None):
        with open(path or self.conf, 'w') as f:
            f.write(text)

    def settings(self, *argv):
        args, _, _ = linemon.parse_settings(['--config', self.conf, *argv])
        return args

    def test_no_file_means_the_old_defaults(self):
        from unittest import mock
        with mock.patch.object(linemon, 'CONFIG_PATH', os.path.join(self.tmp, 'absent.conf')):
            args, _, found = linemon.parse_settings([])
        self.assertFalse(found)
        self.assertEqual((args.iface, args.data, args.interval, args.threshold), ('eth0', '/var/lib/linemon', 1.0, 3))
        self.assertEqual((args.hook, args.hook_during, args.hook_interval, args.hook_timeout), (None, 30, 300, 30))

    def test_precedence_is_defaults_then_file_then_command_line(self):
        self.write('[monitor]\nthreshold = 5\niface = eth1\n[hook]\ncommand = /opt/x.py\nduring = 10\n')
        args = self.settings()
        self.assertEqual((args.threshold, args.iface, args.hook, args.hook_during), (5, 'eth1', '/opt/x.py', 10))
        self.assertEqual(args.interval, 1.0)                                  # not in the file: the default
        args = self.settings('--threshold', '4', '--hook', '/opt/y.py')
        self.assertEqual((args.threshold, args.hook, args.iface), (4, '/opt/y.py', 'eth1'))

    def test_mistakes_are_errors_that_say_where(self):
        for text, expect in (('[monitor]\nthreshhold = 5\n', 'unknown setting "threshhold" in [monitor]'),
                             ('[nope]\nx = 1\n', 'unknown setting "x" in [nope]'),
                             ('[monitor]\nthreshold = many\n', '[monitor] threshold = \'many\' is not a valid int'),
                             ('[monitor]\nthreshold = 0\n', '--threshold must be at least 1'),
                             ('[monitor\nthreshold = 3\n', 'linemon.conf')):
            self.write(text)
            with self.assertRaises(linemon.ConfigError) as cm:
                self.settings()
            self.assertIn(expect, str(cm.exception), text)
        with self.assertRaises(linemon.ConfigError):
            self.settings('--threshold', 'abc')                               # not argparse's exit status 2
        with self.assertRaises(linemon.ConfigError):
            linemon.parse_settings(['--config', os.path.join(self.tmp, 'absent.conf')])  # asked for, so it must exist

    def test_percent_and_empty_values(self):
        self.write('[hook]\ncommand = /opt/a%b.py\n')
        self.assertEqual(self.settings().hook, '/opt/a%b.py')                # like router.conf: '%' is ordinary
        self.write('[hook]\ncommand =\n')
        self.assertIsNone(self.settings().hook)

    def run_linemon(self, *argv):
        import subprocess
        return subprocess.run([sys.executable, os.path.join(ROOT, 'linemon.py'), *argv], capture_output=True, text=True)

    def test_check_config_and_its_exit_status(self):
        self.write('[monitor]\nthreshold = 5\n')
        ok = self.run_linemon('--check-config', '--config', self.conf, '--env-file', self.env)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn('configuration OK', ok.stdout)
        self.write('[monitor]\nthreshhold = 5\n')
        bad = self.run_linemon('--check-config', '--config', self.conf, '--env-file', self.env)
        self.assertEqual(bad.returncode, linemon.EX_CONFIG)                   # the unit won't restart on this
        self.assertIn('unknown setting "threshhold"', bad.stderr)
        self.assertNotIn('Traceback', bad.stderr)

    def test_check_config_looks_at_linemon_args_too(self):
        self.write('')
        self.write('LINEMON_ARGS="--threshold nope"\n', self.env)
        bad = self.run_linemon('--check-config', '--config', self.conf, '--env-file', self.env)
        self.assertEqual(bad.returncode, linemon.EX_CONFIG)

    def test_migrate_prints_an_equivalent_file_and_changes_nothing(self):
        self.write('LINEMON_ARGS="--hook /opt/linemon/routers/zte_livebox.py --hook-interval 600 --threshold 3"\n', self.env)
        before = os.listdir(self.tmp)
        out = self.run_linemon('--migrate', '--env-file', self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(os.listdir(self.tmp), before)
        self.assertIn('[hook]\ncommand = /opt/linemon/routers/zte_livebox.py\ninterval = 600\n', out.stdout)
        self.assertNotIn('threshold', out.stdout)                             # the default is left out
        self.write(out.stdout)                                                # and it means the same as the arguments did
        migrated, old = self.settings(), linemon.build_parser({}).parse_args(linemon.read_env_args(self.env))
        for _, _, flag, *_ in linemon.OPTIONS:
            self.assertEqual(getattr(migrated, linemon.dest(flag)), getattr(old, linemon.dest(flag)), flag)

    def test_the_example_file_matches_the_option_table(self):
        example = os.path.join(ROOT, 'linemon.conf.example')
        values = linemon.read_config(example, required=True)                  # parses, no unknown settings
        defaults = {linemon.dest(flag): default for _, _, flag, _, default, _ in linemon.OPTIONS}
        self.assertEqual(values, {k: v for k, v in defaults.items() if v is not None})
        with open(example) as f:
            text = f.read()
        for section, key, *_ in linemon.OPTIONS:                              # every option is documented
            self.assertRegex(text, rf'(?m)^\[{section}\]')
            self.assertRegex(text, rf'(?m)^#? ?{key} = ')


class InstallFiles(unittest.TestCase):
    """What the tests can see of install.sh and the unit; the rest needs a Pi (issue #24)."""

    def read(self, name):
        with open(os.path.join(ROOT, name)) as f:
            return f.read()

    def test_the_unit_lets_the_file_decide_and_does_not_loop_on_a_bad_config(self):
        unit = self.read('linemon.service')
        exec_start = next(line for line in unit.splitlines() if line.startswith('ExecStart='))
        self.assertEqual(exec_start, 'ExecStart=/usr/bin/python3 /opt/linemon/linemon.py $LINEMON_ARGS')  # no flags that shadow the file
        self.assertIn(f'RestartPreventExitStatus={linemon.EX_CONFIG}', unit)

    def test_install_checks_the_settings_before_changing_anything(self):
        import subprocess
        script = self.read('install.sh')
        self.assertEqual(subprocess.run(['bash', '-n', os.path.join(ROOT, 'install.sh')]).returncode, 0)
        check = script.index('linemon.py --check-config')
        for first_change in ('install -d', 'install -m', 'systemctl restart', 'systemctl daemon-reload'):
            self.assertLess(check, script.index(first_change), first_change)

    def test_every_file_install_copies_exists(self):
        for name in ('linemon.py', 'analyze.py', 'web.py', 'trim.py', 'linemon.service', 'linemon-web.service',
                     'linemon.conf.example'):
            self.assertTrue(os.path.exists(os.path.join(ROOT, name)), name)
            self.assertIn(name, self.read('install.sh'))


class Availability(unittest.TestCase):
    """Availability, MTBF and MTTR over observed time only (issue #20, definitions from spike #8)."""

    TZ = dt.timezone(dt.timedelta(hours=2))

    def at(self, day=0, hour=0, minute=0):
        return dt.datetime(2026, 8, 3, hour, minute, tzinfo=self.TZ) + dt.timedelta(days=day)  # a Monday, in the past

    def data(self, events, minutes):
        """events: [(time, target, event)]; minutes: [(from, to)] with a 'link' row for each minute."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with open(os.path.join(tmp, 'events.csv'), 'w', newline='') as f:
            f.write('time,target,event,duration_s,detail\r\n')
            for t, target, kind in sorted(events, key=lambda e: e[0]):
                f.write(f'{t.isoformat()},{target},{kind},,\r\n')
        if minutes is not None:
            with open(os.path.join(tmp, 'minute.csv'), 'w', newline='') as f:
                f.write('minute,target,sent,lost,rtt_avg_ms,rtt_max_ms\r\n')
                for start, end in minutes:
                    t = start
                    while t < end:
                        f.write(f'{t.isoformat()},link,60,0,,\r\n{t.isoformat()},gateway,60,0,1.0,2.0\r\n')
                        t += dt.timedelta(minutes=1)
        return tmp

    def internet(self, start, end=None):
        events = [(start, h, 'down') for h in analyze.INTERNET_HOSTS]
        if end:
            events += [(end, h, 'up') for h in analyze.INTERNET_HOSTS]
        return events

    def week(self):
        """The synthetic week from spike #8: two outages, a cable pulled, a power cut, an outage in progress."""
        events = [(self.at(), 'monitor', 'start')]
        events += self.internet(self.at(0, 3), self.at(0, 3, 10))          # Mon 03:00, 10 min
        events += self.internet(self.at(2, 9), self.at(2, 9, 30))          # Wed 09:00, 30 min
        events += self.internet(self.at(3, 12), self.at(3, 12, 5))         # Thu 12:00: cable pulled for 5 min
        events += [(self.at(3, 12), 'link', 'down'), (self.at(3, 12, 5), 'link', 'up')]
        events += [(self.at(4, 18, 20), 'monitor', 'start')]               # Fri: power cut 18:00-18:20, no stop row
        events += self.internet(self.at(6, 23))                            # Sun 23:00, still down at the end
        minutes = [(self.at(), self.at(4, 18)), (self.at(4, 18, 20), self.at(6, 23, 59))]
        return self.data(events, minutes)

    def test_the_hand_calculated_week(self):
        a = analyze.availability(self.week(), self.at(), self.at(6, 23, 59), now=self.at(7))
        minutes = lambda seconds: round(seconds / 60, 4)
        self.assertEqual(minutes(a['period_s']), 10079)
        self.assertEqual(minutes(a['monitored_s']), 10059)                 # 20 min of power cut are not monitored
        self.assertEqual(minutes(a['observed_s']), 10054)                  # and 5 min of cable pulled are not either
        self.assertEqual(minutes(a['unknown_s']), 25)
        self.assertEqual(minutes(a['downtime_s']), 99)                     # 10 + 30 + 59 so far; the cable outage is not downtime
        self.assertEqual((a['outages'], a['ongoing'], a['completed']), (3, 1, 2))
        self.assertAlmostEqual(100 * a['availability'], 99.0153, places=4)
        self.assertEqual(minutes(a['mttr_s']), 20.0)                       # the one in progress is left out
        self.assertAlmostEqual(a['mtbf_s'] / 3600, 55.31, places=2)
        self.assertFalse(a['low_confidence'])

    def test_an_outage_belongs_to_the_period_it_started_in(self):
        data = self.data(self.internet(self.at(0, 3), self.at(0, 4)) + [(self.at(), 'monitor', 'start')],
                         [(self.at(), self.at(1))])
        a = analyze.availability(data, self.at(0, 3, 30), self.at(0, 12), now=self.at(1))      # starts inside the outage
        self.assertEqual((a['outages'], a['downtime_s']), (0, 1800))       # the half hour still counts as down
        self.assertIsNone(a['mtbf_s'])
        self.assertIsNone(a['mttr_s'])
        a = analyze.availability(data, self.at(0, 2), self.at(0, 3, 30), now=self.at(1))       # ends after the period
        self.assertEqual((a['outages'], a['downtime_s']), (1, 1800))
        self.assertEqual(a['completed'], 1)

    def test_no_outages_has_no_mtbf(self):
        data = self.data([(self.at(), 'monitor', 'start')], [(self.at(), self.at(0, 10))])
        a = analyze.availability(data, self.at(), self.at(0, 10), now=self.at(1))
        self.assertEqual((a['availability'], a['outages']), (1.0, 0))
        self.assertIsNone(a['mtbf_s'])

    def test_the_period_stops_at_now_and_unmonitored_time_is_unknown(self):
        data = self.data([(self.at(), 'monitor', 'start')], [(self.at(), self.at(0, 0, 10))])  # the monitor stopped at 00:10
        a = analyze.availability(data, self.at(), self.at(1), now=self.at(0, 0, 30))
        self.assertEqual((a['period_s'], a['observed_s'], a['unknown_s']), (1800, 600, 1200))   # not "up" for the missing 20 min
        self.assertTrue(a['low_confidence'])

    def test_without_minute_csv_the_runs_are_used(self):
        data = self.data([(self.at(), 'monitor', 'start'), (self.at(0, 0, 1), 'monitor', 'stop')], None)
        a = analyze.availability(data, self.at(), self.at(0, 0, 10), now=self.at(1))
        self.assertEqual(a['observed_s'], 60)

    def test_a_period_with_nothing_observed(self):
        data = self.data([(self.at(), 'monitor', 'start')], [(self.at(), self.at(0, 10))])
        a = analyze.availability(data, self.at(3), self.at(4), now=self.at(5))
        self.assertEqual(a['observed_s'], 0)
        self.assertIsNone(a['availability'])
        self.assertIsNone(analyze.availability(self.data([], None), self.at(), self.at(1), now=self.at(1)))

    def test_cli_prints_the_figures_for_the_given_period(self):
        import subprocess
        out = subprocess.run([sys.executable, os.path.join(ROOT, 'analyze.py'), self.week(),
                              '--from', '2026-08-03T00:00:00+02:00', '--to', '2026-08-09T23:59:00+02:00'],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        for expected in ('Availability, 03/08/2026 00:00 to 09/08/2026 23:59', 'Availability: 99.0153 % of observed time',
                         'Unknown (monitor not running or its own cable down): 25 min 0 s, 0.25 %',
                         'MTTR: 20 min 0 s (mean of 2 completed outage(s); 1 still in progress left out)',
                         'MTBF: 55 h 18 min 20 s'):
            self.assertIn(expected, out.stdout)

    def test_month_window_and_times_typed_by_the_user(self):
        start, end = analyze.month_window(dt.datetime(2026, 12, 15, 12, 0))
        self.assertEqual((start.replace(tzinfo=None), end.replace(tzinfo=None)), (dt.datetime(2026, 12, 1), dt.datetime(2027, 1, 1)))
        self.assertIsNotNone(analyze.local_time('2026-10-01').tzinfo)
        self.assertEqual(analyze.local_time('2026-10-01T00:00:00+02:00').utcoffset(), dt.timedelta(hours=2))


class LocalErrors(unittest.TestCase):
    """A fault on the Pi is not an outage, and a failed write must not lose one (issues #39, #40)."""

    def setUp(self):
        import contextlib
        import io
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        args = types.SimpleNamespace(iface='eth0', data=self.tmp, interval=0.02, threshold=3,
                                     hook=None, hook_during=30, hook_interval=300, hook_timeout=30)
        original = linemon.default_gateway
        linemon.default_gateway = lambda iface: '192.168.1.1'
        try:
            self.mon = linemon.Monitor(args)
        finally:
            linemon.default_gateway = original
        self.addCleanup(lambda: [f.close() for f in (self.mon.events_f, self.mon.minute_f) if f])
        self.err = io.StringIO()
        stderr = contextlib.redirect_stderr(self.err)
        stderr.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)

    def rows(self, name='events.csv'):
        with open(os.path.join(self.tmp, name), newline='') as f:
            return [line for line in f.read().split('\r\n') if line][1:]

    def fail(self, errno_):
        def broken(*a, **k):
            raise OSError(errno_, os.strerror(errno_))
        return broken

    # --- #39

    def test_a_probe_that_raises_is_not_a_failed_probe(self):
        import errno

        def raises():
            raise OSError(errno.EMFILE, 'Too many open files')
        for _ in range(5):
            self.mon.probe_once('gateway', raises)
        self.assertEqual(self.rows(), [])                                  # no false outage
        self.assertNotIn('gateway', self.mon.down)
        self.assertEqual(self.mon.probe_errors, {'gateway': 5})
        self.assertIn('the probe raised an error', self.err.getvalue())
        self.assertEqual(self.err.getvalue().count('the probe raised an error'), 1)  # said once, not five times

    def test_real_failures_still_make_an_outage(self):
        for _ in range(3):
            self.mon.probe_once('gateway', lambda: (False, None, None))
        self.mon.probe_once('gateway', lambda: (True, 1.0, '192.168.1.1'))
        self.assertEqual([r.split(',')[1:3] for r in self.rows()], [['gateway', 'down'], ['gateway', 'up']])

    def test_an_error_between_failures_does_not_reset_or_count(self):
        self.mon.probe_once('gateway', lambda: (False, None, None))
        self.mon.probe_once('gateway', self.fail(24))
        self.mon.probe_once('gateway', lambda: (False, None, None))
        self.mon.probe_once('gateway', lambda: (False, None, None))
        self.assertEqual(len(self.rows()), 1)                              # still three failures in a row

    # --- #40

    def test_a_failed_fsync_keeps_the_event_and_retries(self):
        import errno
        real = os.fsync
        os.fsync = self.fail(errno.ENOSPC)
        try:
            self.mon.event(analyze.parse_time('2026-10-02T12:00:00+02:00'), 'gateway', 'down')  # must not raise
        finally:
            os.fsync = real
        self.assertEqual(len(self.mon.pending_events), 1)
        self.assertIn('cannot write events.csv', self.err.getvalue())
        self.mon.event(analyze.parse_time('2026-10-02T12:01:00+02:00'), 'gateway', 'up', '60')
        self.assertEqual(self.mon.pending_events, [])
        times = [r.split(',')[0] for r in self.rows()]
        self.assertEqual(times[0], '2026-10-02T12:00:00.000+02:00')        # the outage is there, written first
        self.assertEqual(times[-1], '2026-10-02T12:01:00.000+02:00')
        self.assertLessEqual(len(times), 3)                                # a repeat of the first row is possible, and harmless
        self.assertEqual([(r['target'], r['event']) for r in analyze.load_events(self.tmp)][0], ('gateway', 'down'))

    def test_a_failed_write_is_retried_by_the_maintenance_cycle(self):
        import errno
        self.mon.events = types.SimpleNamespace(writerow=self.fail(errno.EIO))
        self.mon.event(analyze.parse_time('2026-10-02T12:00:00+02:00'), '1.1.1.1', 'down')
        self.assertEqual(len(self.mon.pending_events), 1)
        self.assertIsNone(self.mon.events_f)                                # not trusted after an error
        self.mon.retry_events()
        self.assertEqual((self.mon.pending_events, len(self.rows())), ([], 1))

    def test_minute_rows_are_kept_when_they_cannot_be_written(self):
        import errno
        now_ = linemon.now()
        for i in range(3):
            self.mon.record(now_ - dt.timedelta(minutes=5 + i), 'gateway', True, 1.0)
        self.mon.minute = types.SimpleNamespace(writerow=self.fail(errno.ENOSPC))
        self.mon.flush_minutes()
        self.assertEqual(len(self.mon.pending_minutes), 3)
        self.assertIn('cannot write minute.csv', self.err.getvalue())
        self.mon.flush_minutes()                                            # the disk is back
        self.assertEqual((self.mon.pending_minutes, len(self.rows('minute.csv'))), ([], 3))

    def test_a_disk_that_stays_broken_does_not_eat_the_memory(self):
        self.mon.pending_minutes = [['2026-10-02T12:00:00+02:00', 'gateway', 60, 0, '1.0', '2.0']] * (linemon.MAX_PENDING_MINUTES + 5)
        self.mon.minute = types.SimpleNamespace(writerow=self.fail(28))
        self.mon.flush_minutes()
        self.assertEqual(len(self.mon.pending_minutes), linemon.MAX_PENDING_MINUTES)
        self.assertIn('dropped the 5 oldest', self.err.getvalue())

    def test_an_unexpected_error_does_not_end_the_target_thread(self):
        self.mon.record = self.fail(5)                                      # a bug in the loop, not in the probe
        original, linemon.random.random = linemon.random.random, lambda: 0
        try:
            th = threading.Thread(target=self.mon.run_target, args=('gateway', lambda: (True, 1.0, 'x')), daemon=True)
            th.start()
            time.sleep(0.3)
            self.assertTrue(th.is_alive())
        finally:
            linemon.random.random = original
            self.mon.stop.set()
            th.join(2)
        self.assertIn('gateway: unexpected error', self.err.getvalue())

    def test_the_supervisor_restarts_a_thread_that_ended(self):
        ran = []
        specs = [(lambda name: ran.append(name), ('gateway',))]
        threads = [threading.Thread(target=specs[0][0], args=specs[0][1])]
        threads[0].start()
        threads[0].join()
        self.assertEqual(self.mon.restart_dead(threads, specs), 1)
        threads[0].join()
        self.assertEqual(ran, ['gateway', 'gateway'])
        self.assertIn('ended unexpectedly', self.err.getvalue())
        self.mon.stop.set()
        self.assertEqual(self.mon.restart_dead(threads, specs), 0)          # not while shutting down

    def test_the_hook_thread_survives_a_failing_capture(self):
        self.mon.args.hook, self.mon.args.hook_interval = 'unused', 1
        self.mon.run_hook = self.fail(5)
        th = threading.Thread(target=self.mon.hook_loop, daemon=True)
        th.start()
        time.sleep(2.5)
        alive = th.is_alive()
        self.mon.stop.set()
        th.join(3)
        self.assertTrue(alive)
        self.assertIn('router capture failed unexpectedly', self.err.getvalue())


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
