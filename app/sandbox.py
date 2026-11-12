"""Custom LangChain tool: lint Python source inside a throw-away sandbox.

The code is only ever analysed statically (flake8 + ``black --check``); it is
never imported or executed. Isolation layers:

* the source is written to a fresh temporary directory that is deleted afterwards
* linters run as subprocesses (no shell) with a minimal environment, the temp
  dir as cwd/HOME, and a hard timeout
* oversized inputs are rejected before anything touches the disk

For stronger isolation run the whole service in the provided Docker image.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from langchain_core.tools import tool

CLEAN_MARKER = "OK: no lint issues found."
ERROR_PREFIX = "ERROR:"
MAX_SOURCE_BYTES = 512_000
TIMEOUT_SECONDS = 30
MAX_LINE_LENGTH = 88  # black's default; keeps flake8 and black from fighting


def _sandbox_env(workdir: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": workdir,
        "BLACK_CACHE_DIR": workdir,
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
    }


def _run(cmd: list[str], workdir: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=workdir,
        env=_sandbox_env(workdir),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        shell=False,
        check=False,
    )


@tool
def run_linter_tool(code_string: str) -> str:
    """Writes python code to a temporary file and runs flake8 + black --check.

    Returns the linter findings, one per line (``code.py:LINE:COL: CODE msg``
    for flake8, ``BLACK: ...`` for formatting). Returns a fixed success marker
    when the code is clean and a line starting with ``ERROR:`` if the sandbox
    itself failed (timeout, missing linter, oversized input).
    """
    if len(code_string.encode("utf-8")) > MAX_SOURCE_BYTES:
        return f"{ERROR_PREFIX} source exceeds {MAX_SOURCE_BYTES} bytes"

    findings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="lint_sandbox_") as workdir:
        Path(workdir, "code.py").write_text(code_string, encoding="utf-8")
        try:
            flake = _run(
                [
                    sys.executable,
                    "-m",
                    "flake8",
                    f"--max-line-length={MAX_LINE_LENGTH}",
                    "--extend-ignore=E203,W503",
                    "code.py",
                ],
                workdir,
            )
            black = _run(
                [sys.executable, "-m", "black", "--check", "--no-color", "code.py"],
                workdir,
            )
        except subprocess.TimeoutExpired:
            return f"{ERROR_PREFIX} linter timed out after {TIMEOUT_SECONDS}s"
        except OSError as exc:  # interpreter/linter could not be started
            return f"{ERROR_PREFIX} could not start linter: {exc}"

    findings.extend(line for line in flake.stdout.splitlines() if line.strip())
    if flake.returncode not in (0, 1) and flake.stderr.strip():
        findings.append(f"{ERROR_PREFIX} flake8: {flake.stderr.strip()}")

    if black.returncode == 123:  # black: internal error / cannot parse
        reason = black.stderr.strip().splitlines()
        findings.append(f"BLACK: {reason[0] if reason else 'cannot format code'}")
    elif black.returncode == 1:
        findings.append("BLACK: file would be reformatted (run black)")
    elif black.returncode not in (0,):
        findings.append(f"{ERROR_PREFIX} black exited {black.returncode}")

    return "\n".join(findings) if findings else CLEAN_MARKER


def parse_findings(output: str) -> list[str]:
    """Turn the tool's string output into a list of findings ([] when clean)."""
    return [
        line for line in output.splitlines() if line.strip() and line != CLEAN_MARKER
    ]
