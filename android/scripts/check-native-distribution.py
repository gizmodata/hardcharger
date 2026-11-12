#!/usr/bin/env python3
"""Record native metadata only after checking the linked extension capability."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

unstripped, packaged, distribution, nm, output = sys.argv[1:]
def require(condition, message):
    if not condition:
        raise SystemExit(message)

require(distribution in ("local", "play"), "Unknown distribution")
symbols = subprocess.check_output([nm, "--defined-only", unstripped], text=True)
capability = "duckdb_extension_loadable_extensions_describe"
require((capability in symbols) == (distribution == "local"), "Incorrect executable-extension capability")
for name in ("autocomplete", "tpch", "httpfs", "ducklake", "postgres_scanner", "core_functions", "parquet", "json", "icu"):
    require(f"duckdb_extension_{name}_describe" in symbols, f"Missing bundled extension: {name}")
digest = hashlib.sha256()
with Path(packaged).open("rb") as binary:
    for chunk in iter(lambda: binary.read(65536), b""):
        digest.update(chunk)
metadata = {"distribution": distribution, "sha256": digest.hexdigest()}
Path(output).write_text(json.dumps(metadata, indent=2) + "\n")
print(f"Verified {distribution} native artifact; external extensions {'included' if distribution == 'local' else 'excluded'}.")
