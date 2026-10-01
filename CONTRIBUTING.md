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
  `analyzer`, `web`, `install`, `trim`, `zte_livebox` or another router script, `docs`, `tests`.
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

## Privacy

Never commit real credentials, MAC or IP addresses, serial numbers or measurement
data. `*.csv` and `linemon-data/` are ignored by git for that reason.
