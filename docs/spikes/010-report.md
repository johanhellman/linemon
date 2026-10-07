# 010: An evidence report for any period

Issue: #10 · Time spent: about 3 of 12 hours so far · Date: 2026-10-07

## Question

What should a report for a chosen period contain and look like, so that it can be sent to an
ISP as it is?

## Answer

What the ISP updates in early October were built from by hand, in one step:
`report.py --from … --to … --lang es|en -o report.html` writes a single HTML page (printable to
PDF from a browser) with the period, the linemon version, how much was observed, availability,
MTTR and MTBF, where the path broke, every internet outage with what the router reported, and a
short method. The figures come from `analyze.py`, so the report, the page and the analyzer agree.

## What we tried

- **The content** follows the hand-made updates (see the [field notes](../field-notes.md#reports-made-by-hand)):
  a summary, then one row per outage with start, end, duration, where it broke and the router's
  state, in Spanish for this ISP.
- **On the real data** from 01/10 18:00 to 07/10 03:30 (Mac, a copy of the Pi's data): 203
  outages, 4 h 17 min 44 s without internet, 96.6805 % availability, the same as the analyzer.
- **One figure for downtime.** The first draft also summed the outages per layer, which counts
  time the monitor didn't observe and gave a second, larger total (4 h 19 min 55 s). The
  per-layer table now shows counts only, so the report has one downtime figure.
- **Spanish** comes from a small word list for the headings, the layers and the router's terms
  (fibre → fibra, no IP → sin IP), as the hand-made reports wrote them, with decimal commas.

## Fit with the principles

- **Evidence before features (2):** every number is the analyzer's; the version that produced it
  is stated; unknown time is shown, never counted as up.
- **Private by default (7):** no names, addresses, customer or ticket numbers, and no IP
  addresses. The issue suggested optional personal fields from the config; they were left out,
  as the message the report goes with is the place for them.
- **Stock Pi (6):** standard library only; PDF is left to the browser.

## Recommendation

- Done: `report.py`, installed with the rest.
- Still to do for this spike: run it on one full real week (01/10 18:00 to 08/10 18:00, with #21)
  and have someone unfamiliar with linemon read it, in both languages.
- Possible later, if wanted: a link to the report from the web page. Not built, to keep the page
  as it is.

## Open questions

- Does the ISP want anything else, for example the router's session restarts? The first report
  sent will tell.
