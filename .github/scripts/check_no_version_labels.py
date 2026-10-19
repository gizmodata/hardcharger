#!/usr/bin/env python3
"""Fail if the prose labels Hardcharger with a 0.x version ("Hardcharger 0.3.0", "Hardcharger v0.2").

TEMPORARY: until Hardcharger 2.0.0 (its first public release), comparisons and notes say just
"Hardcharger", or "Hardcharger, commit <sha>" to pin a build. Retire this check at 2.0.0 by deleting
this script and the "No version labels" step in .github/workflows/workflow.yml.

Skipped: CHANGELOG.md (history belongs there). Install pins and image tags (`HARDCHARGER_VERSION=0.3.0`,
`hardcharger:0.3.0`) don't match: the pattern needs "Hardcharger", a space and the number.

    python3 .github/scripts/check_no_version_labels.py
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATTERN = re.compile(r"\bhardcharger\s+v?0\.\d+", re.I)


def main() -> int:
    files = subprocess.run(
        ["git", "ls-files", "README.md", "docs"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.split()
    bad = []
    for name in files:
        if not name.endswith(".md"):
            continue
        for number, line in enumerate((ROOT / name).read_text(encoding="utf-8").splitlines(), 1):
            if PATTERN.search(line):
                bad.append(f"{name}:{number}: {line.strip()[:140]}")
    if bad:
        print('Hardcharger version label in the docs. Until Hardcharger 2.0.0, say "Hardcharger" '
              'or "Hardcharger, commit <sha>", not "Hardcharger 0.x":')
        print("\n".join(bad))
        return 1
    print("no Hardcharger version labels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
