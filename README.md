# Autonomous Code Review & Refactoring Agent

A LangGraph agent that receives a GitHub PR webhook, lints the changed Python
files in a sandbox, asks an LLM for a refactor, **pauses for human approval**,
and only then commits to the PR branch. Exposed through FastAPI.

```
webhook -> fetch_pr_node -> lint_code_node -> (clean? END)
                                 |
                          propose_refactor_node <--- rejection + feedback
                                 |                        (rollback)
                         [interrupt_before]
                                 |
                          apply_changes_node -> END   (needs is_approved=True)
```

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in GITHUB_TOKEN and OPENAI_API_KEY
uvicorn main:app --reload   # http://127.0.0.1:8000/docs
```

No credentials yet? Set `MOCK_MODE=true` in `.env` to use a canned PR file, a
deterministic refactorer and a fake commit.

Docker: `docker build -t review-agent . && docker run --env-file .env -p 8000:8000 review-agent`

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/webhooks/github` | Start a review (202 + `thread_id`). Accepts a real `pull_request` event or `{"repository": {"html_url": ...}, "branch": ...}`. Verifies `X-Hub-Signature-256` when `GITHUB_WEBHOOK_SECRET` is set. |
| GET | `/api/reviews/{thread_id}` | Status, lint errors, draft, diffs, audit history. |
| POST | `/api/reviews/{thread_id}/approve` | `{"approved": true}` commits. `{"approved": false, "feedback": "..."}` regenerates the draft. |
| GET | `/health` | Liveness. |

Statuses: `running`, `awaiting_approval`, `completed`, `clean`, `rejected`,
`max_revisions_reached`, `failed`. Errors: 404 unknown thread, 409 thread not
awaiting approval, 422 bad payload, 401 bad signature.

OpenAPI spec: `python scripts/export_openapi.py` writes `openapi.json`
(also live at `/openapi.json`).

## Design notes

- **State** (`app/state.py`): `pr_url`, `code_contents`, `lint_errors`,
  `refactored_code`, `is_approved`, `reviewer_feedback`, plus an append-only
  `history` audit trail.
- **Sandbox tool** (`app/sandbox.py`): `run_linter_tool(code_string)` is a
  LangChain `@tool`. It writes the code to a temp dir and runs flake8 and
  `black --check` as subprocesses (no shell, minimal env, 30s timeout, size cap).
  Code is never imported or executed. Use the Docker image for stronger isolation.
- **Human-in-the-loop**: the graph is compiled with
  `interrupt_before=["apply_changes_node"]`, so it halts after every draft.
  `apply_changes_node` additionally raises if `is_approved` is not `True`.
- **Rollback**: a rejection with feedback is written to the checkpoint as if
  `apply_changes_node` had run (`update_state(..., as_node=...)`); the
  conditional edge then routes back to `propose_refactor_node`, which re-runs
  with `reviewer_feedback` and halts at the gate again. Capped by `MAX_REVISIONS`.
- **Persistence**: `SqliteSaver` by default (`data/checkpoints.sqlite`), so
  paused threads survive restarts; `CHECKPOINTER=memory` for ephemeral runs.
  The graph is run in a background task; poll the GET endpoint.

## Verification test cases

1. **Webhook** - `POST /api/webhooks/github` -> thread starts, runs
   fetch -> lint -> propose, then halts (`awaiting_approval`), nothing committed.
2. **Rejection** - `POST .../approve` with `{"approved": false, "feedback": "Use list comprehensions instead of loops"}` -> a second draft is generated using the feedback, thread halts again.
3. **Approval** - `POST .../approve` with `{"approved": true}` -> commit made to the PR branch, status `completed`, SHA in `commit_shas`.

```bash
B=http://127.0.0.1:8000
curl -X POST $B/api/webhooks/github -H 'Content-Type: application/json' \
  -d '{"repository":{"html_url":"https://github.com/OWNER/REPO"},"branch":"feature/x"}'
curl $B/api/reviews/THREAD_ID
curl -X POST $B/api/reviews/THREAD_ID/approve -H 'Content-Type: application/json' \
  -d '{"approved":false,"feedback":"Use list comprehensions instead of loops"}'
curl -X POST $B/api/reviews/THREAD_ID/approve -H 'Content-Type: application/json' \
  -d '{"approved":true}'
```

## Tests

```bash
python -m pytest          # 36 tests, no network or API keys needed
```
