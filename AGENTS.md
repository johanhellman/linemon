# AGENTS.md

Guidance for AI coding agents (and humans) working on linemon. Read this before
changing anything.

## What this is

A layer-by-layer internet line monitor for a Raspberry Pi wired directly to an ISP
router. It probes the cable, the ISP router, the first operator hops, three internet
hosts and DNS once a second, logs every outage, and shows where the path broke.
Optionally it captures the ISP router's own status through a router-specific hook.
The output is evidence for an ISP, so **correct timestamps and honest results matter
more than features**.

## Layout

| Path | What |
|---|---|
| `linemon.py` | The monitor (systemd service `linemon`, runs as root) |
| `analyze.py` | Outage analysis, CSV export, matching against a UniFi gateway's WAN log |
| `web.py` | Read-only status page on port 8080 (service `linemon-web`, unprivileged); reuses `analyze.py` |
| `routers/` | Capture hooks for specific ISP routers, e.g. `zte_livebox.py` |
| `install.sh` | Installs or upgrades the services; restarts the monitor only if `linemon.py` or its unit changed |
| `docs/checks.md` | The exact checks, outage rules, hook contract and file formats |
| `tests/` | `unittest` suite with sanitised router fixtures and a fake router |

## Commands

```bash
python3 -m unittest discover tests        # run before every commit
python3 analyze.py <data-dir>             # summarise a copy of /var/lib/linemon
python3 web.py --data <data-dir> --port 8765   # preview the page locally
git config core.hooksPath .githooks       # enable the commit message check
```

## Rules

- **Standard library only.** No pip dependencies: the target is a stock Raspberry Pi OS
  (Python 3.11) with `ping` from iputils. Don't add a `requirements.txt`.
- **Commits follow Conventional Commits** (`type(scope): description`), checked by
  `.githooks/commit-msg` and CI. Scopes: `monitor`, `analyzer`, `web`, `install`,
  `zte_livebox` (or another router script), `docs`, `tests`. See `CONTRIBUTING.md`.
- **Never commit personal or network data.** No credentials, real IP or MAC
  addresses, serial numbers, customer or ticket numbers, or measurement data. Router
  fixtures must be sanitised (use `192.0.2.x`, `10.0.0.x` and `02:00:00:…` values).
  Scan the diff before pushing.
- **Never print secrets.** Router scripts must not log passwords, password hashes,
  session tokens or cookie values, including in `--debug` output.
- **Don't lose outages.** `down` events are written and fsynced the moment they
  happen; keep it that way. Changes to `events.csv`, `minute.csv` or
  `captures.jsonl` formats must stay readable by `analyze.py` for existing data, and be
  documented in `docs/checks.md`.
- **Probes stay on the wired interface** (`ping -I`, `SO_BINDTODEVICE`), so results
  can't be blamed on Wi-Fi.
- **The web page is read-only** and runs as an unprivileged dynamic user. Never expose
  router credentials or raw captures through it.
- **Router hooks follow the contract** in `docs/checks.md#router-captures`: one JSON
  object on the last line of stdout, with `ok`, `summary`, `uptime_s` or `error`. They
  must back off after a failed login so the router account can't get locked.
- **Keep the docs in step.** User-visible changes update `README.md`; changes to checks,
  thresholds or formats update `docs/checks.md`; the web page's "What is checked"
  section must match.

## Testing changes

- Add or update tests for behaviour changes. For router scripts, extend the fake
  router in `tests/test_linemon.py` so it reproduces the real router's behaviour
  (the Livebox, for example, only serves a page's data while the session is on that
  page, and only accepts the latest session token).
- Things the tests can't cover (real `ping` timing, a real router login, systemd) need
  a check on the Pi; say so in the pull request or commit body instead of assuming.
