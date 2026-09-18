"""Local validation CLI."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .runtime import GoalRuntime
from .state import DualStateStore, MemorySessionStore


def main() -> int:
    parser = argparse.ArgumentParser(prog="hermes-judge-v2")
    parser.add_argument("command", choices=["self-check"])
    args = parser.parse_args()
    if args.command == "self-check":
        with tempfile.TemporaryDirectory() as tmp:
            runtime = GoalRuntime(DualStateStore(MemorySessionStore()))
            state = runtime.create_goal(
                session_id="self-check", goal="Validate Hermes Judge V2",
                workspace_root=tmp,
                success_criteria=[{"id": "core", "description": "core works", "status": "pending"}],
            )
            loaded = runtime.load("self-check", tmp)
            result = {
                "ok": loaded is not None and loaded.verify_digest(),
                "revision": loaded.revision if loaded else None,
                "mirror": str(Path(tmp) / ".hermes_goal_state.json"),
            }
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
