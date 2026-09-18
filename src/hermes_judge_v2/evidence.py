"""Safe, bounded evidence collection."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Sequence

from .state import WorkspaceGuard


def file_evidence(root: str, path: str) -> dict[str, Any]:
    target = WorkspaceGuard(root).resolve(path)
    if not target.exists():
        return {"exists": False, "path": str(target)}
    if target.is_dir():
        return {"exists": True, "is_dir": True, "path": str(target)}
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    stat = target.stat()
    return {
        "exists": True, "is_dir": False, "path": str(target),
        "size": stat.st_size, "sha256": digest, "modified_at": stat.st_mtime,
    }


def command_evidence(
    root: str, argv: Sequence[str], *, timeout: float = 300
) -> dict[str, Any]:
    if not argv:
        raise ValueError("argv cannot be empty")
    cwd = WorkspaceGuard(root).root
    try:
        proc = subprocess.run(
            list(argv), cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, shell=False,
        )
        return {
            "argv": list(argv), "exit_code": proc.returncode,
            "stdout": proc.stdout[-12000:], "stderr": proc.stderr[-12000:],
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": list(argv), "exit_code": -1,
            "stdout": (exc.stdout or "")[-12000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-12000:] if isinstance(exc.stderr, str) else "",
            "timed_out": True,
        }
