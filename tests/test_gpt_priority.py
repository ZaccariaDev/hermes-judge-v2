from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from hermes_judge_v2 import integration
from hermes_judge_v2.state import GoalState


def response(content='{"verdict":"CONTINUE"}'):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class GPTPriorityCase(unittest.TestCase):
    def setUp(self):
        self.state = GoalState.create(session_id="priority", initial_goal="test", workspace_root="/tmp")

    def test_gpt_is_explicit_primary_not_auto(self):
        with patch.object(integration, "_runtime_config", return_value={}), patch.object(
            integration, "_call_judge_candidate", return_value=response(), create=True
        ) as call:
            self.assertEqual(integration._call_isolated_judge(self.state, "worker")["verdict"], "CONTINUE")
        self.assertEqual(call.call_args.args[:2], ("openai-codex", "gpt-6.1-sol"))
        self.assertEqual(call.call_count, 1)


    def test_exhausted_quota_unlocks_ranked_nous_fallback(self):
        error = RuntimeError("insufficient_quota: credits exhausted")
        with patch.object(integration, "_runtime_config", return_value={}), patch.object(
            integration, "_call_judge_candidate", side_effect=[error, response()]
        ) as call:
            result = integration._call_isolated_judge(self.state, "worker")
        self.assertEqual(result["verdict"], "CONTINUE")
        self.assertEqual(call.call_args_list[0].args[:2], ("openai-codex", "gpt-6.1-sol"))
        self.assertEqual(call.call_args_list[1].args[:2], ("nous", "meituan/longcat-2.5-preview:free"))
        self.assertEqual(call.call_args_list[0].args[2], call.call_args_list[1].args[2])


    def test_non_quota_errors_never_fallback(self):
        errors = [TimeoutError("timeout"), RuntimeError("HTTP 500"),
                  RuntimeError("429 rate_limit_exceeded: tokens per minute"),
                  RuntimeError("401 unauthorized"), RuntimeError("context_length_exceeded")]
        for error in errors:
            with self.subTest(error=str(error)), patch.object(integration, "_runtime_config", return_value={}), patch.object(
                integration, "_call_judge_candidate", side_effect=error
            ) as call:
                with self.assertRaises(type(error)):
                    integration._call_isolated_judge(self.state, "worker")
                self.assertEqual(call.call_count, 1)

    def test_structured_exhaustion_vs_transient_rate_limit(self):
        for code in ("insufficient_quota", "usage_limit_reached", "credits_exhausted"):
            error = RuntimeError("HTTP 429")
            error.body = {"error": {"type": code}}
            self.assertTrue(integration.quota_exhausted(error))
        error = RuntimeError("credits exhausted in unrelated diagnostic")
        error.body = {"error": {"code": "rate_limit_exceeded"}}
        self.assertFalse(integration.quota_exhausted(error))

    def test_repeated_provider_failures_do_not_rewrite_primary(self):
        import tempfile
        from unittest.mock import Mock
        from hermes_judge_v2.state import MemorySessionStore
        with tempfile.TemporaryDirectory() as workspace:
            bridge = integration.GatewayBridge(MemorySessionStore(), {"enabled": True})
            manager = SimpleNamespace(session_id="priority", _save=Mock(), state=SimpleNamespace(
                goal="test", contract=None, gates=[], status="active"))
            with patch.object(integration, "_bridge", return_value=bridge), patch.object(
                integration, "_runtime_config", return_value={}
            ), patch.object(integration, "_call_isolated_judge", side_effect=TimeoutError("timeout")), patch.object(
                integration, "_refresh_nous_catalogue", return_value=None, create=True
            ) as refresh, patch.object(integration, "_open_incident_if_needed"):
                for _ in range(3):
                    integration.evaluate_goal_manager(manager, "worker", workspace)
                refresh.assert_not_called()


    def test_gpt_stress_cannot_trigger_non_quota_switch(self):
        from hermes_judge_v2.routing import ModelRouter, ModelCandidate
        self.state.active_provider = "openai-codex"
        self.state.active_model = "gpt-6.1-sol"
        router = ModelRouter([ModelCandidate("nous", "fallback", score=99)])
        for _ in range(5):
            router.record(self.state, "timeout")
        plan = router.plan_switch(self.state, profile="general", subtask_boundary=True,
                                  critical_failure=True, next_action="continue")
        self.assertFalse(plan.should_switch)
        router.record(self.state, "quota")
        plan = router.plan_switch(self.state, profile="general", subtask_boundary=True,
                                  critical_failure=True, next_action="continue")
        self.assertTrue(plan.should_switch)


    def test_transport_calls_only_requested_provider(self):
        import sys
        from unittest.mock import Mock
        create = Mock(return_value=response())
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        resolve = Mock(return_value=(client, "gpt-6.1-sol"))
        module = SimpleNamespace(resolve_provider_client=resolve)
        with patch.dict(sys.modules, {"agent.auxiliary_client": module}):
            integration._call_judge_candidate("openai-codex", "gpt-6.1-sol", [])
        resolve.assert_called_once_with("openai-codex", model="gpt-6.1-sol")
        self.assertEqual(create.call_args.kwargs["timeout"], 60)

    def test_gpt_is_retried_on_next_evaluation_after_fallback(self):
        with patch.object(integration, "_runtime_config", return_value={}), patch.object(
            integration, "_call_judge_candidate", side_effect=[
                RuntimeError("usage_limit_reached"), response(), response()
            ]
        ) as call:
            integration._call_isolated_judge(self.state, "first")
            integration._call_isolated_judge(self.state, "next")
        self.assertEqual([c.args[0] for c in call.call_args_list], ["openai-codex", "nous", "openai-codex"])

    def test_invalid_gpt_json_does_not_unlock_fallback(self):
        with patch.object(integration, "_runtime_config", return_value={}), patch.object(
            integration, "_call_judge_candidate", return_value=response("not JSON")
        ) as call:
            self.assertIsNone(integration._call_isolated_judge(self.state, "worker"))
        self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
