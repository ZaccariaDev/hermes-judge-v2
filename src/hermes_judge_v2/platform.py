"""Strict platform contract for history, checkpoint, rename and Discord DM."""

from __future__ import annotations

import time
from dataclasses import dataclass
import re
from typing import Any, Protocol

from .state import GoalState

CHECKPOINT_MARKER = "HERMES_CHECKPOINT_V2"


class PlatformAdapter(Protocol):
    async def get_thread_history(self, channel_id: str, **page: Any) -> list[dict[str, Any]]: ...
    async def get_message(self, channel_id: str, message_id: str) -> dict[str, Any] | None: ...
    async def get_pins(self, channel_id: str) -> list[dict[str, Any]]: ...
    async def create_message(self, channel_id: str, content: str) -> dict[str, Any]: ...
    async def update_message(self, channel_id: str, message_id: str, content: str) -> dict[str, Any]: ...
    async def pin_message(self, channel_id: str, message_id: str) -> None: ...
    async def rename_thread(self, channel_id: str, name: str) -> None: ...
    async def open_dm(self, recipient_id: str) -> dict[str, Any]: ...
    async def send_message(self, channel_id: str, content: str, attachments: list[Any] | None = None) -> dict[str, Any]: ...


@dataclass
class PlatformResult:
    success: bool
    code: str
    message: str = ""
    channel_id: str | None = None
    message_id: str | None = None
    retryable: bool = False


def classify_discord_error(code: int | str | None) -> PlatformResult:
    value = str(code or "unknown")
    if value == "50007":
        return PlatformResult(False, value, "Discord refused the DM for this user.", retryable=False)
    if value == "50278":
        return PlatformResult(False, value, "No mutual server permits this DM.", retryable=True)
    if value == "429":
        return PlatformResult(False, value, "Discord rate limit.", retryable=True)
    return PlatformResult(False, value, "Discord operation failed.", retryable=True)


def checkpoint_content(state: GoalState, next_action: str = "") -> str:
    return "\n".join([
        f"[{CHECKPOINT_MARKER}:{state.goal_id}:{state.goal_version}]",
        f"Projet: {state.initial_goal[:180]}",
        f"Phase: {state.phase} — {state.subtask or 'aucune sous-tâche'}",
        f"Workspace: {state.workspace_root}",
        f"Révision: {state.revision}",
        f"Dernier résultat prouvé: {state.evidence[-1]['description'] if state.evidence else 'aucun'}",
        f"Prochaine action: {next_action or 'collecter une preuve nouvelle'}",
        f"Modèle: {(state.active_provider or '?')}/{(state.active_model or '?')}",
    ])


def parse_checkpoint(content: str) -> dict[str, Any] | None:
    match = re.search(r"\[HERMES_CHECKPOINT_V2:([^:\]]+):(\d+)\]", content or "")
    if not match:
        return None
    workspace = re.search(r"^Workspace: (.+)$", content, re.M)
    revision = re.search(r"^Révision: (\d+)$", content, re.M)
    return {
        "goal_id": match.group(1), "goal_version": int(match.group(2)),
        "workspace_root": workspace.group(1).strip() if workspace else "",
        "revision": int(revision.group(1)) if revision else 0,
        "content": content,
    }


class PlatformCoordinator:
    def __init__(self, adapter: PlatformAdapter, *, rename_cooldown: float = 900):
        self.adapter = adapter
        self.rename_cooldown = rename_cooldown

    async def history_after(
        self, channel_id: str, message_id: str | None, *, page_size: int = 100,
        max_pages: int = 100,
    ) -> list[dict[str, Any]]:
        """Read a bounded, de-duplicated history delta with adapter pagination."""
        after = message_id
        collected: dict[str, dict[str, Any]] = {}
        for _ in range(max_pages):
            page = await self.adapter.get_thread_history(
                channel_id, after=after, limit=page_size
            )
            if not page:
                break
            for message in page:
                mid = str(message.get("id") or "")
                if mid:
                    collected[mid] = message
            next_after = str(page[-1].get("id") or "")
            if not next_after or next_after == after or len(page) < page_size:
                break
            after = next_after
        return sorted(collected.values(), key=lambda m: int(str(m.get("id") or "0")))

    async def upsert_checkpoint(self, state: GoalState, channel_id: str, next_action: str) -> PlatformResult:
        content = checkpoint_content(state, next_action)
        try:
            if state.checkpoint_message_id:
                result = await self.adapter.update_message(
                    channel_id, state.checkpoint_message_id, content
                )
            else:
                result = await self.adapter.create_message(channel_id, content)
                state.checkpoint_message_id = str(result["id"])
                state.checkpoint_channel_id = channel_id
            message_id = state.checkpoint_message_id
            reread = await self.adapter.get_message(channel_id, message_id)
            if not reread or reread.get("content") != content:
                return PlatformResult(False, "checkpoint_readback_failed")
            pins = await self.adapter.get_pins(channel_id)
            if not any(str(p.get("id")) == message_id for p in pins):
                await self.adapter.pin_message(channel_id, message_id)
                pins = await self.adapter.get_pins(channel_id)
            if not any(str(p.get("id")) == message_id for p in pins):
                return PlatformResult(False, "checkpoint_pin_failed")
            state.set_checkpoint(channel_id, message_id, content)
            return PlatformResult(True, "checkpoint_verified", message_id=message_id)
        except Exception as exc:
            return PlatformResult(False, "checkpoint_error", str(exc), retryable=True)

    async def rename_if_needed(
        self, state: GoalState, channel_id: str, new_title: str, reason: str,
        *, now: float | None = None,
    ) -> PlatformResult:
        now = time.time() if now is None else now
        title = " ".join(new_title.replace("\n", " ").split())[:100]
        if not title:
            return PlatformResult(False, "invalid_title")
        if state.rename.get("title") == title:
            return PlatformResult(True, "rename_idempotent")
        if now - float(state.rename.get("at", 0)) < self.rename_cooldown:
            return PlatformResult(False, "rename_cooldown")
        try:
            await self.adapter.rename_thread(channel_id, title)
            old = state.rename.get("title")
            state.rename = {"title": title, "previous": old, "reason": reason, "at": now}
            state.touch("thread_renamed", old=old, new=title, reason=reason)
            return PlatformResult(True, "thread_renamed")
        except Exception as exc:
            return PlatformResult(False, "rename_error", str(exc), retryable=True)

    async def send_dm(
        self, state: GoalState, recipient_id: str, content: str,
        attachments: list[Any] | None = None,
    ) -> PlatformResult:
        if recipient_id not in state.dm_authorized_recipients:
            return PlatformResult(False, "dm_not_authorized")
        try:
            opened = await self.adapter.open_dm(recipient_id)
            channel_id = str(opened["id"])
            sent = await self.adapter.send_message(channel_id, content, attachments)
            message_id = str(sent["id"])
            reread = await self.adapter.get_message(channel_id, message_id)
            if not reread or reread.get("content") != content:
                return PlatformResult(False, "dm_delivery_unverified")
            return PlatformResult(
                True, "dm_verified", channel_id=channel_id, message_id=message_id
            )
        except Exception as exc:
            code = getattr(exc, "code", None)
            result = classify_discord_error(code)
            result.message = str(exc) or result.message
            return result
