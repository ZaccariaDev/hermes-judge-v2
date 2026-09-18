"""Goal lifecycle orchestration independent from any chat transport."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from .context import ContextBuilder, ContextPacket
from .judge import JudgeDecision, JudgePolicy, Verdict
from .state import DualStateStore, GoalContract, GoalState


@dataclass
class RuntimeDecision:
    status: str
    should_continue: bool
    verdict: str
    reason: str
    next_required_outcome: str = ""
    continuation: ContextPacket | None = None
    notify_owner: bool = False
    reason_code: str = ""
    failed_requirements: list[str] | None = None
    evidence_against: list[str] | None = None


class GoalRuntime:
    def __init__(self, store: DualStateStore, policy: JudgePolicy | None = None):
        self.store = store
        self.policy = policy or JudgePolicy()
        self.context_builder = ContextBuilder()

    def create_goal(
        self,
        *,
        session_id: str,
        goal: str,
        workspace_root: str,
        contract: GoalContract | None = None,
        discord: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        success_criteria: list[dict[str, Any]] | None = None,
    ) -> GoalState:
        state = GoalState.create(
            session_id=session_id, initial_goal=goal,
            workspace_root=workspace_root, contract=contract,
            discord=discord, attachments=attachments,
        )
        state.success_criteria = list(success_criteria or [])
        self.store.save(state)
        return state

    def load(self, session_id: str, workspace_root: str) -> GoalState | None:
        return self.store.load(session_id, workspace_root)

    def evaluate_turn(
        self,
        state: GoalState,
        *,
        worker_response: str,
        new_evidence: list[dict[str, Any]] | None = None,
        proposed_judgment: JudgeDecision | None = None,
        message_delta: list[dict[str, Any]] | None = None,
        pinned_checkpoint: dict[str, Any] | None = None,
    ) -> RuntimeDecision:
        for item in new_evidence or []:
            evidence = state.add_evidence(
                item["kind"], item.get("description", ""),
                item.get("data"), verified=bool(item.get("verified")),
            )
            for criterion_id in item.get("criterion_ids", []) or []:
                state.verify_criterion(str(criterion_id), evidence["id"])
        decision = self.policy.evaluate(state, worker_response, proposed_judgment)
        state.decisions.append(decision.to_dict())
        state.status = self._status_for(decision.verdict)
        state.touch("turn_judged", verdict=decision.verdict.value)
        continuation = None
        if state.status == "active":
            continuation = self.context_builder.build(
                state, pinned_checkpoint=pinned_checkpoint,
                messages_after_checkpoint=message_delta or [],
            )
        self.store.save(state)
        return RuntimeDecision(
            status=state.status,
            should_continue=state.status == "active",
            verdict=decision.verdict.value,
            reason=decision.summary,
            next_required_outcome=decision.next_required_outcome,
            continuation=continuation,
            notify_owner=decision.notify_owner,
            reason_code=decision.reason_code,
            failed_requirements=list(decision.failed_requirements),
            evidence_against=list(decision.evidence_against),
        )

    @staticmethod
    def _status_for(verdict: Verdict) -> str:
        if verdict == Verdict.COMPLETE:
            return "done"
        if verdict in {Verdict.WAIT_HUMAN, Verdict.BLOCKED_EXTERNAL, Verdict.INSIGHT}:
            return "waiting_human" if verdict == Verdict.WAIT_HUMAN else (
                "blocked_external" if verdict == Verdict.BLOCKED_EXTERNAL else "active"
            )
        return "active"

    def heartbeat(
        self,
        state: GoalState,
        *,
        worker_alive: bool,
        now: float | None = None,
        lease_ttl: float = 120,
    ) -> str | None:
        now = time.time() if now is None else now
        if state.status not in {"active", "retrying"} or worker_alive:
            return None
        if state.lease and state.lease.get("expires_at", 0) > now:
            return None
        worker_id = "heartbeat-" + str(uuid.uuid4())
        if not state.acquire_lease(worker_id, lease_ttl, now):
            return None
        state.active_worker = worker_id
        state.heartbeat = {
            "last_at": now, "next_at": now + lease_ttl / 2,
            "worker_id": worker_id,
        }
        state.touch("heartbeat_restart", worker_id=worker_id)
        self.store.save(state)
        return worker_id

    def confirm_model_switch(
        self, state: GoalState, packet: ContextPacket, provider: str, model: str
    ) -> bool:
        if not packet.verify():
            return False
        if packet.payload.get("state_digest") != state.digest:
            return False
        state.active_provider = provider
        state.active_model = model
        state.generation += 1
        state.touch("model_switched", provider=provider, model=model)
        self.store.save(state)
        return True
