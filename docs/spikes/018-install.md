# 018: An install and upgrade path others can trust

Issue: #18 · Time spent: about 1 of 8 hours · Date: 2026-10-07

## Question

What is the best way for other people to install and upgrade linemon: git clone plus
install.sh, a one-line installer, or a Debian package?

## Answer

Keep `install.sh` as the one installer, and offer two ways to get it: a git clone, or a
release archive checked against its SHA-256 file (from #17). Both are readable before anything
runs as root, both upgrade the same way, and neither needs anything beyond Raspberry Pi OS. A
one-line `curl | bash` installer is rejected, and a Debian package is parked.

## What we tried

- **The release archive.** `git archive` of the release branch is 568 KB, holds everything
  `install.sh` copies plus `VERSION`, and leaves out `.github`. Without `.git`, `install.sh`
  records the version from `VERSION` (0.1.0).
- **Upgrades keep data and settings.** Checked on the Pi on 06/10 under #24: upgrading with
  and without `/etc/default/linemon` and with a broken `linemon.conf` kept the data, the
  settings and the router password, and stopped before touching a working monitor when the
  settings were wrong. The same `install.sh` runs from an archive, so the result holds there.
- **A Debian package.** Not built. What it would need: maintainer scripts that repeat
  `install.sh`'s logic (check the settings first, restart the monitor only if it changed,
  never touch `/var/lib/linemon`), conffiles for `/etc/linemon`, and a way to take over an
  existing git install. Without an apt repository it would be downloaded by hand like the
  archive, so it adds no automatic upgrades, only a second installer to keep in step.
- **A one-line installer.** Piping a download into a root shell runs code nobody has read,
  on a device that is meant to be trusted as a witness.

## Fit with the principles

- **Runs unattended on a stock Pi (6):** both paths need only Raspberry Pi OS; git is
  optional.
- **Evidence before features (2):** the installed version is recorded either way (#17).
- **Never lose an outage (4):** one installer means one upgrade path to get right.

## Recommendation

- Done: the README's install section covers both paths and the checksum (in #77).
- Parked in the roadmap: a Debian package, until there is an apt repository worth publishing
  to or people ask for one.
- No follow-up issues.

## Open questions

- Installing from an archive on a fresh Pi has not been run end to end; the first release
  is the natural moment to do it.
