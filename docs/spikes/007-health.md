# 007: Let linemon notice when it is unhealthy

Issue: #7 · Time spent: within the 1 day box · Date: 2026-10-02

## Question

How does linemon detect and show that its own measurements can no longer be trusted:
hung, clock not synchronised, power throttled, disk filling up, cable unplugged?

## Answer

Sample a short list of signals every 15 seconds, fold them into **healthy / degraded /
unhealthy** with a little hysteresis, and record every change in `events.csv`
(`monitor,unhealthy` and `monitor,healthy`) and in a small `health.json` for the page.
Unhealthy time is **unknown**, handled like the monitor's own cable being down. Before any of
that, two things in today's monitor must be fixed, because they make it measure wrongly
without any signal: a local error is recorded as a line outage, and a failed write can end a
target's thread for good. systemd's watchdog is worth adding on top, driven by the same
heartbeats, with `sd_notify` written in about ten lines of standard library.

## What we tried

I ran the real `Monitor` with fake probes (`docs/spikes/007-health/demo_failures.py`) and
prototyped the health pieces (`health_proto.py`). Both are throwaway scripts, not product
code, run on a Mac, so nothing about Raspberry Pi behaviour was measured.

### Two silent failures in today's monitor

| What goes wrong | What happens today |
|---|---|
| A probe raises a local error (tried with `EMFILE`, too many open files) | `run_target` turns any exception into a failed probe, so after 3 the monitor **writes a `gateway,down` row**. A fault on the Pi is recorded as an outage of the line. |
| Writing an event fails (tried with `ENOSPC`) | The exception isn't caught, the target's thread **ends**, the outage is never recorded and the target stays "up" in memory. The only trace is a traceback in the journal. |

Nothing watches the threads either: `run()` just waits on the stop event. The same is true
of the maintenance thread, which writes `minute.csv`, and the hook thread. A hung or dead
thread leaves a gap that looks like a healthy line.

### Signals and thresholds

| Signal | How it is read | Unhealthy (measurements can't be trusted) | Degraded (worth knowing) |
|---|---|---|---|
| Probe threads | A heartbeat per target thread, and `is_alive()` | A thread is dead, or has produced nothing for 30 s (a probe takes about 1.1 s, even during outages) | |
| Probe errors | Exceptions from a probe, counted apart from failed probes | Any in the last minute | |
| Writes | `OSError` from an event, `minute.csv` write or fsync | A write is failing and being retried | |
| Clock | Wall clock against the monotonic clock every 15 s (a jump over 2 s is a step); `NTPSynchronized` each minute | A step, and for a minute after; not synchronised 10 minutes after boot | NTP state unreadable |
| Power | `vcgencmd get_throttled`, each minute instead of hourly | Under-voltage now | Throttled or temperature-limited now. "Has occurred since boot" bits are noted, not alarmed. |
| Disk | `shutil.disk_usage` of the data directory | Under 100 MB free | Under 1 GB or 10 % free |
| Router capture | Consecutive errors in `captures.jsonl` | | 3 in a row |
| Own cable | The existing `link` check | Already its own signal; shown, and unknown in the figures | |

For scale, the data grows by about 0.7 MB a day (spike #5), so 1 GB is years away on a
healthy card; the disk signal exists for a card that is filling with something else.
The power flag layout comes from the Raspberry Pi documentation and was not confirmed on a Pi.

### Hysteresis

One bad sample must not flap the state. In the prototype a state turns unhealthy after 2
bad samples in a row (30 s) and healthy again after 4 good ones (60 s); a single stray
good sample in between resets the count.

### systemd watchdog

`sd_notify` needs no library: send a datagram to the socket in `$NOTIFY_SOCKET` (a leading
`@` means an abstract socket). The prototype sends `READY=1` and `WATCHDOG=1` correctly to a
fake notify socket, ignores a missing or dead socket without an exception, and takes about
4 µs per send. **Not tested against real systemd.**

The design: the main thread, which today only waits, wakes every 10 s as a supervisor. It
sends `WATCHDOG=1` only if every probe thread and the maintenance thread have a recent
heartbeat. With `Type=notify` and `WatchdogSec=90`, a hung monitor is killed and restarted
by systemd. The restart leaves a start row with no stop row, which the analyzer already
treats as a crash (#19), so the gap is honest.

### Showing it

- **`events.csv`:** `monitor,unhealthy,,,<reasons>` when the state turns unhealthy and
  `monitor,healthy,<seconds>,,` when it recovers, both fsynced like outages. Reasons come from
  a fixed list (`probe-stalled`, `probe-error`, `write-failing`, `clock`, `power`). The
  current analyzer ignores unknown `monitor` events, so old tools still read the file; the
  new ones go into `docs/checks.md`.
- **`health.json`:** written atomically every 15 s by the monitor, readable by the page's
  unprivileged user, no secrets: each signal's state and value, and the overall state.
- **The page:** a banner when unhealthy ("Measurements may be unreliable since 14:02: clock
  not synchronised"), a health card with every signal, and "monitor not reporting" if
  `health.json` is more than 60 s old, on top of the page's existing stale-data check.
- **The figures (#20):** unhealthy time is taken out of observed time like cable-down time.
  Outages inside it are listed as measured while unhealthy rather than dropped.

## Fit with the principles

- **Honest about what it doesn't know (3):** the point of the spike. Unhealthy time is
  unknown, marked in the evidence, never hidden or counted as up.
- **Never lose an outage (4):** failed event writes are queued and retried instead of
  ending a thread.
- **Evidence before features (2):** a local error stops being recorded as a line outage.
- **Observe, don't interfere (5):** all signals are read-only. The only action is systemd
  restarting linemon's own process.
- **Stock Pi, standard library only (6):** `sd_notify`, `/sys` and `/proc` reads and the
  existing `timedatectl` and `vcgencmd` calls; no new dependencies.
- **Trade-off:** more `monitor` rows in `events.csv` and one more file to write every 15
  seconds, on an SD card. Both are tiny, but the writes per day were not measured.

## Recommendation

Follow-up issues: #39 to #43, in order:

1. **#39 fix(monitor): a probe that raises a local error is not an outage.** Skip it like a
   hop that isn't discovered, count it as a probe error for the health signals.
2. **#40 fix(monitor): don't lose events or minute rows when a write fails, and notice dead
   threads.** Queue and retry, report on stderr, never let an exception end a thread.
3. **#41 feat(monitor): health signals, `monitor,unhealthy/healthy` events and `health.json`.**
   Needs the config file (#4) for thresholds, and a `docs/checks.md` section.
4. **#42 feat(install): systemd watchdog.** `Type=notify`, `WatchdogSec`, supervisor in the main
   thread. Check on a Pi, including that a deliberately hung thread triggers a restart.
5. **#43 feat(web, analyzer): the health banner and card, and unhealthy time as unknown.**

## Open questions

- **Does the watchdog behave on a Pi?** Test: stall a probe thread on purpose and watch
  systemd restart the service. Also confirm that `Type=notify` doesn't delay start-up.
- **Are the thresholds right in practice?** The 30 s, 2 s and 10 minute values are
  reasoned, not measured. Answer: a few weeks of `health.json` history from the Pi.
- **Is the power-flag layout right?** Answer: read `vcgencmd get_throttled` on a Pi while
  loading it, against the documentation.
- **How often can the Pi take a `vcgencmd` call?** A minute is a guess. Answer: time it.
