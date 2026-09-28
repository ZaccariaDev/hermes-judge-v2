#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Model Router — Sélection intelligente des modèles cloud gratuits non-contributor.

Implements the directive from /home/zac/home/discord/hermes_provider_autonomy_goal.md:
- Allowlist of 5 free non-contributor models
- Task-fit-based routing with Task Fit Matrix
- Dynamic health-aware scoring
- Fallback chain respecting capability equivalence
"""

import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

# ---------------------------------------------------------------------------
# Configuration — from the directive's allowlist
# ---------------------------------------------------------------------------

PRIMARY_MODEL = "meituan/longcat-2.0"

# Exhaustive zero-price catalogue returned by Nous /v1/models on 2026-09-28.
# meituan/longcat-2.0:free stays documented below but is disabled because live
# inference returns HTTP 404 and asks callers to use the paid identifier.
MODEL_SCORES: Dict[str, float] = {
    "stealth/space-bunny-alpha": 98.0,
    "upstage/solar-pro4:free": 94.0,
    "poolside/laguna-s-2.1:free": 91.0,
    "stepfun/step-3.7-flash:free": 88.0,
    "inclusionai/ling-3.0-flash-fin:free": 84.0,
    "poolside/laguna-xs-2.1:free": 79.0,
    "inclusionai/ling-3.0-flash-sante:free": 75.0,
}

ALLOWED_MODELS = list(MODEL_SCORES)

EXCLUDED_MODELS = {
    "meituan/longcat-2.0:free",
}

# Kept for API compatibility. Every task uses the exact same global ranking.
TASK_FIT_MATRIX: Dict[str, List[str]] = {
    task: list(ALLOWED_MODELS)
    for task in ("reasoning", "coding", "fast", "finance", "general")
}

# ---------------------------------------------------------------------------
# Model Health State (local, in-memory + file persistence)
# ---------------------------------------------------------------------------

HEALTH_DIR = Path("/tmp/.hermes_model_health")
HEALTH_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ModelHealth:
    model: str
    status: str = "AVAILABLE"
    last_success_at: Optional[float] = None
    last_failure_at: Optional[float] = None
    consecutive_failures: int = 0
    recent_latency_ms: float = 0.0
    active_requests: int = 0
    recent_429: int = 0
    cooldown_until: Optional[float] = None
    cooldown_factor: float = 1.0
    last_updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "status": self.status,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "consecutive_failures": self.consecutive_failures,
            "recent_latency_ms": self.recent_latency_ms,
            "active_requests": self.active_requests,
            "recent_429": self.recent_429,
            "cooldown_until": self.cooldown_until,
            "cooldown_factor": self.cooldown_factor,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelHealth":
        return cls(
            model=d.get("model", ""),
            status=d.get("status", "AVAILABLE"),
            last_success_at=d.get("last_success_at"),
            last_failure_at=d.get("last_failure_at"),
            consecutive_failures=d.get("consecutive_failures", 0),
            recent_latency_ms=d.get("recent_latency_ms", 0.0),
            active_requests=d.get("active_requests", 0),
            recent_429=d.get("recent_429", 0),
            cooldown_until=d.get("cooldown_until"),
            cooldown_factor=d.get("cooldown_factor", 1.0),
            last_updated=d.get("last_updated", time.time()),
        )


def _health_path(model: str) -> Path:
    return HEALTH_DIR / f"{model}.json"


def load_health(model: str) -> ModelHealth:
    path = _health_path(model)
    if not path.exists():
        return ModelHealth(model=model)
    try:
        data = json.loads(path.read_text())
        return ModelHealth.from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        return ModelHealth(model=model)


def save_health(health: ModelHealth) -> None:
    health.last_updated = time.time()
    path = _health_path(health.model)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(health.to_dict(), indent=2))
    tmp.replace(path)


def record_success(model: str, latency_ms: float) -> None:
    h = load_health(model)
    h.last_success_at = time.time()
    h.last_failure_at = None
    h.consecutive_failures = 0
    h.recent_latency_ms = latency_ms
    h.active_requests = max(0, h.active_requests - 1)
    h.recent_429 = max(0, h.recent_429 - 1)
    h.cooldown_until = None
    h.cooldown_factor = max(0.5, h.cooldown_factor - 0.1)
    if h.status in ("SATURATED", "COOLDOWN"):
        h.status = "AVAILABLE"
    save_health(h)


def record_failure(model: str, error_type: str, latency_ms: Optional[float] = None) -> None:
    h = load_health(model)
    now = time.time()
    h.last_failure_at = now
    h.consecutive_failures += 1
    h.active_requests = max(0, h.active_requests - 1)
    if latency_ms is not None:
        h.recent_latency_ms = latency_ms

    if error_type == "429":
        h.recent_429 += 1
        h.cooldown_factor = min(3.0, h.cooldown_factor + 0.5)
        h.cooldown_until = now + _compute_cooldown("429", h.recent_429, h.cooldown_factor)
        h.status = "SATURATED"
    elif error_type in ("timeout", "provider_unavailable", "socket_failure", "5xx"):
        h.cooldown_until = now + _compute_cooldown(error_type, h.consecutive_failures, h.cooldown_factor)
        if h.consecutive_failures >= 3:
            h.status = "DEGRADED" if h.status == "AVAILABLE" else h.status
        else:
            h.status = "DEGRADED"
    elif error_type == "empty_response":
        h.status = "DEGRADED"
    else:
        h.status = "DEGRADED"

    save_health(h)


def _compute_cooldown(reason: str, attempt: int, factor: float = 1.0) -> float:
    base = {
        "429": 8.0,
        "timeout": 15.0,
        "provider_unavailable": 30.0,
        "socket_failure": 10.0,
        "5xx": 20.0,
        "empty_response": 5.0,
    }.get(reason, 10.0)
    backoff = min(base * (2 ** min(attempt - 1, 5)), 300.0)
    backoff *= factor
    jitter = random.uniform(0.5, 1.5)
    return round(backoff * jitter, 2)


def is_available(model: str) -> bool:
    h = load_health(model)
    if h.status == "UNAVAILABLE":
        return False
    if h.cooldown_until and time.time() < h.cooldown_until:
        return False
    if h.active_requests > 5:
        return False
    return True


def get_health_summary(model: str) -> dict:
    h = load_health(model)
    return {
        "model": h.model,
        "status": h.status,
        "active_requests": h.active_requests,
        "recent_429": h.recent_429,
        "consecutive_failures": h.consecutive_failures,
        "recent_latency_ms": h.recent_latency_ms,
        "cooldown_remaining": max(0, (h.cooldown_until or 0) - time.time()),
    }


# ---------------------------------------------------------------------------
# Model Score computation
# ---------------------------------------------------------------------------

# The intrinsic score is global. Health can temporarily remove availability or
# apply a runtime penalty, but task labels and context size never reorder it.
QUALITY = MODEL_SCORES


def compute_model_score(model: str, task_type: str = "general") -> float:
    health = load_health(model)
    quality = QUALITY.get(model, 0.0)

    health_penalty = 0.0
    if health.status == "SATURATED":
        health_penalty += 50.0
    elif health.status in ("DEGRADED", "COOLDOWN"):
        health_penalty += 20.0
    health_penalty += min(health.consecutive_failures * 5.0, 30.0)

    return round(max(quality - health_penalty, 0.0), 4)

def select_model(
    task_type: str = "general",
    required_capabilities: Optional[List[str]] = None,
    max_latency_ms: Optional[float] = None,
    hint: Optional[str] = None,
    budget_tokens: Optional[int] = None,
) -> "SelectionResult":
    """
    Select the best model for a given task type, considering health state.
    Returns a SelectionResult with selected_id, success, and confidence_score.
    """
    candidates = ALLOWED_MODELS

    scored = []
    for model in candidates:
        if model not in ALLOWED_MODELS:
            continue
        if not is_available(model):
            continue
        score = compute_model_score(model, task_type)
        scored.append((score, model))

    if not scored:
        for model in ALLOWED_MODELS:
            if is_available(model):
                return SelectionResult(
                    success=True,
                    selected_id=model,
                    confidence_score=compute_model_score(model, task_type),
                    reason="fallback_no_scored_candidates",
                )
        return SelectionResult(
            success=False,
            selected_id=None,
            confidence_score=0.0,
            reason="no_available_models",
        )

    scored.sort(key=lambda x: (x[0], -load_health(x[1]).consecutive_failures), reverse=True)
    selected = scored[0][1]
    return SelectionResult(
        success=True,
        selected_id=selected,
        confidence_score=compute_model_score(selected, task_type),
        reason="scored_best",
    )


def get_fallback_for(model: str, task_type: str = "general") -> Optional[str]:
    chain = ALLOWED_MODELS
    try:
        idx = chain.index(model)
    except ValueError:
        idx = 0
    for candidate in chain[idx + 1:]:
        if candidate in ALLOWED_MODELS and is_available(candidate):
            return candidate
    for candidate in ALLOWED_MODELS:
        if candidate != model and is_available(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# SelectionResult — retour du sélecteur
# ---------------------------------------------------------------------------

@dataclass
class SelectionResult:
    success: bool
    selected_id: Optional[str]
    confidence_score: float
    reason: str = ""
    provider: str = "nous"
    base_url: str = ""
    model_display: str = ""


# ---------------------------------------------------------------------------
# Multi-thread / Multi-goal scheduler integration
# ---------------------------------------------------------------------------

CLAIM_LIMIT = 10


def claim_model(model: str) -> bool:
    h = load_health(model)
    if h.active_requests >= CLAIM_LIMIT:
        return False
    h.active_requests += 1
    save_health(h)
    return True


def release_model(model: str) -> None:
    h = load_health(model)
    h.active_requests = max(0, h.active_requests - 1)
    save_health(h)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def demo() -> None:
    print("=" * 70)
    print("  MODEL ROUTER — DÉMO")
    print("=" * 70)

    print("\n--- Modèles autorisés ---")
    for m in ALLOWED_MODELS:
        h = load_health(m)
        print(f"  ✓ {m:40s} status={h.status:12s} active={h.active_requests}")

    print("\n--- Modèles exclus ---")
    for m in sorted(EXCLUDED_MODELS):
        print(f"  ✗ {m}")

    print("\n--- Sélection par type de tâche ---")
    for task in ["reasoning", "coding", "fast", "finance", "general"]:
        result = select_model(task_type=task)
        print(f"  {task:12s} → {result.selected_id or 'NONE':40s} (score={result.confidence_score:.3f})")

    print("\n--- Fallback équivalent ---")
    for primary in ["nemotron-3-ultra-free", "deepseek-v4-flash-free", "nemotron-3.5-lightning-free"]:
        fb = get_fallback_for(primary, "reasoning")
        print(f"  {primary:40s} [reasoning] → {fb or 'NONE'}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    demo()
