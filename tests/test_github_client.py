"""GitHub client tests with the HTTP choke point stubbed."""

from __future__ import annotations

import base64

import pytest

from app import github_client as gh


def b64(text: str) -> dict:
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


@pytest.fixture
def real_client(monkeypatch):
    """Undo the autouse fakes so the real functions run against a stubbed _request."""
    import importlib

    importlib.reload(gh)
    monkeypatch.setenv("MOCK_MODE", "false")
    return gh


def test_parse_pr_url():
    assert gh.parse_pr_url("https://github.com/acme/demo/pull/42") == ("acme", "demo", 42)
    assert gh.parse_pr_url("https://github.com/acme/demo/pull/42/files")[2] == 42
    with pytest.raises(ValueError):
        gh.parse_pr_url("https://example.com/acme/demo")


def test_parse_repo_variants():
    assert gh.parse_repo("https://github.com/acme/demo") == "acme/demo"
    assert gh.parse_repo("https://github.com/acme/demo.git") == "acme/demo"
    assert gh.parse_repo("acme/demo") == "acme/demo"
    with pytest.raises(ValueError):
        gh.parse_repo("nonsense")


def test_fetch_pr_files_only_returns_modified_python_files(real_client, monkeypatch):
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path))
        if path == "/repos/acme/demo/pulls/5":
            return {
                "html_url": "https://github.com/acme/demo/pull/5",
                "head": {"ref": "feat", "sha": "abc123", "repo": {"full_name": "fork/demo"}},
            }
        if path == "/repos/acme/demo/pulls/5/files":
            return [
                {"filename": "a.py", "status": "modified"},
                {"filename": "README.md", "status": "modified"},
                {"filename": "gone.py", "status": "removed"},
            ]
        if path == "/repos/fork/demo/contents/a.py":
            assert kwargs["params"] == {"ref": "abc123"}
            return b64("x = 1\n")
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(gh, "_request", fake_request)
    result = gh.fetch_pr_files("acme/demo", pr_number=5)
    assert result.files == {"a.py": "x = 1\n"}
    assert (result.repo, result.branch, result.pr_number) == ("fork/demo", "feat", 5)


def test_fetch_by_branch_finds_open_pr(real_client, monkeypatch):
    def fake_request(method, path, **kwargs):
        if path == "/repos/acme/demo/pulls" and "params" in kwargs:
            assert kwargs["params"]["head"] == "acme:feat"
            return [{"number": 9}]
        if path == "/repos/acme/demo/pulls/9":
            return {"html_url": "u", "head": {"ref": "feat", "sha": "s", "repo": None}}
        if path.endswith("/pulls/9/files"):
            return []
        raise AssertionError(path)

    monkeypatch.setattr(gh, "_request", fake_request)
    assert gh.fetch_pr_files("acme/demo", branch="feat").pr_number == 9


def test_commit_file_puts_base64_content_with_current_sha(real_client, monkeypatch):
    seen = {}

    def fake_request(method, path, **kwargs):
        if method == "GET":
            return {"sha": "blobsha"}
        seen.update(kwargs["json"])
        return {"commit": {"sha": "newcommit"}}

    monkeypatch.setattr(gh, "_request", fake_request)
    sha = gh.commit_file("acme/demo", "feat", "a.py", "y = 2\n", "msg")
    assert sha == "newcommit"
    assert seen["sha"] == "blobsha" and seen["branch"] == "feat"
    assert base64.b64decode(seen["content"]) == b"y = 2\n"


def test_http_errors_become_github_error(real_client, monkeypatch):
    class Resp:
        status_code = 404
        text = "Not Found"

    monkeypatch.setattr(gh.httpx, "request", lambda *a, **k: Resp())
    with pytest.raises(gh.GitHubError):
        gh._request("GET", "/repos/x/y")
