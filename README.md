# linemon

A small internet line monitor for a Raspberry Pi (or any Linux box with systemd) that
is plugged straight into your ISP's router. Once a second it checks every layer between
you and the internet, logs every outage to the second, and shows **where the path
broke**: your own cable, the ISP router, the operator's access network, or further out.

It was built to collect evidence for an intermittent fibre fault: short outages, many
times a day, that are gone by the time support or a technician looks. A second,
independent monitor (on its own cable, not behind your own router) answers two
questions the ISP will ask:

- *Is it your equipment?* If the monitor, plugged directly into the ISP router, sees the
  same outages at the same moments as your own router, it isn't.
- *Where is the fault?* If the ISP router still answers but the operator's first routers
  don't, the break is in the operator's network, not in your home.

No dependencies beyond Python 3 and `ping`, which Raspberry Pi OS already has.

## What it checks

| Target | What it is | If this and everything after it fails |
|---|---|---|
| **Cable (link)** | The monitor's own network port | The monitor's cable or port |
| **ISP router** | The router the monitor is plugged into, e.g. a Livebox | The router itself, or the cable to it |
| **ISP hop 1** | The first router beyond yours that answers, a few hops into the operator's network | The access network: the fibre or line, or the operator's first equipment |
| **ISP hop 2** | The next router after hop 1 | Further into the operator's network |
| **Internet** (1.1.1.1, 8.8.8.8, 9.9.9.9) | Cloudflare, Google and Quad9 | An internet outage is when **all three** are down at once |
| **DNS** (via the router, and 1.1.1.1 directly) | Name lookups | Name resolution |

A target is **down after 3 failed probes in a row**, and the outage is timestamped from
the first failed probe. For every internet outage, linemon records the first layer that
was also down, so you can see where the path broke.

The exact checks are in **[docs/checks.md](docs/checks.md)**.

## Requirements

- Linux with systemd. Tested on a Raspberry Pi 4 with Raspberry Pi OS (Debian 12 Bookworm).
- Python 3.9 or later, and `ping` from iputils (both included in Raspberry Pi OS).
- A **wired** connection to the ISP router. The monitor only uses that port (`eth0` by
  default), even if Wi-Fi is also connected. Turning Wi-Fi off removes any doubt:
  `sudo nmcli radio wifi off`.
- An NTP-synchronised clock, so the times can be compared with other logs.
- The official power supply: an undervolted Pi can drop its network link and log false
  outages. `path.log` records the Pi's throttling status every hour.

## Install

```bash
git clone https://github.com/johanhellman/linemon.git
cd linemon && sudo ./install.sh
```

This installs two systemd services and starts them at boot:

| Service | What it does |
|---|---|
| `linemon` | The monitor. Writes to `/var/lib/linemon`. Runs as root, which is needed to bind probes to the wired port. |
| `linemon-web` | A read-only status page on port 8080. Runs as an unprivileged dynamic user with read-only access to the data. |

You can install it anywhere (for example behind your own router) and then move the
cable to the ISP router. The monitor notices the new router within 15 seconds, finds the
new path, and the analyzer ignores everything measured before the move.

Check that it's running and found the path:

```bash
tail -n 1 /var/lib/linemon/path.log
```

This should show the ISP router as `gateway`, with `isp_hop1` and `isp_hop2` filled in.

To upgrade, pull and run `sudo ./install.sh` again. The monitor is only restarted if
`linemon.py` changed, so upgrading the page or the analyzer doesn't interrupt the
measurements.

## Capturing the router's own status

Optionally, linemon can also record what the ISP router itself reports, using a
capture script for your router model. It runs when all internet hosts go down, every
30 seconds while they stay down, once when they come back, and every 5 minutes
otherwise. The result appears next to each outage ("Router said") on the web page and
in the analyzer, and the raw responses from outages are kept as evidence.

The periodic captures also record the router's **connection uptime**, so linemon can
list every time the internet session was re-established, even when an outage was too
short to catch during it.

Included: **`routers/zte_livebox.py`** for the ZTE-made Livebox 6s (ZXHN F6640P) used by
Orange, MasOrange and Pepephone in Spain. It reports the fibre's GPON state (O5 =
operational), loss of signal, whether the router has an internet address, and the
connection uptime.

Set it up on the monitor:

