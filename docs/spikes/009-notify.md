# 009: Notifications that work when the line itself is down

Issue: #9 · Time spent: within the 1 day box · Date: 2026-10-02

## Question

How can linemon tell the user about outages reliably, given that the internet connection it
would send through is often the thing that has failed?

## Answer

Run notifications as a **separate service** that reads the monitor's `events.csv`, keeps
every notice in a durable spool until it has been delivered, and merges bursts into one
message. That makes "notification failures never affect measurements" true by construction,
and survives both a restart and a line that stays down for hours. Be plain about the limit:
while the line is down, a notice can only reach something on the local network. Everything
that goes through the internet arrives **after** recovery, as a summary. Start with ntfy,
Telegram and email, all possible with the standard library, plus a generic webhook for the
local network. A second path while the line is down needs extra hardware and belongs to the
backup-link spike (#13).

## What we tried

A throwaway prototype (`docs/spikes/009-notify/notify_proto.py`, run with
`test_scenarios.py`) on a Mac, Python 3.14. **Everything ran against fake servers on
localhost; nothing was sent to ntfy, Telegram or any mail provider**, so the reliability of
those services, rate limits and delivery delays are not tested.

| Check | Result |
|---|---|
| ntfy, Telegram and email requests, built with `urllib` and `smtplib` | Correct headers, JSON body and message against fake servers |
| Send to an unreachable address with `timeout=2` | Gave up after 2.0 s, no longer |
| 5 outages in 20 minutes while sending fails, with a restart in the middle | None lost, none repeated. One message after the line returned: "5 internet outages between 14:00 and 14:16, 3 min down in total." |
| An outage still in progress | Not announced as finished |
| Quiet hours 23:00 to 07:00 | Work across midnight and inside a day |

What is **not** shown by this: real DNS failure. `socket.getaddrinfo` ignores a socket's
timeout, so a dead resolver can block a send for far longer than `timeout`. That is known
Python behaviour but wasn't reproduced here. It is the reason to keep sending out of the
monitor's process.

### Why a separate service

The monitor already writes every transition to `events.csv` and fsyncs it. A notifier that
tails that file needs nothing from the monitor:

- **No risk to measurements.** A hung send, a blocked DNS lookup or a crash only affects the
  notifier. Today's monitor has no thread supervision (spike #7), so adding sending threads to
  it would add exactly the failure that spike warns about.
- **Durable by design.** The notifier remembers how far it has read (an offset, written
  atomically) and keeps unsent notices in a spool, one fsynced JSON line each. A cut-short
  last line is skipped. Delivery is at-least-once: after a crash between sending and
  clearing, one notice can be sent twice, which is better than none.
- **Replaceable.** A user who wants a different tool can read `events.csv` themselves.

The costs: one more service, and a delay of a few seconds from the event to the notice,
which doesn't matter for this.

### What can be delivered when

| Channel | While the line is down | After recovery | Setup | Notes |
|---|---|---|---|---|
| Generic webhook or MQTT to something on the LAN (Home Assistant, a local ntfy) | **Yes**, if the receiver is local | Yes | A URL, maybe a token | Reaches people only as far as that receiver does |
| ntfy (HTTPS POST) | No | Yes | Pick a topic, install the app; no account | On the public server the topic name is the only secret: use a long random one, or self-host |
| Telegram bot (HTTPS POST) | No | Yes | Create a bot with BotFather, find the chat id | The bot token is a secret; needs a Telegram account |
| Email (`smtplib`) | No | Yes | An SMTP host, port and login | Providers often need app passwords or block simple logins; can land in spam |
| Backup uplink (a phone hotspot or modem) | **Yes** | Yes | Hardware, and routing sending through it | Needs `SO_BINDTODEVICE` on the sending socket. Out of scope here, see #13. |

### Merging, quiet hours and digests

- **Bursts:** completed outages wait until nothing newer has arrived for a quiet period
  (60 s in the prototype), then go out as one message with the count and total time.
- **Quiet hours:** a setting such as `23:00` to `07:00`. During them notices are held and
  go out at the end. An outage still in progress at that moment is reported as it is.
- **Digest:** a daily or weekly message built from the figures in #20 (`analyze.availability`),
  so it states unknown time the same way the report does.
- **Retry:** back off from 30 s up to 5 minutes, and try again straight away when an `up`
  event shows the line is back.
- **Bounded:** a cap on the spool (for example 1000 notices) that merges the oldest rather
  than growing without end.

## Fit with the principles

- **Private by default (7):** off unless configured. Messages say what and when ("Internet
  was down for 3 min"), and leave out addresses and names unless the user adds them.
- **Observe, don't interfere (5):** sending is light and happens after the fact. It must not
  repeat while the line is down in a way that adds load: backoff and one send at a time.
- **Never lose an outage (4):** the spool is durable. This spike's own design never writes to
  the monitor's files.
- **Secrets stay out of reach of the web service (spike #4):** each channel's credentials live
  in their own root-only file, named from `[notify.<name>]` in `linemon.conf`. The notifier
  runs as root, sandboxed by systemd (read-only data, one writable state directory), since
  the secret files are root-only.
- **Trade-off:** no honest way exists to send an internet notice during an outage, so the
  user hears about it afterwards. For a live alert the options are a local receiver or a
  second uplink.

## Recommendation

Follow-up issues: #44 to #48, in order:

1. **#44 feat(notify): a `linemon-notify` service.** Tails `events.csv`, spools durably, retries
   with backoff, merges bursts; off by default; its own unit and sandbox; settings in
   `linemon.conf`.
2. **#45 feat(notify): ntfy, Telegram, email and a generic webhook channels,** each with
   secrets in a root-only file.
3. **#46 feat(notify): quiet hours, and a daily or weekly digest** from `analyze.availability`.
4. **#47 feat(notify): send monitor-unhealthy events too** (after #7 writes them).
5. **#48 Check on a real line:** unplug the Pi's uplink for a while with real ntfy, Telegram and
   email configured, and confirm one summary after reconnecting. This closes what the fake
   servers could not show.

The backup-uplink alert waits for #13.

## Open questions

- **Real services.** How reliable are ntfy.sh, Telegram and a typical SMTP provider from a
  home line right after it recovers? Answer: the real-line test above.
- **DNS during recovery.** Does the first send after reconnecting fail because the resolver
  isn't ready? Answer: the same test; the backoff should cover it.
- **Two kinds of receiver.** Should a LAN channel get an immediate "down" and an "up", while
  internet channels get summaries? The design allows it with a per-channel `when`; whether
  it is wanted is a question for the first users.
- **Which user.** Running the notifier as root with systemd sandboxing is the simplest; a
  dedicated user that owns the secret files would be tighter. Decide with #1.
