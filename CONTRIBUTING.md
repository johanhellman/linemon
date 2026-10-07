# Contributing

## Commit messages

Commits follow [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope): description

Optional body explaining why.
```

- **type:** `feat` (new behaviour), `fix` (bug fix), `docs`, `test`, `refactor`, `perf`,
  `style`, `build`, `ci`, `chore` or `revert`.
- **scope** (optional): the part of linemon it touches: `monitor` (linemon.py),
  `analyzer`, `web`, `install`, `trim`, `tools`, `zte_livebox` or another router script, `docs`, `tests`.
- **description:** imperative and lowercase, e.g. `fix(web): keep the scroll position on refresh`.
- **breaking changes:** add `!` after the type or scope, e.g. `feat(monitor)!: …`, and
  explain in a `BREAKING CHANGE:` footer.

A hook checks this when you commit. Enable it once per clone:

```bash
git config core.hooksPath .githooks
```

CI runs the same check on every push and pull request.

## Tests

```bash
python3 -m unittest discover tests
```

The tests need no network or router: they use sanitised router responses in
`tests/fixtures` and a fake router. When adding support for another router, add its
responses as fixtures with MAC addresses, IP addresses and serial numbers replaced.

## Releases

Versions follow [semantic versioning](https://semver.org); until 1.0 a new feature or a
breaking change raises the minor number (0.2.0), anything else the patch (0.1.1).

1. In a pull request, set the new version in `VERSION`.
2. After it is merged, tag it on `main` and push the tag:
   `git tag -a v0.2.0 -m "linemon 0.2.0" && git push origin v0.2.0`

CI then publishes the release with a source archive, its SHA-256 checksum, and notes listing
the features, fixes and performance changes since the previous tag, taken from the commit
subjects. That is why the subjects should make sense to someone reading the release notes.

## Privacy

Never commit real credentials, MAC or IP addresses, serial numbers or measurement
data. `*.csv` and `linemon-data/` are ignored by git for that reason.
