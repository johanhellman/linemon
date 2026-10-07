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
- Python 3.11 or later (tested in CI on 3.11, as shipped with Raspberry Pi OS Bookworm, and
  3.13), and `ping` from iputils (both included in Raspberry Pi OS).
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

To install a specific release instead of the latest code, check out its tag first
(`git checkout v0.1.0`), or download the archive from the
[releases page](https://github.com/johanhellman/linemon/releases), check it against its
`.sha256` file (`sha256sum -c linemon-0.1.0.tar.gz.sha256`), unpack it and run
`sudo ./install.sh` in it. The installed version is shown at the bottom of the web page, by
`/opt/linemon/linemon.py --version`, and in each `start` row of `events.csv`, so evidence
always says which version produced it.

To upgrade, pull and run `sudo ./install.sh` again. The monitor is only restarted if
`linemon.py` or its service file changed, so upgrading the page or the analyzer doesn't
interrupt the measurements. The upgrade checks your settings first, and stops without
touching the running monitor if they have a mistake.

## Configuration

linemon works without any settings. To change one, copy the example and edit it:

```bash
sudo install -d -m 700 /etc/linemon
sudo cp linemon.conf.example /etc/linemon/linemon.conf
sudo nano /etc/linemon/linemon.conf
sudo /opt/linemon/linemon.py --check-config    # catches typos before they matter
sudo systemctl restart linemon                 # settings are read at start
```

Every setting is in [`linemon.conf.example`](linemon.conf.example) with its default and
what it does. Precedence: defaults, then the file, then command-line arguments, including
`LINEMON_ARGS` in `/etc/default/linemon`. Unknown settings are an error, so a typo can't
silently do nothing; the monitor then refuses to start, and says why in
`journalctl -u linemon`.

The file holds **no secrets**, so you can paste it into a bug report. Passwords live in
their own files that only root can read, such as `router.conf` below.

**Upgrading from `LINEMON_ARGS`.** Nothing breaks: it still works and still overrides the
file. To move to the file, run `sudo /opt/linemon/linemon.py --migrate`. It only prints the
equivalent `linemon.conf`; review it, save it, empty `/etc/default/linemon` and restart.

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

   If the file can be read by other users, the capture script warns in the journal
   (`journalctl -u linemon`) every time it runs, and `install.sh` warns when you upgrade.
   It keeps working: the warning is there to be fixed, not to stop your captures.

2. Test it. It should print `"ok": true` and a summary like
   `fibre O5 operational · signal OK · internet up`:

   ```bash
   sudo /opt/linemon/routers/zte_livebox.py --test
   ```

3. Turn it on in `/etc/linemon/linemon.conf` (see [Configuration](#configuration)) and
   restart the monitor:

   ```ini
   [hook]
   command = /opt/linemon/routers/zte_livebox.py
   ```

   ```bash
   sudo /opt/linemon/linemon.py --check-config
   sudo systemctl restart linemon
   ```

   `router.conf` is read only by this script. The monitor itself never opens it, and
   `linemon.conf` only names the script.

Results go to `/var/lib/linemon/captures.jsonl`, and raw responses to
`/var/lib/linemon/captures/`. After a failed login the script waits 30 minutes before
trying again, so a wrong password can't get the router's admin account locked. The
script logs in and out for every capture; if the router allows only one admin session,
you may occasionally be logged out of its web page while a capture runs.

To support another router, write a script that prints one JSON object such as
`{"ok": false, "summary": "...", "uptime_s": 120}`; the details are in
[docs/checks.md](docs/checks.md#router-captures).

## Supervised line tests

An ISP may want to watch the line while an "end user device" generates ordinary
traffic. `tools/browse.py` does that next to the monitor: it visits well-known sites over
HTTPS at random intervals using the router's DNS, downloads a 20 MB file every 5
minutes, and logs every request to a CSV. It is a test tool, not part of the monitor:
run it only for a test agreed with the ISP. It averages under 1 Mbit/s. A request whose TLS
certificate doesn't check out is logged as `intercepted`, together with the address the
name resolved to. During an outage that is typically the ISP router answering with its
own "no connection" page, which a private address confirms; the label alone only says
the certificate failed.

```bash
sudo systemd-run --unit=linemon-browse --uid=$USER --collect --property=RuntimeMaxSec=11400 \
  python3 ~/linemon/tools/browse.py --out ~/browse.csv --duration 10800
journalctl -u linemon-browse -f          # follow it (failures are printed)
python3 ~/linemon/tools/browse.py --summary ~/browse.csv
```

It stops by itself after `--duration` seconds; `sudo systemctl stop linemon-browse` stops
it early.

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

The page also shows the **monitor's own health**: a banner when linemon can't trust its own
measurements (a stalled thread, an unsteady clock, an under-voltage power supply, a full disk), a
card with each check, and "Internet status uncertain" in place of OK or DOWN while that lasts.
That time counts as unknown in the analyzer's availability figures, never as up. If the monitor
hangs, systemd's watchdog restarts it within about 90 seconds, and the gap counts as unknown too.

## Analyzer

```bash
python3 /opt/linemon/analyze.py /var/lib/linemon
```

This prints the outages per target, the availability, MTBF and MTTR for this month, the
internet outages grouped by where the path broke and per day, and the longest ones. Add
`--csv outages.csv` to export the internet outages, or `--all` to include data from before
the last router change.

Availability and the other figures cover only the time linemon was observing, and say how
much of the period is unknown (the monitor wasn't running, or its own cable was down), so
they hold up when you show them to your ISP. Choose another period with
`--from 2026-09-01 --to 2026-10-01`. The definitions are in
[docs/checks.md](docs/checks.md#availability-mtbf-and-mttr).

### A report to send to your ISP

```bash
python3 /opt/linemon/report.py --from 2026-10-01T18:00 --to 2026-10-08T18:00 --lang es -o report.html
```

writes one HTML page for the period: how much of it the monitor observed, availability, every
internet outage with where the path broke and what the router reported, how it was measured,
and the linemon version. `--lang` is `en` or `es`. Open it in a browser and print it to PDF if a
PDF is wanted. The figures are the analyzer's. It holds no personal details or addresses; add
your customer or ticket number in the message it goes with.

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

The backup archive is written next to the data directory, on the same SD card, so it
protects against a mistaken trim but not against the card failing; trim.py reminds you
to copy it off. To write it somewhere else, such as a USB stick or a mounted network
share, add `--backup-dir /mnt/usb`. If that directory doesn't exist, nothing is changed.

## Backing up

The data lives on the Pi's SD card, and SD cards fail. A year of data is about 260 MB,
so copy it off regularly. Pull it from your own computer or NAS: the Pi then holds no
credentials for your machine, and a day adds only about 0.6 MB to copy.

`tools/pull.py` does this with rsync over SSH. Run it on your computer, from a copy of
this repository, with SSH access to the Pi:

```bash
python3 tools/pull.py <user>@<monitor-address>:/var/lib/linemon ~/linemon-backup
```

To run it every night, add a line with `crontab -e` (cron works on Linux and macOS; this
assumes the repository is in `~/linemon`):

```
30 3 * * * python3 ~/linemon/tools/pull.py <user>@<monitor-address>:/var/lib/linemon ~/linemon-backup >> ~/linemon-backup.log 2>&1
```

It needs SSH keys set up so it can log in without a password, and rsync on your computer.
It copies only what was added since the last run, never deletes anything from the
backup, and checks the copy by running `analyze.py` on it.

**Careful with `trim.py` and mirrors.** Deleting old data makes files on the Pi smaller.
A plain mirror (for example `rsync --delete`, or `rsync --append`, which also skips a file
that got smaller) would then lose the trimmed data from the backup too, or keep a stale
copy. `pull.py` moves any file that got smaller to a dated folder next to the backup
(`~/linemon-backup-kept-<time>`) before copying, and says so. If you use your own tool,
make sure it does the same.

**Restoring.** The backup is the same files as on the Pi. To look at the data, run the
analyzer on the copy (`python3 analyze.py ~/linemon-backup`). To put it back on a new
card after installing linemon, copy it to the Pi and then, on the Pi:

```bash
scp -r ~/linemon-backup <user>@<monitor-address>:linemon-backup   # on your computer
sudo systemctl stop linemon
sudo cp -a ~/linemon-backup/. /var/lib/linemon/
sudo chown -R root:root /var/lib/linemon
sudo systemctl start linemon
```

## Files

Everything is in `/var/lib/linemon`:

| File | Contents |
|---|---|
| `events.csv` | One row per down/up transition per target, plus monitor start/stop and router changes |
| `minute.csv` | Per target and minute: probes sent, lost, average and max round-trip time |
| `path.log` | The discovered path, NTP sync and power status, at start, after a router change, and hourly |
| `health.json` | Whether linemon trusts its own measurements right now, rewritten every 15 seconds. Changes to and from *unhealthy* are also in `events.csv`. |
| `captures.jsonl` | The router's own status, if a router capture is set up |
| `captures/` | Raw router responses from captures taken around outages |

The formats are described in [docs/checks.md](docs/checks.md#data-files).

Settings are in `/etc/linemon`, readable by root only:

| File | Contents |
|---|---|
| `linemon.conf` | The monitor's settings, no secrets (optional; see [Configuration](#configuration)) |
| `router.conf` | The router's admin login, for the router capture only |

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

## Roadmap

See [docs/roadmap.md](docs/roadmap.md): where linemon is heading as a permanent monitor, as
time-boxed spikes tracked in [GitHub issues](https://github.com/johanhellman/linemon/issues?q=label%3Aspike),
and the ideas deliberately parked.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Commits follow Conventional Commits, and AI coding
agents should read [AGENTS.md](AGENTS.md) first.

## License

MIT, see [LICENSE](LICENSE).
