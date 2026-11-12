"""FastAPI application: GitHub webhook + human review endpoints."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from app import service
from app.config import get_settings
from app.github_client import GitHubError, parse_pr_url, parse_repo
from app.schemas import ApprovalRequest, ReviewStatus, WebhookAccepted

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Autonomous Code Review & Refactoring Agent",
    version="1.0.0",
    description="LangGraph agent that lints PRs, proposes refactors and waits "
    "for human approval before committing.",
)

HANDLED_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


def _verify_signature(secret: str, body: bytes, signature: Optional[str]) -> None:
    if not secret:
        return
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


def extract_target(payload: dict[str, Any]) -> tuple[str, Optional[str], Optional[int], Optional[str]]:
    """Return (repo 'owner/name', branch, pr_number, pr_url) from a webhook payload.

    Accepts a real GitHub ``pull_request`` event or the simplified
    ``{"repository": {"html_url": ...}, "branch": ...}`` shape.
    """
    pr = payload.get("pull_request") or {}
    repository = payload.get("repository")
    repo_ref: Optional[str] = None
    if isinstance(repository, dict):
        repo_ref = repository.get("full_name") or repository.get("html_url")
    elif isinstance(repository, str):
        repo_ref = repository
    repo_ref = repo_ref or payload.get("repository_url") or payload.get("repo_url")

    branch = (pr.get("head") or {}).get("ref") or payload.get("branch")
    pr_number = pr.get("number") or payload.get("number") or payload.get("pr_number")
    pr_url = pr.get("html_url") or payload.get("pr_url")

    if pr_url and not repo_ref:
        owner, name, number = parse_pr_url(pr_url)
        repo_ref, pr_number = f"{owner}/{name}", pr_number or number
    if not repo_ref:
        raise ValueError("payload has no repository URL")
    repo = parse_repo(repo_ref)
    if not branch and not pr_number:
        raise ValueError("payload needs a branch name or a pull request number")
    return repo, branch, int(pr_number) if pr_number else None, pr_url


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok", "mock_mode": str(get_settings().mock_mode).lower()}


@app.post(
    "/api/webhooks/github",
    response_model=WebhookAccepted,
    status_code=202,
    tags=["webhooks"],
    summary="Receive a GitHub pull_request event and start a review thread",
)
async def github_webhook(
    request: Request,
    background: BackgroundTasks,
    x_github_event: Optional[str] = Header(default=None),
    x_hub_signature_256: Optional[str] = Header(default=None),
) -> WebhookAccepted:
    body = await request.body()
    _verify_signature(get_settings().webhook_secret, body, x_hub_signature_256)
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON payload: {exc}")

    if x_github_event == "ping":
        return WebhookAccepted(thread_id="", status="pong", message="pong")
    if x_github_event and x_github_event != "pull_request":
        return WebhookAccepted(
            thread_id="", status="ignored", message=f"event {x_github_event!r} ignored"
        )
    if payload.get("action") and payload["action"] not in HANDLED_ACTIONS:
        return WebhookAccepted(
            thread_id="",
            status="ignored",
            message=f"action {payload['action']!r} ignored",
        )

    try:
        repo, branch, pr_number, pr_url = extract_target(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    thread_id, inputs = service.create_thread(repo, branch, pr_number, pr_url)
    background.add_task(service.run_thread, thread_id, inputs)
    logger.info("webhook accepted: thread %s for %s", thread_id, repo)
    return WebhookAccepted(
        thread_id=thread_id,
        message=f"Review started; poll GET /api/reviews/{thread_id}",
    )


@app.get(
    "/api/reviews/{thread_id}",
    response_model=ReviewStatus,
    tags=["reviews"],
    summary="Current state of a review thread (draft, diffs, history)",
)
def get_review(thread_id: str) -> dict[str, Any]:
    try:
        return service.get_review(thread_id)
    except service.ReviewNotFound:
        raise HTTPException(status_code=404, detail=f"Unknown thread {thread_id!r}")


@app.post(
    "/api/reviews/{thread_id}/approve",
    response_model=ReviewStatus,
    tags=["reviews"],
    summary="Approve, or reject with feedback, then resume the thread",
)
def approve_review(thread_id: str, decision: ApprovalRequest) -> dict[str, Any]:
    try:
        return service.submit_review(thread_id, decision.approved, decision.feedback)
    except service.ReviewNotFound:
        raise HTTPException(status_code=404, detail=f"Unknown thread {thread_id!r}")
    except service.ReviewNotAwaitingApproval as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
