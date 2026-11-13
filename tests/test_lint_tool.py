"""Lint tool isolation tests."""

from __future__ import annotations

import os

from langchain_core.tools import BaseTool

from app import sandbox
from app.sandbox import CLEAN_MARKER, parse_findings, run_linter_tool


def test_is_a_langchain_tool():
    assert isinstance(run_linter_tool, BaseTool)
    assert run_linter_tool.name == "run_linter_tool"


def test_identifies_syntax_error_in_bad_code_string():
    out = run_linter_tool.invoke({"code_string": "def broken(:\n    pass\n"})
    assert "E999" in out  # flake8 syntax error code
    assert "SyntaxError" in out
    assert out != CLEAN_MARKER


def test_flags_style_problems():
    out = run_linter_tool.invoke({"code_string": "x=1\n"})
    assert "E225" in out
    assert "BLACK" in out


def test_clean_code_returns_marker_and_no_findings():
    out = run_linter_tool.invoke({"code_string": "x = 1\n"})
    assert out == CLEAN_MARKER
    assert parse_findings(out) == []


def test_code_is_analysed_not_executed(tmp_path, monkeypatch):
    marker = tmp_path / "pwned"
    evil = f"open({str(marker)!r}, 'w').write('x')\n"
    run_linter_tool.invoke({"code_string": evil})
    assert not marker.exists()


def test_sandbox_leaves_no_files_behind(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    run_linter_tool.invoke({"code_string": "x = 1\n"})
    assert os.listdir(tmp_path) == []


def test_oversized_input_is_rejected_before_running():
    out = run_linter_tool.invoke({"code_string": "#" * (sandbox.MAX_SOURCE_BYTES + 1)})
    assert out.startswith(sandbox.ERROR_PREFIX)


def test_timeout_is_reported(monkeypatch):
    import subprocess

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="flake8", timeout=1)

    monkeypatch.setattr(sandbox.subprocess, "run", boom)
    out = run_linter_tool.invoke({"code_string": "x = 1\n"})
    assert out.startswith(sandbox.ERROR_PREFIX) and "timed out" in out
