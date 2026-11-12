"""LangGraph state schema for one review session (one thread = one PR review)."""

from __future__ import annotations

import operator
from typing import Annotated, Optional

from typing_extensions import TypedDict


class ReviewState(TypedDict, total=False):
    # --- required keys from the brief -------------------------------------
    pr_url: str
    code_contents: dict[str, str]  # path -> original source
    lint_errors: dict[str, list[str]]  # path -> linter findings
    refactored_code: dict[str, str]  # path -> proposed source
    is_approved: Optional[bool]
    reviewer_feedback: str  # most recent reviewer feedback
    history: Annotated[list[str], operator.add]  # append-only audit trail

    # --- supporting keys ---------------------------------------------------
    repo: str  # "owner/name" that commits are written to (PR head repo)
    branch: str  # PR head branch
    pr_number: Optional[int]
    refactor_lint_errors: dict[str, list[str]]  # findings left in the draft
    revision: int  # number of drafts proposed so far
    commit_shas: list[str]
    status: str
