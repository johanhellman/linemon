# How linemon checks the line

This describes exactly what `linemon.py` does, so you can judge what the results mean
and explain them to an ISP.

## The path being tested

```
monitor ──cable── ISP router ── (fibre / line) ── operator network ── internet
 [link]          [gateway]                       [isp_hop1] [isp_hop2]   [1.1.1.1, 8.8.8.8, 9.9.9.9]
```

Each target is tested in its own thread, about once a second. All probes are bound to
the wired interface (`--iface`, default `eth0`) with `SO_BINDTODEVICE` (via `ping -I`
for ICMP), so they can never leave through Wi-Fi or another interface.

## The checks

### `link`: the monitor's own cable

Reads `/sys/class/net/<iface>/carrier`. Up if it is `1`, meaning the port has an
Ethernet link. If this fails, the problem is the monitor's own cable or port, and the
other results for that period don't say anything about the line.

### `gateway`: the ISP router

```
ping -n -c 1 -W 1 -I eth0 <default gateway>
```

The default gateway is read from `ip -4 route show default dev eth0` at start and every
15 seconds. Up if the router answers within 1 second.

If the router answers but nothing beyond it does, the router and your cable are fine and
the problem is between the router and the operator.

### `isp_hop1` and `isp_hop2`: the first routers in the operator's network

Many operator routers don't answer normal pings, so these use the same trick as
`traceroute`: send a ping towards 1.1.1.1 with a limited time-to-live (TTL). Every
router decreases the TTL by one; the router where it reaches zero drops the packet and
replies "Time to live exceeded". That reply proves the monitor can reach that router.

```
ping -n -c 1 -W 1 -I eth0 -t <ttl> 1.1.1.1
```

**Discovery.** At start, after a router change and then hourly, linemon tries TTL 2 to 8
(up to 3 attempts each) and picks the first two TTLs that get a "Time to live exceeded"
reply from a router. Those become `isp_hop1` and `isp_hop2`. TTL 1 is skipped because it
is the ISP router itself. Hops that never answer are skipped too: operators often hide a
hop or two, so `isp_hop1` may be 3 or 4 hops out. The chosen TTLs and router addresses
are written to `path.log`. If discovery fails (for example during an outage), the
previous path is kept; if there is none yet, discovery is retried every 15 seconds and
these two targets are not probed until it succeeds.

**Check.** Up if a reply comes back within 1 second, from whichever router is at that
hop (the address may change if the operator reroutes).

If the ISP router answers but `isp_hop1` doesn't, the break is between your router and
the operator's first answering router: the fibre or line, the operator's first
equipment, or the service that gives your router its address and route. This is the
access network, and it is the operator's responsibility.

### `1.1.1.1`, `8.8.8.8`, `9.9.9.9`: the internet

```
ping -n -c 1 -W 1 -I eth0 <host>
```

Three large, independent anycast services: Cloudflare, Google and Quad9. Any one of
them can have a hiccup on its own, so an **internet outage** is only counted when all
three are down at the same time.

### `dns_gateway` and `dns_1.1.1.1`: name lookups

A DNS query (type A) over UDP port 53, sent to the ISP router and to 1.1.1.1. The name
is random every time (`lm<random hex>.google.com`), so no cache can answer it: the
query has to reach a real resolver upstream. Up if a reply with the matching query ID
arrives within 1 second with the answer code NOERROR or NXDOMAIN (both prove an upstream
resolver answered). SERVFAIL, a malformed reply or no reply count as failure.

These show whether name lookups fail together with the rest, or on their own.

## From probes to outages

- **Down:** a target is down after **3 consecutive failed probes** (`--threshold`). This
  filters out single lost packets.
- **Start time:** the time of the **first** failed probe, so the start is accurate to
  about a second.
- **Up:** the first successful probe after that. The duration is from the first failed
  probe to the first successful one.
- **Saved immediately:** the `down` row is written to `events.csv` (and synced to disk)
  as soon as the target is declared down, so an outage is not lost if the monitor
  crashes or loses power. The duration is written on the matching `up` row.
