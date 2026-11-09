"""Use the gateway's single policy source when running inside a patched Hermes runtime."""

def fallback_allowed() -> bool:
    try:
        from agent.fallback_policy import fallback_allowed as runtime_allowed
    except ModuleNotFoundError as exc:
        if exc.name not in {"agent", "agent.fallback_policy"}:
            raise
        # Standalone JudgeV2 / older Hermes has no thread-local gateway command.
        return True
    return runtime_allowed()
