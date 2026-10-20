#!/usr/bin/env python3
"""Tests of .github/scripts/versions_json.py, against a small checkout it builds (a git
repository with the files the script reads and a warp-sql submodule). server.yml's `changes`
job runs it on every run (a few seconds); the build job runs the script itself on the real
checkout, so a pin whose format changes fails there, not on a tag.

    python3 .github/scripts/test_versions_json.py
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

DUCKDB_COMMIT = "d54cbbe3df17e8e5ce68312f483b4155836112aa"
HTTPFS = "77392a72077a85897db2519fae11e2c0da972b8a"
DUCKLAKE = "417a9117f590e8141742ac0579d02f6a90162e37"

CMAKELISTS = textwrap.dedent("""\
    cmake_minimum_required(VERSION 3.25)
    project(hardcharger VERSION 2.0.0 LANGUAGES C CXX)
    set(HARDCHARGER_VERSION_SUFFIX "")
""")
EXTENSIONS = textwrap.dedent(f"""\
    # duckdb_extension_load(commented_out) is not one
    duckdb_extension_load(tpch)
    duckdb_extension_load(httpfs
        GIT_URL https://github.com/duckdb/duckdb-httpfs
        GIT_TAG {HTTPFS}
    )
    duckdb_extension_load(ducklake
        GIT_URL https://github.com/duckdb/ducklake
        GIT_TAG {DUCKLAKE}
        APPLY_PATCHES
    )
    duckdb_extension_statically_link(tpch httpfs ducklake)
""")
BUILD_DUCKDB = f"#!/usr/bin/env bash\nDUCKDB_COMMIT={DUCKDB_COMMIT}\nDUCKDB_VERSION_NAME=v2.0.0\n"
WARP_CMAKELISTS = "cmake_minimum_required(VERSION 3.25)\nproject(warpsql VERSION 1.0.1 LANGUAGES C CXX)\n"
OPENAPI = "openapi: 3.1.1\ninfo:\n  title: Warp SQL\n  version: 1.0.1\n  summary: x\npaths: {}\n"


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(args=["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class Checkout:
    """A fake hardcharger checkout: release.sh, the files versions_json.py reads, and
    third_party/warp-sql as a submodule (a gitlink to a commit of a nested repository)."""

    def __init__(self, base: Path):
        self.root = base / "hardcharger"
        warp = self.root / "third_party" / "warp-sql"
        write(warp / "cpp" / "CMakeLists.txt", WARP_CMAKELISTS)
        write(warp / "cpp" / "scripts" / "build-duckdb.sh", BUILD_DUCKDB)
        write(warp / "spec" / "openapi.yaml", OPENAPI)
        git(warp.parent, "init", "-q", str(warp))
        git(warp, "add", "-A")
        git(warp, "commit", "-q", "-m", "warp-sql")
        self.warp_commit = git(warp, "rev-parse", "HEAD")
        write(self.root / "server" / "CMakeLists.txt", CMAKELISTS)
        write(self.root / "server" / "scripts" / "duckdb-extensions.cmake", EXTENSIONS)
        (self.root / ".github" / "scripts").mkdir(parents=True)
        for name in ("release.sh", "versions_json.py"):
            shutil.copy(HERE / name, self.root / ".github" / "scripts" / name)
        git(self.root, "init", "-q")
        git(self.root, "add", "server", ".github")
        git(self.root, "update-index", "--add", "--cacheinfo", f"160000,{self.warp_commit},third_party/warp-sql")
        git(self.root, "commit", "-q", "-m", "hardcharger")
        self.commit = git(self.root, "rev-parse", "HEAD")

    def edit(self, relative: str, old: str, new: str) -> None:
        path = self.root / relative
        text = path.read_text(encoding="utf-8")
        assert old in text, (relative, old)
        path.write_text(text.replace(old, new), encoding="utf-8")

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(args=[sys.executable, str(self.root / ".github" / "scripts" / "versions_json.py"),
                                    "--root", str(self.root), *args], capture_output=True, text=True, timeout=60)

    def fake_server(self, version: str, duckdb: str) -> Path:
        path = self.root.parent / "hardcharger-server"
        path.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            case "$1" in
              --version) echo "Hardcharger {version}" ;;
              --print-duckdb-version) echo "{duckdb}" ;;
              *) exit 2 ;;
            esac
        """), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path


class VersionsJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.checkout = Checkout(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def ok(self, *args: str) -> dict:
        out = self.checkout.run(*args)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def refused(self, why: str, *args: str) -> None:
        out = self.checkout.run(*args)
        self.assertEqual(out.returncode, 1, f"exited {out.returncode}, want 1: {out.stdout}{out.stderr}")
        self.assertIn(why, out.stderr)
        self.assertEqual(out.stdout, "", "nothing is printed when it fails")

    def test_the_document(self):
        self.assertEqual(self.ok(), {
            "hardcharger": {"version": "2.0.0", "commit": self.checkout.commit},
            "warp_sql": {"version": "1.0.1", "spec_version": "1.0.1", "commit": self.checkout.warp_commit},
            "duckdb": {"version": "v2.0.0", "commit": DUCKDB_COMMIT},
            "extensions": {
                "built_in": ["core_functions", "parquet", "json", "icu", "tpch", "httpfs", "ducklake"],
                "pinned": {"httpfs": {"repository": "https://github.com/duckdb/duckdb-httpfs", "commit": HTTPFS},
                           "ducklake": {"repository": "https://github.com/duckdb/ducklake", "commit": DUCKLAKE}},
                "repository": "https://extensions.duckdb.org/v2.0.0",
            },
        })

    def test_a_release_candidate_version(self):
        self.checkout.edit("server/CMakeLists.txt", 'SUFFIX "")', 'SUFFIX "-rc.2")')
        self.assertEqual(self.ok("--tag", "v2.0.0-rc.2")["hardcharger"]["version"], "2.0.0-rc.2")

    def test_the_tag_must_be_the_version(self):
        self.assertEqual(self.ok("--tag", "v2.0.0")["hardcharger"]["version"], "2.0.0")
        self.refused("tag v2.0.0-rc.2 != v2.0.0", "--tag", "v2.0.0-rc.2")

    def test_the_binary_must_say_the_same_versions(self):
        self.ok("--binary", str(self.checkout.fake_server(version="2.0.0", duckdb="v2.0.0")))
        self.refused("--version says 'Hardcharger 2.0.0-rc.1'", "--binary",
                     str(self.checkout.fake_server(version="2.0.0-rc.1", duckdb="v2.0.0")))
        self.refused("--print-duckdb-version says 'v2.0.0-alpha48624'", "--binary",
                     str(self.checkout.fake_server(version="2.0.0", duckdb="v2.0.0-alpha48624")))

    def test_the_checked_out_submodule_must_be_the_recorded_one(self):
        warp = self.checkout.root / "third_party" / "warp-sql"
        (warp / "x").write_text("x", encoding="utf-8")
        git(warp, "add", "x")
        git(warp, "commit", "-q", "-m", "moved on")
        self.refused("HEAD records " + self.checkout.warp_commit)

    def test_a_malformed_or_missing_duckdb_pin_is_refused(self):
        self.checkout.edit("third_party/warp-sql/cpp/scripts/build-duckdb.sh", f"DUCKDB_COMMIT={DUCKDB_COMMIT}",
                           "DUCKDB_COMMIT=d54cbbe3df")
        self.refused("build-duckdb.sh's DUCKDB_COMMIT: expected exactly one match, found 0")

    def test_a_malformed_duckdb_version_name_is_refused(self):
        self.checkout.edit("third_party/warp-sql/cpp/scripts/build-duckdb.sh", "DUCKDB_VERSION_NAME=v2.0.0",
                           "DUCKDB_VERSION_NAME=")
        self.refused("build-duckdb.sh's DUCKDB_VERSION_NAME: expected exactly one match, found 0")

    def test_an_extension_pin_that_is_not_a_commit_is_refused(self):
        self.checkout.edit("server/scripts/duckdb-extensions.cmake", f"GIT_TAG {HTTPFS}", "GIT_TAG main")
        self.refused("httpfs: expected one GIT_URL and one GIT_TAG commit")

    def test_a_loaded_extension_that_is_not_linked_is_refused(self):
        self.checkout.edit("server/scripts/duckdb-extensions.cmake", "statically_link(tpch httpfs ducklake)",
                           "statically_link(tpch httpfs)")
        self.refused("is not the loaded set")

    def test_a_missing_warp_sql_version_is_refused(self):
        self.checkout.edit("third_party/warp-sql/spec/openapi.yaml", "  version: 1.0.1\n", "")
        self.refused("info.version: expected exactly one match, found 0")


if __name__ == "__main__":
    unittest.main(verbosity=1)
