#!/usr/bin/env python3
"""Read fibre and internet status from a ZTE-made Livebox 6s (ZXHN F6640P, Spain).

A linemon capture hook: logs in to the router's web interface, reads the same
data as its "estado de los indicadores luminosos (LED)" and home pages, logs
out, and prints one JSON line:

  {"ok": true, "summary": "fibre O5 operational · signal OK · internet up",
   "uptime_s": 1060, "details": {...}}

  zte_livebox.py          run as a linemon hook (uses LINEMON_* environment)
  zte_livebox.py --test   print the status, to check the configuration
  zte_livebox.py --debug  also trace every request to stderr (no secrets)

Credentials are read from /etc/linemon/router.conf (override with
LINEMON_ROUTER_CONF), which should be readable by root only (chmod 600); it warns
on stderr, which goes to the journal, if other users can read it:

  [router]
  host = 192.168.1.1
  username = admin
  password = <router admin password>

After a failed login it does not try again for 30 minutes, so a wrong
password cannot get the account locked.
"""
import configparser
import datetime as dt
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

CONFIG = os.environ.get('LINEMON_ROUTER_CONF', '/etc/linemon/router.conf')
DATA_DIR = os.environ.get('LINEMON_DATA', '/var/lib/linemon')
LOGIN_BACKOFF_S = 1800
# While the router re-registers on the fibre, which is when captures matter most, its web pages can
# take well over 8 s to answer (seen 05-07/10/2026: a request with no answer after 8 s was the main
# reason captures failed during outages, while other captures took up to 18 s in total).
REQUEST_TIMEOUT_S = 20
# The whole capture stays within this, so it ends and says why before linemon gives up on the
# script (--hook-timeout, 30 s by default).
BUDGET_S = 25

# ITU-T G.984.3 ONU activation states, as reported in RegStatus
GPON_STATES = {
    1: 'O1 initial', 2: 'O2 standby', 3: 'O3 serial number', 4: 'O4 ranging',
    5: 'O5 operational', 6: 'O6 intermittent loss', 7: 'O7 emergency stop',
}


class LoginError(Exception):
    """A rejected login. The message is for the journal; the router's reply is not logged as is."""

    def __init__(self, message, locked=False):
        super().__init__(message)
        self.locked = locked


