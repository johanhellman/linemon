# 004: One config file for the monitor and its extras

Issue: #4 · Time spent: within the 0.5 day box · Date: 2026-10-01

## Question

What should a single configuration file for linemon look like, so that later features
(notifications, more paths, integrations) have one place for their settings?

## Answer

One INI file, `/etc/linemon/linemon.conf`, read with `configparser`, one section per
feature. Precedence is defaults, then the file, then command-line arguments. The file
holds **no secrets**: anything secret stays in its own root-only file that the config
names, as `router.conf` does today. Existing installs keep working untouched, because
`LINEMON_ARGS` stays valid and keeps winning over the file.

## What we tried

A throwaway prototype (not merged) built the argument parser from one table of
`(section, key, flag, type, default)` and fed the file's values in with
`set_defaults`. Results:

| Case | Result |
|---|---|
| No file | Identical to today's defaults |
| File sets `threshold = 5` and a hook | Both take effect |
| `--threshold 4` on the command line | Wins over the file |
| Today's unit: `--iface eth0 --data /var/lib/linemon $LINEMON_ARGS` | **The hard-coded flags win over the file**, so a `[monitor] iface` would be silently ignored |
| `threshhold = 5` (typo) | Rejected: `unknown setting [monitor] threshhold` |
| `threshold = many` | Rejected with the key and the bad value |
| Broken section header | Rejected with the file and line |
| `command = /opt/a%b.py` | Kept literally, but only with `ConfigParser(interpolation=None)`. The default raises on `%`. |

Driving the parser and the file from one table also keeps flags, file keys and the
example file from drifting apart.

The prototype did not cover systemd behaviour (restart loops, `RestartPreventExitStatus`)
or an upgrade on a real Pi; those need a check there.

## Decisions

**Format and location.** INI via `configparser(interpolation=None)` at
`/etc/linemon/linemon.conf`, overridable with `--config`. Sections: `[monitor]`, `[hook]`
now; later features add `[notify.<name>]`, `[path.<name>]` (backup link, #13),
`[unifi]` (#14), `[metrics]` (#15). Every optional extra has `enabled = no` as its
default. The full current option set is in
[`linemon.conf.example`](../../linemon.conf.example).

**Precedence.** Defaults < file < command line. `LINEMON_ARGS` is just more command
line, so it overrides the file; nothing changes for anyone who uses it today.

**Strict, not forgiving.** Unknown keys, bad values and parse errors are errors. A
silently ignored typo in a monitor that runs unattended is worse than a refusal.

**Secrets.** Secrets live in separate files readable by root only, named from the
config (`secret_file = …`); `router.conf` already is one. The reasons:
- `linemon.conf` can then be pasted into a bug report, which protects principle 7.
- Nothing about the web service changes: it runs as an unprivileged dynamic user and
  `/etc/linemon` is mode 700, so it can read neither.
- Extras stay optional (see "Core and extras" in `AGENTS.md`): `router.conf` belongs to
  the router hook alone. The core monitor never reads it, and `linemon.conf` mentions
  it only through `[hook] command`, which names the script that does. Someone who
  doesn't use an extra has no secret on disk for it.
- A secrets file's mode can be checked on its own. Follow-up: warn, and refuse for new
  files, when a secrets file is readable by group or others.

**What the web page reads.** Nothing. `web.py` keeps its own flags (`--port`, `--data`)
in its unit. If the page ever needs a setting, it gets a separate, non-secret file; see
open questions.

**Environment variables.** Not added. systemd's `EnvironmentFile` already covers that
use, and a third source would make "where did this value come from" harder to answer.

**Bad config at start.** The monitor exits with status 78 (`EX_CONFIG`) and a clear
message in the journal. Because a stopped monitor means unknown time (principle 3), the
bad config must be caught *before* the restart: `install.sh` runs
`linemon.py --check-config` first and aborts without touching the running monitor. The
unit gets `RestartPreventExitStatus=78` so a typo doesn't become a restart loop.

**Changing the config.** Out of scope for the spike: no reload. Edit the file, then
`systemctl restart linemon`. That starts a new run in `events.csv`, so edit sparingly.

## Migration from `/etc/default/linemon`

An existing Pi can follow this in order, and every step is safe to stop after:

1. **Upgrade.** The new `linemon.service` drops the hard-coded `--iface eth0 --data
   /var/lib/linemon` (the code's defaults are the same) and keeps `$LINEMON_ARGS`.
   Without this, the hard-coded flags would shadow the file, as the table shows. Because
   the unit changes, `install.sh` restarts the monitor once on this upgrade, which
   starts a new run in `events.csv`. Behaviour is otherwise identical.
2. **Optional: move to the file.** `linemon.py --migrate` reads the current
   `LINEMON_ARGS` and **prints** the equivalent `linemon.conf` to stdout. It never writes
   or deletes anything. The user reviews it, saves it as `/etc/linemon/linemon.conf`,
   empties `/etc/default/linemon` and restarts.
3. **Hint, not a rewrite.** If `install.sh` finds `LINEMON_ARGS` and no `linemon.conf`,
   it prints one line pointing at `--migrate`. It never converts anything on its own.

Both mechanisms keep working side by side indefinitely, so nobody is forced to migrate.

## Fit with the principles

- **Runs unattended on a stock Pi (6):** standard library only; defaults reproduce
  today's behaviour; an invalid config is caught at install time, not at the next
  reboot.
- **Private by default (7):** no secrets in the shareable file, nothing new readable by
  the web service, extras off by default.
- **Honest about what it doesn't know (3):** the restart-loop and early-validation
  choices exist to avoid a silently stopped monitor.
- **Observe, don't interfere (5):** configuration only changes what linemon does itself.
- **Trade-off:** strictness means an upgrade can refuse to restart on a config the old
  code tolerated. That only applies to a `linemon.conf` the user wrote, and the old
  monitor keeps running while the message tells them what to fix.

## Recommendation

Follow-up issues: #23, #24, #25.

1. **#23 feat(monitor): read `/etc/linemon/linemon.conf`.** Table-driven options, `--config`,
   `--check-config`, `--migrate` (prints only), exit status 78, `interpolation=None`.
   Tests for precedence, typos, bad values and `%`. Update `README.md` and
   `docs/checks.md`.
2. **#24 feat(install): validate the config before restarting.** Drop the hard-coded flags
   and add `RestartPreventExitStatus=78` in `linemon.service`, run `--check-config` in
   `install.sh`, print the migration hint, and install the example as
   `/etc/linemon/linemon.conf.example`. Check on a Pi: upgrade with and without
   `/etc/default/linemon`.
3. **#25 feat(monitor): check secrets file permissions.** Warn when a secrets file
   (starting with `router.conf`) is readable by group or others.

Spikes #7, #9, #13, #14 and #18 can then add sections without inventing their own
mechanism.

## Open questions

- **Web settings.** If the page needs a setting, where does it live? `/etc/linemon` is
  mode 700 for the secrets. Answer: decide when the first web setting appears, likely
  a separate world-readable file.
- **Restart behaviour on a Pi.** Does `RestartPreventExitStatus=78` plus the install-time
  check behave as intended? Answer: test on the Pi with a deliberately bad file.
- **Multiple hooks.** The hook section holds one command. If a second capture script is
  ever wanted, `[hook.<name>]` would be the shape; not needed now.
