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

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        tag = q.get('_tag', [''])[0]
        if not q:
            if self.logged_in():
                self.reply('<script>var _sessionTmpToken = "\\x41\\x42\\x43";</script>', 'text/html')
            else:
                self.reply('<script>var _sessionTmpToken = "";</script>', 'text/html', cookie='SID=new; path=/')
        elif tag == 'login_entry':
            self.reply(json.dumps({'sess_token': 'S1', 'lockingTime': 0}), 'application/json')
        elif tag == 'login_token':
            self.reply(f'<ajax_response_xml_root>{self.login_token}</ajax_response_xml_root>')
        elif not self.logged_in():
            self.reply('<html>login page</html>', 'text/html')
        elif tag == 'osp_led_status_orange_lua.lua':
            self.reply(fixture('led_ok.xml'))
        elif tag == 'wan_internetstatus_lua.lua':
            self.reply(fixture('wan_ok.xml'))
        else:
            self.reply('<ajax_response_xml_root><IF_ERRORSTR>SUCC</IF_ERRORSTR></ajax_response_xml_root>')

    def do_POST(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
        tag = q['_tag'][0]
        if tag == 'login_entry':
            expected = hashlib.sha256((self.password + self.login_token).encode()).hexdigest()
            ok = (form.get('Password') == [expected] and form.get('Username') == ['admin']
                  and form.get('action') == ['login'] and form.get('_sessionTOKEN') == ['S1'])
            FakeLivebox.logins.append(ok)
            self.reply(json.dumps({'login_need_refresh': ok, 'loginErrMsg': '' if ok else 'wrong password'}),
                       'application/json', cookie='SID=good; path=/' if ok else None)
        elif tag == 'logout_entry':
            FakeLivebox.logouts.append(form.get('_sessionTOKEN'))
            self.reply(json.dumps({'need_refresh': True}), 'application/json')


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
        self.assertEqual(FakeLivebox.logouts, [['ABC']])  # the decoded _sessionTmpToken
        self.assertEqual(sorted(os.listdir(capture_dir)), ['led_status.xml', 'wan_status.xml'])

    def test_periodic_capture_keeps_no_raw_files(self):
        _, capture_dir = self.capture('correct horse', reason='periodic')
        self.assertEqual(os.listdir(capture_dir), [])

    def test_wrong_password_backs_off(self):
        result, _ = self.capture('wrong')
        self.assertIn('login rejected', result['error'])
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


if __name__ == '__main__':
    unittest.main()
