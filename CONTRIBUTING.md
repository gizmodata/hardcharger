# Contributing to Hardcharger

Thank you for helping. Bug reports, questions and pull requests are welcome in this repository's
[issues](../../issues) and [pull requests](../../pulls). For a security problem, don't open an
issue: see [SECURITY.md](SECURITY.md).

## Licensing of contributions

Hardcharger is licensed under the [Apache License 2.0](LICENSE). Per section 5 of that license, any
contribution you intentionally submit for inclusion is licensed under Apache-2.0, without any
additional terms or conditions. There is no contributor license agreement to sign.

## Developer Certificate of Origin (sign-off)

Every commit must carry a `Signed-off-by` line certifying the
[Developer Certificate of Origin 1.1](https://developercertificate.org/): that you wrote the
change, or have the right to submit it under the project's license. Add it with

```bash
git commit -s
```

which appends `Signed-off-by: Your Name <you@example.com>` from your git `user.name` and
`user.email`. Use a real name. Pull requests whose commits lack the sign-off are not merged; fix
one commit with `git commit --amend -s`, or a series with `git rebase --signoff <base>`.

## Pull requests

- Open an issue first for anything larger than a bug fix, so the design is agreed before the work.
- Keep a pull request to one change. Add tests for behaviour you add or fix, and run the checks
  the README (or `docs/`) describes before pushing; CI runs the same checks on every pull request.
- Add a line to the `[Unreleased]` section of [CHANGELOG.md](CHANGELOG.md)
  (Keep a Changelog format: Added / Changed / Fixed).
- Python code uses keyword arguments at call sites (`f(table_name=name, mode="create")`).
- Never include credentials, customer data or internal host names in code, tests, fixtures or
  commit messages.

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
