# 006: Retention and off-device backup

Issue: #6 · Time spent: within the 0.5 day box · Date: 2026-10-02

## Question

How long should each kind of data be kept, and how does it get off the Pi regularly so an
SD card failure doesn't lose the evidence?

## Answer

**Keep everything.** A year of data is about 260 MB, so a 32 GB card holds more than a
century of it, and nothing the issue proposed to thin out (per-minute stats, periodic
captures) costs enough to be worth losing. Deleting stays an explicit act, backed up first,
as `trim.py` does today. For getting data off the Pi, make a **pull from the user's own
computer or NAS** the documented default: the Pi then holds no credentials, and a day of
growth is about 0.6 MB to move. Offer a push from the Pi as an opt-in extra. One thing
needs care in both: `trim.py` shrinks files, and a plain mirror would then silently throw
away the older data from the backup too.

## What we tried

Synthetic data from spike #5 (a year, 9 targets) and a Mac (Apple M4 Pro, macOS's
`openrsync`). **Not a Pi and not real data.** The synthetic round-trip times are random,
so real files will compress differently, and the Pi's rsync (3.x) was not available here.

### How much data there is

| Data | Per year | Notes |
|---|---|---|
| `minute.csv` | 226 MB (0.62 MB a day) | Per-minute stats for 9 targets |
| `captures.jsonl` | 30 MB | One periodic router capture every 5 minutes |
| `events.csv` | 0.1 MB | Every outage, forever |
| `captures/` raw files | about 3 MB | About 4.8 KB per capture taken during an outage (the test fixtures are 1.0 and 3.8 KB); the synthetic year has about 600 such captures |
| `path.log` | about 1 MB | One line an hour, estimated, not measured |

In total about 260 MB a year. The operating system and its journal share the same card, but
nothing here threatens it.

### Thinning out old per-minute data is not worth it

| Idea | What it saves | What it costs |
|---|---|---|
| Daily rollup instead of per-minute rows | 226 MB becomes 119 KB for a year (3 285 rows) | Loses the per-minute loss and round-trip series that line-quality views (#12) and availability for any past window need. The evidence is not recoverable. |
| Compress closed months | One month of `minute.csv`, 19.2 MB, becomes 2.4 MB with gzip (8.1x, 0.19 s) or 1.4 MB with xz (13.5x, 7 s) | Splitting the file by month, and every reader (analyzer, page, trim) then has to read compressed archives |

Saving 200 MB a year isn't worth either cost, so retention is: keep every file. If a user
with a very small card ever needs it, compressing old months is the way to do it, as a later,
opt-in step.

### Getting it off the Pi

| Option | Credentials on the Pi | Needs | Verdict |
|---|---|---|---|
| **Pull** from the user's computer or NAS with rsync over SSH | None | SSH enabled on the Pi (the README already uses `scp`), something on the user's side to run it daily | **Default.** Documented, plus a small script. |
| **Push** from the Pi (a systemd timer running rsync over SSH) | An SSH key for the destination | A NAS or computer that accepts it | Opt-in extra. The key should be restricted to one directory and root-only, so a compromised Pi can't reach the rest of the NAS. |
| USB stick | None | Mounting, and the stick staying plugged in | A special case of push to a path the user mounts. Not promoted. |
| Cloud services | | | Out of scope. |

A pull never goes through the web page, so router details aren't exposed to the whole
network, and nothing leaves the Pi unless the user sets it up.

### What the tests showed

| Check | Result |
|---|---|
| Whole year as `tar czf` | 28.4 MB. Extracted elsewhere, `analyze.py` gives **identical output** to the original. A restore is just a copy of the files. |
| `rsync --append` after one day of growth (+0.62 MB) | Sent 0.62 MB. So a daily or hourly pull is cheap. |
| `rsync --append` after `trim.py`-style shrinking (a file is replaced by a smaller one) | **The backup copy stayed at the old size and differed from the source.** A plain full copy was correct. |
| Plain `rsync -a` after growth | Resent the whole file, but that is because rsync copies whole files when both ends are local. The delta behaviour over a network was **not** measured. |
| Prefix of the file rewritten while it also grew, with `--append` | Came out identical in `openrsync`, which may check the existing part. **Inconclusive**; real rsync's `--append` does not check, and `--append-verify` does. |

So `--append` and `--append-verify` are for files that **only grow**. Per rsync's manual (not
run here: `openrsync` lacks `--append-verify`), a file whose copy is the same size or longer is
skipped in either mode, so a file that got smaller would never be updated. The backup tool has
to notice that, keep the old copy, and then sync that file without an append flag. Plain
`--append` is also unsafe because it doesn't check that the part already copied is unchanged.

### The trim problem

`trim.py` replaces files with shorter ones. A backup that mirrors the Pi would then replace
its older copy with the trimmed one, and the data trim was told to delete is gone from the
backup too. `trim.py` does write its own safety archive first, but **next to the data
directory on the same SD card**, so it protects against a mistaken trim, not against card
failure.

The backup tool should therefore keep any file that has got smaller: before replacing it,
copy the old version to a dated folder and say so. `trim.py` should also tell the user to
copy its archive off the card, and let them choose where it goes.

## Fit with the principles

- **Never lose an outage (4):** nothing is deleted automatically; the trim case is closed in
  the backup tool instead of left to chance.
- **Private by default (7):** nothing is sent anywhere unless the user runs a pull or turns on
  a push. Data never goes through the unauthenticated page.
- **Stock Pi, standard library only (6):** `rsync` and `ssh` are in Raspberry Pi OS but are
  not Python standard library. Backup is an optional extra: the core works without it.
- **Evidence people can use (2):** a backup is the same CSV files, readable by any tool, as
  the restore test showed.
- **Trade-off:** keeping everything makes the data directory grow forever. At about 260 MB
  a year that is the right side of the trade; revisit if per-minute data ever gets denser.

## Recommendation

Follow-up issues: #49 to #53.

1. **#49 docs: a "Backing up" section in the README.** The pull command with
   `--append-verify`, how often, how to restore, and the warning about trim.
2. **#50 feat(tools): a pull script** for the user's computer that refuses to overwrite a file
   that got smaller until it has kept the old copy, and checks the copy with the analyzer.
3. **#51 feat(trim): put the safety archive where the user says, and say it is on the card.**
   An option for the destination, and a reminder to copy it off.
4. **#52 feat(install): an optional push timer** (`[backup]` in `linemon.conf`: destination and a
   root-only key file), off by default, with the key restricted on the receiving side.
5. **#53 Check on a real Pi and NAS:** rsync 3.x over SSH (transfer size per day, the trim
   case), compression of real files, and SD writes per day.

## Open questions

- **Real rsync.** Does `--append-verify` over SSH send about a day's growth, and does it
  resend a trimmed file? Answer: the Pi and NAS check above.
- **Real compression.** Real round-trip times may compress better or worse than the random
  ones used here. Answer: measure a month of real `minute.csv`.
- **What do users run the pull with?** A cron job, a NAS task or launchd differ; the README
  should cover at least cron. Answer: the first users.
- **SD wear.** The 15 second `minute.csv` flush and the health file (#7) both write to the
  card. Answer: `/proc/diskstats` on the Pi, together with #30.
