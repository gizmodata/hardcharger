"""Unpublished development versions wait; publishing and API errors fail closed."""

import importlib.util
import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/python_server_release.py"
SPEC = importlib.util.spec_from_file_location("python_server_release", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)
TAG = "v2.13.0"


@pytest.mark.parametrize("required", [False, True])
def test_published_server_runs_download_checks(required):
    assert POLICY.available(release={"tag_name": TAG, "draft": False}, tag=TAG, required=required)


@pytest.mark.parametrize("release", [None, {"tag_name": TAG, "draft": True}])
def test_unpublished_server_waits_only_on_development_runs(release):
    assert not POLICY.available(release=release, tag=TAG, required=False)
    with pytest.raises(ValueError, match="must be published"):
        POLICY.available(release=release, tag=TAG, required=True)


@pytest.mark.parametrize("release", [{}, {"tag_name": TAG}, {"tag_name": "v0.0.0", "draft": False}])
def test_unexpected_release_response_is_not_skipped(release):
    with pytest.raises(ValueError, match="unexpected release"):
        POLICY.available(release=release, tag=TAG, required=False)


def test_release_404_checks_repository_access(monkeypatch):
    requests = []

    def open_url(*, url, timeout):
        requests.append(url.full_url)
        if len(requests) == 1:
            raise HTTPError(url=url.full_url, code=404, msg="not found", hdrs=None, fp=None)
        return io.BytesIO(initial_bytes=json.dumps({"full_name": "owner/repo"}).encode())

    monkeypatch.setattr(POLICY, "urlopen", open_url)
    assert POLICY.fetch_release(repository="owner/repo", tag=TAG, token="test-token") is None
    assert requests == [
        f"https://api.github.com/repos/owner/repo/releases/tags/{TAG}",
        "https://api.github.com/repos/owner/repo",
    ]


@pytest.mark.parametrize("code", [401, 403, 404, 429, 500])
def test_inaccessible_repository_or_api_error_is_not_skipped(monkeypatch, code):
    def open_url(*, url, timeout):
        raise HTTPError(url=url.full_url, code=code, msg="API error", hdrs=None, fp=None)

    monkeypatch.setattr(POLICY, "urlopen", open_url)
    with pytest.raises(HTTPError):
        POLICY.fetch_release(repository="owner/repo", tag=TAG, token="test-token")


def test_network_error_is_not_skipped(monkeypatch):
    def open_url(*, url, timeout):
        raise URLError(reason="network unavailable")

    monkeypatch.setattr(POLICY, "urlopen", open_url)
    with pytest.raises(URLError):
        POLICY.fetch_release(repository="owner/repo", tag=TAG, token="test-token")


# ---- The workflow's wiring -----------------------------------------------------------
#
# The policy above only gates a publish because of how the workflow's jobs depend on each
# other. These hold that in place: an `if: always()` on `publish`, or `server` dropped from
# its `needs`, would pass every test above and publish a package no real server had run.

import re  # noqa: E402

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/workflow.yml"
STATUS_FUNCTIONS = ("always()", "failure()", "cancelled()", "success()")


def workflow_jobs() -> dict[str, str]:
    """Each job's text, by its id."""
    jobs = WORKFLOW.read_text(encoding="utf-8").split("\njobs:\n", maxsplit=1)[1]
    parts = re.split(pattern=r"(?m)^  ([A-Za-z_][\w-]*):\n", string=jobs)
    return dict(zip(parts[1::2], parts[2::2]))


def job_key(job: str, key: str) -> str | None:
    """A job-level key's value (four spaces in), as written."""
    found = re.search(pattern=rf"(?m)^    {key}: (.*)$", string=workflow_jobs()[job])
    return found.group(1).strip() if found else None


def test_publish_needs_every_gate_and_never_overrides_their_outcome():
    assert job_key(job="publish", key="needs") == "[test, server, client]"
    condition = job_key(job="publish", key="if")
    # With no status function, GitHub implies success(): a failed or skipped need skips the publish.
    assert condition is not None and not any(function in condition for function in STATUS_FUNCTIONS), condition
    assert job_key(job="github-release", key="needs") == "publish"
    assert job_key(job="github-release", key="if") is None
    for job in ("test", "server", "client", "publish"):
        assert "continue-on-error" not in workflow_jobs()[job], f"{job} may not shrug off a failure"


def test_the_server_tests_are_skipped_only_when_the_policy_says_the_release_is_not_there():
    assert job_key(job="server", key="needs") == "test"
    assert job_key(job="server", key="if") == "needs.test.outputs.server_available == 'true'"
    assert "server_available: ${{ steps.server-release.outputs.available }}" in workflow_jobs()["test"]


def test_every_publishing_trigger_requires_the_server_release():
    publish = job_key(job="publish", key="if")
    test = workflow_jobs()["test"]
    require = re.search(pattern=r"(?m)^ +REQUIRE_SERVER: \$\{\{ (.*) \}\}$", string=test)
    assert require is not None, "the test job no longer says when the server release is required"
    # Each way `publish` can run (a python-v tag; a dispatch that names a release) is one
    # for which the policy runs with --required, so an unpublished release fails `test`.
    for trigger in ("startsWith(github.ref, 'refs/tags/python-v')", "inputs.release != ''"):
        assert trigger in publish, publish
        assert trigger in require.group(1), require.group(1)
    assert 'if [ "$REQUIRE_SERVER" = true ]; then args+=(--required); fi' in test