- **Still in progress:** an outage that hasn't ended when the data ends (or, on the web
  page, now) is reported as *ongoing*. It is counted up to that moment, classified like any
  other, and in the analyzer's CSV has an empty `end` and `ongoing` set to `yes`.
- **When the monitor wasn't running:** that time is unknown, never counted as up. A run
  that ends with a `stop` row ended there. A run that is followed by a `start` with no `stop`
  (a crash or power cut) is taken to have ended when it was last seen: its last event or
  the end of its last `minute.csv` row, whichever is later. Outages still open then end
  there too, so the gap isn't counted as downtime either.
- **A fault on the Pi is not an outage.** If a probe raises an error on the monitor itself (out of
  file descriptors, a bug), that probe is skipped: it is not counted as a failure, and the error is
  logged to the journal (once a minute at most) and counted. It neither starts nor ends an outage.
- **A failed write isn't lost.** If `events.csv` or `minute.csv` can't be written (disk full, I/O
  error), the rows are kept in memory and written again by the next 15 second cycle, after
  reopening the file. A row can then appear twice, which the analyzer ignores. If `minute.csv`
  stays unwritable for about a week, the oldest per-minute rows are dropped to protect the
  memory. Event rows are kept for as long as the monitor runs, but only in memory: if the
  monitor is stopped or crashes while the disk is still unwritable, the rows still waiting are
  lost, and it says how many on stderr when it stops cleanly. The monitor's threads catch
  their own errors and are restarted if one ends anyway.
- **Cadence:** each target is probed every second. A probe that times out takes about a
  second itself, so during outages the cadence is about 1.1 seconds.

## Where the path broke

The analyzer and the web page find every period where all three internet hosts were
down at once. For each one, they check the layers in order and report the first one
that was also down at any point during that period:

| Result | Meaning |
|---|---|
| monitor cable/port down | The monitor's own cable. Ignore this outage. |
| ISP router not responding | The ISP router stopped answering on your side: it crashed, rebooted or hung. |
| first ISP hop unreachable (access network) | The router answered, but the operator's first router didn't: fibre/line, access network or address assignment. |
| second ISP hop unreachable | The first operator router answered but the second didn't. |
| beyond the ISP hops | The router and both hops answered, but the internet hosts didn't: further into the operator's network or beyond. |

## Configuration

Settings are read at start from `/etc/linemon/linemon.conf` (an INI file; see
`linemon.conf.example` for every setting and its default), then overridden by
command-line arguments, which include `LINEMON_ARGS` from `/etc/default/linemon`.
The settings are `[monitor]` `iface`, `data`, `interval` and `threshold`, and `[hook]`
`command`, `during`, `interval` and `timeout`: the same as the `--iface`, `--data`,
`--interval`, `--threshold`, `--hook`, `--hook-during`, `--hook-interval` and
`--hook-timeout` arguments.

- **Strict.** An unknown setting, a value of the wrong type or out of range (for example a
  `threshold` below 1), a file that can't be parsed, or an unknown argument is an error.
  The monitor prints it to stderr (the systemd journal) and exits with status 78, which
  the service is set not to restart on.
- **No secrets.** `linemon.conf` never holds credentials. Secrets live in their own
  root-only files, such as `router.conf`, read only by the extra that needs them. If such
  a file can be read by group or others, the extra warns on stderr (the journal, never the
  web page or `captures.jsonl`), and `install.sh` lists it. Neither refuses to work or
  changes the file's permissions.
- **`--check-config`** validates the file and `LINEMON_ARGS` together, as the service
  would use them, and exits 0 or 78. **`--migrate`** prints a `linemon.conf` equivalent to
  `LINEMON_ARGS` and changes nothing.
- A value in the file is read literally: `%` needs no escaping.

## Availability, MTBF and MTTR

`analyze.py` prints these for a period (`--from` and `--to`; by default this calendar month
in local time, up to now). They are worked out over **observed time only**. Time linemon
couldn't see is unknown, and is reported next to every figure, never counted as up.

