"""Canonical Nous model catalogue and quality-ranked fallback chain."""

from __future__ import annotations

from dataclasses import dataclass

PRIMARY_MODEL = "meituan/longcat-2.0"


@dataclass(frozen=True)
class NousModel:
    model: str
    score: float
    context_tokens: int
    enabled: bool = True
    disabled_reason: str = ""


# Zero-price entries returned by Nous /v1/models on 2026-09-28. This catalogue
# is exhaustive even when live inference proves that one advertised entry is
# stale. The tuple is ranked by global model quality, never by task or context.
NOUS_FREE_MODELS: tuple[NousModel, ...] = (
    NousModel("stealth/space-bunny-alpha", 98.0, 1_000_000),
    NousModel("upstage/solar-pro4:free", 94.0, 524_288),
    NousModel("poolside/laguna-s-2.1:free", 91.0, 262_144),
    NousModel("stepfun/step-3.7-flash:free", 88.0, 262_144),
    NousModel("inclusionai/ling-3.0-flash-fin:free", 84.0, 262_144),
    NousModel("poolside/laguna-xs-2.1:free", 79.0, 262_144),
    NousModel("inclusionai/ling-3.0-flash-sante:free", 75.0, 262_144),
    NousModel(
        "meituan/longcat-2.0:free",
        0.0,
        1_048_756,
        enabled=False,
        disabled_reason=(
            "Nous still advertises zero pricing, but inference returns HTTP 404: "
            "the model is no longer free; use meituan/longcat-2.0"
        ),
    ),
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