def redact(value):
    """A copy of a JSON value with tokens, passwords and hashes removed, for logs."""
    if isinstance(value, dict):
        return {k: '<hidden>' if any(w in k.lower() for w in ('token', 'pass', 'hash', 'sess', 'cookie'))
                else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def parse_xml(text):
    """Parse a ZTE ajax_response_xml_root into {object: [ {param: value} ]}.

    Uses the standard library parser to stay dependency-free: it never fetches
    external entities, the expat in current Debian/Raspberry Pi OS limits entity
    expansion, and the only source is the router on the local network.
    """
    root = ET.fromstring(text)
    error = root.findtext('IF_ERRORSTR')
    if error not in (None, 'SUCC'):
        raise ValueError(f'router returned {error}')
    objects = {}
    for obj in root:
        if obj.tag.startswith('IF_'):
            continue
        instances = []
        for inst in obj.findall('Instance'):
            names = [e.text for e in inst.findall('ParaName')]
            values = [e.text or '' for e in inst.findall('ParaValue')]
            instances.append(dict(zip(names, values)))
        objects[obj.tag] = instances
    return objects


def summarise(led, wan):
    """Turn the two parsed responses into linemon's hook output."""
    reg = int(led['OBJ_GPONREGSTATUS_ID'][0]['RegStatus'])
    los = led.get('OBJ_LOS_INFO_ID', [{}])[0].get('LosInfo', '0') != '0'
    leds = led.get('OBJ_LED_STA_ID', [{}])[0]
    internet = next((i for i in wan.get('ID_WAN_COMFIG', []) if 'INTERNET' in i.get('StrServList', '')),
                    next((i for i in wan.get('ID_WAN_COMFIG', []) if i.get('IsDefGW') == '1'), {}))
    has_ip = internet.get('ConnStatus') == 'Connected' and internet.get('IPAddress', '0.0.0.0') != '0.0.0.0'
    fibre_ok = reg == 5 and not los

    parts = [f"fibre {GPON_STATES.get(reg, f'state {reg}')}", 'NO SIGNAL (LOS)' if los else 'signal OK']
    if has_ip:
        parts.append('internet up')
    else:
        error = internet.get('ConnError', '')
        parts.append('no IP' + (f' ({error})' if error and error != 'ERROR_NONE' else ''))
    uptime = internet.get('UpTime')
    return {
        'ok': fibre_ok and has_ip,
        'summary': ' · '.join(parts),
        'uptime_s': int(uptime) if uptime and uptime.isdigit() and has_ip else None,
        'details': {
            'gpon_state': reg, 'los': los, 'internet_led': leds.get('WanState'),
            'conn_status': internet.get('ConnStatus'), 'conn_error': internet.get('ConnError'),
            'ip': internet.get('IPAddress'), 'gateway': internet.get('GateWay'),
            'vlan': internet.get('VLANID'), 'lease_remaining_s': internet.get('RemainLeaseTime'),
        },
    }


class Router:
    def __init__(self, host, username, password, timeout=None, budget=None, debug=False):
        self.base = f'http://{host}/'
        self.username, self.password, self.debug = username, password, debug
        self.timeout = timeout or REQUEST_TIMEOUT_S
        self.budget = budget or BUDGET_S
        self.deadline = time.monotonic() + self.budget
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.session_token = ''

    def _open(self, query, data=None):
        req = urllib.request.Request(self.base + '?' + query if query else self.base,
                                     data=urllib.parse.urlencode(data).encode() if data is not None else None,
                                     headers={'Referer': self.base, 'X-Requested-With': 'XMLHttpRequest'})
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f'capture took longer than {self.budget:g} s')
        started = time.monotonic()
        try:
            with self.opener.open(req, timeout=min(self.timeout, remaining)) as r:
                body = r.read().decode('utf-8', 'replace')
                if self.debug:
                    self._trace(req, r, data, body)
        except (TimeoutError, OSError) as e:
            # For the journal: which request, and how long it waited (the query holds no secrets).
            print(f'{req.get_method()} {query or "home page"} failed after {time.monotonic() - started:.1f} s: {e}',
                  file=sys.stderr)
            raise
        # Every page embeds a fresh session token, and only the latest one is
        # accepted for POSTs such as logout.
        m = re.search(r'_sessionTmpToken = "([^"]*)"', body)
        if m and m.group(1):
            self.session_token = re.sub(r'\\x([0-9a-fA-F]{2})', lambda x: chr(int(x.group(1), 16)), m.group(1))
        return body

    def _trace(self, req, r, data, body):
        """Print one request to stderr: never cookie values, the password or its hash."""
        fields = {k: ('<hidden>' if k in ('Password', '_sessionTOKEN') else v) for k, v in (data or {}).items()}
        set_cookies = [h.split('=', 1)[0] for h in r.headers.get_all('Set-Cookie') or []]
        sent = sorted({c.name for c in self.cookies})
        snippet = body if len(body) < 400 else f'<{len(body)} bytes, _sessionTmpToken found: {"_sessionTmpToken" in body}>'
        snippet = re.sub(r'("sess_token"\s*:\s*")[^"]*', r'\1<hidden>', snippet)
        print(f'--- {req.get_method()} {req.full_url}\n    form: {fields}\n    status: {r.status}  '
              f'set-cookie: {set_cookies}  cookies now held: {sent}\n    body: {snippet.strip()[:400]}',
              file=sys.stderr)

    def login(self):
        self._open('')  # sets the initial session cookie
        entry = json.loads(self._open('_type=loginData&_tag=login_entry'))
        token = ''.join(ET.fromstring(self._open('_type=loginData&_tag=login_token')).itertext())
        result = json.loads(self._open('_type=loginData&_tag=login_entry', {
            'action': 'login',
            'Password': hashlib.sha256((self.password + token).encode()).hexdigest(),
            'Username': self.username,
            '_sessionTOKEN': entry.get('sess_token', ''),
        }))
        if not result.get('login_need_refresh'):
            # The reply can carry a session token: log it (redacted) for the owner of the
            # Pi, and give linemon only a fixed message, since errors appear on the web page.
            print(f'login rejected; router replied: {json.dumps(redact(result))[:500]}', file=sys.stderr)
            locked = str(result.get('lockingTime', ''))
            if locked.isdigit() and int(locked) > 0:
                raise LoginError(f'the router has locked the login for {locked} s', locked=True)
            raise LoginError('wrong username or password?')

    def logout(self):
        """Log out, so the session doesn't linger. Returns whether the router confirmed it."""
        try:
            body = self._open('_type=loginData&_tag=logout_entry', {'IF_LogOff': 1, '_sessionTOKEN': self.session_token})
            return bool(json.loads(body).get('need_refresh'))
        except Exception:
            return False

    def read(self):
        # The router only serves a page's data while the session is "on" that page:
        # the internet status belongs to the home page, the fibre status to the LED page.
        ms = int(time.time() * 1000)
        self._open('')  # home page
        wan = self._open(f'_type=menuData&_tag=wan_internetstatus_lua.lua&TypeUplink=2&pageType=1&_={ms}')
        self._open(f'_type=menuView&_tag=vmenu-ledstatus&Menu3Location=0&_={ms + 1}')
        led = self._open(f'_type=menuData&_tag=osp_led_status_orange_lua.lua&_={ms + 2}')
        return led, wan


