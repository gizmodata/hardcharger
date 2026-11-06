"""A successful release can skip cache warm-up, but never a required test gate."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("already-tested.sh")
RELEASE_JOBS = [
    "web UI (build)", "web UI (lint, typecheck, unit tests, client provenance)",
    "build and test (linux-amd64)", "build and test (linux-arm64)", "build and test (macos-arm64)",
    "install on glibc 2.28 (linux-amd64)", "install on glibc 2.28 (linux-arm64)",
    "web UI e2e (chromium)", "web UI e2e (webkit)",
    "windows / build and test (windows-amd64)", "windows / build and test (windows-arm64)",
    "windows / msi (windows-amd64)", "windows / msi (windows-arm64)", "checksums", "image",
    "address sanitizer (linux-amd64)", "thread sanitizer (linux-amd64)", "release", "downstream",
]
CACHE = "warm sanitizer dependency caches (${{ matrix.name }})"


class AlreadyTestedTests(unittest.TestCase):
    def check_run(self, *, cache="skipped", failed=None, extra=None, artifacts=True):
        jobs = [("failure" if name == failed else "success", name) for name in RELEASE_JOBS]
        jobs.append((cache, CACHE))
        if extra:
            jobs.append(extra)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "jobs").write_text("".join(f"{status}\t{name}\n" for status, name in jobs))
            (root / "artifacts").write_text("hardcharger-linux-amd64\nhardcharger-linux-arm64\n" if artifacts else "")
            gh = root / "gh"
            gh.write_text('''#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = " ".join(sys.argv)
root = Path(os.environ["TEST_GH_FIXTURE"])
if "workflows/server.yml/runs?" in args:
    print("17 push v2.9.0")
elif "/runs/17/jobs?" in args:
    print((root / "jobs").read_text(), end="")
elif "/runs/17/artifacts?" in args:
    print((root / "artifacts").read_text(), end="")
else:
    raise SystemExit(1)
''')
            gh.chmod(0o755)
            result = subprocess.run(args=["bash", str(SCRIPT), "example/repo", "a" * 40, "18"],
                                    env={**os.environ, "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                                         "TEST_GH_FIXTURE": directory},
                                    capture_output=True, text=True, check=True)
            return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def test_skipped_cache_warmup_allows_the_successful_release(self):
        result = self.check_run()
        self.assertEqual(result["tested"], "true")
        self.assertEqual(result["run"], "17")

    def test_failed_cache_job_is_not_ignored(self):
        self.assertEqual(self.check_run(cache="failure")["tested"], "false")

    def test_failed_sanitizer_still_blocks_reuse(self):
        self.assertEqual(self.check_run(failed="thread sanitizer (linux-amd64)")["tested"], "false")

    def test_unknown_skipped_job_still_blocks_reuse(self):
        self.assertEqual(self.check_run(extra=("skipped", "new release gate"))["tested"], "false")

    def test_missing_artifacts_still_block_reuse(self):
        self.assertEqual(self.check_run(artifacts=False)["tested"], "false")


if __name__ == "__main__":
    unittest.main()
