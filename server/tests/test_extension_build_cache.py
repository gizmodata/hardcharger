"""Old dependency installs must not reintroduce retired static extensions or patches."""

import os
import pathlib
import subprocess

from conftest import BASH

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def test_tsan_extension_fixture_refuses_an_uninstrumented_build():
    result = subprocess.run(args=[BASH, str(SCRIPTS / "build-tsan-duckdb.sh"), "duckdb"],
                            env={**os.environ, "CXXFLAGS": "-O2"}, capture_output=True, text=True)
    assert result.returncode == 2
    assert "requires -fsanitize=thread" in result.stderr


def cmake(script, **definitions):
    subprocess.run(args=["cmake", *[f"-D{k}={v}" for k, v in definitions.items()], "-P", str(SCRIPTS / script)],
                   check=True, capture_output=True, text=True)


def test_pruning_a_stale_native_install_keeps_standard_extensions_and_runtime_files(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ["httpfs", "postgres_scanner", "ducklake", "tpch", "autocomplete", "json"]:
        for archive in [f"lib{name}_extension.a", f"{name}_extension.lib"]:
            (lib / archive).write_text("cached archive")
    module = lib / "tpch.duckdb_extension"
    module.write_text("runtime module")
    cmake("prune-duckdb-install.cmake", DUCKDB_INSTALL_DIR=tmp_path)
    assert sorted(p.name for p in lib.iterdir()) == ["json_extension.lib", "libjson_extension.a",
                                                    "tpch.duckdb_extension"]


def test_retired_patch_cleanup_preserves_upstream_patches_and_is_idempotent(tmp_path):
    source = tmp_path / "duckdb"
    patches = source / ".github/patches/extensions/ducklake"
    patches.mkdir(parents=True)
    upstream = patches / "0018-upstream.patch"
    upstream.write_text("keep")
    retired = patches / "hardcharger-keep-metadata-storage-version.patch"
    retired.write_text("retired")
    deps = tmp_path / "duckdb-build/_deps"
    fetched = deps / "ducklake_extension_fc-src"
    fetched.mkdir(parents=True)
    (fetched / "patched-source.cc").write_text("old patch")
    stamp = deps / ".hardcharger-ducklake-patches"
    stamp.write_text("old hash")
    for _ in range(2):
        cmake("retire-ducklake-patch.cmake", DUCKDB_SOURCE_DIR=source)
        assert upstream.read_text() == "keep"
        assert not retired.exists() and not fetched.exists() and not stamp.exists()
