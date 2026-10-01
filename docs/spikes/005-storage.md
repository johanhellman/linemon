# 005: Storage that stays fast with a year of data

Issue: #5 · Time spent: within the 1 day box · Date: 2026-10-01

## Question

Which storage keeps the analyzer and the web page fast with a year of data on a
Raspberry Pi 4, without weakening crash safety?

## Answer

Keep the CSV files. The slowness is not the format, it is that the page and the analyzer
re-scan files that only grow at the end. Reading just the tail of `minute.csv`, and
parsing `captures.jsonl` once and searching it by time, took the page's work from 9.2 s to
0.4 s on a year of data, with no format change and no migration. SQLite answered the
queries faster still but costs more than it saves here. **The Pi 4 numbers and the
power-cut test are still missing**, so the issue's "done when" is not met yet.

## What we tried

**Hardware: an Apple M4 Pro, Python 3.14, SQLite 3.53. Not a Pi 4.** A Pi 4 will be
several times slower, by an amount I did not measure. Treat the numbers below as ratios
between options, and re-run `docs/spikes/005-bench/bench.py` on the Pi for absolute
figures. (The scripts are throwaway prototypes under `docs/spikes/005-bench/`, not
product code.)

Data: one synthetic year from `gen.py`, in the monitor's real formats. 365 days, 9 targets,
one row per target per minute:

| File | Size | Rows |
|---|---|---|
| `minute.csv` | 226 MB | 4.73 M (12 960 a day) |
| `captures.jsonl` | 30 MB | 105 120 (one per 5 min) |
| `events.csv` | 0.1 MB | 1 833 |

### Where the time goes today

| What | Time |
|---|---|
| `web.status()`, run by the page every 10 s | **9.2 s** |
| of which `minute_series` (reads all of `minute.csv`) | 8.5 s |
| of which `analyze.last_minute_end` (reads it all again, called twice) | 3.2 s |
| `analyze.py` from the command line | 2.9 s, nearly all `last_minute_end` |

`events.csv` is tiny (4 ms to load). Both slow paths want only the newest rows of an
append-only file.

### Options compared

| | Today | (b) CSV, read the tail | (c) SQLite, WAL |
|---|---|---|---|
| End of data | 1.7 s | ~0 ms | ~0 ms |
| Rows of the last 24 h | full scan | 14 ms | 5.4 ms |
| `status()` | 9.2 s | 1.0 s | not measured |
| `status()` + captures cached and searched by time | – | **0.38 s** | – |
| Size of `minute` data | 226 MB | 226 MB | 368 MB (+63 %) |
| One-off conversion | – | none | 11 s here (migration of every install) |
| CSV export | already CSV | already CSV | needs a new export path |
| Format change | no | no | yes |

After tail reads the remaining second was all in `captures.jsonl`: `router_during` scans
every capture once per outage (41 × 105 120), `session_starts` walks all of them, and the
file is re-parsed on every poll. Caching the parsed captures until the file changes and
bisecting by time brought it to 0.38 s. What is left is mostly `session_starts`; making
that incremental is the obvious next step and is not prototyped.

Two details from the prototype matter for implementation:

- **Bisect on parsed times, not on text.** Times are local with a UTC offset, which changes
  at daylight saving time, so ISO strings don't sort by instant across the change.
- **A truncated last line must be tolerated.** The tail reader skips lines it can't parse,
  as `load_captures` already does.

### Crash safety

Not tested by cutting power, which can't be done from here. What I could check:

- **Only `events.csv` is fsynced.** `minute.csv` is flushed but not synced, so a power cut
  can lose up to a few minutes of stats. That is acceptable because outages live in
  `events.csv`. Whatever replaces it must keep fsync-per-event for `down` rows.
- **A real gap in the CSV files, found by testing.** If a power cut leaves a torn last line
  in `events.csv`, the restarted monitor appends its `monitor,start` row straight onto it,
  because `_open_csv` doesn't check the file ends with a newline. The two rows merge into
  one garbage row, so a start marker is lost, and a `down` or `up` row that was
  already damaged stays damaged. The analyzer still loads the file, but its monitoring
  periods are wrong. SQLite's atomic commits would avoid this, but so does a three-line
  fix to `_open_csv`: if the file doesn't end in a newline, write one first.
- SQLite with `synchronous=FULL` fsyncs each commit, the same guarantee as the CSV today,
  so it would not weaken event durability. It was not crash-tested.

Procedure still to do on a Pi: write events with an incrementing counter, pull the plug at
random moments, then check that every event acknowledged before the cut is on disk and the
files still parse. Repeat 20 times per option.

### Not measured

- **SD card writes per day.** Estimate only: the monitor flushes `minute.csv` every 15 s,
  so each flush touches a 4 KB page, about 5 760 flushes and 23 MB a day. The real figure
  needs `/proc/diskstats` on the Pi.
- **Monthly splits with daily summaries.** Not needed for the page. They may become useful
  for line-quality views over months (#12), where scanning a month of minutes would cost
  about 2 s here, and likely several times that on a Pi.

## Fit with the principles

- **Never lose an outage (4):** unchanged for the recommendation, and the newline fix
  closes a hole that exists today. Switching to SQLite would put every outage in one
  binary file, where corruption loses much more than a damaged CSV line does.
- **Evidence people can read (2):** CSV stays the stored format, so people and ISPs can
  open it with any tool, with no export step.
- **Stock Pi, standard library only (6):** both options qualify; the recommendation needs
  no new component and no migration.
- **Trade-off:** staying on CSV means each new view that needs history must be written
  with tail reads or bisecting, instead of a free `WHERE` clause. That is more code per
  feature, and the reason SQLite stays on the table for #12 and #13.

## Recommendation

Follow-up issues: #27, #28, #29, #30.

1. **#27 fix(monitor): repair a torn last line when opening the CSV files.** Write a newline
   first if the file doesn't end in one. Test with a truncated `events.csv`. This is the
   only item that touches outage durability, so it comes first.
2. **#28 perf(analyzer): read only the end of `minute.csv`.** Tail-read `last_minute_end` and
   the page's 24 h series, bisecting on parsed times. Tests against the current results.
3. **#29 perf(web): don't re-parse `captures.jsonl` on every poll.** Cache until the file
   changes, find captures by time with bisect, and make `session_starts` incremental.
4. **#30 Benchmark and power-cut test on a Pi 4** with `docs/spikes/005-bench/`, including SD
   writes per day. This closes the spike's "done when". If the page is still over 1 s on
   the Pi, revisit SQLite, or a daily summary file, with those numbers.

## Open questions

- **Pi 4 speed.** Is `status()` under 1 s and the analyzer under 5 s on a year of data?
  Answer: run `bench.py` and the patched paths on the Pi.
- **Does power loss behave?** Answer: the pull-the-plug procedure above.
- **Do #12 and #13 need queries CSV can't serve cheaply?** If so, SQLite (or a derived
  summary file rebuilt from the CSVs) gets reconsidered with the tail-read numbers as the
  baseline.
