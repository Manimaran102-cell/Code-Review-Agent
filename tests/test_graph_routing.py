"""Graph routing: HITL pause, rejection rollback, approval, guard rails."""

from __future__ import annotations

import pytest

from app import service
from app.nodes import APPLY, PROPOSE, apply_changes_node
from tests.conftest import GOOD_SOURCE, cfg, start_inputs


def run_to_gate(graph, thread="t1", **overrides):
    graph.invoke(start_inputs(**overrides), cfg(thread))
    return graph.get_state(cfg(thread))


def reject(graph, thread, feedback):
    """Same state mutation the API performs for a rejection."""
    graph.update_state(
        cfg(thread),
        {
            "is_approved": False,
            "reviewer_feedback": feedback,
            "status": "revision_requested",
            "history": [f"reviewer: rejected {feedback!r}"],
        },
        as_node=APPLY,
    )
    graph.invoke(None, cfg(thread))
    return graph.get_state(cfg(thread))


def test_pauses_before_write_node_without_committing(graph, fakes):
    state = run_to_gate(graph)
    assert state.next == (APPLY,)
    assert state.values["lint_errors"]["pkg/mod.py"]  # linter ran in sandbox
    assert state.values["refactored_code"]["pkg/mod.py"].startswith(GOOD_SOURCE)
    assert fakes.commits == []  # nothing written yet


def test_rejection_rolls_back_to_refactor_node_and_pauses_again(graph, fakes):
    run_to_gate(graph)
    state = reject(graph, "t1", "Use list comprehensions instead of loops")

    assert len(fakes.refactor_calls) == 2  # refactor node ran again
    assert fakes.refactor_calls[0]["feedback"] is None
    assert fakes.refactor_calls[1]["feedback"] == (
        "Use list comprehensions instead of loops"
    )
    assert state.next == (APPLY,)  # halted at the gate again
    assert state.values["revision"] == 2
    assert state.values["reviewer_feedback"].startswith("Use list")
    assert state.values["is_approved"] is None  # fresh decision required
    assert state.values["refactored_code"]["pkg/mod.py"].endswith("# rev 2\n")
    assert fakes.commits == []


def test_rollback_route_is_visible_in_checkpoint_history(graph):
    run_to_gate(graph)
    reject(graph, "t1", "tighten it")
    visited = [n for snap in graph.get_state_history(cfg("t1")) for n in snap.next]
    assert visited.count(PROPOSE) >= 2  # propose was scheduled twice
    notes = graph.get_state(cfg("t1")).values["history"]
    assert sum("propose_refactor_node: draft" in n for n in notes) == 2


def test_multiple_rejections_accumulate_feedback_history(graph, fakes):
    run_to_gate(graph)
    reject(graph, "t1", "first")
    state = reject(graph, "t1", "second")
    assert [c["feedback"] for c in fakes.refactor_calls] == [None, "first", "second"]
    assert state.values["revision"] == 3
    assert state.next == (APPLY,)


def test_approval_resumes_and_commits_to_branch(graph, fakes):
    run_to_gate(graph)
    graph.update_state(
        cfg("t1"),
        {"is_approved": True, "history": ["reviewer: approved"]},
        as_node=PROPOSE,
    )
    graph.invoke(None, cfg("t1"))
    state = graph.get_state(cfg("t1"))

    assert state.next == ()  # thread finished
    assert state.values["status"] == "completed"
    assert state.values["commit_shas"] == ["sha1"]
    commit = fakes.commits[0]
    assert (commit["repo"], commit["branch"], commit["path"]) == (
        "acme/demo",
        "feature/test",
        "pkg/mod.py",
    )
    assert commit["content"] == state.values["refactored_code"]["pkg/mod.py"]


def test_clean_code_skips_refactor_and_gate(graph, fakes):
    fakes.files = {"pkg/mod.py": GOOD_SOURCE}
    state = run_to_gate(graph)
    assert state.next == ()
    assert state.values["status"] == "clean"
    assert fakes.refactor_calls == []


def test_apply_node_refuses_to_write_without_approval(fakes):
    state = {
        "repo": "acme/demo",
        "branch": "b",
        "code_contents": {"a.py": "x"},
        "refactored_code": {"a.py": "y"},
        "is_approved": None,
    }
    with pytest.raises(PermissionError):
        apply_changes_node(state)
    assert fakes.commits == []


def test_service_rejection_without_feedback_closes_thread(graph, fakes):
    thread_id, inputs = service.create_thread("acme/demo", "b", None, None)
    service.run_thread(thread_id, inputs)
    result = service.submit_review(thread_id, approved=False, feedback=None)
    assert result["status"] == "rejected"
    assert len(fakes.refactor_calls) == 1 and fakes.commits == []


def test_service_stops_after_max_revisions(graph, fakes, monkeypatch):
    monkeypatch.setenv("MAX_REVISIONS", "2")
    thread_id, inputs = service.create_thread("acme/demo", "b", None, None)
    service.run_thread(thread_id, inputs)
    service.submit_review(thread_id, False, "again")  # draft 2
    result = service.submit_review(thread_id, False, "and again")
    assert result["status"] == "max_revisions_reached"
    assert len(fakes.refactor_calls) == 2 and fakes.commits == []


def test_fetch_failure_marks_thread_failed(graph, monkeypatch):
    from app import github_client

    def fail(*args, **kwargs):
        raise github_client.GitHubError("boom")

    monkeypatch.setattr(github_client, "fetch_pr_files", fail)
    thread_id, inputs = service.create_thread("acme/demo", "b", None, None)
    service.run_thread(thread_id, inputs)
    review = service.get_review(thread_id)
    assert review["status"] == "failed" and "boom" in review["error"]
