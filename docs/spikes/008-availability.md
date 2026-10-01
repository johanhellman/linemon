# 008: Define availability, MTBF and MTTR honestly

Issue: #8 · Time spent: within the 0.5 day box · Date: 2026-10-01

## Question

How should linemon calculate availability, mean time between failures and mean time to
recovery so the figures hold up when shown to an ISP?

## Answer

Compute every figure over **observed time only**. Time the monitor wasn't running, or
couldn't see the line (its own cable down), is reported as **unknown** next to the
figures, never counted as up. Availability is the share of observed time with no internet
outage; MTTR and MTBF follow from the same outage list. Today's `analyze.py` gets one
thing wrong that these figures depend on: it counts the gap after a crash or power cut as
monitored time (see below), so that has to be fixed first.

## What we tried

No real data was available where this spike ran (it lives on the Pi), so the "one real
week" check in the issue is **still open**. Instead I built a synthetic week in the
shape of real data, loaded it with `analyze.load_outages`, and calculated the proposed
figures by hand and in a throwaway script (not merged). The week has:

| When | What |
|---|---|
| Mon 03:00 | Internet outage, 10 min |
| Wed 09:00 | Internet outage, 30 min |
| Thu 12:00 | Cable pulled for 5 min: `link` down and all three hosts down |
| Fri 18:00-18:20 | Power cut. The monitor wrote no `stop` row and restarted with a second `start`. |
| Sun 23:00 | Outage still in progress when the data ends at 23:59 (59 min so far) |

Window: Mon 00:00 to Sun 23:59 = 10 079 min.

### Two problems in today's analyzer

1. **A crash counts as monitored time.** `load_outages` (`analyze.py:143-146`) closes a
   run at the next `start` when there was no `stop`. The 20 minute power cut is therefore
   reported as monitored: the script printed 167.98 h monitored for a week with 20 minutes
   missing. This breaks principle 3 and would inflate availability.
2. **A cable outage is listed as an internet outage.** The Thursday event is classified
   "monitor cable/port down", and the docs say to ignore it, but it is still in the
   outage list and would count as downtime.

### The definitions, applied to the synthetic week

| Quantity | Definition | Result |
|---|---|---|
| Window `W` | The period asked about (a calendar month in local time by default) | 10 079 min |
| Monitored `M` | Minutes with a row in `minute.csv`, clipped to the window | 10 059 min |
| Observed `O` | `M` minus time the `link` check was down | 10 054 min |
| Unknown | `W - O` | 25 min (0.25 % of the window) |
| Downtime `D` | Internet outages (all three hosts down), with link-down time removed and the window's edges clipped. An outage in progress counts up to the end of the data. | 10 + 30 + 59 = 99 min |
| Outages `N` | Outages left after removing link-down time | 3 (1 in progress) |
| **Availability** | `(O - D) / O` | **99.0153 %** |
| **MTTR** | Mean duration of the **completed** outages | 20.0 min (2 outages) |
| **MTBF** | `(O - D) / N`: observed up time per outage | 55.31 h |

The prototype and the hand calculation agree on all of these.

In one sentence each:
- **Availability:** of the time linemon could see the line, the share with the internet reachable.
- **MTTR:** the average length of an outage that has ended.
- **MTBF:** the observed up time divided by the number of outages.
- **Unknown:** time linemon can't vouch for either way, shown beside every figure.

### Rules that fell out of the data

- **Monitored time comes from `minute.csv`, not from `start`/`stop` rows.** A minute row
  is evidence the monitor was running; a `start` without a `stop` is not. Fall back to
  the event periods only when `minute.csv` is missing (e.g. after `trim.py`, which should
  keep the minutes it keeps events for).
- **Cable-down time is unknown, not down and not up.** While the monitor's own port is
  down, nothing it measures says anything about the line.
- **Outages in progress count toward downtime, but not toward MTTR.** Their length is
  provisional, so MTTR uses completed outages only and says how many were left out.
  They do count in `N` for MTBF, because the failure has happened.
- **No outages is not infinite.** With `N = 0`, MTBF is shown as "no outages in
  *O* of observation", never as a number.
- **The 3 s floor is the monitor's, not the analyzer's.** The monitor needs 3
  consecutive failures, so shorter outages never reach `events.csv` and availability is
  an upper bound by up to a few seconds per real event. The analyzer must not filter
  again; the figures should say "outages of 3 s or more".
- **Unknown is shown, not hidden.** A figure is printed with its unknown share, e.g.
  "99.02 % of observed time (0.25 % of the period unknown)". If unknown exceeds a small
  threshold (suggest 1 %), the figure is marked as low confidence.
- **Windows are independent.** An outage crossing a window boundary is clipped to each
  window; it counts once in `N`, in the window where it started.
- **Planned maintenance** can't be known to linemon, so nothing is excluded
  automatically. A report may let the user mark periods to exclude, listed explicitly.

### Comparison with how ISPs state availability

Not researched in this spike, so nothing below is verified. The common pattern in
service agreements is a monthly percentage, measured by the operator at its own network
edge, excluding planned maintenance and outages under a minimum length. If so, linemon's
figure measures the customer's experience including the access line, so the two will
legitimately differ. This should be checked against the user's own contract and any
regulator guidance before the report spike (#10) quotes a comparison.

## Fit with the principles

- **Honest about the unknown (3):** the core of the answer. Unknown time is excluded from
  the denominator and reported beside every figure rather than folded into up.
- **Evidence before features (2):** no smoothing, no rounding up, no filtering of short
  outages beyond what the monitor itself guarantees. Ongoing outages are counted.
- **Never lose an outage (4):** nothing here changes what is written. `analyze.py`
  only reads, and the fix for the crash gap uses data already on disk.
- **Trade-off:** excluding unknown time from the denominator makes availability look
  slightly better than if it were counted as down. That is why the unknown share is
  always printed with it, and why a pessimistic bound `(O - D) / W` could be shown too.

## Recommendation

Follow-up issues: #19, #20, #21.

1. **#19 fix(analyzer): don't count the gap after a crash as monitored time.** Derive
   monitored periods from `minute.csv`. Add a regression test using the crash-without-`stop`
   shape from the synthetic week.
2. **#20 feat(analyzer): availability, MTBF and MTTR for a period**, with unknown time and
   cable-down handling as defined above, a `--from`/`--to` window and calendar-month
   default. Document the definitions in `docs/checks.md`.
3. **#21 Real data check:** run the definitions by hand on one real week from the Pi, which
   closes this spike's "done when". Record the result as an addendum to this note.

Spike #10 (evidence report) and #15 (metrics) then use these definitions unchanged.

## Open questions

- **Real data.** Do the definitions behave on a real week? Answer: spot-check one week
  from the Pi against `analyze.py` output.
- **How do ISPs and regulators state it?** Answer: read the user's contract and the
  relevant regulator's guidance, then update the comparison above.
- **What counts as unhealthy?** Spike #7 may define periods when the monitor ran but was
  unhealthy (e.g. unsynchronised clock). Those should become unknown too.
- **Does `trim.py` keep `minute.csv` and `events.csv` aligned?** Monitored time now
  depends on `minute.csv`, so trimming must not leave events with no minutes.
