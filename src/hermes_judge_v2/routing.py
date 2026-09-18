"""Provider/model stress and boundary-aware switching."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .context import ContextBuilder, ContextPacket
from .state import GoalState


@dataclass(frozen=True)
class ModelCandidate:
    provider: str
    model: str
    profiles: frozenset[str] = frozenset({"general"})
    context_tokens: int = 0
    cost_rank: int = 0


@dataclass
class Health:
    stress: float = 0.0
    cooldown_until: float = 0.0
    failures: dict[str, int] = field(default_factory=dict)
    identical_failures: int = 0
    contradictions: int = 0
    no_progress: int = 0
    last_signature: str = ""


class StressRegistry:
    WEIGHTS = {
        "429": 35, "quota": 40, "timeout": 22, "5xx": 18,
        "corrupt": 25, "invalid_tool": 18, "context": 25,
        "contradiction": 20, "no_progress": 20,
    }

    def __init__(self) -> None:
        self._health: dict[tuple[str, str, str], Health] = {}

    def get(self, provider: str, model: str, thread_id: str = "") -> Health:
        return self._health.setdefault((provider, model, thread_id), Health())

    def record(
        self, provider: str, model: str, thread_id: str, event: str,
        *, signature: str = "", now: float | None = None,
    ) -> float:
        now = time.time() if now is None else now
        health = self.get(provider, model, thread_id)
        if event == "success":
            health.stress = max(0.0, health.stress - 18)
            health.no_progress = 0
            health.identical_failures = 0
        else:
            health.failures[event] = health.failures.get(event, 0) + 1
            health.stress = min(100.0, health.stress + self.WEIGHTS.get(event, 12))
            if event == "contradiction":
                health.contradictions += 1
            if event == "no_progress":
                health.no_progress += 1
            if signature and signature == health.last_signature:
                health.identical_failures += 1
                health.stress = min(100.0, health.stress + 15)
            health.last_signature = signature or health.last_signature
            if event in {"429", "quota"}:
                health.cooldown_until = max(health.cooldown_until, now + 60)
        return health.stress


@dataclass
class SwitchPlan:
    should_switch: bool
    reason: str
    target: ModelCandidate | None = None
    compaction: ContextPacket | None = None


class ModelRouter:
    def __init__(self, candidates: list[ModelCandidate], registry: StressRegistry | None = None):
        self.candidates = list(candidates)
        self.registry = registry or StressRegistry()

    @staticmethod
    def _state_key(provider: str, model: str, thread_id: str) -> str:
        return "|".join((provider, model, thread_id))

    def record(
        self, state: GoalState, event: str, *, signature: str = "",
        provider: str | None = None, model: str | None = None,
    ) -> float:
        provider = provider or state.active_provider or ""
        model = model or state.active_model or ""
        thread = state.initial_thread_id or ""
        score = self.registry.record(
            provider, model, thread, event, signature=signature
        )
        health = self.registry.get(provider, model, thread)
        state.model_health[self._state_key(provider, model, thread)] = {
            "stress": health.stress, "cooldown_until": health.cooldown_until,
            "failures": dict(health.failures),
            "identical_failures": health.identical_failures,
            "contradictions": health.contradictions,
            "no_progress": health.no_progress,
            "last_signature": health.last_signature,
        }
        state.touch("model_health_updated", provider=provider, model=model, event_type=event)
        return score

    def restore(self, state: GoalState) -> None:
        for raw_key, data in state.model_health.items():
            parts = raw_key.split("|", 2)
            if len(parts) != 3:
                continue
            health = self.registry.get(*parts)
            for name in (
                "stress", "cooldown_until", "failures", "identical_failures",
                "contradictions", "no_progress", "last_signature",
            ):
                if name in data:
                    setattr(health, name, data[name])

    def plan_switch(
        self,
        state: GoalState,
        *,
        profile: str,
        subtask_boundary: bool,
        critical_failure: bool,
        next_action: str,
    ) -> SwitchPlan:
        self.restore(state)
        current_health = self.registry.get(
            state.active_provider or "", state.active_model or "",
            state.initial_thread_id or "",
        )
        stressed = current_health.stress >= 70
        if not critical_failure and not (subtask_boundary and stressed):
            return SwitchPlan(False, "switch requires a boundary plus high stress")
        now = time.time()
        eligible = []
        for candidate in self.candidates:
            if candidate.provider == state.active_provider and candidate.model == state.active_model:
                continue
            health = self.registry.get(
                candidate.provider, candidate.model, state.initial_thread_id or ""
            )
            if health.cooldown_until > now:
                continue
            if profile not in candidate.profiles and "general" not in candidate.profiles:
                continue
            if health.stress + 10 >= current_health.stress and not critical_failure:
                continue
            eligible.append((health.stress, candidate.cost_rank, -candidate.context_tokens, candidate))
        if not eligible:
            return SwitchPlan(False, "no healthier compatible fallback")
        target = sorted(eligible, key=lambda row: row[:3])[0][3]
        packet = ContextBuilder().compaction_for_switch(
            state, next_action=next_action, target_model=target.model
        )
        if not packet.verify():
            return SwitchPlan(False, "compaction verification failed")
        return SwitchPlan(True, "verified compaction and healthy fallback", target, packet)
