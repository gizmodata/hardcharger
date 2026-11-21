"""Check the contents and replacement of cached engine dependencies."""

import hashlib
import os
import pathlib
import runpy
import subprocess
import tarfile

import pytest

from conftest import BASH

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def test_tsan_extension_fixture_refuses_an_uninstrumented_build():
    result = subprocess.run(args=[BASH, str(SCRIPTS / "build-tsan-duckdb.sh"), "duckdb"],
                            env={**os.environ, "CXXFLAGS": "-O2"}, capture_output=True, text=True)
    assert result.returncode == 2
    assert "requires -fsanitize=thread" in result.stderr


def test_tsan_fixture_registers_parquet_before_ducklake_without_disabling_native_loading(tmp_path):
    # DuckLake resolves parquet_scan during initialization. It must already be loaded
    # or autoinstall can download an uninstrumented module and initialize HTTP/secrets.
    project = tmp_path / "CMakeLists.txt"
    project.write_text("""cmake_minimum_required(VERSION 3.20)
project(extension_order NONE)
set(CMAKE_CXX_FLAGS "-fsanitize=thread")
function(duckdb_extension_load name)
  set_property(GLOBAL APPEND PROPERTY registered_extensions "${name}")
endfunction()
function(duckdb_extension_statically_link)
endfunction()
function(find_package)
endfunction()
function(target_link_libraries)
endfunction()
include("${FIXTURE}")
get_property(extensions GLOBAL PROPERTY registered_extensions)
list(FIND extensions parquet parquet_index)
list(FIND extensions ducklake ducklake_index)
if(parquet_index LESS 0 OR ducklake_index LESS 0 OR NOT parquet_index LESS ducklake_index)
  message(FATAL_ERROR "Parquet must load before DuckLake: ${extensions}")
endif()
if(NOT DUCKDB_CAPABILITIES STREQUAL "httplib;loadable_extensions")
  message(FATAL_ERROR "The fixture must retain native runtime extension loading")
endif()
""")
    subprocess.run(args=["cmake", "-S", str(tmp_path), "-B", str(tmp_path / "build"),
                         f"-DFIXTURE={SCRIPTS / 'duckdb-extensions-tsan.cmake'}"],
                   check=True, capture_output=True, text=True)


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


@pytest.mark.parametrize("mismatch", [None, "checksum", "commit", "platform"])
def test_install_verified_engine_sdk_over_cached_install(tmp_path, mismatch):
    source = tmp_path / "sdk"
    include = source / "include"
    metadata = source / "share/hardcharger-engine"
    include.mkdir(parents=True)
    metadata.mkdir(parents=True)
    (include / "duckdb_v2.h").write_text("sdk header")
    commit = "a" * 40
    platform = "macos-arm64"
    (metadata / "commit").write_text(commit)
    (metadata / "platform").write_text(platform)
    archive = tmp_path / f"hardcharger-engine-sdk-{platform}.tar.gz"
    with tarfile.open(name=archive, mode="w:gz") as package:
        package.add(name=source, arcname=".")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    destination = tmp_path / "duckdb-install"
    destination.mkdir()
    (destination / "old").write_text("cached install")
    install = runpy.run_path(str(SCRIPTS / "install-engine-sdk.py"))["install"]

    args = dict(archive=archive, checksums=f"{digest}  ./{archive.name}\n", commit=commit,
                platform=platform, destination=destination, stage=tmp_path / "stage")
    if mismatch:
        key = "checksums" if mismatch == "checksum" else mismatch
        args[key] = "mismatch"
        with pytest.raises(ValueError):
            install(**args)
        assert (destination / "old").read_text() == "cached install"
    else:
        install(**args)
        assert (destination / "include/duckdb_v2.h").read_text() == "sdk header"
        assert not (destination / "old").exists()