def backoff_file():
    return os.path.join(DATA_DIR, 'router-login-failed')


def warn_if_readable_by_others(path):
    """Tell the journal (stderr, never the web page) if other users can read the password file."""
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        print(f'warning: {path} is readable by other users (mode {mode:03o}); run: chmod 600 {path}', file=sys.stderr)


def failed(message, detail=None):
    """A failed capture. `message` is fixed and goes to captures.jsonl and the web page, which
    has no login; `detail` (exception text, the settings path) goes to stderr, i.e. the journal."""
    if detail:
        print(f'{message}: {detail}', file=sys.stderr)
    return {'error': message}


def capture():
    warn_if_readable_by_others(CONFIG)
    cfg = configparser.ConfigParser(interpolation=None)  # passwords may contain '%'
    if not cfg.read(CONFIG) or 'router' not in cfg:
        return failed('no router settings', f'no [router] section in {CONFIG}')
    r = cfg['router']
    try:
        failed_at = os.path.getmtime(backoff_file())
        if time.time() - failed_at < LOGIN_BACKOFF_S:
            retry = dt.datetime.fromtimestamp(failed_at + LOGIN_BACKOFF_S).strftime('%H:%M')
            return failed(f'login failed recently; not retrying until {retry}', f'check the password in {CONFIG}')
    except FileNotFoundError:
        pass

    router = Router(r.get('host', '192.168.1.1'), r.get('username', 'admin'), r.get('password', ''),
                    debug='--debug' in sys.argv)
    try:
        router.login()
    except LoginError as e:
        with open(backoff_file(), 'w') as f:
            f.write(f'{dt.datetime.now().isoformat()} {e}\n')
        return failed('login rejected; the router has locked the login' if e.locked else 'login rejected', e)
    except Exception as e:
        return failed('router not reachable', repr(e))
    try:
        led_xml, wan_xml = router.read()
    except Exception as e:
        return failed('reading status failed', repr(e))
    finally:
        router.logout()

    capture_dir = os.environ.get('LINEMON_CAPTURE_DIR')
    if capture_dir and os.environ.get('LINEMON_REASON') != 'periodic':
        for name, text in (('led_status.xml', led_xml), ('wan_status.xml', wan_xml)):
            with open(os.path.join(capture_dir, name), 'w') as f:
                f.write(text)
    try:
        return summarise(parse_xml(led_xml), parse_xml(wan_xml))
    except Exception as e:
        return failed('unexpected response', repr(e))


if __name__ == '__main__':
    result = capture()
    if '--test' in sys.argv or '--debug' in sys.argv:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(result, ensure_ascii=False))
