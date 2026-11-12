"""LangGraph node functions and routing for the review pipeline."""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END

from app import github_client, llm
from app.sandbox import parse_findings, run_linter_tool
from app.state import ReviewState

logger = logging.getLogger(__name__)

PROPOSE = "propose_refactor_node"
APPLY = "apply_changes_node"


def fetch_pr_node(state: ReviewState) -> dict[str, Any]:
    """Pull the modified Python files of the PR from GitHub into the state."""
    fetched = github_client.fetch_pr_files(
        state["repo"],
        pr_number=state.get("pr_number"),
        branch=state.get("branch"),
    )
    logger.info("fetched %d file(s) from %s", len(fetched.files), fetched.pr_url)
    return {
        "repo": fetched.repo,
        "branch": fetched.branch,
        "pr_number": fetched.pr_number,
        "pr_url": fetched.pr_url,
        "code_contents": fetched.files,
        "status": "fetched",
        "history": [f"fetch_pr_node: fetched {len(fetched.files)} python file(s)"],
    }


def lint_code_node(state: ReviewState) -> dict[str, Any]:
    """Run the sandboxed linter over every fetched file."""
    errors: dict[str, list[str]] = {}
    for path, source in state.get("code_contents", {}).items():
        findings = parse_findings(run_linter_tool.invoke({"code_string": source}))
        if findings:
            errors[path] = findings
    total = sum(len(v) for v in errors.values())
    logger.info("lint found %d issue(s) in %d file(s)", total, len(errors))
    return {
        "lint_errors": errors,
        "status": "linted" if errors else "clean",
        "history": [
            f"lint_code_node: {total} issue(s) in {len(errors)} file(s)"
            if errors
            else "lint_code_node: no issues found"
        ],
    }


def route_after_lint(state: ReviewState) -> str:
    """Nothing to fix -> finish; otherwise propose a refactor."""
    return PROPOSE if state.get("lint_errors") else END


def propose_refactor_node(state: ReviewState) -> dict[str, Any]:
    """Ask the LLM for a refactor (re-entered with feedback after a rejection)."""
    feedback = state.get("reviewer_feedback") or None
    revision = state.get("revision", 0) + 1
    drafts: dict[str, str] = {}
    leftovers: dict[str, list[str]] = {}
    for path, errors in state.get("lint_errors", {}).items():
        draft = llm.generate_refactor(
            path, state["code_contents"][path], errors, feedback
        )
        drafts[path] = draft
        remaining = parse_findings(run_linter_tool.invoke({"code_string": draft}))
        if remaining:
            leftovers[path] = remaining
    note = f"propose_refactor_node: draft #{revision} for {len(drafts)} file(s)"
    if feedback:
        note += f" using reviewer feedback: {feedback!r}"
    if leftovers:
        note += f" ({sum(map(len, leftovers.values()))} lint issue(s) remain)"
    return {
        "refactored_code": drafts,
        "refactor_lint_errors": leftovers,
        "revision": revision,
        "is_approved": None,  # a new draft always needs a fresh decision
        "status": "awaiting_approval",
        "history": [note],
    }


def apply_changes_node(state: ReviewState) -> dict[str, Any]:
    """Commit the approved refactor to the PR branch. Never runs unapproved."""
    if state.get("is_approved") is not True:
        raise PermissionError("apply_changes_node requires explicit approval")
    shas: list[str] = []
    for path, new_source in state.get("refactored_code", {}).items():
        if new_source == state["code_contents"].get(path):
            continue
        shas.append(
            github_client.commit_file(
                state["repo"],
                state["branch"],
                path,
                new_source,
                f"refactor: fix lint issues in {path} (code-review-agent)",
            )
        )
    logger.info("committed %d file(s) to %s@%s", len(shas), state["repo"], state["branch"])
    return {
        "commit_shas": shas,
        "status": "completed",
        "history": [f"apply_changes_node: committed {len(shas)} file(s)"],
    }


def route_after_apply(state: ReviewState) -> str:
    """After the write node (or a reviewer decision standing in for it).

    * committed             -> END
    * rejected + feedback   -> back to propose_refactor_node (the rollback)
    * anything else         -> END (rejected without feedback / revisions used up)
    """
    if state.get("status") == "completed":
        return END
    if state.get("is_approved") is False and state.get("status") == "revision_requested":
        return PROPOSE
    return END
