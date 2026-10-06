# Field notes

What running linemon on a real line has shown, and the changes made because of it.
Spikes and pull requests record their own reasoning; this file covers work that
came from using linemon on a real line, especially changes made directly on `main`
without a pull request, so that their decisions are still written down.

No personal or network data here: no addresses, ticket or customer numbers, or raw
measurements. The case files sent to the ISP stay with the user, not in the repo.

## October 2026: the first real line

linemon has run on a Raspberry Pi wired to a fibre ISP's Livebox since 1 Oct 2026, with
the `zte_livebox` hook capturing the router's status during outages.

### Changes made directly on `main` (5 Oct)

These three commits were made in a separate Claude session and pushed without an issue
or pull request. CI passed on each, and the unit tests cover them
(`tests/test_browse.py`, and hop discovery in `tests/test_linemon.py`).

| Commit | What | Decision that would have gone in the pull request |
|---|---|---|
| `e68d683` feat(tools): add browse.py | `tools/browse.py` generates ordinary HTTPS browsing and a 20 MB download every 5 minutes, and logs every request to a CSV, for a test where the ISP watches the line while an "end user device" uses it. | **An exception to principle 5 (observe, don't interfere), kept outside the monitor.** It deliberately loads the line, so it lives in `tools/`, is never started by the services, runs only for a set duration, averages under 1 Mbit/s, and is documented as being only for a test agreed with the ISP. The monitor itself still never loads the line. |
| `1962380` feat(tools): log resolved addresses and recognise intercepted requests | Logs the address each name resolved to, labels certificate failures as `intercepted`, and splits incidents in the summary at any successful request or a gap of over 45 s. A log in the old format is left alone and a new file is started. | Based on the observation below that the router answers HTTPS itself during outages. |
| `1532cfc` fix(monitor): probe each ISP hop the way it answers reliably | At discovery each hop gets 5 ordinary pings. A hop that answers all of them is probed with ordinary pings to its address, any other keeps the TTL-limited probe. `path.log` records the method (`/echo` or `/ttl`). | Retrying unanswered probes was rejected: it adds traffic exactly when a router is pushing back. **Not yet checked on the Pi: [#60](https://github.com/johanhellman/linemon/issues/60).** |

**Consequence for existing data:** `isp_hop1` and `isp_hop2` before and after the
upgrade to `1532cfc` may have been measured differently. On this Pi the upgrade took effect
at **2026-10-05 16:25:09** (the first `path.log` line with `/echo` or `/ttl`); lines without
the suffix are from before it. Compare a hop's outage counts only within one method.

### A change made directly on `main` (6 Oct)

| Commit | What | Decision that would have gone in the pull request |
|---|---|---|
| `cc6abd2` fix(analyzer): blame the cable or router only if it was down when an outage began | A 14-minute outage that began at 07:57 with the cable and router fine (the fibre unregistered) was blamed on the cable, because the router was restarted near its end. The cable and router are now blamed only if they were down when the outage began (within 5 s); later drops are listed as "during the outage", with times and no guessed cause, in the analyzer, a new `during` CSV column and the page. | The ISP hops keep the half-the-outage rule. Checked on the 41 outages from 1-4 Oct: boundaries and classifications unchanged. The new `during` column sits before `router`, so a spreadsheet that reads the export's columns by position needs updating. |

### Checks on the Pi (6 Oct)

Run over SSH against the installed `main` (with the watchdog, deployed at 15:59:35).

- **Per-hop probe method ([#60](https://github.com/johanhellman/linemon/issues/60)).** Since the
  change, hop 1 is probed with ordinary pings and hop 2 with TTL-limited ones, as the 5 Oct test
  predicted. Hop 1 went from 49 "outages" an hour (median 3 s) in the 94 hours before to 0.6 an
  hour in the 25 hours after, the same rate as hop 2. It was down for 97-100 % of each of the 15
  internet outages since, and had no outage outside them.
- **Settings check on upgrade ([#24](https://github.com/johanhellman/linemon/issues/24)).**
  `install.sh` with nothing changed, with a broken `linemon.conf`, and without
  `/etc/default/linemon`: the monitor kept running each time (same process), the broken file was
  reported with "Nothing was changed", and the `--migrate` tip appeared. Restarting with the broken
  file stopped the service with status 78 and no restart loop (`NRestarts=0`). Seen: install.sh's
  summary says "Settings: none (defaults)" when the settings come from `LINEMON_ARGS`.
- **Watchdog ([#42](https://github.com/johanhellman/linemon/issues/42)).** The monitor was frozen
  with `SIGSTOP` at 17:15:11. systemd reported a watchdog timeout at 17:16:41, killed it with
  SIGABRT, restarted it at 17:16:46 and it was probing again at 17:16:52 (start-up 5.5 s, mostly
  hop discovery). `events.csv` shows the `start` with no `stop`, as for any crash.
- **Pulling the data ([#53](https://github.com/johanhellman/linemon/issues/53), part).**
  `tools/pull.py` copied the 8 MB data directory from the Pi (rsync 3.2.7) to a Mac (openrsync) in
  1.3 s and `analyze.py` read the copy. Still open: a NAS, the trim case over SSH, compression of
  real files and SD writes per day.
- **Found while checking:** the analyzer counted the frozen run and the minutes around both test
  stops as monitored time, fixed in [#67](https://github.com/johanhellman/linemon/pull/67).

**Deliberate gaps in this Pi's data.** These were tests, not faults: 2026-10-06 17:14:40-17:15:01
(stopped with a broken config) and 17:15:11-17:16:52 (frozen to test the watchdog). Both count as
unknown time.

### What the line showed

- **The first ISP hop throttles "time exceeded" replies.** It showed hundreds, later
  thousands, of 3-4 s "outages" while the internet hosts lost nothing. A test on 5 Oct:
  42 of 60 TTL-limited probes answered, 60 of 60 ordinary pings; the next router the
  other way round. Handled twice: the analyzer only blames a hop that was down for at
  least half of an outage (`7616024`, via [#59](https://github.com/johanhellman/linemon/pull/59)),
  and the monitor now probes such a hop with ordinary pings (`1532cfc`).
- **During an outage the Livebox answers HTTPS itself.** Requests to well-known sites got
  a self-signed certificate within milliseconds from a private address: the router's own
  "no connection" page. A browser-level test therefore sees fast failures rather than
  timeouts. `browse.py` now labels these `intercepted` (`1962380`).
- **The router hook was worth having.** In nearly every internet outage the Livebox
  reported its fibre registration out of the operational state (O2-O4 instead of O5)
  with optical signal present and no IP; a short period of complete loss of signal (LOS)
  was captured the same way. That is what moved the discussion with the ISP from "your
  equipment" to the operator's side.
- **Router captures can time out while the router answers pings.** The router's web
  interface seems slow while it re-registers, which is when a capture matters most:
  [#61](https://github.com/johanhellman/linemon/issues/61).
- **The router lists only its 20 most recent internet sessions,** so a night with many
  outages pushes the evening's sessions out before anyone looks. Only captures taken
  during each outage keep them.
- **Time before monitoring began was counted as unknown,** which made the first,
  fully observed period look unreliable. Fixed in `7616024` (#59).

### Reports made by hand

Updates to the ISP were assembled by hand from the page's `/api/status` JSON: a summary
by period (outages, downtime, outages per hour, outages of a minute or more) and a CSV in
Spanish with one row per internet outage: start, end, duration in seconds, where the
path broke, and what the router reported during it, without IP addresses. Also an HTML
and PDF version of the same table. This is the shape spike
[#10](https://github.com/johanhellman/linemon/issues/10) (an evidence report for any
period) should produce in one step.
