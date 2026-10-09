"""Canonical Nous model catalogue and quality-ranked fallback chain."""

from __future__ import annotations

from dataclasses import dataclass

PRIMARY_MODEL = "meituan/longcat-2.5-preview:free"


@dataclass(frozen=True)
class NousModel:
    model: str
    score: float
    context_tokens: int
    enabled: bool = True
    disabled_reason: str = ""


# Zero-price entries returned by Nous /v1/models on 2026-10-09. Verified live.
# The tuple is ranked by global model quality, never by task or context.
NOUS_FREE_MODELS: tuple[NousModel, ...] = (
    NousModel("meituan/longcat-2.5-preview:free", 98.0, 1_048_576),
    NousModel("stepfun/step-5-preview:free", 94.0, 1_000_000),
    NousModel("upstage/solar-mini4:free", 91.0, 524_288),
    NousModel("inclusionai/ling-3.1-flash", 88.0, 262_144),
    NousModel("poolside/laguna-s-2.1:free", 84.0, 262_144),
    NousModel("stepfun/step-3.7-flash:free", 82.0, 262_144),
)

FREE_FALLBACKS = tuple(model for model in NOUS_FREE_MODELS if model.enabled)


def fallback_candidates(provider: str = "nous") -> list["ModelCandidate"]:
    """Build router candidates lazily to avoid an import cycle."""
    from .routing import ModelCandidate

    return [
        ModelCandidate(provider, item.model, context_tokens=item.context_tokens,
                       score=item.score)
        for item in FREE_FALLBACKS
    ]
