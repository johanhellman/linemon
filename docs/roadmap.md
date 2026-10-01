# Roadmap

Where linemon is heading now that it runs as a permanent monitor. Everything here sits within the
[product principles and boundaries](../AGENTS.md#product-principles): an independent, honest, read-only
witness for the line, on a stock Raspberry Pi.

Each item starts as a [spike](spikes/README.md): a time-boxed investigation tracked as a GitHub issue
labelled `spike`. Spikes produce findings and follow-up issues; features are built afterwards.

## Order

Two spikes unblock most others, so they come first: **one config file** and **storage that stays fast**.
After that, reports and notifications give the most value per hour of work.

## M1 Unattended operation

Run for months without care: config, storage, retention, self-monitoring.

| Spike | Time-box | Depends on |
|---|---|---|
| [#4](https://github.com/johanhellman/linemon/issues/4) One config file for the monitor and its extras | 0.5 day | – |
| [#5](https://github.com/johanhellman/linemon/issues/5) Storage that stays fast with a year of data | 1 day | – |
| [#6](https://github.com/johanhellman/linemon/issues/6) Retention and off-device backup | 0.5 day | [#5](https://github.com/johanhellman/linemon/issues/5) |
| [#7](https://github.com/johanhellman/linemon/issues/7) Let linemon notice when it is unhealthy | 1 day | [#4](https://github.com/johanhellman/linemon/issues/4) |

## M2 Alerts and reports

Tell the user when the line fails, and produce evidence they can send to the ISP.

| Spike | Time-box | Depends on |
|---|---|---|
| [#8](https://github.com/johanhellman/linemon/issues/8) Define availability, MTBF and MTTR honestly | 0.5 day | – |
| [#9](https://github.com/johanhellman/linemon/issues/9) Notifications that work when the line itself is down | 1 day | [#4](https://github.com/johanhellman/linemon/issues/4) |
| [#10](https://github.com/johanhellman/linemon/issues/10) An evidence report for any period | 1.5 days | [#8](https://github.com/johanhellman/linemon/issues/8) |

## M3 Deeper measurement

Locate faults more precisely and measure line quality, including backup links.

| Spike | Time-box | Depends on |
|---|---|---|
| [#11](https://github.com/johanhellman/linemon/issues/11) Capture the route at the moment of an outage | 1 day | – |
| [#12](https://github.com/johanhellman/linemon/issues/12) Show line quality, not only outages | 1 day | [#5](https://github.com/johanhellman/linemon/issues/5) |
| [#13](https://github.com/johanhellman/linemon/issues/13) Monitor a backup link alongside the main line | 1 day | [#4](https://github.com/johanhellman/linemon/issues/4), [#5](https://github.com/johanhellman/linemon/issues/5) |

## M4 Integrations

Work with the gateway, monitoring tools and routers people already have.

| Spike | Time-box | Depends on |
|---|---|---|
| [#14](https://github.com/johanhellman/linemon/issues/14) Live data from a UniFi gateway instead of support exports | 1 day | [#4](https://github.com/johanhellman/linemon/issues/4) |
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
| Speed tests | They load the line being measured, can trigger the very faults linemon records, use data caps, and need a dependency. Conflicts with principle 5. |
| IPv6 probes | Can't be developed or tested until the ISP provides IPv6 on the line. Revisit then. |
| Login for the web page | The page is designed for a trusted local network. Use a VPN or a reverse proxy for remote access instead of exposing linemon. |
| Scripts for specific routers we don't have | Untestable without the hardware. The hook kit spike makes them possible for people who own those routers. |
| A ready-made Raspberry Pi OS image | High maintenance for little gain over a package. Revisit after the packaging spike. |

## Proposing something new

1. Check it against the [principles and boundaries](../AGENTS.md#product-principles).
2. Open an issue with the **Spike** template: one question, a time-box, and "done when".
3. Add it to this file under the right milestone, or under *Parked* with the reason.
