"""Evidence-first judge policy and strict LLM output validation."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .state import GoalState

class Verdict(str, Enum):
    CONTINUE = "CONTINUE"
    REPAIR = "REPAIR"
    RETRY_DIFFERENT_STRATEGY = "RETRY_DIFFERENT_STRATEGY"
    WAIT_HUMAN = "WAIT_HUMAN"
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"
    COMPLETE = "COMPLETE"
    INSIGHT = "INSIGHT"


@dataclass
class JudgeDecision:
    verdict: Verdict = Verdict.CONTINUE
    reason_code: str = "in_progress"
    summary: str = ""
    failed_requirements: list[str] = field(default_factory=list)
    evidence_for: list[str] = field(default_factory=list)
    evidence_against: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    next_required_outcome: str = ""
    forbidden_repetitions: list[str] = field(default_factory=list)
    subtask_boundary: bool = False
    suggested_worker_profile: str = "reasoning"
    suggested_thread_title: str | None = None
    notify_owner: bool = False
    confidence: float = 0.5

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, Verdict):
            self.verdict = Verdict(str(self.verdict).upper())
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if self.suggested_worker_profile not in {
            "reasoning", "coding", "research", "fast", "vision"
        }:
            self.suggested_worker_profile = "reasoning"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["verdict"] = self.verdict.value
        return data

    @classmethod
    def from_payload(cls, payload: str | dict[str, Any]) -> "JudgeDecision":
        data = json.loads(payload) if isinstance(payload, str) else dict(payload)
        allowed = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in allowed})


_MUTATION_FAILURE = re.compile(
    r"file[- ]mutation verifier:.*?(?:were|was) not modified", re.I | re.S
)
_RECOVERABLE = re.compile(
    r"(syntax error|importerror|modulenotfounderror|patch .*failed|"
    r"permission denied|file not found|tests? (?:failed|failing)|"
    r"timeout|rate.?limit|http 429|http 5\d\d)",
    re.I,
)


class JudgePolicy:
    """Deterministic safety envelope around an optional model judgment."""

    def evaluate(
        self,
        state: GoalState,
        worker_response: str,
        proposed: JudgeDecision | None = None,
    ) -> JudgeDecision:
        text = worker_response or ""
        # Deterministic receipts from observed tool events (not LLM output) pass through.
        if proposed and proposed.reason_code in {
            'no_tool_execution', 'execution_telemetry_unavailable',
            'content_too_long_fragmented',
        }:
            return proposed
        if _MUTATION_FAILURE.search(text):
            return JudgeDecision(
                verdict=Verdict.REPAIR,
                reason_code="file_mutation_failed",
                summary="The requested mutation was not observed on disk.",
                failed_requirements=["file mutation proof"],
                next_required_outcome="Read the exact target, repair the write, then verify its diff.",
                forbidden_repetitions=[state.last_action_signature] if state.last_action_signature else [],
                suggested_worker_profile="coding",
                confidence=1.0,
            )
        if state.no_progress_count >= 2:
            return JudgeDecision(
                verdict=Verdict.RETRY_DIFFERENT_STRATEGY,
                reason_code="repeated_failure",
                summary="The same action failed repeatedly without new evidence.",
                next_required_outcome="Use a materially different strategy and preserve the failure signature.",
                forbidden_repetitions=[state.last_action_signature],
                suggested_worker_profile="reasoning",
                confidence=1.0,
            )
        if _RECOVERABLE.search(text):
            return JudgeDecision(
                verdict=Verdict.REPAIR,
                reason_code="recoverable_error",
                summary="A recoverable implementation or provider error remains.",
                next_required_outcome="Repair the concrete error and rerun the failed verification.",
                suggested_worker_profile="coding",
                confidence=0.95,
            )

        decision = proposed or JudgeDecision(
            verdict=Verdict.CONTINUE,
            reason_code="in_progress",
            summary="Completion has not been independently proven.",
            next_required_outcome=self._next_unverified(state),
        )

        contradictions = self._contradictions(state, text)
        if contradictions:
            decision.contradictions = sorted(set(decision.contradictions + contradictions))
            if decision.verdict == Verdict.COMPLETE:
                decision.verdict = Verdict.REPAIR
                decision.reason_code = "evidence_contradiction"

        if decision.verdict == Verdict.COMPLETE:
            missing = self._missing_completion_proof(state)
            if missing:
                return JudgeDecision(
                    verdict=Verdict.REPAIR,
                    reason_code="completion_unproven",
                    summary="COMPLETE was rejected because required evidence is missing.",
                    failed_requirements=missing,
                    evidence_against=decision.evidence_against,
                    contradictions=decision.contradictions,
                    next_required_outcome=missing[0],
                    suggested_worker_profile="coding",
                    confidence=1.0,
                )

        elif decision.verdict in {Verdict.BLOCKED_EXTERNAL, Verdict.WAIT_HUMAN}:
            external = [
                e for e in state.evidence
                if e.get("verified") and e.get("kind") in {'external_block', 'human_input_required'}
            ]
            distinct = {a.get("strategy_id") for a in state.attempts if a.get("strategy_id")}
            if decision.verdict == Verdict.WAIT_HUMAN and proposed and proposed.reason_code == 'execution_stalled':
                # 3 tours sans outil = insight, pas un blocage externe
                decision.verdict = Verdict.INSIGHT
                decision.reason_code = 'execution_stalled'
                decision.next_required_outcome = (
                    "Exécuter via terminal ou execute_code la prochaine étape "
                    "vérifiable du dernier message, même partielle."
                )
                decision.notify_owner = False
                decision.summary = str(proposed.summary)
            elif not external or len(distinct) < 2:
                decision.verdict = Verdict.RETRY_DIFFERENT_STRATEGY
                decision.reason_code = "external_block_unproven"
                decision.next_required_outcome = (
                    "Try another reasonable strategy or collect a verified external response."
                )

        return decision

    @staticmethod
    def _next_unverified(state: GoalState) -> str:
        for criterion in state.success_criteria:
            if criterion.get("status") != "verified":
                return str(criterion.get("description") or criterion.get("id") or "next criterion")
        return "Collect independent evidence for the requested outcome."

    @staticmethod
    def _missing_completion_proof(state: GoalState) -> list[str]:
        missing = [
            str(c.get("description") or c.get("id") or "criterion")
            for c in state.success_criteria if c.get("status") != "verified"
        ]
        if not state.success_criteria:
            missing.append("at least one explicit success criterion")
        verified = [e for e in state.evidence if e.get("verified")]
        if not verified:
            missing.append("verified evidence")
        contract_text = " ".join([
            state.contract.outcome, state.contract.verification,
            state.contract.constraints, state.contract.boundaries,
        ]).lower()
        requires_production = any(
            term in contract_text for term in ("production", "deploy", "déploi")
        ) or any(bool(c.get("requires_production")) for c in state.success_criteria)
        if requires_production and state.deployment_status != "PRODUCTION_VALIDATED":
            missing.append("PRODUCTION_VALIDATED deployment")
        return missing

    @staticmethod
    def _contradictions(state: GoalState, response: str) -> list[str]:
        lower = response.lower()
        found: list[str] = []
        for item in state.evidence:
            if not item.get("verified"):
                continue
            data = item.get("data") or {}
            if item.get("kind") == "test_result" and data.get("exit_code", 0) != 0:
                if re.search(r"all tests? (?:pass|green)|tests? réussis", lower):
                    found.append("Worker claims green tests but verified test evidence failed.")
            if item.get("kind") == "file" and data.get("exists") is False:
                if re.search(r"(file|fichier).*(created|exists|créé|présent)", lower):
                    found.append("Worker claims a file exists but verified evidence says it does not.")
        return found


JUDGE_SCHEMA = {
    "verdict": [v.value for v in Verdict],
    "required_fields": [
        "verdict", "reason_code", "summary", "failed_requirements",
        "evidence_for", "evidence_against", "contradictions",
        "next_required_outcome", "forbidden_repetitions", "subtask_boundary",
        "suggested_worker_profile", "suggested_thread_title", "notify_owner",
        "confidence",
    ],
}


def build_judge_prompt(
    state: GoalState, worker_response: str, evidence: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Create an isolated judge conversation; never reuse worker messages."""
    system = (
        "You are the read-only Hermes Judge V2. Treat worker claims as untrusted. "
        "Use only verified evidence. Return one JSON object matching the supplied "
        "schema. Recoverable errors are REPAIR, repeated failures require a new "
        "strategy, and COMPLETE requires every explicit success criterion."
    )
    user = json.dumps({
        "schema": JUDGE_SCHEMA,
        "goal": state.initial_goal,
        "contract": state.contract.__dict__,
        "success_criteria": state.success_criteria,
        "workspace_root": state.workspace_root,
        "phase": state.phase,
        "subtask": state.subtask,
        "worker_response": worker_response,
        "verified_evidence": [e for e in evidence if e.get("verified")],
        "previous_attempts": state.attempts[-20:],
        "forbidden_repetitions": [state.last_action_signature]
        if state.no_progress_count >= 2 else [],
    }, ensure_ascii=False)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
