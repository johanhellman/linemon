# Roadmap

Where linemon is heading now that it runs as a permanent monitor. Everything here sits within the
[product principles and boundaries](../AGENTS.md#product-principles): an independent, honest, read-only
witness for the line, on a stock Raspberry Pi.

Each item starts as a [spike](spikes/README.md): a time-boxed investigation tracked as a GitHub issue
labelled `spike`. Spikes produce findings and follow-up issues; features are built afterwards.
What running on a real line has shown, and changes made outside a spike, are in
[field notes](field-notes.md).

## Order

M1 is built and checked on a Pi, apart from tests that need hands on the hardware (#30, #53) and the
optional push backup (#52). Revised on 7 Oct 2026 after a week on a real line, where evidence for the
ISP was still assembled by hand:

1. [#21](https://github.com/johanhellman/linemon/issues/21): the availability definitions on the
   first real week.
2. [#10](https://github.com/johanhellman/linemon/issues/10) with
   [#17](https://github.com/johanhellman/linemon/issues/17): the evidence report, stating the
   version that produced it.
3. [#61](https://github.com/johanhellman/linemon/issues/61): router captures that time out while
   the router is busy, since its status is the strongest evidence.
4. Notifications ([#44](https://github.com/johanhellman/linemon/issues/44)-[#48](https://github.com/johanhellman/linemon/issues/48)),
   then a quick prototype for [#12](https://github.com/johanhellman/linemon/issues/12) from the
   per-minute data already recorded.

## M1 Unattended operation

Run for months without care: config, storage, retention, self-monitoring.

| Spike | Time-box | Depends on |
|---|---|---|
| [#4](https://github.com/johanhellman/linemon/issues/4) One config file for the monitor and its extras ([findings](spikes/004-config.md); follow-ups [#23](https://github.com/johanhellman/linemon/issues/23), [#24](https://github.com/johanhellman/linemon/issues/24), [#25](https://github.com/johanhellman/linemon/issues/25)) | 0.5 day | – |
| [#5](https://github.com/johanhellman/linemon/issues/5) Storage that stays fast with a year of data ([findings](spikes/005-storage.md); follow-ups [#27](https://github.com/johanhellman/linemon/issues/27), [#28](https://github.com/johanhellman/linemon/issues/28), [#29](https://github.com/johanhellman/linemon/issues/29), [#30](https://github.com/johanhellman/linemon/issues/30)) | 1 day | – |
| [#6](https://github.com/johanhellman/linemon/issues/6) Retention and off-device backup ([findings](spikes/006-retention.md); follow-ups [#49](https://github.com/johanhellman/linemon/issues/49), [#50](https://github.com/johanhellman/linemon/issues/50), [#51](https://github.com/johanhellman/linemon/issues/51), [#52](https://github.com/johanhellman/linemon/issues/52), [#53](https://github.com/johanhellman/linemon/issues/53)) | 0.5 day | [#5](https://github.com/johanhellman/linemon/issues/5) |
| [#7](https://github.com/johanhellman/linemon/issues/7) Let linemon notice when it is unhealthy ([findings](spikes/007-health.md); follow-ups [#39](https://github.com/johanhellman/linemon/issues/39), [#40](https://github.com/johanhellman/linemon/issues/40), [#41](https://github.com/johanhellman/linemon/issues/41), [#42](https://github.com/johanhellman/linemon/issues/42), [#43](https://github.com/johanhellman/linemon/issues/43)) | 1 day | [#4](https://github.com/johanhellman/linemon/issues/4) |

## M2 Alerts and reports

Tell the user when the line fails, and produce evidence they can send to the ISP.

| Spike | Time-box | Depends on |
|---|---|---|
| [#8](https://github.com/johanhellman/linemon/issues/8) Define availability, MTBF and MTTR honestly ([findings](spikes/008-availability.md); follow-ups [#19](https://github.com/johanhellman/linemon/issues/19), [#20](https://github.com/johanhellman/linemon/issues/20), [#21](https://github.com/johanhellman/linemon/issues/21)) | 0.5 day | – |
| [#9](https://github.com/johanhellman/linemon/issues/9) Notifications that work when the line itself is down ([findings](spikes/009-notify.md); follow-ups [#44](https://github.com/johanhellman/linemon/issues/44), [#45](https://github.com/johanhellman/linemon/issues/45), [#46](https://github.com/johanhellman/linemon/issues/46), [#47](https://github.com/johanhellman/linemon/issues/47), [#48](https://github.com/johanhellman/linemon/issues/48)) | 1 day | [#4](https://github.com/johanhellman/linemon/issues/4) |
| [#10](https://github.com/johanhellman/linemon/issues/10) An evidence report for any period | 1.5 days | [#8](https://github.com/johanhellman/linemon/issues/8) |

## M3 Deeper measurement

Locate faults more precisely and measure line quality, including backup links.

| Spike | Time-box | Depends on |
|---|---|---|
| [#12](https://github.com/johanhellman/linemon/issues/12) Show line quality, not only outages | 1 day | [#5](https://github.com/johanhellman/linemon/issues/5) |
| [#13](https://github.com/johanhellman/linemon/issues/13) Monitor a backup link alongside the main line | 1 day | [#4](https://github.com/johanhellman/linemon/issues/4), [#5](https://github.com/johanhellman/linemon/issues/5) |

## M4 Integrations

Work with the gateway, monitoring tools and routers people already have.

| Spike | Time-box | Depends on |
|---|---|---|
| [#15](https://github.com/johanhellman/linemon/issues/15) A metrics interface for Prometheus and Home Assistant | 0.5 day | [#8](https://github.com/johanhellman/linemon/issues/8) |
| [#16](https://github.com/johanhellman/linemon/issues/16) Make it easy to add a capture script for another router | 1 day | – |

## M5 Distribution

Versioned releases and an install path others can trust.

| Spike | Time-box | Depends on |
|---|---|---|
| [#17](https://github.com/johanhellman/linemon/issues/17) Versioned releases and a changelog from commit messages | 0.5 day | – |
| [#18](https://github.com/johanhellman/linemon/issues/18) An install and upgrade path others can trust | 1 day | [#17](https://github.com/johanhellman/linemon/issues/17), [#4](https://github.com/johanhellman/linemon/issues/4) |

## Parked

Considered and deliberately not on the roadmap for now. Each needs a new reason to come back.

| Idea | Why it is parked |
|---|---|
| Speed tests | They load the line being measured, can trigger the very faults linemon records, use data caps, and need a dependency. Conflicts with principle 5. A Pi measuring a fast line over TLS from Python may also report its own limit as the line's speed (not measured), which would undermine principle 2. If an ISP dispute is ever about speed, an on-demand tool in `tools/` run for an agreed test, like `browse.py`, fits better than a daily test. |
| IPv6 probes | Can't be developed or tested until the ISP provides IPv6 on the line. Revisit then. |
| Login for the web page | The page is designed for a trusted local network. Use a VPN or a reverse proxy for remote access instead of exposing linemon. |
| Scripts for specific routers we don't have | Untestable without the hardware. The hook kit spike makes them possible for people who own those routers. |
| A ready-made Raspberry Pi OS image | High maintenance for little gain over a package. Revisit after the packaging spike. |
| Capturing the route at the moment of an outage ([#11](https://github.com/johanhellman/linemon/issues/11)) | On the real line the router's own status already says where it broke (the fibre lost its registration), and the first ISP hop being down follows from that. A route sweep would add probes exactly during outages, against principle 5, for little new information. Revisit if outages appear that the hops and the router capture can't place. |
| Live data from a UniFi gateway ([#14](https://github.com/johanhellman/linemon/issues/14)) | The gateway's own log matched all 31 outages it recorded, and the monitor is now the independent witness; an occasional support export is enough to compare. Revisit if the comparison becomes a regular need. |

## Proposing something new

1. Check it against the [principles and boundaries](../AGENTS.md#product-principles).
2. Open an issue with the **Spike** template: one question, a time-box, and "done when".
3. Add it to this file under the right milestone, or under *Parked* with the reason.
