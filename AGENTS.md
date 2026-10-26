# Repository instructions for coding agents

Read [CONTRIBUTING.md](CONTRIBUTING.md) for contribution, test and DCO sign-off conventions.

## Python server package versioning

- The PyPI **`hardcharger`** package in `python/` tracks the Hardcharger server version in
  `server/CMakeLists.txt`. A server release `X.Y.Z` ships Python package `X.Y.Z`.
- A Python-only packaging, documentation or implementation fix uses a PEP 440 post-release:
  `X.Y.Z.post1`, then `.post2`, etc. Do not increment the Python patch/minor version without
  the corresponding server release. Its `SERVER_VERSION` remains `X.Y.Z`, pointing to the
  existing server release, never a nonexistent `vX.Y.Z.postN` server tag.
- Release candidates use PEP 440 for the package (`X.Y.ZrcN`) and the server's spelling
  (`X.Y.Z-rc.N`) for the download pin. Both refer to the same server release.
- Keep `python/pyproject.toml`, `hardcharger.__version__` and the default `SERVER_VERSION`
  consistent. Run `.github/scripts/check_python_version.py` and its tests when changing
  any server or Python package version. A server release requires the exact matching
  Python package version; post-releases are for subsequent Python-only fixes.
- This rule concerns the `hardcharger` server package. Do not infer a release/version bump
  for the independent ADBC driver or other client packages.

## Native extension defaults

Non-iOS builds use automatic installation and loading of known DuckDB extensions by
default, matching GizmoSQL. Administrators can override those settings or repositories.
Keep signature verification enabled. iOS retains statically included extensions and
disables dynamic extension loading.

For signed/notarized macOS artifacts, the actual `hardcharger-server` executable must
retain `com.apple.security.cs.disable-library-validation=true` so DuckDB can load
extensions signed outside GizmoData's Apple team. This applies inside the desktop app
too: Tauri re-signs bundled sidecars. Verify it after bundling with
`desktop/scripts/check-extension-entitlement.py`. Keep DuckDB's own extension-signature
verification enabled; this entitlement does not replace it.
