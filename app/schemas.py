"""Pydantic request/response models (these drive the OpenAPI spec)."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ApprovalRequest(BaseModel):
    approved: bool = Field(description="true to commit the draft, false to reject it")
    feedback: Optional[str] = Field(
        default=None,
        description="Reviewer feedback; a rejection with feedback regenerates the draft",
        examples=["Use list comprehensions instead of loops"],
    )


class WebhookAccepted(BaseModel):
    thread_id: str
    status: str = "running"
    message: str


class ReviewStatus(BaseModel):
    thread_id: str
    status: str = Field(
        description="running | awaiting_approval | completed | clean | rejected "
        "| max_revisions_reached | failed"
    )
    pr_url: Optional[str] = None
    repo: Optional[str] = None
    branch: Optional[str] = None
    revision: int = 0
    lint_errors: dict[str, list[str]] = {}
    refactored_code: dict[str, str] = {}
    refactor_lint_errors: dict[str, list[str]] = {}
    diffs: dict[str, str] = {}
    reviewer_feedback: Optional[str] = None
    commit_shas: list[str] = []
    history: list[str] = []
    error: Optional[str] = None
