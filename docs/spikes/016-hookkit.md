# 016: Make it easy to add a capture script for another router

Issue: #16 · Time spent: about 2 of 8 hours · Date: 2026-10-07

## Question

What would let someone who owns a different ISP router add a capture script for it safely,
without help from the maintainers?

## Answer

A guide and a template are enough. [docs/router-hooks.md](../router-hooks.md) walks through
finding the router's status pages with the browser's developer tools, writing the one
router-specific function, testing it against a fake router, and sanitising fixtures, using the
Livebox script as the example. [routers/template.py](../../routers/template.py) has everything
else a hook must get right: the settings, the 30-minute back-off after a rejected login, fixed
error messages with details on stderr, and the JSON line.

## What we tried

- **Redoing the Livebox script from the template**, as the issue asks: a test fills the
  template's `read_status()` with the Livebox protocol and runs it against the fake Livebox. It
  captures, backs off after a rejected login without a second attempt, keeps the password out
  of the journal, and an unfinished copy reports `capture script not finished`.
- **A generic fake-router base for tests:** not built. Routers differ most in exactly what such
  a base would have to abstract (login flow, tokens, page state), so a framework would be
  harder to follow than the Livebox fake router it would replace. The guide points at that one
  to copy.
- **A fixture sanitiser tool:** not built. A checklist and one `grep` in the guide catch the
  same things (addresses, MACs, serials, tokens) for the few fixtures a router needs.

## Fit with the principles

- **Observe, don't interfere (5):** the guide and template say read only, and the back-off
  protects the router's admin account.
- **Private by default (7):** fixed messages on the page, details on stderr, sanitised fixtures.
- **Proportional (8):** two files instead of four pieces of machinery, until there is a second
  router script to learn from.

## Recommendation

- Done: the guide, the template and the test; the README and AGENTS.md point to them.
- No follow-up issues. Revisit the fake-router base if a second router script is contributed.

## Open questions

- Whether someone without linemon experience can follow the guide: the first contributor will
  show.
