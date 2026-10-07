# 017: Versioned releases and a changelog from commit messages

Issue: #17 · Time spent: about 2 of 4 hours · Date: 2026-10-07

## Question

How should linemon produce version numbers, release notes and a changelog from its
Conventional Commits?

## Answer

Keep it small. One `VERSION` file holds the release number. `install.sh` writes the exact
version installed (`git describe` from a checkout, e.g. `0.1.0-3-gabc1234`) next to the
installed scripts, and the monitor's `start` row, the page and the analyzer state it. A
release is a version bump in a pull request plus a pushed tag; CI builds the archive, its
checksum and the notes from the commit subjects. The GitHub releases are the changelog.

## What we tried

- **A release script** (`tools/release.py`, about 150 lines with tests): suggested the next
  version, wrote `CHANGELOG.md`, committed and tagged. It worked, but it was more machinery
  than a project that releases now and then needs, and it was dropped.
- **release-please** (a GitHub Action that opens release pull requests): no code to keep,
  but a bot in the workflow and a second place where versions are decided. Not tried.
- **Notes from commit subjects** in a few lines of shell in the release workflow: the 51
  commits so far all follow Conventional Commits, and listing `feat`, `fix` and `perf` gives
  readable notes (15 features and 15 fixes for 0.1.0).
- **The installed version.** Running as root in a user's clone, git refuses the repository
  unless it is marked safe, so `install.sh` passes `-c safe.directory`. Without git (an
  unpacked archive) it falls back to `VERSION`.

## Fit with the principles

- **Evidence before features (2):** every run in `events.csv` now says which version
  measured it, and the page and analyzer show it, so a report can state its version (#10).
- **Stock Pi (6):** nothing new on the Pi; git is optional. The release workflow runs on
  GitHub only.
- **Private (7):** the version string is filtered to plain characters before the page shows it.

## Recommendation

- Done in the same pull request: the version mechanism, the release workflow, and the steps
  in `CONTRIBUTING.md`.
- Release 0.1.0 once this is merged.
- #18 builds on the release archive for installing without git.

## Open questions

- A `CHANGELOG.md` in the repository, if someone wants one offline: the release notes can be
  copied into it at release time. Not needed yet.
