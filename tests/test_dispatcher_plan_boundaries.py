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
        self.root = Path(temporary).resolve()
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

        self.assertEqual(result["role_activation"]["error"], "role store unavailable")
        fallback_prompt = result["role_activation"]["prompt"]
        self.assertTrue(fallback_prompt.startswith("repo prompt\n\n"))
        self.assertIn("`cursor/issue-7`", fallback_prompt)
        self.assertEqual(self.activate.call_args.args[1], fallback_prompt)
        self.assertEqual(result["capability_ids"], ["rail", "new-rail"])
        self.assertEqual(result["influenced_by_role_run_ids"], ["prior"])
        self.assertEqual(result["rejected_role_run_ids"], ["prior-rejected"])
        self.command.assert_called_once_with(
            "cursor",
            "persona\n\n" + fallback_prompt,
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
        self.assertEqual(tagged_assignment["rejected_role_run_ids"], ["prior-rejected", "rejected"])

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
        self.activate.assert_called_once()
        self.assertEqual(self.activate.call_args.args[0], self.assignment)
        supplied_prompt = self.activate.call_args.args[1]
        self.assertTrue(supplied_prompt.startswith("repo prompt\n\n"))
        self.assertIn("`cursor/issue-7`", supplied_prompt)
        self.assertEqual(self.activate.call_args.kwargs, {"cwd": str(self.cwd), "dry_run": True})

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

    def test_review_loop_handoff_is_appended_before_role_activation(self):
        self.assignment["review_loop"] = True
        self.assignment["review_run_id"] = "review-root"
        result = self.plan()
        supplied = self.activate.call_args.args[1]
        self.assertIn("REVIEW-LOOP HANDOFF", supplied)
        self.assertIn("PR_URL=https://github.com/<owner>/<repo>/pull/<number>", supplied)
        self.assertTrue(result["review_loop"])
        self.assertEqual(result["review_run_id"], "review-root")

    def test_invalid_branch_prefix_returns_a_skip_error_without_provisioning(self):
        self.enterContext(
            patch.object(dispatcher, "_dispatch_branch", side_effect=ValueError("bad prefix"))
        )
        result = self.plan()
        self.assertEqual(
            result,
            {"error": "bad prefix", "target": "fixture/repo#7", "agent": "cursor"},
        )
        self.worktree_path.assert_not_called()
        self.activate.assert_not_called()
        self.command.assert_not_called()

    def test_active_provision_failure_skips_without_spawning(self):
        self.enterContext(
            patch.object(
                dispatcher.provision,
                "provision",
                side_effect=RuntimeError("clone denied"),
            )
        )
        result = dispatcher.plan_dispatch(self.assignment, dry_run=False)
        self.assertEqual(
            result,
            {
                "error": "provision failed: clone denied",
                "target": "fixture/repo#7",
                "agent": "cursor",
            },
        )
        self.command.assert_not_called()
        self.external.assert_not_called()

    def test_refused_workspace_is_reported_like_a_provision_failure(self):
        self.enterContext(
            patch.object(
                dispatcher.adapters,
                "broad_workspace_reason",
                return_value="workspace is home directory",
            )
        )
        result = self.plan()
        self.assertEqual(
            result,
            {
                "error": "provisioned workspace refused: workspace is home directory",
                "target": "fixture/repo#7",
                "agent": "cursor",
            },
        )
        self.activate.assert_not_called()
        self.command.assert_not_called()

    def test_selected_capability_marks_dry_run_fallback_before_command_build(self):
        self.assignment["selected_capability_id"] = "compiled-rail"
        result = self.plan()
        tagged = self.tags.call_args.args[0]
        self.assertEqual(tagged["capability_fallback"], "baseline")
        self.assertEqual(
            tagged["capability_dispatch_result"],
            {"invoked": False, "accepted": False, "reason": "dry_run"},
        )
        self.assertEqual(result["capability_ids"], ["rail", "new-rail"])

    def test_missing_prompt_uses_the_task_template_path(self):
        del self.assignment["prompt"]
        built = self.enterContext(
            patch.object(dispatcher, "build_prompt", return_value="templated brief")
        )
        self.plan()
        built.assert_called_once_with("implement", "fixture/repo#7", lane="opener")
        self.assertIn("templated brief", self.activate.call_args.args[1])

    def test_closer_lane_skips_branch_policy_and_still_plans(self):
        self.assignment["lane"] = "closer"
        branch = self.enterContext(patch.object(dispatcher, "_dispatch_branch"))
        result = self.plan()
        branch.assert_not_called()
        self.assertIsNone(result["branch"])
        self.assertNotIn("BRANCH POLICY", self.activate.call_args.args[1])
        self.assertEqual(result["lane"], "closer")


class NetHygieneBoundaries(unittest.TestCase):
    """Proxy scrubbing must stay executable, not only mocked away by plan tests."""

    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory(prefix="dispatcher-net-"))
        self.root = Path(temporary).resolve()
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ORCH_STATE_DIR": str(self.root / "state"),
                    "ORCH_LOCAL_RUNTIME": str(self.root / "runtime"),
                },
                clear=False,
            )
        )
        for name in dispatcher._PROXY_VARS + (
            "ORCH_KEEP_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS",
            "NODE_OPTIONS",
        ):
            os.environ.pop(name, None)

    def test_net_hygiene_prelude_unsets_the_proxy_family_unless_kept(self):
        prelude = dispatcher._net_hygiene_prelude()
        self.assertTrue(prelude.startswith("unset "))
        self.assertIn("HTTPS_PROXY", prelude)
        self.assertTrue(prelude.endswith("; "))
        os.environ["ORCH_KEEP_PROXY"] = "1"
        self.assertEqual(dispatcher._net_hygiene_prelude(), "")

    def test_suspicious_net_env_masks_proxy_userinfo_and_lists_present_overrides(self):
        os.environ["HTTPS_PROXY"] = "http://user:secret@proxy.example:8080"
        os.environ["NODE_OPTIONS"] = "--max-old-space-size=2048"
        rows = dispatcher._suspicious_net_env()
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].startswith("HTTPS_PROXY="))
        self.assertNotIn("secret", rows[0])
        self.assertIn("[REDACTED]", rows[0])
        self.assertEqual(rows[1], "NODE_OPTIONS=--max-old-space-size=2048")
