"""Orchestration layer between FastAPI and the compiled LangGraph."""

from __future__ import annotations

import difflib
import logging
import threading
import uuid
from typing import Any, Optional

from app.config import get_settings
from app.graph import build_graph
from app.nodes import APPLY

logger = logging.getLogger(__name__)

_graph: Any = None
_graph_lock = threading.Lock()
_running: set[str] = set()
_errors: dict[str, str] = {}


class ReviewNotFound(LookupError):
    pass


class ReviewNotAwaitingApproval(RuntimeError):
    pass


def get_graph():
    global _graph
    with _graph_lock:
        if _graph is None:
            _graph = build_graph()
        return _graph


def set_graph(graph: Any) -> None:
    """Swap the compiled graph (used by tests to inject a MemorySaver graph)."""
    global _graph
    _graph = graph
    _running.clear()
    _errors.clear()


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


# --------------------------------------------------------------------------
# Start a review (webhook)
# --------------------------------------------------------------------------
def create_thread(
    repo: str, branch: Optional[str], pr_number: Optional[int], pr_url: Optional[str]
) -> tuple[str, dict[str, Any]]:
    thread_id = uuid.uuid4().hex
    _running.add(thread_id)
    inputs = {
        "repo": repo,
        "branch": branch or "",
        "pr_number": pr_number,
        "pr_url": pr_url or "",
        "history": [f"webhook: review requested for {repo}@{branch or pr_number}"],
        "revision": 0,
    }
    return thread_id, inputs


def run_thread(thread_id: str, inputs: dict[str, Any]) -> None:
    """Run fetch -> lint -> propose until the graph halts at the approval gate."""
    try:
        get_graph().invoke(inputs, _config(thread_id))
        state = get_graph().get_state(_config(thread_id))
        if state.next == (APPLY,):
            logger.info(
                "thread %s halted at approval checkpoint (draft #%s)",
                thread_id,
                state.values.get("revision"),
            )
        else:
            logger.info(
                "thread %s finished with status %s",
                thread_id,
                state.values.get("status"),
            )
    except Exception as exc:  # noqa: BLE001 - surfaced through the status endpoint
        logger.exception("thread %s failed", thread_id)
        _errors[thread_id] = f"{type(exc).__name__}: {exc}"
    finally:
        _running.discard(thread_id)


# --------------------------------------------------------------------------
# Inspect
# --------------------------------------------------------------------------
def _diffs(values: dict[str, Any]) -> dict[str, str]:
    originals = values.get("code_contents", {})
    out: dict[str, str] = {}
    for path, new in values.get("refactored_code", {}).items():
        old = originals.get(path, "")
        out[path] = "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
    return out


def get_review(thread_id: str) -> dict[str, Any]:
    state = get_graph().get_state(_config(thread_id))
    values = dict(state.values or {})
    if not values and thread_id not in _running and thread_id not in _errors:
        raise ReviewNotFound(thread_id)

    if thread_id in _errors:
        status = "failed"
    elif thread_id in _running or (state.next and state.next != (APPLY,)):
        status = "running"
    elif state.next == (APPLY,):
        status = "awaiting_approval"
    else:
        status = values.get("status", "running")

    return {
        "thread_id": thread_id,
        "status": status,
        "pr_url": values.get("pr_url"),
        "repo": values.get("repo"),
        "branch": values.get("branch"),
        "revision": values.get("revision", 0),
        "lint_errors": values.get("lint_errors", {}),
        "refactored_code": values.get("refactored_code", {}),
        "refactor_lint_errors": values.get("refactor_lint_errors", {}),
        "diffs": _diffs(values),
        "reviewer_feedback": values.get("reviewer_feedback") or None,
        "commit_shas": values.get("commit_shas", []),
        "history": values.get("history", []),
        "error": _errors.get(thread_id),
    }


# --------------------------------------------------------------------------
# Human decision
# --------------------------------------------------------------------------
def submit_review(
    thread_id: str, approved: bool, feedback: Optional[str]
) -> dict[str, Any]:
    graph = get_graph()
    cfg = _config(thread_id)
    state = graph.get_state(cfg)
    if not state.values:
        raise ReviewNotFound(thread_id)
    if state.next != (APPLY,):
        raise ReviewNotAwaitingApproval(
            f"thread {thread_id} is not waiting for approval "
            f"(status: {get_review(thread_id)['status']})"
        )

    feedback = (feedback or "").strip()
    if approved:
        graph.update_state(
            cfg,
            {
                "is_approved": True,
                "history": ["reviewer: approved"],
            },
            as_node="propose_refactor_node",  # next step stays apply_changes_node
        )
    elif feedback and state.values.get("revision", 0) < get_settings().max_revisions:
        # Roll the thread back: record the decision as if the write node had
        # run, so the conditional edge routes to propose_refactor_node.
        graph.update_state(
            cfg,
            {
                "is_approved": False,
                "reviewer_feedback": feedback,
                "status": "revision_requested",
                "history": [f"reviewer: rejected with feedback {feedback!r}"],
            },
            as_node=APPLY,
        )
    else:
        reason = (
            "max_revisions_reached" if feedback else "rejected"
        )  # no feedback -> nothing to improve on, close the thread
        graph.update_state(
            cfg,
            {
                "is_approved": False,
                "status": reason,
                "history": [f"reviewer: rejected ({reason}); thread closed"],
            },
            as_node=APPLY,
        )

    graph.invoke(None, cfg)  # resume from the checkpoint
    after = graph.get_state(cfg)
    if after.next == (APPLY,):
        logger.info("thread %s halted again at approval checkpoint", thread_id)
    return get_review(thread_id)
