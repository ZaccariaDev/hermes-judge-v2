"""Durable, integrity-checked goal state.

SessionDB remains authoritative. The workspace mirror is a portable recovery
copy and can never silently overwrite SessionDB.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

FORMAT_VERSION = 2
MIRROR_NAME = ".hermes_goal_state.json"


def _canonical(data: dict[str, Any]) -> bytes:
    clean = deepcopy(data)
    clean.pop("digest", None)
    return json.dumps(
        clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest_dict(data: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(data)).hexdigest()


@dataclass
class GoalContract:
    outcome: str = ""
    verification: str = ""
    constraints: str = ""
    boundaries: str = ""
    stop_when: str = ""


@dataclass
class GoalState:
    goal_id: str
    session_id: str
    initial_goal: str
    workspace_root: str
    goal_version: int = 1
    revision: int = 0
    generation: int = 0
    updated_at: float = 0.0
    digest: str = ""
    status: str = "active"
    phase: str = "initialization"
    subtask: str = ""
    deployment_status: str = "PROTOTYPE_ONLY"
    contract: GoalContract = field(default_factory=GoalContract)
    success_criteria: list[dict[str, Any]] = field(default_factory=list)
    initial_message_id: str | None = None
    initial_channel_id: str | None = None
    initial_thread_id: str | None = None
    initial_guild_id: str | None = None
    owner_id: str | None = None
    initial_attachments: list[dict[str, Any]] = field(default_factory=list)
    checkpoint_message_id: str | None = None
    checkpoint_channel_id: str | None = None
    checkpoint_revision: int = 0
    checkpoint_content: str = ""
    last_message_consumed: str | None = None
    active_worker: str | None = None
    active_judge: str | None = None
    active_provider: str | None = None
    active_model: str | None = None
    model_health: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    strategies: list[dict[str, Any]] = field(default_factory=list)
    user_corrections: list[dict[str, Any]] = field(default_factory=list)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    no_progress_count: int = 0
    fragmented_count: int = 0
    last_action_signature: str = ""
    lease: dict[str, Any] = field(default_factory=dict)
    heartbeat: dict[str, Any] = field(default_factory=dict)
    rename: dict[str, Any] = field(default_factory=dict)
    dm_authorized_recipients: list[str] = field(default_factory=list)
    incidents: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        root = Path(self.workspace_root).expanduser().resolve()
        if not root.is_absolute():
            raise ValueError("workspace_root must be absolute")
        self.workspace_root = str(root)
        if not self.initial_goal.strip():
            raise ValueError("initial_goal cannot be empty")
        if not self.goal_id:
            self.goal_id = str(uuid.uuid4())

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        initial_goal: str,
        workspace_root: str,
        contract: GoalContract | None = None,
        discord: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> "GoalState":
        discord = discord or {}
        state = cls(
            goal_id=str(uuid.uuid4()),
            session_id=session_id,
            initial_goal=initial_goal,
            workspace_root=workspace_root,
            contract=contract or GoalContract(),
            initial_message_id=discord.get("message_id"),
            initial_channel_id=discord.get("channel_id"),
            initial_thread_id=discord.get("thread_id"),
            initial_guild_id=discord.get("guild_id"),
            owner_id=discord.get("owner_id"),
            initial_attachments=deepcopy(attachments or []),
        )
        state.touch("goal_created")
        return state

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["_format"] = "hermes-goal-v2"
        data["_format_version"] = FORMAT_VERSION
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoalState":
        if data.get("_format") != "hermes-goal-v2":
            raise ValueError("not a Hermes V2 goal state")
        if int(data.get("_format_version", 0)) != FORMAT_VERSION:
            raise ValueError("unsupported Hermes V2 state version")
        raw_digest = str(data.get("digest") or "")
        if not raw_digest or raw_digest != digest_dict(data):
            raise ValueError("state digest mismatch")
        values = {
            k: deepcopy(v) for k, v in data.items() if k in cls.__dataclass_fields__
        }
        values["contract"] = GoalContract(**(values.get("contract") or {}))
        state = cls(**values)
        # Same-format schema additions use dataclass defaults safely.
        state.digest = digest_dict(state.to_dict())
        
        return state

    def verify_digest(self) -> bool:
        return bool(self.digest) and self.digest == digest_dict(self.to_dict())

    def touch(self, event: str, **details: Any) -> None:
        self.revision += 1
        self.updated_at = time.time()
        self.events.append({"at": self.updated_at, "event": event, **details})
        self.digest = digest_dict(self.to_dict())

    def add_evidence(
        self, kind: str, description: str, data: Any, *, verified: bool
    ) -> dict[str, Any]:
        item = {
            "id": str(uuid.uuid4()), "at": time.time(), "kind": kind,
            "description": description, "data": deepcopy(data),
            "verified": bool(verified),
        }
        self.evidence.append(item)
        self.touch("evidence_added", evidence_id=item["id"])
        return item

    def verify_criterion(self, criterion_id: str, evidence_id: str) -> None:
        """Mark one criterion verified, always tied to verified evidence."""
        evidence = next(
            (item for item in self.evidence if item.get("id") == evidence_id), None
        )
        if evidence is None or not evidence.get("verified"):
            raise ValueError("criterion verification requires verified evidence")
        criterion = next(
            (item for item in self.success_criteria if item.get("id") == criterion_id),
            None,
        )
        if criterion is None:
            raise KeyError(f"unknown success criterion: {criterion_id}")
        criterion["status"] = "verified"
        references = criterion.setdefault("evidence_ids", [])
        if evidence_id not in references:
            references.append(evidence_id)
        self.touch(
            "criterion_verified", criterion_id=criterion_id,
            evidence_id=evidence_id,
        )

    def record_attempt(
        self, strategy_id: str, action_signature: str, result: str, error: str = ""
    ) -> None:
        if action_signature and action_signature == self.last_action_signature and result != "success":
            self.no_progress_count += 1
        elif result == "success":
            self.no_progress_count = 0
        self.last_action_signature = action_signature
        self.attempts.append({
            "at": time.time(), "strategy_id": strategy_id,
            "action_signature": action_signature, "result": result, "error": error,
        })
        self.touch("attempt_recorded", result=result)

    def add_correction(self, correction: str, message_id: str | None = None) -> None:
        self.user_corrections.append({
            "at": time.time(), "correction": correction, "message_id": message_id
        })
        self.touch("user_correction")

    def set_checkpoint(self, channel_id: str, message_id: str, content: str) -> None:
        self.checkpoint_channel_id = channel_id
        self.checkpoint_message_id = message_id
        self.checkpoint_content = content
        self.checkpoint_revision = self.revision + 1
        self.touch("checkpoint_saved", message_id=message_id)

    def find_incident(self, fingerprint: str) -> dict[str, Any] | None:
        return next(
            (item for item in self.incidents if item.get("fingerprint") == fingerprint),
            None,
        )

    def record_incident(self, incident: dict[str, Any]) -> None:
        """Persist one incident ticket without duplicating the same failure."""
        fingerprint = str(incident.get("fingerprint") or "")
        if not fingerprint:
            raise ValueError("incident fingerprint is required")
        existing = self.find_incident(fingerprint)
        if existing is None:
            self.incidents.append(deepcopy(incident))
        else:
            existing.update(deepcopy(incident))
        self.touch("incident_recorded", fingerprint=fingerprint)

    def acquire_lease(self, worker_id: str, ttl: float, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if self.lease and self.lease.get("expires_at", 0) > now:
            return self.lease.get("worker_id") == worker_id
        self.generation += 1
        self.lease = {
            "worker_id": worker_id, "acquired_at": now, "renewed_at": now,
            "expires_at": now + max(1.0, ttl), "generation": self.generation,
        }
        self.touch("lease_acquired", worker_id=worker_id)
        return True

    def release_lease(self, worker_id: str) -> bool:
        if not self.lease or self.lease.get("worker_id") != worker_id:
            return False
        self.lease = {}
        self.active_worker = None
        self.touch("lease_released", worker_id=worker_id)
        return True


class SessionStore(Protocol):
    def get_meta(self, key: str) -> str | None: ...
    def set_meta(self, key: str, value: str) -> None: ...


class MemorySessionStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_meta(self, key: str) -> str | None:
        return self.values.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self.values[key] = value


class WorkspaceViolation(RuntimeError):
    pass


class WorkspaceGuard:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    def resolve(self, candidate: str | Path) -> Path:
        path = Path(candidate)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceViolation(f"path escapes workspace: {path}") from exc
        return path


class DualStateStore:
    """SessionDB-first store with an atomic, verified workspace mirror."""

    def __init__(self, session_store: SessionStore) -> None:
        self.session_store = session_store

    @staticmethod
    def key(session_id: str) -> str:
        return f"goal_v2:{session_id}"

    @staticmethod
    def mirror_path(workspace_root: str) -> Path:
        return Path(workspace_root).resolve() / MIRROR_NAME

    def save(self, state: GoalState) -> None:
        state.touch("state_saved")
        raw = json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True)
        key = self.key(state.session_id)
        self.session_store.set_meta(key, raw)
        if self.session_store.get_meta(key) != raw:
            raise IOError("SessionDB read-after-write verification failed")
        target = self.mirror_path(state.workspace_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        if self._read_mirror(target).digest != state.digest:
            raise IOError("mirror read-after-write verification failed")

    def _read_mirror(self, path: Path) -> GoalState:
        return GoalState.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def load(self, session_id: str, workspace_root: str) -> GoalState | None:
        raw = self.session_store.get_meta(self.key(session_id))
        path = self.mirror_path(workspace_root)
        db_state = GoalState.from_dict(json.loads(raw)) if raw else None
        mirror_state = None
        mirror_error = None
        if path.exists():
            try:
                mirror_state = self._read_mirror(path)
            except Exception as exc:
                mirror_error = str(exc)
        if db_state is not None:
            if mirror_error or (
                mirror_state is not None and mirror_state.digest != db_state.digest
            ):
                db_state.events.append({
                    "at": time.time(), "event": "DIVERGENCE_SESSIONDB_MIRROR",
                    "mirror_error": mirror_error, "db_revision": db_state.revision,
                    "mirror_revision": getattr(mirror_state, "revision", None),
                })
                self.save(db_state)
            return db_state
        if mirror_state is not None and mirror_state.session_id == session_id:
            mirror_state.events.append({"at": time.time(), "event": "FALLBACK_TO_MIRROR"})
            return mirror_state
        return None
