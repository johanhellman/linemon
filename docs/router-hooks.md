# Writing a capture script for your router

linemon can read your ISP router's own status during outages (the "Router said" column). For
the Livebox 6s that showed, in nearly every outage, that the fibre had lost its registration:
the strongest evidence in the dispute it was built for. Only someone who owns a router can
write and test a script for it, so this guide is for you if you have a different one.

The finished script is one Python file, standard library only, that linemon runs and that
prints one line of JSON. [`routers/template.py`](../routers/template.py) has everything but the
router-specific part; [`routers/zte_livebox.py`](../routers/zte_livebox.py) is a complete
example. The contract it must follow is in [docs/checks.md](checks.md#the-hook-contract).

## Rules

- **Read only.** Log in, read status pages, log out. Never change a setting or restart
  anything (principle 5): the router is the thing being measured.
- **Never print secrets**: not the password, its hash, session tokens or cookie values, also
  not in `--debug` output. Errors on the page are fixed messages; details go to stderr.
- **Back off after a rejected login.** The template waits 30 minutes, so a wrong password can't
  get the router's admin account locked. Keep that.

## 1. Find the status pages

1. Log in to the router's web page in a browser, with the developer tools open on the
   **Network** tab ("Preserve log" on).
2. Watch what the login sends. Many routers don't send the password itself but a hash of it
   with a one-time token (the Livebox sends `sha256(password + token)`); note which requests
   fetch the tokens.
3. Open the page that shows the line status (for fibre: the GPON or PON state, optical power or
   signal, and the internet connection's address and uptime) and find the request that
   returns the data. It is often XML or JSON, not the page itself.
4. Log out and note that request too. Some routers allow only one admin session at a time.

Things to look out for, all seen on the Livebox: a page's data may only be served while the
session is "on" that page (open the page first, then its data); every page may carry a new
session token, and only the latest one is accepted; and the router can take many seconds to
answer while the line is resyncing, which is exactly when a capture matters.

## 2. Write `read_status()`

Copy the template to `routers/<maker>_<model>.py` and fill in `read_status(host, username,
password)`: log in, fetch the status data, log out, and return `ok`, `summary`, `uptime_s` and,
if you like, `details`. Raise `LoginError` when the router rejects the login. Use
`urllib.request` with a cookie jar, as the Livebox script's `Router` class does, and give every
request a timeout.

Keep `summary` short and plain, e.g. `fibre O5 operational · signal OK · internet up`. If the
router reports an uptime, return it: linemon uses it to date when the operator restarted the
internet session.

Check it by hand on the Pi:

```bash
sudo python3 routers/<your_script>.py --test
```

## 3. Test it without the router

Tests run without a network, against a fake router. Copy the pattern of `FakeLivebox` and
`LiveboxCapture` in [tests/test_linemon.py](../tests/test_linemon.py): a small HTTP server that
answers like your router does (including its quirks, such as the latest-token rule), and tests
for a good capture, a wrong password (it must back off), and an unreachable router.

Save the router's real replies as fixtures in `tests/fixtures/`, but **sanitise them first**.
Replace:

- IP addresses with `192.0.2.x` (public) or `10.0.0.x` (private),
- MAC addresses with `02:00:00:00:00:xx`,
- serial numbers, device names, Wi-Fi names, customer and account numbers with placeholders,
- any token, session ID or password hash with `<hidden>`.

Then search the files for anything left: `grep -nE '[0-9]{1,3}(\.[0-9]{1,3}){3}|([0-9a-fA-F]{2}:){5}' tests/fixtures/*`.

## 4. Use it

Point linemon at it in `/etc/linemon/linemon.conf` and restart the monitor:

```ini
[hook]
command = /opt/linemon/routers/<your_script>.py
```

`journalctl -u linemon` shows what went wrong if a capture fails. To share it, open a pull
request with the script, its tests and the sanitised fixtures, and a line next to the Livebox
in the README's "Capturing the router's own status".
