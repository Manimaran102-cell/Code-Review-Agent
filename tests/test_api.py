"""FastAPI endpoint tests (webhook, approval, validation errors)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.api import app, extract_target

PAYLOAD = {
    "repository": {"html_url": "https://github.com/acme/demo"},
    "branch": "feature/test",
}


@pytest.fixture
def client(graph):
    return TestClient(app)


def start(client, payload=PAYLOAD):
    response = client.post("/api/webhooks/github", json=payload)
    assert response.status_code == 202, response.text
    return response.json()["thread_id"]


def test_webhook_starts_thread_and_halts_at_gate(client, fakes):
    thread_id = start(client)
    body = client.get(f"/api/reviews/{thread_id}").json()
    assert body["status"] == "awaiting_approval"
    assert body["lint_errors"] and body["refactored_code"] and body["diffs"]
    assert fakes.commits == []


def test_reject_then_approve_full_flow(client, fakes):
    thread_id = start(client)
    url = f"/api/reviews/{thread_id}/approve"

    rejected = client.post(
        url,
        json={"approved": False, "feedback": "Use list comprehensions instead of loops"},
    ).json()
    assert rejected["status"] == "awaiting_approval"
    assert rejected["revision"] == 2
    assert fakes.refactor_calls[-1]["feedback"].startswith("Use list comprehensions")

    approved = client.post(url, json={"approved": True}).json()
    assert approved["status"] == "completed"
    assert approved["commit_shas"] == ["sha1"]
    assert len(fakes.commits) == 1


def test_approve_unknown_thread_is_404(client):
    response = client.post("/api/reviews/nope/approve", json={"approved": True})
    assert response.status_code == 404


def test_approve_twice_is_409(client):
    thread_id = start(client)
    url = f"/api/reviews/{thread_id}/approve"
    assert client.post(url, json={"approved": True}).status_code == 200
    assert client.post(url, json={"approved": True}).status_code == 409


def test_missing_approved_field_is_422(client):
    thread_id = start(client)
    response = client.post(f"/api/reviews/{thread_id}/approve", json={"feedback": "x"})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [{}, {"repository": {"html_url": "https://github.com/acme/demo"}}, {"branch": "x"}],
)
def test_webhook_rejects_incomplete_payloads(client, payload):
    assert client.post("/api/webhooks/github", json=payload).status_code == 422


def test_webhook_rejects_non_json(client):
    response = client.post(
        "/api/webhooks/github",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_ping_and_irrelevant_events_do_not_start_threads(client):
    ping = client.post("/api/webhooks/github", json={}, headers={"X-GitHub-Event": "ping"})
    assert ping.json()["status"] == "pong"
    closed = client.post(
        "/api/webhooks/github",
        json={"action": "closed", **PAYLOAD},
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert closed.json()["status"] == "ignored"


def test_real_github_pull_request_payload_shape(client):
    payload = {
        "action": "opened",
        "number": 12,
        "pull_request": {
            "number": 12,
            "html_url": "https://github.com/acme/demo/pull/12",
            "head": {"ref": "feature/test"},
        },
        "repository": {"full_name": "acme/demo", "html_url": "https://github.com/acme/demo"},
    }
    assert extract_target(payload) == (
        "acme/demo",
        "feature/test",
        12,
        "https://github.com/acme/demo/pull/12",
    )
    assert client.post(
        "/api/webhooks/github", json=payload, headers={"X-GitHub-Event": "pull_request"}
    ).status_code == 202


def test_signature_is_enforced_when_secret_configured(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cret")
    body = json.dumps(PAYLOAD).encode()
    headers = {"Content-Type": "application/json"}
    assert client.post("/api/webhooks/github", content=body, headers=headers).status_code == 401

    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    headers["X-Hub-Signature-256"] = good
    assert client.post("/api/webhooks/github", content=body, headers=headers).status_code == 202
