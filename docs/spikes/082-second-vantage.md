# 082: A second vantage point behind the home router

Issue: #82 · Time spent: about 1 hour of 2 days (desk analysis; no prototype run yet) · Date: 2026-10-07

## Question

Should linemon support a second vantage point behind the user's own router (for example a VM on
a home server), and if so, how should the two instances' data be brought together and analysed,
without weakening the independence of the monitor on the ISP router?

## Answer

Not as the full design the issue sketches. Its main reason, answering the ISP's objection that
the user's own router causes the outages, is already met by the monitor on the ISP router, and a
monitor behind the user's router can't settle that objection any better. What a second instance
does add is a view of the home network itself (outages seen inside but not on the line). If that
is wanted, try it first with linemon as it is, compare the data with a throwaway script, and
build only a small `analyze.py --compare` if the trial shows something worth explaining.

## What we tried

Desk analysis against the data of the first week; no inside instance has run yet.

**The ISP's objection has two readings, and neither needs a second monitor:**

1. *"The outages you experience are your own router's fault, not the line's."* The edge monitor
   answers this already: it is cabled to the ISP router, its probes never pass through the user's
   router, and it records the outages, while the ISP router's own status shows the fibre or the
   internet connection down. A monitor behind the user's router sees the same outages plus any of
   its own router's, so it adds nothing to this answer.
2. *"Your router disturbs the ISP router and makes the line drop."* Only taking the user's router
   off the line tests this. A monitor behind it is downstream of both routers and can't tell.

So for evidence towards the ISP the edge monitor is the independent witness (principle 1), and a
second instance adds little.

**What a second instance would show** is the row "no outage at the edge, outage inside": faults
in the home network (the user's router, a switch, the VM host). That is a real question, but a
smaller and different one. There is already a first answer to it: the UniFi gateway's own log,
matched with `analyze.py --udm`, had all 31 of its outages also seen by the edge monitor and none
of its own, so in that period the home network added no outages.

**What the full design would cost:** vantage roles with their own layer labels and
classification rules, read-only export endpoints and peer folders, joint analysis, a web card,
notification variants, and handling of VM pauses. That is close to a second product, built to
answer a question the data so far says is quiet.

**Running linemon as it is behind the user's router** works for comparing internet outages:
- An internet outage is all three internet hosts down at once, whatever the other layers are
  called, so the outage boundaries are directly comparable.
- The layer names are off by one on such an instance: its "ISP router" is the user's router and
  its "ISP hop 1" is the ISP router. A note is enough while it is a trial.
- It must run without the router capture hook: the ISP router allows one admin session at a time.
- The cable check means little on a virtual network card, and VM pauses (backups, migration) can
  freeze probing. The existing health signals (stalled threads, clock steps) should mark long
  pauses as unknown; the trial would measure whether that is enough.

## Fit with the principles

- **Independent witness (1):** unchanged. The edge monitor stays self-contained; nothing is added
  to it.
- **Honest about unknowns (3):** in any comparison, time when either instance was unhealthy,
  paused or not running is "not comparable", never agreement or disagreement.
- **Observe, don't interfere (5):** a second instance doubles a negligible probe load and must not
  log in to the ISP router.
- **Proportional (8):** no new subsystem until a trial shows a need.

## Recommendation

1. **Not now:** vantage roles and inside labels, export endpoints and peer folders, a web card,
   notification variants.
2. **If the home-network question is wanted:** a trial with no new code. Install linemon as it is
   on a VM behind the user's router, without the hook, for a few days including a backup night;
   pull both data directories with `tools/pull.py`; compare them with a throwaway script
   (seen by both / edge only / inside only / not comparable, and the clock offset between them).
3. **Only if the trial shows inside-only outages worth explaining:** add `analyze.py --compare
   OTHER_DATA_DIR`, reusing the existing UniFi-log matching. Tens of lines, not a subsystem.
4. **Otherwise park it** with the reasoning above, as #11 and #14 were.

The choice between 2 and 4 is open.

## Open questions

- Does the home network ever drop on its own? The UniFi log says not in the period compared; a
  trial would say for the VM host and switches too.
- How long do VM pauses last during the host's backups, and do the health signals catch them?
  Only a run through a backup window can tell.
