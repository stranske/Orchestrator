"""Planning must preserve lineage even when optional role activation is unavailable."""

import copy
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import dispatcher
import roles


class DispatchPlanBoundaries(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory(prefix="dispatcher-plan-"))
        self.root = Path(temporary)
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ORCH_STATE_DIR": str(self.root / "state"),
                    "ORCH_LOCAL_RUNTIME": str(self.root / "runtime"),
                },
            )
        )
        self.external = Mock(side_effect=AssertionError("unexpected external effect"))
        for module, names in (
            (subprocess, ("run", "Popen")),
            (socket, ("socket", "create_connection")),
            (dispatcher, ("_spawn", "delegate", "delegate_remote", "offload")),
            (dispatcher.provision, ("provision",)),
            (dispatcher.feedback, ("record_run",)),
        ):
            for name in names:
                self.enterContext(patch.object(module, name, self.external))
        self.addCleanup(self.external.assert_not_called)
        self.cwd = self.root / "checkout"
        self.cwd.mkdir()
        self.worktree_path = self.enterContext(
            patch.object(dispatcher.provision, "worktree_path", return_value=self.cwd)
        )
        self.activate = self.enterContext(
            patch.object(roles, "activate_dispatch_roles", return_value={"prompt": "role prompt"})
        )
        self.context = self.enterContext(
            patch.object(dispatcher.repo_knowledge, "append_context", return_value="repo prompt")
        )
        self.profile = self.enterContext(
            patch.object(dispatcher.execution_profiles, "get_profile", return_value=None)
        )
        self.enterContext(patch.object(dispatcher.adapters, "model_identity", return_value="model"))
        self.command = self.enterContext(
            patch.object(dispatcher.adapters, "build_command", return_value=["agent", "task"])
        )
        self.tags = self.enterContext(
            patch.object(dispatcher, "_exercised_capability_ids", return_value=["rail", "new-rail"])
        )
        for name, value in (
            ("_agent_preamble", "persona"),
            ("_path_prefix", "PATH=fixture"),
            ("_auth_prelude", ""),
            ("_agent_runtime_prelude", ""),
            ("_net_hygiene_prelude", ""),
        ):
            self.enterContext(patch.object(dispatcher, name, return_value=value))
        self.assignment = {
            "agent": "cursor",
            "target": "fixture/repo#7",
            "task_type": "implement",
            "prompt": "finite task",
            "capability_ids": ["rail"],
            "influenced_by_role_run_ids": ["prior"],
            "rejected_role_run_ids": ["prior-rejected"],
        }

    def plan(self):
        return dispatcher.plan_dispatch(self.assignment, dry_run=True)

    def test_role_failure_keeps_deterministic_tags_and_original_assignment(self):
        self.activate.side_effect = OSError("role store unavailable")
        original = copy.deepcopy(self.assignment)

        result = self.plan()

        self.assertEqual(
            result["role_activation"],
            {"error": "role store unavailable", "prompt": "repo prompt"},
        )
        self.assertEqual(result["capability_ids"], ["rail", "new-rail"])
        self.assertEqual(result["influenced_by_role_run_ids"], ["prior"])
        self.assertEqual(result["rejected_role_run_ids"], ["prior-rejected"])
        self.command.assert_called_once_with(
            "cursor",
            "persona\n\nrepo prompt",
            None,
            cwd=self.cwd,
            reasoning_effort=None,
            requested_model=None,
            commits_in_worktree=True,
        )
        self.assertEqual(self.assignment, original)

    def test_accepted_and_rejected_lineage_deduplicates_without_mutating_input(self):
        activation = {
            "prompt": "accepted role prompt",
            "accepted_role_run_ids": ["prior", "accepted", "accepted"],
            "rejected_role_run_ids": ["prior-rejected", "rejected", "rejected"],
        }
        self.activate.return_value = activation
        original = copy.deepcopy(self.assignment)

        result = self.plan()

        self.assertEqual(result["influenced_by_role_run_ids"], ["prior", "accepted"])
        self.assertEqual(result["rejected_role_run_ids"], ["prior-rejected", "rejected"])
        self.assertEqual(result["role_activation"], activation)
        self.assertEqual(self.command.call_args.args[1], "persona\n\naccepted role prompt")
        self.assertEqual(self.assignment, original)
        tagged_assignment = self.tags.call_args.args[0]
        self.assertEqual(tagged_assignment["influenced_by_role_run_ids"], ["prior", "accepted"])
        self.assertEqual(
            tagged_assignment["rejected_role_run_ids"], ["prior-rejected", "rejected"]
        )

    def test_profile_overrides_and_commit_boundary_reach_command_builder(self):
        profile = {"profile_id": "fixture-profile", "requested_model": "profile-model"}
        self.profile.return_value = profile
        self.assignment.update(
            selected_profile_id="fixture-profile",
            requested_model="explicit-model",
            reasoning_effort="high",
            permission_mode="workspace-write",
            transport="local",
        )

        result = self.plan()

        self.profile.assert_called_once_with("fixture-profile")
        self.command.assert_called_once_with(
            "cursor",
            "persona\n\nrole prompt",
            None,
            cwd=self.cwd,
            profile=profile,
            transport="local",
            permission_mode="workspace-write",
            reasoning_effort="high",
            requested_model="explicit-model",
            commits_in_worktree=True,
        )
        self.assertEqual(result["selected_profile_id"], "fixture-profile")
        self.assertEqual(result["requested_model"], "profile-model")

    def test_unbuildable_command_skips_instead_of_returning_a_spawnable_plan(self):
        self.command.side_effect = ValueError("unknown agent")
        self.assertIsNone(self.plan())
        self.command.assert_called_once()

    def test_unexpected_command_failure_is_not_disguised_as_an_unknown_agent(self):
        self.command.side_effect = OSError("adapter unavailable")
        with self.assertRaisesRegex(OSError, "adapter unavailable"):
            self.plan()

    def test_dry_run_predicts_missing_worktree_without_provisioning(self):
        result = self.plan()
        self.worktree_path.assert_called_once_with("fixture/repo#7", "opener")
        self.assertTrue(result["worktree_missing"])
        self.assertEqual(result["cwd"], str(self.cwd))
        self.activate.assert_called_once_with(
            self.assignment, "repo prompt", cwd=str(self.cwd), dry_run=True
        )

    def test_existing_git_file_is_a_present_worktree_in_dry_run(self):
        (self.cwd / ".git").write_text("gitdir: fixture\n")
        self.assertFalse(self.plan()["worktree_missing"])

    def test_gemini_receives_the_resolved_workspace_after_role_activation(self):
        self.assignment["agent"] = "gemini"
        alias = self.root / "checkout-alias"
        alias.symlink_to(self.cwd, target_is_directory=True)
        self.worktree_path.return_value = alias
        workspace_prompt = self.enterContext(
            patch.object(dispatcher, "_gemini_workspace_prompt", return_value="workspace prompt")
        )

        result = self.plan()

        workspace_prompt.assert_called_once_with("persona\n\nrole prompt", self.cwd)
        self.assertEqual(self.command.call_args.args, ("gemini", "workspace prompt", None))
        self.assertEqual(self.command.call_args.kwargs["cwd"], self.cwd)
        self.assertEqual(result["cwd"], str(self.cwd))
