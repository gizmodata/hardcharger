"""Port of GizmoSQL 1.x tests/integration/test_inline_license_key.cpp (10 cases).

1.x unit-tested its license manager in-process; here every case starts the server and
reads the outcome where a user sees it: the edition (GIZMOSQL_EDITION()) or a refusal to
start ("License Error", exit 1). The two "reinitializing" cases become restarts: each
process loads its license once, so a restart without a key (or with a bad one) is how
earlier entitlements go away. 1.x skipped 7 cases without GIZMOSQL_LICENSE_KEY_FILE;
these never skip (see conftest: licenses this run signs, for the license-test build).

Added: the shipped binary refuses a key signed by anyone but GizmoData, even when the
environment names another trust anchor, so the license-test build's anchor can't leak.
"""

import pytest

from conftest import BINARY, LICENSE_TEST_BINARY


@pytest.fixture
def signer(license_signer):
    return license_signer


def start(launcher, signer, env: dict, args: list[str] | None = None, expect_up: bool = True, binary=None):
    return launcher.start(binary=binary or LICENSE_TEST_BINARY, args=args, expect_up=expect_up,
                          env={"HARDCHARGER_TEST_LICENSE_PUBLIC_KEY": str(signer.public_pem), **env})


def edition(server) -> str:
    return server.admin().scalar(sql="SELECT GIZMOSQL_EDITION()")


def refused(server) -> str:
    assert server.process.wait(timeout=20) == 1, server.log()
    assert "License Error" in server.log(), server.log()
    return server.log()


def test_empty_string_is_invalid(launcher, signer):
    refused(start(launcher=launcher, signer=signer, env={}, args=["--license-key="], expect_up=False))


def test_whitespace_only_string_is_invalid(launcher, signer):
    refused(start(launcher=launcher, signer=signer, env={"HARDCHARGER_LICENSE_KEY": "   \n "}, expect_up=False))


def test_garbage_jwt_is_rejected(launcher, signer):
    refused(start(launcher=launcher, signer=signer, env={"HARDCHARGER_LICENSE_KEY": "not.a.jwt"}, expect_up=False))


def test_valid_inline_jwt_validates_like_file(launcher, signer, tmp_path):
    key = signer.sign(features=["metrics"])
    assert edition(start(launcher=launcher, signer=signer, env={"GIZMOSQL_LICENSE_KEY": key})) == "Enterprise"
    key_file = tmp_path / "license.jwt"
    key_file.write_text(key)
    assert edition(start(launcher=launcher, signer=signer, env={"GIZMOSQL_LICENSE_KEY_FILE": str(key_file)})) == "Enterprise"


def test_trailing_whitespace_is_trimmed(launcher, signer):
    key = signer.sign(features=["metrics"]) + "\n  \t\n"
    assert edition(start(launcher=launcher, signer=signer, env={"GIZMOSQL_LICENSE_KEY": key})) == "Enterprise"


def test_inline_key_wins_over_bogus_file(launcher, signer, tmp_path):
    server = start(launcher=launcher, signer=signer,
                   env={"GIZMOSQL_LICENSE_KEY": signer.sign(features=["metrics"]),
                        "GIZMOSQL_LICENSE_KEY_FILE": str(tmp_path / "no-such-license.jwt")})
    assert edition(server) == "Enterprise"


def test_restart_without_license_clears_previous_entitlements(launcher, signer):
    licensed = start(launcher=launcher, signer=signer, env={"GIZMOSQL_LICENSE_KEY": signer.sign(features=["kill_session"])})
    assert edition(licensed) == "Enterprise"
    licensed.stop()
    core = start(launcher=launcher, signer=signer, env={})
    assert edition(core) == "Core"
    problem = core.admin().error(sql="KILL SESSION '00000000-0000-0000-0000-000000000000'")
    assert "enterprise" in problem["detail"].lower(), problem


def test_restart_with_invalid_license_refuses_to_start(launcher, signer):
    licensed = start(launcher=launcher, signer=signer, env={"GIZMOSQL_LICENSE_KEY": signer.sign(features=["metrics"])})
    licensed.stop()
    refused(start(launcher=launcher, signer=signer, env={"GIZMOSQL_LICENSE_KEY": "not.a.valid.license"},
                  expect_up=False))


def test_inline_key_takes_precedence_even_when_invalid(launcher, signer, tmp_path):
    key_file = tmp_path / "license.jwt"
    key_file.write_text(signer.sign(features=["metrics"]))
    refused(start(launcher=launcher, signer=signer,
                  env={"GIZMOSQL_LICENSE_KEY": "not.a.valid.license", "GIZMOSQL_LICENSE_KEY_FILE": str(key_file)},
                  expect_up=False))


def test_no_license_is_core_edition(launcher, signer):
    assert edition(start(launcher=launcher, signer=signer, env={})) == "Core"
    assert edition(launcher.start()) == "Core"


@pytest.mark.parametrize("problem_claim", [{"exp_offset": -3600}, {"missing": "sub"}, {"iss": "Someone Else"}])
def test_expired_incomplete_or_foreign_keys_are_refused(launcher, signer, problem_claim):
    import datetime
    if "exp_offset" in problem_claim:
        key = signer.sign(features=[], lifetime=datetime.timedelta(seconds=problem_claim["exp_offset"]))
        expected = "License has expired"
    elif "missing" in problem_claim:
        key = signer.sign(features=[], sub=None)
        expected = "License missing required claim: sub"
    else:
        key = signer.sign(features=[], iss=problem_claim["iss"])
        expected = "could not be verified"
    log = refused(start(launcher=launcher, signer=signer, env={"GIZMOSQL_LICENSE_KEY": key}, expect_up=False))
    assert expected in log, log


def test_shipped_binary_refuses_test_signed_keys(launcher, signer):
    server = start(launcher=launcher, signer=signer, binary=BINARY,
                   env={"GIZMOSQL_LICENSE_KEY": signer.sign(features=["metrics"])}, expect_up=False)
    log = refused(server)
    assert "could not be verified" in log, log
