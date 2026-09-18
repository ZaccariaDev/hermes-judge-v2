"""Exact context reconstruction and switch compaction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import GoalState


class ContextDivergence(RuntimeError):
    pass


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


@dataclass
class ContextPacket:
    payload: dict[str, Any]
    digest: str

    def verify(self) -> bool:
        return self.digest == _digest(self.payload)


class ContextBuilder:
    def build(
        self,
        state: GoalState,
        *,
        pinned_checkpoint: dict[str, Any] | None,
        messages_after_checkpoint: list[dict[str, Any]],
    ) -> ContextPacket:
        pin = pinned_checkpoint or {}
        if pin:
            if pin.get("goal_id") != state.goal_id:
                raise ContextDivergence("checkpoint belongs to another goal")
            if int(pin.get("goal_version", -1)) != state.goal_version:
                raise ContextDivergence("checkpoint goal version diverges")
            if Path(pin.get("workspace_root", "")).resolve() != Path(state.workspace_root):
                raise ContextDivergence("checkpoint workspace diverges")
            pin_revision = int(pin.get("revision", 0))
            if pin_revision > state.revision:
                raise ContextDivergence("checkpoint is newer than authoritative state")

        delta = []
        for message in messages_after_checkpoint:
            channel = str(message.get("channel_id") or "")
            if state.initial_channel_id and channel and channel != state.initial_channel_id:
                continue
            delta.append({
                "id": str(message.get("id") or ""),
                "author_id": str(message.get("author_id") or ""),
                "content": str(message.get("content") or ""),
                "timestamp": message.get("timestamp"),
            })

        payload = {
            "goal_id": state.goal_id,
            "goal_version": state.goal_version,
            "initial_goal_immutable": state.initial_goal,
            "workspace_root": state.workspace_root,
            "contract": state.contract.__dict__,
            "phase": state.phase,
            "subtask": state.subtask,
            "authoritative_revision": state.revision,
            "last_checkpoint": {
                "message_id": state.checkpoint_message_id,
                "revision": state.checkpoint_revision,
                "content": state.checkpoint_content,
                "pin": pin or None,
            },
            "message_delta": delta,
            "user_corrections": list(state.user_corrections),
            "verified_evidence": [
                e for e in state.evidence if e.get("verified")
            ][-50:],
            "attempts": state.attempts[-30:],
            "strategies": state.strategies[-20:],
            "contradictions": state.contradictions[-20:],
            "next_action_constraints": {
                "forbidden_action_signature": state.last_action_signature
                if state.no_progress_count >= 2 else "",
                "no_progress_count": state.no_progress_count,
            },
        }
        return ContextPacket(payload=payload, digest=_digest(payload))

    def compaction_for_switch(
        self, state: GoalState, *, next_action: str, target_model: str
    ) -> ContextPacket:
        payload = {
            "kind": "model_switch_compaction",
            "goal_id": state.goal_id,
            "goal_version": state.goal_version,
            "workspace_root": state.workspace_root,
            "phase": state.phase,
            "subtask": state.subtask,
            "initial_goal_immutable": state.initial_goal,
            "contract": state.contract.__dict__,
            "state_revision": state.revision,
            "state_digest": state.digest,
            "deployment_status": state.deployment_status,
            "verified_evidence": [
                e for e in state.evidence if e.get("verified")
            ][-50:],
            "attempts": state.attempts[-30:],
            "strategies": state.strategies[-20:],
            "user_corrections": state.user_corrections[-20:],
            "contradictions": state.contradictions[-20:],
            "last_message_consumed": state.last_message_consumed,
            "next_action": next_action,
            "current_provider": state.active_provider,
            "current_model": state.active_model,
            "target_model": target_model,
            "forbidden_repetition": state.last_action_signature
            if state.no_progress_count >= 2 else "",
        }
        return ContextPacket(payload=payload, digest=_digest(payload))