| Term | Meaning |
|---|---|
| Period | The time asked about, up to now at most |
| Monitored | The minutes with a `link` row in `minute.csv`, i.e. when the monitor was running. If there is no `minute.csv`, the monitoring runs from `events.csv` are used. |
| Observed | Monitored time minus the time the monitor's own cable was down (`link`), because nothing it measures then says anything about the line |
| Unknown | Period minus observed: the monitor wasn't running, or its own cable was down |
| Downtime | Internet outages (all three hosts down at once) that fall in observed time. An outage in progress counts up to the end of the data. |
| Availability | (observed − downtime) ÷ observed |
| MTTR | The mean length of the completed outages. Outages still in progress are left out, and the report says how many. |
| MTBF | (observed − downtime) ÷ number of outages. With no outages it says so instead of giving a number. |

- **Outages that cross the edge of the period** count their downtime inside the period, but are
  counted (for MTTR and MTBF) in the period where they started.
- **Only outages of 3 seconds or more** are recorded (see [From probes to
  outages](#from-probes-to-outages)), so availability is an upper bound by a few seconds per
  real outage.
- **Low confidence.** If more than 1 % of the period is unknown, the report says so.
- **Planned maintenance** can't be known to linemon, so nothing is excluded. Operators often
  state availability per month at their own network edge, excluding planned work, so their
  figure and this one can legitimately differ.
- The crash and power-cut rule in [From probes to outages](#from-probes-to-outages) is what
  keeps time with no monitor out of "monitored".

The reasoning is in [docs/spikes/008-availability.md](spikes/008-availability.md).

## Router captures

The probes show *where* the path broke from the outside. A router capture adds what the
ISP router *itself* says, which is often what an ISP will accept as proof.

### When captures run

With `--hook <command>`, a separate thread checks once a second whether all three
internet hosts are down, and runs the command:

| Reason | When |
|---|---|
| `outage-start` | As soon as all three internet hosts are down (about 3 s into an outage) |
| `outage-ongoing` | Every `--hook-during` seconds (default 30) while they stay down |
| `outage-end` | Once when they come back |
| `periodic` | Every `--hook-interval` seconds (default 300) while the line is up. `0` turns this off. |

Captures run one at a time. A capture that takes longer than `--hook-timeout` (default
30 s) is abandoned and logged as an error.

### The hook contract

The command gets these environment variables:

| Variable | Contents |
|---|---|
| `LINEMON_REASON` | One of the reasons above |
| `LINEMON_TIME` | When the capture started (ISO 8601) |
| `LINEMON_DATA` | The data directory |
| `LINEMON_CAPTURE_DIR` | A fresh, empty directory for raw files. It is removed if left empty. |

It must print one JSON object on its last line of output. linemon understands:

| Key | Meaning |
|---|---|
| `ok` | `true` if the router reports everything healthy |
| `summary` | One line for people, shown as "Router said" |
| `uptime_s` | Seconds since the router's internet connection was established, if it has one |
| `error` | Set instead of the above when the capture failed. Shown on the web page, which has no login: use a short fixed message, never the router's reply or exception text. |

Write details for troubleshooting to stderr: linemon passes them to the systemd journal
(`journalctl -u linemon`), never to `captures.jsonl`. Anything else (e.g. `details`) is
stored as is. linemon adds `time`, `reason` and,
if raw files were saved, `files`, and appends the line to `captures.jsonl`.

### Session starts

Each capture with `uptime_s` dates when the router's current internet session began:
capture time minus uptime. Captures that agree within 30 seconds belong to the same
session; a new start time means the session was dropped and re-established. Periodic
captures every 5 minutes are enough to catch every reconnection, including ones
during outages too short to capture while they last.

### ZTE Livebox 6s (`routers/zte_livebox.py`)

For the ZTE-made Livebox 6s (ZXHN F6640P) used in Spain. It does what the router's own
web pages do:

1. **Log in.** `GET /?_type=loginData&_tag=login_entry` returns a session token;
   `GET /?_type=loginData&_tag=login_token` returns a one-time token; then
   `POST /?_type=loginData&_tag=login_entry` with `action=login`, `Username`,
   `Password = sha256(password + one-time token)` and the session token. The password
   itself is never sent.
2. **Read** `/?_type=menuData&_tag=osp_led_status_orange_lua.lua` (the LED page) and
   `/?_type=menuData&_tag=wan_internetstatus_lua.lua&TypeUplink=2&pageType=1`
   (the internet connection).
3. **Log out** with `POST /?_type=loginData&_tag=logout_entry`.

From the responses it reports:

| Field | Source | Meaning |
|---|---|---|
| GPON state | `RegStatus` | The fibre registration state from ITU-T G.984.3: 5 = O5 operational; 1–4 = not (yet) registered; 6–7 = fault states |
| Signal | `LosInfo` | Loss of optical signal: anything but 0 means the fibre is not receiving light |
| Internet | `ConnStatus`, `IPAddress`, `ConnError` | Whether the internet connection (VLAN 20 on this ISP) is connected and has an address |
| Uptime | `UpTime` | Seconds since the internet connection was established |

`ok` is true only when the fibre is operational, there is no loss of signal and the
router has an address. The raw XML responses are saved for outage captures, not for
periodic ones. After a rejected login it does not try again for 30 minutes.

## Data files

All in `/var/lib/linemon`. Times are ISO 8601 in local time with the UTC offset, e.g.
`2026-10-01T16:33:05.256+02:00`.

A power cut can leave the last line of `events.csv` or `minute.csv` half written. When
the monitor starts it ends such a line, so the next row isn't glued on to it; the half
row stays in the file and the analyzer and web page skip it, because it has no usable
time or target.

### `events.csv`

| Column | Meaning |
|---|---|
| `time` | For `down`: the first failed probe. For `up`: the first successful probe. Otherwise when it happened. |
| `target` | `link`, `gateway`, `isp_hop1`, `isp_hop2`, `1.1.1.1`, `8.8.8.8`, `9.9.9.9`, `dns_gateway`, `dns_1.1.1.1`, or `monitor` |
| `event` | `down` or `up` for targets. `start`, `stop` or `gateway` (router changed) for `monitor`. |
| `duration_s` | On `up` rows: seconds since the first failed probe |
| `detail` | On `down`: the last address that answered. On `up`: the address that answered. On `monitor` rows: the interface and router. |

### `minute.csv`

One row per target and minute: `minute`, `target`, `sent`, `lost`, `rtt_avg_ms`,
`rtt_max_ms`. Round-trip times are empty for the hop targets (a "Time to live exceeded"
reply carries no round-trip time in `ping`'s output) and the cable check.

### `path.log`

One line at start, after each router change, and hourly: the router, the discovered
hops (`isp_hop1=ttl4:10.20.30.1`), whether NTP is synchronised, and on a Raspberry Pi
the `vcgencmd get_throttled` value (`0x0` means the power supply has been fine).

### `captures.jsonl`

One JSON object per line, as described under [the hook contract](#the-hook-contract),
for example:

```json
{"time": "2026-10-01T17:06:04.120+02:00", "reason": "outage-start", "ok": false,
 "summary": "fibre O5 operational · signal OK · no IP", "uptime_s": null,
 "details": {"gpon_state": 5, "los": false, "conn_status": "Connecting", ...},
 "files": "captures/20261001T170604-outage-start"}
```

## Limitations

- **One second resolution.** Outages shorter than about 3 seconds are not counted.
- **Hop rate limits.** Routers may rate-limit "Time to live exceeded" replies. A hop
  can then look briefly down on its own; that only matters when the internet hosts are
  down at the same time, which is what the classification uses.
- **Hidden hops.** If the operator hides the hops right after your router, `isp_hop1`
  is further out, and "access network" covers everything up to it.
- **Same building, same power.** The monitor and the ISP router share the same mains.
  A power cut shows up as the monitor stopping, not as an outage.
- **IPv4 only.** IPv6 is not tested.
