"""LLM-backed refactor generation (with a deterministic mock for offline runs)."""

from __future__ import annotations

import ast
import re
from typing import Optional

import black

from app.config import get_settings

SYSTEM_PROMPT = (
    "You are a meticulous senior Python engineer doing code review. "
    "Rewrite the given file so that every listed linter finding is fixed. "
    "Preserve the original behaviour and public interfaces exactly. "
    "Follow black formatting (88 columns). If reviewer feedback is given, "
    "apply it as well. Reply with ONLY the complete corrected file contents "
    "- no explanations, no markdown fences."
)

_FENCE_RE = re.compile(r"^```[\w-]*\n(.*?)\n?```\s*$", re.DOTALL)


def strip_code_fences(text: str) -> str:
    text = text.strip()
    match = _FENCE_RE.match(text)
    body = match.group(1) if match else text
    return body.rstrip() + "\n"


def generate_refactor(
    path: str,
    code: str,
    lint_errors: list[str],
    feedback: Optional[str] = None,
) -> str:
    """Return a refactored version of ``code`` that fixes ``lint_errors``."""
    if get_settings().mock_mode:
        return _mock_refactor(code, feedback)

    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI

    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set (or enable MOCK_MODE=true)")

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            (
                "human",
                "File: {path}\n\nLinter findings:\n{errors}\n\n"
                "Reviewer feedback: {feedback}\n\nSource:\n{code}",
            ),
        ]
    )
    model = ChatOpenAI(
        model=settings.openai_model,
        temperature=0,
        api_key=settings.openai_api_key,
    )
    reply = (prompt | model).invoke(
        {
            "path": path,
            "errors": "\n".join(lint_errors) or "(none)",
            "feedback": feedback or "(none)",
            "code": code,
        }
    )
    return strip_code_fences(str(reply.content))


# --------------------------------------------------------------------------
# Deterministic mock (MOCK_MODE=true): black + a tiny loop->comprehension rewrite
# --------------------------------------------------------------------------
class _LoopToComprehension(ast.NodeTransformer):
    """``r = []; for x in xs: r.append(e); return r`` -> ``return [e for x in xs]``."""

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        body = node.body
        if len(body) != 3:
            return node
        init, loop, ret = body
        if not (
            isinstance(init, ast.Assign)
            and len(init.targets) == 1
            and isinstance(init.targets[0], ast.Name)
            and isinstance(init.value, ast.List)
            and not init.value.elts
            and isinstance(loop, ast.For)
            and not loop.orelse
            and len(loop.body) == 1
            and isinstance(ret, ast.Return)
            and isinstance(ret.value, ast.Name)
        ):
            return node
        acc = init.targets[0].id
        stmt = loop.body[0]
        call = stmt.value if isinstance(stmt, ast.Expr) else None
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "append"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == acc
            and ret.value.id == acc
            and len(call.args) == 1
        ):
            return node
        comp = ast.ListComp(
            elt=call.args[0],
            generators=[
                ast.comprehension(
                    target=loop.target, iter=loop.iter, ifs=[], is_async=0
                )
            ],
        )
        node.body = [ast.Return(value=comp)]
        return ast.fix_missing_locations(node)


def _mock_refactor(code: str, feedback: Optional[str]) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code  # nothing sensible to do offline
    if feedback and "comprehension" in feedback.lower():
        tree = _LoopToComprehension().visit(tree)
        code = ast.unparse(tree) + "\n"
    try:
        return black.format_str(code, mode=black.Mode(line_length=88))
    except black.InvalidInput:
        return code
