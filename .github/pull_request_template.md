<!-- Security fixes: don't describe a vulnerability in a public pull request. Report it privately first (SECURITY.md). -->

## What this changes

<!-- What and why, in a few sentences. Link the issue: "Fixes #123". -->

## How it was tested

<!-- The tests you added or ran (unit, server suite, conformance, UI), on which platforms, with counts. -->

## Checklist

- [ ] Every commit is signed off (`git commit -s`, [DCO](https://developercertificate.org/); see [CONTRIBUTING.md](../CONTRIBUTING.md)).
- [ ] A line in `CHANGELOG.md` under `## [Unreleased]` (Added / Changed / Fixed), for anything a user would notice.
- [ ] Tests for the behaviour added or fixed; a negative test asserts the exact error.
- [ ] Documentation updated (`docs/`, `README.md`, `--help`) where behaviour or flags changed.
- [ ] No credentials, customer data or internal host names in code, tests, fixtures, logs or commit messages.
- [ ] Python call sites use keyword arguments.
