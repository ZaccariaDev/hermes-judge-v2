"""Human-gated, de-duplicated Discord incident tasks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .state import GoalState

DISCORD_API_BASE = "https://discord.com/api/v10"
ERROR_VERDICTS = {"REPAIR", "RETRY_DIFFERENT_STRATEGY"}
ERROR_REASONS = {
    "file_mutation_failed", "recoverable_error", "repeated_failure",
    "evidence_contradiction", "judge_provider_error",
}


class IncidentClient(Protocol):
    def list_channels(self, guild_id: str) -> list[dict[str, Any]]: ...
    def create_channel(
        self, guild_id: str, *, name: str, channel_type: int,
        parent_id: str | None = None, topic: str | None = None,
    ) -> dict[str, Any]: ...
    def create_message(self, channel_id: str, content: str) -> dict[str, Any]: ...


@dataclass
class IncidentConfig:
    enabled: bool = True
    guild_id: str = ""
    category_name: str = "Hermes - bugs"
    channel_prefix: str = "bug"


class DiscordRestError(RuntimeError):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body[:1000]
        super().__init__(f"Discord API {status}: {self.body}")


class DiscordRestClient:
    def __init__(self, token: str, *, timeout: float = 15):
        if not token.strip():
            raise ValueError("Discord bot token is unavailable")
        self._token = token.strip()
        self.timeout = timeout

    @classmethod
    def from_runtime_secret(cls) -> "DiscordRestClient":
        token = ""
        try:
            from agent.secret_scope import get_secret
            token = get_secret("DISCORD_BOT_TOKEN", "") or ""
        except Exception:
            token = os.getenv("DISCORD_BOT_TOKEN", "")
        return cls(token)

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        request = urllib.request.Request(
            DISCORD_API_BASE + path,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            method=method,
            headers={
                "Authorization": f"Bot {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "Hermes-Judge-V2/2.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024)
                return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as exc:
            body_text = exc.read(65536).decode("utf-8", errors="replace")
            raise DiscordRestError(exc.code, body_text) from exc

    def list_channels(self, guild_id: str) -> list[dict[str, Any]]:
        return list(self._request("GET", f"/guilds/{guild_id}/channels") or [])

    def create_channel(
        self, guild_id: str, *, name: str, channel_type: int,
        parent_id: str | None = None, topic: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "type": channel_type}
        if parent_id:
            body["parent_id"] = parent_id
        if topic:
            body["topic"] = topic[:1024]
        return dict(self._request(
            "POST", f"/guilds/{guild_id}/channels", body
        ) or {})

    def create_message(self, channel_id: str, content: str) -> dict[str, Any]:
        body = {
            "content": content[:2000],
            "allowed_mentions": {"parse": ["users"]},
        }
        return dict(self._request(
            "POST", f"/channels/{channel_id}/messages", body
        ) or {})


def _clean(value: Any, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(
        r"(?i)(token|secret|password|authorization)\s*[:=]\s*\S+",
        r"\1=[REDACTED]", text,
    )
    text = re.sub(r"\b[A-Za-z0-9_-]{32,}\b", "[REDACTED]", text)
    return re.sub(r"https?://\S+", "[URL REDACTED]", text)[:limit]


def safe_error_excerpt(worker_response: str) -> str:
    """Return one useful error line, never the whole worker transcript."""
    pattern = re.compile(
        r"traceback|\berror\b|exception|failed|failing|timeout|rate.?limit|"
        r"not found|permission denied|modulenotfound|importerror",
        re.I,
    )
    lines = [line.strip() for line in str(worker_response or "").splitlines()]
    return _clean(next((line for line in lines if pattern.search(line)), ""), 500)


def incident_fingerprint(state: GoalState, decision: dict[str, Any]) -> str:
    material = json.dumps({
        "goal_id": state.goal_id,
        "reason_code": decision.get("reason_code"),
        "reason": _clean(decision.get("reason")),
        "failed": sorted(_clean(x) for x in decision.get("failed_requirements", [])),
        "integration_error": _clean(decision.get("integration_error")),
        "error_detail": _clean(decision.get("error_detail")),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", value.lower().replace("_", "-"))
    return re.sub(r"-+", "-", value).strip("-")[:45] or "erreur"


def _is_error(decision: dict[str, Any]) -> bool:
    if decision.get("integration_error"):
        return True
    verdict = str(decision.get("verdict") or "").upper()
    reason = str(decision.get("reason_code") or "")
    return verdict in ERROR_VERDICTS and (
        reason in ERROR_REASONS or reason.endswith("error")
    )


class IncidentCoordinator:
    def __init__(
        self, client: IncidentClient, config: IncidentConfig | None = None
    ):
        self.client = client
        self.config = config or IncidentConfig()

    def open_for_decision(
        self, state: GoalState, decision: dict[str, Any]
    ) -> bool:
        if not self.config.enabled or not _is_error(decision):
            return False
        guild_id = str(self.config.guild_id or state.initial_guild_id or "")
        if not guild_id:
            return False
        fingerprint = incident_fingerprint(state, decision)
        if state.find_incident(fingerprint):
            return False

        channels = self.client.list_channels(guild_id)
        marker = f"hermes-incident:{fingerprint}"
        existing = next((
            c for c in channels if marker in str(c.get("topic") or "")
        ), None)
        category = next((
            c for c in channels
            if int(c.get("type", -1)) == 4
            and str(c.get("name", "")).casefold()
            == self.config.category_name.casefold()
        ), None)
        if category is None:
            category = self.client.create_channel(
                guild_id, name=self.config.category_name, channel_type=4
            )

        reason_code = str(
            decision.get("reason_code") or "judge-provider-error"
        )
        channel = existing or self.client.create_channel(
            guild_id,
            name=(
                f"{_slug(self.config.channel_prefix)}-"
                f"{_slug(reason_code)}-{fingerprint[:6]}"
            )[:100],
            channel_type=0,
            parent_id=str(category.get("id") or "") or None,
            topic=(
                f"{marker} goal:{state.goal_id} "
                "status:awaiting-owner-consent"
            ),
        )
        channel_id = str(channel.get("id") or "")
        if not channel_id:
            raise RuntimeError("Discord returned a channel without an id")

        owner = f"<@{state.owner_id}> " if state.owner_id else ""
        failed = ", ".join(
            _clean(x, 120) for x in decision.get("failed_requirements", [])
        ) or "aucun detail supplementaire"
        error = _clean(
            decision.get("integration_error") or decision.get("error_detail")
            or decision.get("reason"), 500
        )
        content = (
            f"{owner}**Nouvelle tache de correction Hermes**\n"
            f"Erreur : `{_clean(reason_code, 80)}`\n"
            f"Constat : {error or 'erreur detectee par le judge'}\n"
            f"Exigences concernees : {failed}\n"
            f"Goal : `{state.goal_id}` - empreinte `{fingerprint}`\n\n"
            "Souhaites-tu que l'agent corrige ce bug ? Reponds "
            "**oui, corrige** pour autoriser la correction, ou **non** "
            "pour laisser la tache en attente. Aucune correction ne doit "
            "commencer avant ton accord explicite."
        )
        message = self.client.create_message(channel_id, content)
        state.record_incident({
            "fingerprint": fingerprint,
            "reason_code": reason_code,
            "status": "awaiting_owner_consent",
            "channel_id": channel_id,
            "message_id": str(message.get("id") or ""),
            "created_at": time.time(),
        })
        return True
