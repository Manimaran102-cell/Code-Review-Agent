"""Runtime configuration, read from environment variables / .env.

Settings are read at call time (not import time) so tests can override
them with ``monkeypatch.setenv``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    github_token: str
    openai_api_key: str
    openai_model: str
    webhook_secret: str
    checkpointer: str
    sqlite_path: str
    max_revisions: int
    mock_mode: bool


def get_settings() -> Settings:
    return Settings(
        github_token=os.getenv("GITHUB_TOKEN", ""),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET", ""),
        checkpointer=os.getenv("CHECKPOINTER", "sqlite").strip().lower(),
        sqlite_path=os.getenv("SQLITE_PATH", "data/checkpoints.sqlite"),
        max_revisions=int(os.getenv("MAX_REVISIONS", "5")),
        mock_mode=_flag("MOCK_MODE"),
    )
