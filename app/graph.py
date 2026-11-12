"""Graph assembly: StateGraph + persistent checkpointer + HITL interrupt gate."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.nodes import (
    APPLY,
    PROPOSE,
    apply_changes_node,
    fetch_pr_node,
    lint_code_node,
    propose_refactor_node,
    route_after_apply,
    route_after_lint,
)
from app.state import ReviewState


def make_checkpointer() -> BaseCheckpointSaver:
    """MemorySaver or SqliteSaver, chosen by the CHECKPOINTER setting."""
    settings = get_settings()
    if settings.checkpointer == "memory":
        return MemorySaver()
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = Path(settings.sqlite_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    return SqliteSaver(conn)


def build_graph(checkpointer: Optional[BaseCheckpointSaver] = None):
    """Compile the review pipeline.

    fetch -> lint -> (clean? END) -> propose -> [HUMAN GATE] -> apply -> END
                                        ^------- rejection with feedback ---'
    """
    builder = StateGraph(ReviewState)
    builder.add_node("fetch_pr_node", fetch_pr_node)
    builder.add_node("lint_code_node", lint_code_node)
    builder.add_node(PROPOSE, propose_refactor_node)
    builder.add_node(APPLY, apply_changes_node)

    builder.add_edge(START, "fetch_pr_node")
    builder.add_edge("fetch_pr_node", "lint_code_node")
    builder.add_conditional_edges(
        "lint_code_node", route_after_lint, {PROPOSE: PROPOSE, END: END}
    )
    builder.add_edge(PROPOSE, APPLY)
    builder.add_conditional_edges(
        APPLY, route_after_apply, {PROPOSE: PROPOSE, END: END}
    )

    return builder.compile(
        checkpointer=checkpointer or make_checkpointer(),
        interrupt_before=[APPLY],  # pause for a human before any write
    )
