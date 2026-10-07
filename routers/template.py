#!/usr/bin/env python3
"""Template for a linemon capture hook for another ISP router. See docs/router-hooks.md.

Copy it to routers/<maker>_<model>.py and write read_status(). Everything else (the settings,
the back-off after a rejected login, safe error messages, the JSON line) is already done, the
same way as in zte_livebox.py:

  <script>.py          run as a linemon hook (uses the LINEMON_* environment)
  <script>.py --test   print the status, to check the settings

The router's login is read from /etc/linemon/router.conf (LINEMON_ROUTER_CONF), readable by
root only:

  [router]
  host = 192.168.1.1
  username = admin
  password = <router admin password>
"""
import configparser
import datetime as dt
import json
import os
import sys
import time

CONFIG = os.environ.get('LINEMON_ROUTER_CONF', '/etc/linemon/router.conf')
DATA_DIR = os.environ.get('LINEMON_DATA', '/var/lib/linemon')
LOGIN_BACKOFF_S = 1800  # after a rejected login, don't try again for 30 minutes: never lock the account


class LoginError(Exception):
    """The router rejected the login (wrong password, or locked)."""


def read_status(host, username, password):
    """Log in, read the router's status, log out, and return a dict with:

      ok        True if the router says its line and internet connection are healthy
      summary   one line for people, e.g. 'fibre O5 operational · signal OK · internet up'
      uptime_s  seconds since the internet connection was established, or None
      details   anything else worth keeping (optional)

    Raise LoginError if the login is rejected; any other exception counts as 'router not
    reachable'. Only read: never change a setting or restart anything. Never print the
    password, its hash, session tokens or cookie values, also not when debugging.
    """
    raise NotImplementedError('read_status() has not been written for this router yet')


def failed(message, detail=None):
    """A failed capture. `message` is fixed and is shown on the web page, which has no login;
    `detail` (exception text, the settings path) goes to stderr, i.e. the journal."""
    if detail:
        print(f'{message}: {detail}', file=sys.stderr)
    return {'error': message}


def capture():
    cfg = configparser.ConfigParser(interpolation=None)  # passwords may contain '%'
    if not cfg.read(CONFIG) or 'router' not in cfg:
        return failed('no router settings', f'no [router] section in {CONFIG}')
    r = cfg['router']
    backoff = os.path.join(DATA_DIR, 'router-login-failed')
    try:
        failed_at = os.path.getmtime(backoff)
        if time.time() - failed_at < LOGIN_BACKOFF_S:
            retry = dt.datetime.fromtimestamp(failed_at + LOGIN_BACKOFF_S).strftime('%H:%M')
            return failed(f'login failed recently; not retrying until {retry}', f'check the password in {CONFIG}')
    except FileNotFoundError:
        pass
    try:
        return read_status(r.get('host', '192.168.1.1'), r.get('username', 'admin'), r.get('password', ''))
    except LoginError as e:
        with open(backoff, 'w') as f:
            f.write(f'{dt.datetime.now().isoformat()} {e}\n')
        return failed('login rejected', e)
    except NotImplementedError as e:
        return failed('capture script not finished', e)
    except Exception as e:
        return failed('router not reachable', repr(e))


if __name__ == '__main__':
    print(json.dumps(capture(), indent=2 if '--test' in sys.argv else None, ensure_ascii=False))
