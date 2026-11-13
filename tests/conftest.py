"""Shared fixtures: in-memory graph with GitHub + LLM replaced by recording fakes."""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app import github_client, llm, service
from app.graph import build_graph

BAD_SOURCE = "def add(a,b):\n    return a+b\n"
GOOD_SOURCE = "def add(a, b):\n    return a + b\n"


class Fakes:
    """Records every call made to the faked external systems."""

    def __init__(self) -> None:
        self.files = {"pkg/mod.py": BAD_SOURCE}
        self.refactor_calls: list[dict] = []
        self.commits: list[dict] = []

    def fetch_pr_files(self, repo, *, pr_number=None, branch=None):
        return github_client.FetchedPR(
            repo=repo,
            branch=branch or "feature/test",
            pr_number=pr_number,
            pr_url=f"https://github.com/{repo}/pull/{pr_number or 7}",
            files=dict(self.files),
        )

    def generate_refactor(self, path, code, lint_errors, feedback=None):
        self.refactor_calls.append(
            {"path": path, "errors": list(lint_errors), "feedback": feedback}
        )
        suffix = f"# rev {len(self.refactor_calls)}\n"
        return GOOD_SOURCE + suffix

    def commit_file(self, repo, branch, path, content, message):
        self.commits.append(
            {"repo": repo, "branch": branch, "path": path, "content": content}
        )
        return f"sha{len(self.commits)}"


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    f = Fakes()
    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "")
    monkeypatch.setenv("MAX_REVISIONS", "5")
    monkeypatch.setattr(github_client, "fetch_pr_files", f.fetch_pr_files)
    monkeypatch.setattr(github_client, "commit_file", f.commit_file)
    monkeypatch.setattr(llm, "generate_refactor", f.generate_refactor)
    return f


@pytest.fixture
def graph():
    g = build_graph(MemorySaver())
    service.set_graph(g)
    return g


def start_inputs(**overrides):
    base = {
        "repo": "acme/demo",
        "branch": "feature/test",
        "pr_number": None,
        "pr_url": "",
        "history": [],
        "revision": 0,
    }
    base.update(overrides)
    return base


def cfg(thread_id: str):
    return {"configurable": {"thread_id": thread_id}}
