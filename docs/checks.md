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

## Data files

All in `/var/lib/linemon`. Times are ISO 8601 in local time with the UTC offset, e.g.
`2026-10-01T16:33:05.256+02:00`.

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
