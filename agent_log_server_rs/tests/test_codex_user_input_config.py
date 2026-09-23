import unittest

from extensions.codex_ext.mcp_contract import build_codex_thread_config
from extensions.codex_ext.devins_contract import effective_developer_instructions


class CodexUserInputConfigTests(unittest.TestCase):
    def test_default_is_enabled_without_te2(self) -> None:
        self.assertEqual(
            build_codex_thread_config(None, te2_enabled=False, base_url=None),
            {
                "features": {"default_mode_request_user_input": True},
                "include_collaboration_mode_instructions": False,
                "suppress_unstable_features_warning": True,
            },
        )

    def test_explicit_disable_and_unrelated_features_survive(self) -> None:
        config = {"features": {"default_mode_request_user_input": False, "other": True}}
        result = build_codex_thread_config(config, te2_enabled=False, base_url=None)
        self.assertEqual(
            result,
            {
                **config,
                "include_collaboration_mode_instructions": False,
                "suppress_unstable_features_warning": True,
            },
        )
        self.assertIsNot(result, config)

    def test_does_not_mutate_existing_features(self) -> None:
        config = {"features": {"other": True}}
        result = build_codex_thread_config(config, te2_enabled=False, base_url=None)
        self.assertEqual(config, {"features": {"other": True}})
        self.assertEqual(
            result,
            {
                "features": {"other": True, "default_mode_request_user_input": True},
                "include_collaboration_mode_instructions": False,
                "suppress_unstable_features_warning": True,
            },
        )

    def test_explicit_unstable_feature_warning_preference_survives(self) -> None:
        config = {"suppress_unstable_features_warning": False}
        result = build_codex_thread_config(config, te2_enabled=False, base_url=None)
        assert result is not None
        self.assertIs(result["suppress_unstable_features_warning"], False)

    def test_explicit_upstream_instructions_and_permissions_are_preserved(self) -> None:
        config = {"include_collaboration_mode_instructions": True, "approval_policy": "never"}
        result = build_codex_thread_config(config, te2_enabled=False, base_url=None)
        assert result is not None
        self.assertIs(result["include_collaboration_mode_instructions"], True)
        self.assertEqual(result["approval_policy"], "never")
        self.assertEqual(effective_developer_instructions({
            "config": config, "developer_instructions": "Custom",
        }), "Custom")

    def test_default_guidance_preserves_effective_context(self) -> None:
        instructions = effective_developer_instructions({
            "__als_devins_context__": {"effective": "Repo and user instructions"},
            "developer_instructions": "Fallback",
        })
        assert instructions is not None
        self.assertTrue(instructions.startswith("Repo and user instructions\n"))
        self.assertNotIn("Fallback", instructions)
        self.assertIn("Current collaboration mode: Default", instructions)
        self.assertIn("workflow approval gates", instructions)
        self.assertIn("not approval", instructions)

    def test_plan_guidance_is_non_mutating(self) -> None:
        instructions = effective_developer_instructions({"mode": "plan"})
        assert instructions is not None
        self.assertIn("Current collaboration mode: Plan", instructions)
        self.assertIn("Do not implement changes", instructions)
        self.assertIn("<proposed_plan>", instructions)
        self.assertNotIn("Current collaboration mode: Default", instructions)
