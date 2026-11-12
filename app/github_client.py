"""Thin GitHub REST client: fetch PR files, commit refactored files back.

All HTTP goes through :func:`_request` so tests can stub a single function.
With ``MOCK_MODE=true`` no network call is made (canned file + fake commit).
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.config import get_settings

API_ROOT = "https://api.github.com"
PYTHON_SUFFIX = ".py"

_PR_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)/pull/(\d+)/?.*$"
)
_REPO_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$"
)
_SLUG_RE = re.compile(r"^([\w.-]+)/([\w.-]+)$")

MOCK_FILES = {
    "src/math_utils.py": (
        "def squares(nums):\n"
        "    result=[]\n"
        "    for n in nums:\n"
        "        result.append(n*n)\n"
        "    return result\n"
        "def total(values):\n"
        "    return sum( values )\n"
    )
}


class GitHubError(RuntimeError):
    """Raised for any failed GitHub interaction."""


@dataclass
class FetchedPR:
    repo: str  # owner/name of the PR *head* repo (where commits go)
    branch: str
    pr_number: Optional[int]
    pr_url: str
    files: dict[str, str]


# --------------------------------------------------------------------------
# URL parsing
# --------------------------------------------------------------------------
def parse_pr_url(url: str) -> tuple[str, str, int]:
    match = _PR_URL_RE.match(url.strip())
    if not match:
        raise ValueError(f"Not a GitHub pull request URL: {url!r}")
    return match.group(1), match.group(2), int(match.group(3))


def parse_repo(value: str) -> str:
    """Accept ``owner/name`` or a github.com repo URL; return ``owner/name``."""
    value = value.strip()
    for pattern in (_REPO_URL_RE, _SLUG_RE):
        match = pattern.match(value)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    raise ValueError(f"Cannot parse repository from {value!r}")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "code-review-agent",
    }
    token = get_settings().github_token
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request(method: str, path: str, **kwargs: Any) -> Any:
    """Single choke point for GitHub HTTP calls. Returns decoded JSON."""
    url = path if path.startswith("http") else f"{API_ROOT}{path}"
    try:
        response = httpx.request(
            method, url, headers=_headers(), timeout=30.0, **kwargs
        )
    except httpx.HTTPError as exc:
        raise GitHubError(f"GitHub request failed: {exc}") from exc
    if response.status_code >= 400:
        raise GitHubError(
            f"GitHub {method} {path} -> {response.status_code}: "
            f"{response.text[:200]}"
        )
    return response.json()


def _paginate(path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        chunk = _request("GET", path, params={"per_page": 100, "page": page})
        items.extend(chunk)
        if len(chunk) < 100:
            return items
        page += 1


def _read_file(repo: str, path: str, ref: str) -> str:
    data = _request("GET", f"/repos/{repo}/contents/{path}", params={"ref": ref})
    if data.get("encoding") != "base64":
        raise GitHubError(f"Unsupported encoding for {path}: {data.get('encoding')}")
    return base64.b64decode(data["content"]).decode("utf-8")


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def fetch_pr_files(
    repo: str,
    *,
    pr_number: Optional[int] = None,
    branch: Optional[str] = None,
) -> FetchedPR:
    """Pull the modified ``.py`` files of a PR (found by number, else by branch)."""
    if get_settings().mock_mode:
        return FetchedPR(
            repo=repo,
            branch=branch or "feature/mock",
            pr_number=pr_number,
            pr_url=f"https://github.com/{repo}/pull/{pr_number or 1}",
            files=dict(MOCK_FILES),
        )

    if pr_number is None:
        if not branch:
            raise ValueError("Need a PR number or a branch name")
        owner = repo.split("/")[0]
        prs = _request(
            "GET",
            f"/repos/{repo}/pulls",
            params={"head": f"{owner}:{branch}", "state": "open"},
        )
        if prs:
            pr_number = prs[0]["number"]

    if pr_number is not None:
        pr = _request("GET", f"/repos/{repo}/pulls/{pr_number}")
        head_repo = (pr["head"].get("repo") or {}).get("full_name", repo)
        head_ref, head_sha = pr["head"]["ref"], pr["head"]["sha"]
        changed = _paginate(f"/repos/{repo}/pulls/{pr_number}/files")
        pr_url = pr["html_url"]
    else:  # no open PR for the branch: diff it against the default branch
        default = _request("GET", f"/repos/{repo}")["default_branch"]
        cmp = _request("GET", f"/repos/{repo}/compare/{default}...{branch}")
        head_repo, head_ref, head_sha = repo, branch, branch
        changed = cmp.get("files", [])
        pr_url = f"https://github.com/{repo}/tree/{branch}"

    files: dict[str, str] = {}
    for entry in changed:
        name = entry["filename"]
        if name.endswith(PYTHON_SUFFIX) and entry.get("status") != "removed":
            files[name] = _read_file(head_repo, name, head_sha)

    return FetchedPR(
        repo=head_repo,
        branch=head_ref,
        pr_number=pr_number,
        pr_url=pr_url,
        files=files,
    )


def commit_file(repo: str, branch: str, path: str, content: str, message: str) -> str:
    """Commit one file to ``branch`` and return the new commit SHA."""
    if get_settings().mock_mode:
        digest = hashlib.sha1(f"{repo}{branch}{path}{content}".encode()).hexdigest()
        return f"mock-{digest[:12]}"

    current = _request(
        "GET", f"/repos/{repo}/contents/{path}", params={"ref": branch}
    )
    result = _request(
        "PUT",
        f"/repos/{repo}/contents/{path}",
        json={
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "sha": current["sha"],
            "branch": branch,
        },
    )
    return result["commit"]["sha"]
