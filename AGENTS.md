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

## Product principles

These hold across the whole roadmap ([docs/roadmap.md](docs/roadmap.md)). A change
that conflicts with one needs an explicit decision, recorded in the pull request,
not a quiet exception.

1. **An independent witness for the line.** linemon watches the connection between the
   home and the ISP, from its own cable, independently of the user's own router. Its
   value is that its account can be trusted when the ISP and the user disagree.
2. **Evidence before features.** Timestamps, outage boundaries and classifications must
   be correct and explainable. A feature that makes results less trustworthy (guessing,
   smoothing away outages, hiding gaps) is not worth having.
3. **Honest about what it doesn't know.** When the monitor itself was down, unplugged or
   unsynchronised, that time is *unknown*, never counted as *up*. Uncertain
   classifications say so.
4. **Never lose an outage.** Outages are written durably the moment they start. Storage,
   retention and upgrades must preserve them; deleting data is always explicit and
   backed up (see `trim.py`).
5. **Observe, don't interfere.** linemon only reads. It doesn't restart routers, change
   ISP equipment settings, or put meaningful load on the line, because any of these
   would change the thing being measured or destroy evidence.
6. **Runs unattended on a stock Pi.** The core needs nothing beyond Raspberry Pi OS:
   Python's standard library and `ping`. It must survive reboots, power cuts, network
   changes and months of data without care.
7. **Private by default.** Data stays on the device and the local network. Nothing is
   sent anywhere unless the user configures it, and reports leave out personal details
   unless the user adds them.
8. **Proportional to the project.** linemon is a small tool for one line. Choose the simplest
   thing that does the job: a few lines over a new script, one place over two kept in sync,
   no option, abstraction or process until it is clearly needed. Say what was left out and why.

## Boundaries

| linemon does | linemon does not |
|---|---|
| Monitor the path from the home to the ISP and the internet | Monitor devices, Wi-Fi or traffic on the home network |
| Read the ISP router's own status, read-only | Change router settings, restart equipment or work around faults |
| Light probes (pings, DNS lookups, TTL-limited pings) | Speed tests or anything that loads the line (parked, see the roadmap) |
| Serve a read-only status page on the local network | Expose itself to the internet or offer accounts and logins |
| Send notifications the user configures | Phone home, collect telemetry or depend on a cloud service for the core |
| Export data and reports for the user to share | Keep personal details (address, customer numbers) unless the user adds them |

**Core and extras.** The core (monitor, analyzer, web page, trim, install) stays
standard library only. Optional extras (router hooks, notification channels, metrics
interfaces) may use other tools only if the core works without them, they are off by
default, and their absence is handled gracefully.

## Working on the roadmap

- New capabilities start as a **spike**: a time-boxed investigation that answers one
  question. Spikes are GitHub issues labelled `spike`, written with the spike template.
- A spike's output is a **findings note** in `docs/spikes/` (see
  [docs/spikes/README.md](docs/spikes/README.md)) and follow-up implementation issues.
  Spikes may include throwaway prototype code, but they don't ship features.
- Check spikes against the principles and boundaries above before starting. If a spike
  finds that a feature can't be built within them, that is a valid result: record it
  and park the feature in the roadmap.
- Work that goes to `main` without a pull request (for example a fix made during an ISP
  test) gets an entry in [docs/field-notes.md](docs/field-notes.md) with the decision a
  pull request would have recorded, and an issue for anything still to check on the Pi.
- Keep [docs/roadmap.md](docs/roadmap.md) in step: link findings, and move items between
  phases or to *Parked* with the reason.

## Layout

| Path | What |
|---|---|
| `linemon.py` | The monitor (systemd service `linemon`, runs as root) |
| `analyze.py` | Outage analysis, CSV export, matching against a UniFi gateway's WAN log |
| `web.py` | Read-only status page on port 8080 (service `linemon-web`, unprivileged); reuses `analyze.py` |
| `routers/` | Capture hooks for specific ISP routers, e.g. `zte_livebox.py` |
| `trim.py` | Deletes data from before a given time, with a backup first; only while the monitor is stopped |
| `tools/browse.py` | Generates ordinary browsing traffic for a test agreed with the ISP; not part of the monitor |
| `tools/pull.py` | Run on the user's computer: copies the data off the Pi with rsync over SSH, keeping files a trim made smaller |
| `install.sh` | Installs or upgrades the services; restarts the monitor only if `linemon.py` or its unit changed |
| `docs/checks.md` | The exact checks, outage rules, hook contract and file formats |
| `docs/roadmap.md` | Roadmap phases, spikes and parked ideas |
| `docs/field-notes.md` | What running on a real line showed, and changes made without a pull request |
| `docs/spikes/` | Spike process, findings template and findings notes |
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
  `.githooks/commit-msg` and CI. Scopes: `monitor`, `analyzer`, `web`, `install`, `trim`, `tools`,
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
- **Errors shown on the page are fixed, safe messages.** The page has no login, so
  anything that reaches it (`captures.jsonl` errors, API responses) must never contain
  router replies, exception text, file paths or tokens. Put details on stderr: they end
  up in the systemd journal, which only root can read.
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
