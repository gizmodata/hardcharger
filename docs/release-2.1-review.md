# v2.1.0 release review

Philip approved shipping v2.1.0 on 2026-10-26, explicitly gated on successful tests.
Merge and tag only after the combined release commit passes the full signed matrix.

## Required ingestion fix

- Include local `main` commit `9e95ef51e4f957742bc492e0eb5bd65e56bd2dd7`
  (**Bulk ingest appends by name (Warp SQL 1.0.2)**) in the final release ancestry.
- Preserve its Warp SQL pin `f0be6977d795fd84bba479fda2a42a80dbc47e0f` (v1.0.2),
  or a later pin that retains the fix. Do not overwrite it with the desktop preview's
  earlier SDK pin when integrating the branches.
- Run `server/tests/test_bulk_ingest_py.py::test_append_binds_columns_by_name` on the
  final binaries: swapped same-type columns must land by name, omitted columns take
  defaults, and unknown columns fail without writing rows. Include `create_append`
  coverage from Warp SQL's conformance suite.
- Re-run the server, SDK and conformance suites after SDK/branch integration. The
  desktop preview's results against its older SDK do not validate this integration.

The ingestion fix was merged from local `main` into `release/v2.1.0`, preserving
its original commit ancestry. The newer SDK pin additionally contains upload limits. Before tagging the final
`main` commit, verify `git merge-base --is-ancestor 9e95ef51 HEAD` succeeds and inspect
the actual submodule pin. This keeps the customer-reported column-order fix in the
release even if other branches or pins change before approval.

## Final artifact checks

- Run the complete CI matrix on the reviewed, integrated commit, including web UI
  end-to-end tests and signed desktop installers.
- Verify macOS signatures, notarization/stapling, extension loading, installation
  and Keychain behavior across signed upgrades; verify Windows Authenticode and MSI
  installation on supported architectures.
- Publish server/Python package version 2.1.0 together; later Python-only fixes use
  `2.1.0.postN`. App-store distribution remains separate future work.

## Backpressure scope

v2.1.0 includes only Gap 3: per-message wire and expanded Arrow upload allocation limits.
The worker scheduling change is tracked in [issue #4](https://github.com/gizmodata/hardcharger/issues/4).
Aggregate retained/unretained memory budgets are tracked in [issue #5](https://github.com/gizmodata/hardcharger/issues/5).
The existing broader limits branch is preserved; those budgets are excluded from this release.
