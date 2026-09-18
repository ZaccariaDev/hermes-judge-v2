from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from hermes_judge_v2.context import ContextBuilder, ContextDivergence
from hermes_judge_v2.judge import (
    JudgeDecision, JudgePolicy, Verdict, build_judge_prompt,
)
from hermes_judge_v2.platform import PlatformCoordinator
from hermes_judge_v2.routing import ModelCandidate, ModelRouter, StressRegistry
from hermes_judge_v2.integration import GatewayBridge
from hermes_judge_v2.runtime import GoalRuntime
from hermes_judge_v2.state import (
    DualStateStore, GoalContract, GoalState, MemorySessionStore,
    WorkspaceGuard, WorkspaceViolation,
)


class ProductCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = MemorySessionStore()
        self.store = DualStateStore(self.db)
        self.runtime = GoalRuntime(self.store)
        self.state = self.runtime.create_goal(
            session_id="s1", goal="Build the product",
            workspace_root=self.tmp.name,
            contract=GoalContract(outcome="production working product", verification="tests pass"),
            discord={
                "message_id": "m0", "channel_id": "c1", "thread_id": "c1",
                "guild_id": "g1", "owner_id": "u1",
            },
            success_criteria=[
                {"id": "tests", "description": "all tests pass", "status": "pending"}
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_01_initial_goal_roundtrip_is_immutable_source(self):
        loaded = self.runtime.load("s1", self.tmp.name)
        self.assertEqual(loaded.initial_goal, "Build the product")
        self.assertEqual(loaded.initial_message_id, "m0")

    def test_02_mirror_digest_rejects_corruption(self):
        path = self.store.mirror_path(self.tmp.name)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["initial_goal"] = "forged"
        path.write_text(json.dumps(data), encoding="utf-8")
        loaded = self.runtime.load("s1", self.tmp.name)
        self.assertEqual(loaded.initial_goal, "Build the product")
        self.assertTrue(any(e["event"] == "DIVERGENCE_SESSIONDB_MIRROR" for e in loaded.events))

    def test_03_sessiondb_wins_divergence(self):
        path = self.store.mirror_path(self.tmp.name)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "done"
        data["digest"] = ""
        from hermes_judge_v2.state import digest_dict
        data["digest"] = digest_dict(data)
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.runtime.load("s1", self.tmp.name).status, "active")

    def test_04_mirror_fallback_when_db_absent(self):
        self.db.values.clear()
        loaded = self.runtime.load("s1", self.tmp.name)
        self.assertEqual(loaded.goal_id, self.state.goal_id)
        self.assertTrue(any(e["event"] == "FALLBACK_TO_MIRROR" for e in loaded.events))

    def test_05_workspace_escape_blocked(self):
        guard = WorkspaceGuard(self.tmp.name)
        with self.assertRaises(WorkspaceViolation):
            guard.resolve(Path(self.tmp.name).parent / "outside.txt")

    def test_06_file_mutation_footer_forces_repair(self):
        result = JudgePolicy().evaluate(
            self.state,
            "File-mutation verifier: 1 file(s) were NOT modified this turn",
        )
        self.assertEqual(result.reason_code, "file_mutation_failed")
        self.assertEqual(result.verdict, Verdict.REPAIR)

    def test_07_recoverable_error_never_waits(self):
        proposed = JudgeDecision(verdict=Verdict.WAIT_HUMAN)
        result = JudgePolicy().evaluate(self.state, "ImportError: missing module", proposed)
        self.assertEqual(result.verdict, Verdict.REPAIR)

    def test_08_false_complete_rejected(self):
        result = JudgePolicy().evaluate(
            self.state, "done", JudgeDecision(verdict=Verdict.COMPLETE)
        )
        self.assertEqual(result.verdict, Verdict.REPAIR)
        self.assertIn("PRODUCTION_VALIDATED deployment", result.failed_requirements)

    def test_09_complete_requires_all_proofs(self):
        self.state.success_criteria[0]["status"] = "verified"
        self.state.deployment_status = "PRODUCTION_VALIDATED"
        self.state.add_evidence("test_result", "suite green", {"exit_code": 0}, verified=True)
        result = JudgePolicy().evaluate(
            self.state, "done", JudgeDecision(verdict=Verdict.COMPLETE)
        )
        self.assertEqual(result.verdict, Verdict.COMPLETE)

    def test_10_failed_tests_contradict_green_claim(self):
        self.state.add_evidence("test_result", "suite", {"exit_code": 1}, verified=True)
        result = JudgePolicy().evaluate(
            self.state, "All tests pass", JudgeDecision(verdict=Verdict.COMPLETE)
        )
        self.assertEqual(result.verdict, Verdict.REPAIR)
        self.assertTrue(result.contradictions)

    def test_11_missing_attachment_cannot_complete(self):
        self.state.add_evidence("file", "attachment", {"exists": False}, verified=True)
        result = JudgePolicy().evaluate(
            self.state, "The file was created", JudgeDecision(verdict=Verdict.COMPLETE)
        )
        self.assertEqual(result.verdict, Verdict.REPAIR)

    def test_12_repeated_action_changes_strategy(self):
        for _ in range(3):
            self.state.record_attempt("same", "tool:x", "failure")
        result = JudgePolicy().evaluate(self.state, "still working")
        self.assertEqual(result.verdict, Verdict.RETRY_DIFFERENT_STRATEGY)
        self.assertIn("tool:x", result.forbidden_repetitions)

    def test_13_external_block_requires_two_strategies(self):
        result = JudgePolicy().evaluate(
            self.state, "blocked", JudgeDecision(verdict=Verdict.BLOCKED_EXTERNAL)
        )
        self.assertEqual(result.verdict, Verdict.RETRY_DIFFERENT_STRATEGY)
        self.state.add_evidence("external_block", "API says no", {"status": 403}, verified=True)
        self.state.record_attempt("a", "a", "failure")
        self.state.record_attempt("b", "b", "failure")
        result = JudgePolicy().evaluate(
            self.state, "blocked", JudgeDecision(verdict=Verdict.BLOCKED_EXTERNAL)
        )
        self.assertEqual(result.verdict, Verdict.BLOCKED_EXTERNAL)

    def test_14_context_uses_origin_checkpoint_and_delta(self):
        pin = {
            "goal_id": self.state.goal_id, "goal_version": 1,
            "workspace_root": self.tmp.name, "revision": self.state.revision,
        }
        self.state.add_correction("Do not remove tests", "m2")
        packet = ContextBuilder().build(
            self.state, pinned_checkpoint=pin,
            messages_after_checkpoint=[
                {"id": "m2", "channel_id": "c1", "author_id": "u1", "content": "correction"},
                {"id": "evil", "channel_id": "other", "content": "contamination"},
            ],
        )
        self.assertTrue(packet.verify())
        self.assertEqual(packet.payload["initial_goal_immutable"], "Build the product")
        self.assertEqual(len(packet.payload["message_delta"]), 1)

    def test_15_checkpoint_divergence_detected(self):
        with self.assertRaises(ContextDivergence):
            ContextBuilder().build(
                self.state,
                pinned_checkpoint={
                    "goal_id": "other", "goal_version": 1,
                    "workspace_root": self.tmp.name, "revision": 1,
                },
                messages_after_checkpoint=[],
            )

    def test_16_missing_pin_falls_back_to_state(self):
        packet = ContextBuilder().build(
            self.state, pinned_checkpoint=None, messages_after_checkpoint=[]
        )
        self.assertTrue(packet.verify())
        self.assertEqual(packet.payload["goal_id"], self.state.goal_id)

    def test_17_switch_only_at_boundary_under_high_stress(self):
        registry = StressRegistry()
        self.state.active_provider, self.state.active_model = "p", "current"
        registry.get("p", "current", "c1").stress = 80
        router = ModelRouter([
            ModelCandidate("p2", "better", frozenset({"coding"}), 100000)
        ], registry)
        blocked = router.plan_switch(
            self.state, profile="coding", subtask_boundary=False,
            critical_failure=False, next_action="implement",
        )
        self.assertFalse(blocked.should_switch)
        planned = router.plan_switch(
            self.state, profile="coding", subtask_boundary=True,
            critical_failure=False, next_action="implement",
        )
        self.assertTrue(planned.should_switch)
        self.assertTrue(planned.compaction.verify())

    def test_18_compaction_required_for_switch(self):
        packet = ContextBuilder().compaction_for_switch(
            self.state, next_action="continue", target_model="next"
        )
        packet.payload["next_action"] = "restart everything"
        self.assertFalse(self.runtime.confirm_model_switch(self.state, packet, "p", "next"))

    def test_19_heartbeat_starts_exactly_one_worker(self):
        first = self.runtime.heartbeat(
            self.state, worker_alive=False, now=100, lease_ttl=60
        )
        second = self.runtime.heartbeat(
            self.state, worker_alive=False, now=101, lease_ttl=60
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_20_two_goals_are_isolated(self):
        other_dir = tempfile.TemporaryDirectory()
        try:
            other = self.runtime.create_goal(
                session_id="s2", goal="Other project", workspace_root=other_dir.name
            )
            self.state.add_correction("only s1")
            self.store.save(self.state)
            loaded = self.runtime.load("s2", other_dir.name)
            self.assertEqual(loaded.goal_id, other.goal_id)
            self.assertFalse(loaded.user_corrections)
        finally:
            other_dir.cleanup()

    def test_21_generic_goal_does_not_require_production(self):
        state = GoalState.create(
            session_id="generic", initial_goal="Write a note",
            workspace_root=self.tmp.name,
            contract=GoalContract(outcome="note", verification="file exists"),
        )
        state.success_criteria = [{"id": "f", "description": "file", "status": "verified"}]
        state.add_evidence("file", "note exists", {"exists": True}, verified=True)
        result = JudgePolicy().evaluate(
            state, "done", JudgeDecision(verdict=Verdict.COMPLETE)
        )
        self.assertEqual(result.verdict, Verdict.COMPLETE)

    def test_22_judge_prompt_is_a_separate_two_message_context(self):
        prompt = build_judge_prompt(self.state, "worker result", self.state.evidence)
        self.assertEqual([m["role"] for m in prompt], ["system", "user"])
        self.assertIn("read-only Hermes Judge V2", prompt[0]["content"])
        self.assertNotIn("worker result", prompt[0]["content"])

    def test_23_model_stress_persists_in_goal_state(self):
        self.state.active_provider, self.state.active_model = "p", "m"
        router = ModelRouter([])
        router.record(self.state, "429", signature="quota")
        key = "p|m|c1"
        self.assertGreaterEqual(self.state.model_health[key]["stress"], 35)
        restored = ModelRouter([])
        restored.restore(self.state)
        self.assertEqual(restored.registry.get("p", "m", "c1").stress,
                         self.state.model_health[key]["stress"])

    def test_24_gateway_bridge_disabled_is_noop(self):
        bridge = GatewayBridge(self.db, {"enabled": False})
        self.assertIsNone(bridge.capture_goal(
            session_id="off", goal="off", workspace_root=self.tmp.name
        ))

    def test_25_gateway_bridge_evaluates_compatible_decision(self):
        bridge = GatewayBridge(self.db, {"enabled": True})
        state = bridge.capture_goal(
            session_id="bridge", goal="Bridge", workspace_root=self.tmp.name,
            success_criteria=[{"id": "x", "description": "x", "status": "pending"}],
        )
        decision = bridge.evaluate_turn(
            session_id="bridge", workspace_root=self.tmp.name,
            worker_response="work continues",
            judge_payload={"verdict": "CONTINUE", "summary": "continue",
                           "next_required_outcome": "finish x"},
        )
        self.assertTrue(decision["should_continue"])
        self.assertEqual(decision["verdict"], "CONTINUE")
        self.assertIn("finish x", decision["continuation_prompt"])

    def test_26_verified_evidence_can_verify_named_criterion(self):
        result = self.runtime.evaluate_turn(
            self.state, worker_response="tests executed",
            new_evidence=[{
                "kind": "test_result", "description": "suite green",
                "data": {"exit_code": 0}, "verified": True,
                "criterion_ids": ["tests"],
            }],
            proposed_judgment=JudgeDecision(
                verdict=Verdict.CONTINUE, summary="continue"
            ),
        )
        criterion = self.state.success_criteria[0]
        self.assertEqual(criterion["status"], "verified")
        self.assertEqual(len(criterion["evidence_ids"]), 1)
        self.assertTrue(result.should_continue)

    def test_27_unverified_evidence_cannot_verify_criterion(self):
        with self.assertRaises(ValueError):
            self.runtime.evaluate_turn(
                self.state, worker_response="claim only",
                new_evidence=[{
                    "kind": "claim", "description": "unverified",
                    "data": {}, "verified": False,
                    "criterion_ids": ["tests"],
                }],
            )


class FakeDiscord:
    def __init__(self):
        self.messages = {}
        self.pins = set()
        self.renames = []
        self.seq = 0

    async def create_message(self, channel_id, content):
        self.seq += 1
        mid = str(self.seq)
        self.messages[(channel_id, mid)] = {"id": mid, "content": content}
        return {"id": mid}

    async def update_message(self, channel_id, message_id, content):
        self.messages[(channel_id, message_id)] = {"id": message_id, "content": content}
        return {"id": message_id}

    async def get_message(self, channel_id, message_id):
        return self.messages.get((channel_id, message_id))

    async def get_pins(self, channel_id):
        return [self.messages[(channel_id, mid)] for cid, mid in self.pins if cid == channel_id]

    async def pin_message(self, channel_id, message_id):
        self.pins.add((channel_id, message_id))

    async def rename_thread(self, channel_id, name):
        self.renames.append((channel_id, name))

    async def open_dm(self, recipient_id):
        return {"id": "dm-" + recipient_id}

    async def send_message(self, channel_id, content, attachments=None):
        return await self.create_message(channel_id, content)

    async def get_thread_history(self, channel_id, **page):
        return []


class DiscordError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Discord {code}")


class PlatformCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = GoalState.create(
            session_id="p", initial_goal="Platform", workspace_root=self.tmp.name
        )
        self.adapter = FakeDiscord()
        self.platform = PlatformCoordinator(self.adapter, rename_cooldown=100)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_28_checkpoint_create_pin_and_readback(self):
        result = await self.platform.upsert_checkpoint(self.state, "c", "next")
        self.assertTrue(result.success)
        self.assertIn(("c", result.message_id), self.adapter.pins)

    async def test_29_checkpoint_updates_single_message(self):
        first = await self.platform.upsert_checkpoint(self.state, "c", "first")
        second = await self.platform.upsert_checkpoint(self.state, "c", "second")
        self.assertEqual(first.message_id, second.message_id)
        self.assertEqual(self.adapter.seq, 1)

    async def test_30_rename_is_idempotent_and_cooled_down(self):
        one = await self.platform.rename_if_needed(
            self.state, "c", "Project — phase", "phase", now=200
        )
        same = await self.platform.rename_if_needed(
            self.state, "c", "Project — phase", "phase", now=201
        )
        soon = await self.platform.rename_if_needed(
            self.state, "c", "Project — next", "phase", now=202
        )
        self.assertTrue(one.success)
        self.assertEqual(same.code, "rename_idempotent")
        self.assertEqual(soon.code, "rename_cooldown")
        self.assertEqual(len(self.adapter.renames), 1)

    async def test_31_dm_requires_authorization_then_verifies(self):
        denied = await self.platform.send_dm(self.state, "u", "hello")
        self.assertEqual(denied.code, "dm_not_authorized")
        self.state.dm_authorized_recipients.append("u")
        sent = await self.platform.send_dm(self.state, "u", "hello")
        self.assertEqual(sent.code, "dm_verified")

    async def test_32_dm_50007_is_classified(self):
        self.state.dm_authorized_recipients.append("u")
        async def fail(_):
            raise DiscordError(50007)
        self.adapter.open_dm = fail
        result = await self.platform.send_dm(self.state, "u", "hello")
        self.assertEqual(result.code, "50007")
        self.assertFalse(result.retryable)

    async def test_33_dm_50278_is_classified(self):
        self.state.dm_authorized_recipients.append("u")
        async def fail(_):
            raise DiscordError(50278)
        self.adapter.open_dm = fail
        result = await self.platform.send_dm(self.state, "u", "hello")
        self.assertEqual(result.code, "50278")
        self.assertTrue(result.retryable)

    async def test_34_history_pagination_is_bounded_and_deduplicated(self):
        calls = []
        async def history(channel_id, **page):
            calls.append(page.get("after"))
            if len(calls) == 1:
                return [{"id": "2"}, {"id": "3"}]
            if len(calls) == 2:
                return [{"id": "3"}, {"id": "4"}]
            return []
        self.adapter.get_thread_history = history
        result = await self.platform.history_after("c", "1", page_size=2)
        self.assertEqual([m["id"] for m in result], ["2", "3", "4"])
        self.assertLessEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