1. Store the router's admin login in a file only root can read:

   ```bash
   sudo install -d -m 700 /etc/linemon
   sudo nano /etc/linemon/router.conf
   ```

   ```ini
   [router]
   host = 192.168.1.1
   username = admin
   password = <the router's admin password>
   ```

   ```bash
   sudo chmod 600 /etc/linemon/router.conf
   ```

2. Test it. It should print `"ok": true` and a summary like
   `fibre O5 operational · signal OK · internet up`:

   ```bash
   sudo /opt/linemon/routers/zte_livebox.py --test
   ```

3. Turn it on and restart the monitor:

   ```bash
   echo 'LINEMON_ARGS="--hook /opt/linemon/routers/zte_livebox.py"' | sudo tee /etc/default/linemon
   sudo systemctl restart linemon
   ```

Results go to `/var/lib/linemon/captures.jsonl`, and raw responses to
`/var/lib/linemon/captures/`. After a failed login the script waits 30 minutes before
trying again, so a wrong password can't get the router's admin account locked. The
script logs in and out for every capture; if the router allows only one admin session,
you may occasionally be logged out of its web page while a capture runs.

To support another router, write a script that prints one JSON object such as
`{"ok": false, "summary": "...", "uptime_s": 120}`; the details are in
[docs/checks.md](docs/checks.md#router-captures).

## Web page

Open `http://<monitor-address>:8080`. It refreshes every 10 seconds and shows:

- whether the internet is up right now, or how long it has been down
- the number of internet outages and downtime, last 24 hours and in total
- lost probes per minute over the last 24 hours, for the ISP router, ISP hop 1 and the internet
- the status of each target
- the internet outages, newest first, with where the path broke
- the latest raw events, with the duration of each outage

There is no login: anyone who can reach port 8080 can see the page, but it can't change
anything.

## Analyzer

```bash
python3 /opt/linemon/analyze.py /var/lib/linemon
```

This prints the outages per target, the internet outages grouped by where the path
broke and per day, and the longest ones. Add `--csv outages.csv` to export the internet
outages, or `--all` to include data from before the last router change.

### Comparing with a UniFi gateway

If your own router is a UniFi gateway (e.g. a UDM Pro), the analyzer can match the
monitor's outages against the gateway's own WAN log. Copy the monitor data to your
computer, download a support file from the UniFi console (the menu location varies by
version), and unpack it:

```bash
scp -r <user>@<monitor-address>:/var/lib/linemon ./linemon-data
python3 analyze.py ./linemon-data --udm ./<unpacked-support-file> --csv outages.csv
```

It reports how many of the gateway's outages the monitor also saw, and lists the ones it
didn't. It reads the `wan-failover-group-base ... is up/down` lines from the gateway's
`messages` log (`.zst` archives too, if `zstd` is installed).

## Deleting old data

To start the series afresh, for example after a setup period, delete everything from
before a given time. The monitor must be stopped while it runs; a backup of the data
directory is written first.

```bash
sudo systemctl stop linemon
sudo python3 /opt/linemon/trim.py --before 2026-10-01T18:00
sudo systemctl start linemon
```

A time without a UTC offset is the monitor's local time. The analyzer and web page then
count the monitoring period from the cut-off.

## Files

Everything is in `/var/lib/linemon`:

| File | Contents |
|---|---|
| `events.csv` | One row per down/up transition per target, plus monitor start/stop and router changes |
| `minute.csv` | Per target and minute: probes sent, lost, average and max round-trip time |
| `path.log` | The discovered path, NTP sync and power status, at start, after a router change, and hourly |
| `captures.jsonl` | The router's own status, if a router capture is set up |
| `captures/` | Raw router responses from captures taken around outages |

The formats are described in [docs/checks.md](docs/checks.md#data-files).

## Tests

```bash
python3 -m unittest discover tests
```

The tests use sanitised copies of real router responses and a fake router that
implements the Livebox login, so they need no network or credentials.

## Uninstall

```bash
sudo systemctl disable --now linemon linemon-web
sudo rm /etc/systemd/system/linemon.service /etc/systemd/system/linemon-web.service
sudo rm -rf /opt/linemon /etc/linemon /etc/default/linemon   # add /var/lib/linemon to also delete the data
sudo systemctl daemon-reload
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Commits follow Conventional Commits, and AI coding
agents should read [AGENTS.md](AGENTS.md) first.

## License

MIT, see [LICENSE](LICENSE).
