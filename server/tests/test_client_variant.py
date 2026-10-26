"""Both the native renderer's encoding fixtures and real SQL/Arrow VARIANT output."""
import os
import json
import subprocess

import pytest

from conftest import CLIENT_BINARY, run_client_tests


def test_variant_renderers():
    results = run_client_tests(gtest_filter="VariantRendererTest.*")
    assert results and all(results.values()), results


@pytest.mark.parametrize("mode", ["json", "jsonlines", "csv", "box"])
def test_cli_variant_values(launcher, mode):
    server = launcher.start()
    sql = ("SELECT 42::VARIANT AS scalar, {'a': 1, 'b': [true, NULL, false]}::VARIANT AS obj, "
           "[42::VARIANT, NULL::VARIANT, 'hi'::VARIANT]::VARIANT AS arr, NULL::VARIANT AS n, 9007199254740993::BIGINT::VARIANT AS big")
    result = subprocess.run([str(CLIENT_BINARY), "--host", "127.0.0.1", "--port", str(server.port),
                             "--username", server.username, "--tls=false",
                             "--no-init", "--quiet", "--command", f".mode {mode}\n{sql};"], capture_output=True, text=True, timeout=30,
                             env={**os.environ, "HARDCHARGER_PASSWORD": server.password})
    assert result.returncode == 0, result.stderr
    assert "metadata" not in result.stdout and "9007199254740993" in result.stdout, result.stdout
    if mode in ("json", "jsonlines"):
        row = json.loads(result.stdout)
        if mode == "json":
            [row] = row
        assert row == {"scalar": 42, "obj": {"a": 1, "b": [True, None, False]}, "arr": [42, None, "hi"],
                       "n": None, "big": 9007199254740993}
    else:
        assert "42" in result.stdout and "true" in result.stdout and "hi" in result.stdout


def test_cli_nested_variants(launcher):
    server = launcher.start()
    result = subprocess.run([str(CLIENT_BINARY), "--host", "127.0.0.1", "--port", str(server.port),
                             "--username", server.username, "--tls=false", "--no-init", "--quiet", "--json",
                             "--command", "SELECT {'nested': 42::VARIANT} AS obj, [42::VARIANT, 'hi'::VARIANT] AS arr"],
                            capture_output=True, text=True, timeout=30,
                            env={**os.environ, "HARDCHARGER_PASSWORD": server.password})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [{"obj": {"nested": 42}, "arr": [42, "hi"]}]
