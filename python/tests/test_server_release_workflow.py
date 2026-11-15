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
