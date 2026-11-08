"""Hermetic persistence and failure contracts for issue #1253's ranked targets."""

from __future__ import annotations

import builtins
import io
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import backlog
import capabilities
import capability_ir as ir
import coverage_testgen_trigger as trigger
import dispatcher
import feedback
import paths
import pattern_miner as miner
import redirect_policy as redirect
from completion_event_adapter import CompletionEvent, CompletionIdentity


class RankedCoverageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="issue1253-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.use_patch(
            patch.dict(
                os.environ,
                {
                    "ORCH_STATE_DIR": str(self.root),
                    "ORCH_LOCAL_RUNTIME": str(self.root),
                    "ORCH_FEEDBACK_DB": str(self.root / "feedback.db"),
                    "ORCH_CAPABILITY_HEARTBEATS": "0",
                },
            )
        )
        for module, name, value in (
            (backlog, "HANDOFF", self.root),
            (backlog, "SENTINEL", self.root / "sentinel.json"),
            (backlog, "BACKLOG_JSON", self.root / "backlog.json"),
            (backlog, "RESEARCH_BACKLOG_JSON", self.root / "research.json"),
            (feedback, "DB_PATH", self.root / "feedback.db"),
        ):
            self.use_patch(patch.object(module, name, value))
        self.denied = Mock(side_effect=AssertionError("unexpected process, network, or ledger I/O"))
        self.addCleanup(self.denied.assert_not_called)
        for module, name in (
            (subprocess, "run"),
            (subprocess, "Popen"),
            (subprocess, "call"),
            (subprocess, "check_call"),
            (subprocess, "check_output"),
            (os, "system"),
            (socket, "socket"),
            (socket, "create_connection"),
            (urllib.request, "urlopen"),
            (sqlite3, "connect"),
            (dispatcher, "offload"),
            (feedback, "record_owner_question"),
        ):
            self.use_patch(patch.object(module, name, self.denied))
        self.heartbeat = self.use_patch(patch.object(capabilities, "production_heartbeat"))

    def use_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def candidate(self):
        occurrence = ir.SourceOccurrence(
            event_id="event:durability",
            event_refs=tuple(f"event:{phase}" for phase in miner.PHASES),
            occurred_at=100,
            subject_id="subject:one",
            observation_id="observation:one",
            family_id=None,
            canonical_target="owner/repo#1",
            repository="owner/repo",
            task_type="testgen",
            normalized_spec_hash="sha256:spec",
            base_sha="abc123",
            profile_id="profile:one",
            arm_id="worker",
            attempt_id="attempt:one",
            artifact_refs=("artifact:patch",),
        )
        return ir.CapabilityIR(
            capability_id="capability:one",
            fingerprint="sha256:one",
            semantic_fingerprint="sha256:semantic",
            output_contract_fingerprint="sha256:contract",
            kind_proposal="skill",
            owner_proposal="repo",
            source_occurrences=(occurrence,),
            counterexamples=(),
            independent_subjects=("subject:one",),
            independent_repositories=("owner/repo",),
            selector={},
            graph={"phase_order": list(miner.PHASES)},
            artifact_refs=("artifact:patch",),
            gates={},
            telemetry={"distinct_subject_count": 1},
            lifecycle=ir.Lifecycle(expires_at=200),
        )

    def completion_events(self):
        identity = CompletionIdentity(
            subject_id="subject:one",
            observation_id="observation:one",
            family_id=None,
            attempt_id="attempt:one",
            canonical_target="owner/repo#1",
            repository="owner/repo",
            task_type="testgen",
            normalized_spec_hash="sha256:spec",
            base_sha="abc123",
            profile_id="profile:one",
            arm_id="worker",
            resolved_provider="fixture",
            resolved_model="fixture",
        )
        payloads = {
            "decision": {"workflow_ids": ["workflow:testgen"]},
            "artifact": {
                "artifact_refs": [{"artifact_id": "patch:one", "kind": "patch"}],
                "changed_path_classes": ["tests"],
            },
            "verification": {"verification": {"name": "pytest", "verifier_verdict": "pass"}},
            "outcome": {"result": {"outcome_verdict": "success"}},
            "durability": {"durability": {"status": "durable"}},
        }
        return {
            phase: CompletionEvent(
                event_id=f"event:{phase}",
                run_id="run:one",
                attempt_id="attempt:one",
                event_type="completion",
                phase=phase,
                producer="dispatcher",
                status="accepted" if phase == "artifact" else "complete",
                validation_status="valid",
                payload=payloads.get(phase, {}),
                content_hash="sha256:event",
                redaction_count=0,
                occurred_at=100 + index,
                created_at=100 + index,
                identity=identity,
            )
            for index, phase in enumerate(miner.PHASES)
        }

    def test_miner_normalization_derives_kind_and_owner_from_decision(self):
        for selected, producer, kind, owner in (
            ({"role_ids": ["role:review"]}, "dispatcher", "role", "orchestrator"),
            ({"skill_ids": ["skill:testgen"]}, "workflow_runner", "skill", "workflows"),
            (
                {"acceptance_gate_ids": ["gate:tests"]},
                "dispatcher",
                "acceptance_gate",
                "orchestrator",
            ),
            ({}, "", "unknown", "unknown"),
        ):
            with self.subTest(kind=kind, owner=owner):
                events = self.completion_events()
                events["decision"] = replace(
                    events["decision"], payload=selected, producer=producer
                )
                episode = miner.normalize_episode(events)
                self.assertEqual((episode.kind_proposal, episode.owner_proposal), (kind, owner))
                self.assertTrue(episode.positive)

    def test_miner_normalization_names_negative_evidence(self):
        for phase, payload, reason in (
            ("artifact", {"result_hashes": ["sha256:result"]}, "missing_output_contract"),
            ("outcome", {"result": {"outcome_verdict": "failure"}}, "outcome_not_successful"),
            ("durability", {"durability": {"status": "reverted"}}, "durability_regressed"),
            ("durability", {"durability": {"status": "pending"}}, "no_durable_result"),
        ):
            with self.subTest(reason=reason):
                events = self.completion_events()
                events[phase] = replace(events[phase], payload=payload)
                episode = miner.normalize_episode(events)
                self.assertFalse(episode.positive)
                self.assertEqual(episode.evidence_reason, reason)

    def test_miner_assembly_rejects_mixed_identity_within_one_attempt(self):
        events = list(self.completion_events().values())
        events[-1] = replace(
            events[-1], identity=replace(events[-1].identity, observation_id="observation:other")
        )
        instance = miner.PatternMiner()
        with patch.object(miner, "adapt_completion_event_envelope", side_effect=events):
            episodes, count = instance._assemble([{}] * len(events))
        self.assertEqual((episodes, count), ([], 7))
        self.assertEqual(instance.rejections[0].reasons, ("episode_identity_mismatch",))

    def test_miner_assembly_rejects_duplicate_phases(self):
        events = list(self.completion_events().values())
        events.append(replace(events[0], event_id="event:trigger:duplicate"))
        instance = miner.PatternMiner()
        with patch.object(miner, "adapt_completion_event_envelope", side_effect=events):
            episodes, count = instance._assemble([{}] * len(events))
        self.assertEqual((episodes, count), ([], 8))
        self.assertEqual(instance.rejections[0].reasons, ("duplicate_phase:trigger",))

    def test_miner_deduplication_joins_successors_in_both_arrival_orders(self):
        base = miner.normalize_episode(self.completion_events())
        episodes = [
            replace(
                base,
                episode_id=name,
                fingerprint=f"sha256:{name}",
                semantic_fingerprint=f"sha256:semantic:{name}",
                output_contract_fingerprint=f"sha256:output:{name}",
                successor_of=predecessors,
            )
            for name, predecessors in (
                ("first", ("sha256:parent",)),
                ("sibling", ("sha256:parent",)),
                ("parent", ()),
                ("last", ("sha256:first",)),
                ("unrelated", ()),
            )
        ]
        groups = miner.PatternMiner._dedupe_groups(episodes)
        self.assertEqual(
            {frozenset(item.episode_id for item in group) for group in groups},
            {frozenset({"first", "sibling", "parent", "last"}), frozenset({"unrelated"})},
        )

    def test_miner_only_revives_retired_candidates_with_newer_evidence(self):
        episode = miner.normalize_episode(self.completion_events())
        retired = replace(
            self.candidate(),
            lifecycle=ir.Lifecycle(state="retired", expires_at=100),
            telemetry={"distinct_subject_count": 1, "last_evidence_at": 100},
        )
        tombstone = ir.CandidateTombstone(
            retired.fingerprint, retired.capability_id, "expired", 100
        )
        for timestamp, expected in ((99, "retired"), (100, "retired"), (101, "clustered")):
            with self.subTest(timestamp=timestamp):
                instance = miner.PatternMiner()
                instance.candidates = [retired]
                instance.tombstones = [tombstone]
                fresh = replace(
                    retired,
                    lifecycle=ir.Lifecycle(expires_at=200),
                    telemetry={"distinct_subject_count": 1, "last_evidence_at": timestamp},
                )
                with (
                    patch.object(instance, "_assemble", return_value=([episode], 7)),
                    patch.object(
                        instance, "_candidate", return_value=(fresh, {"next_action": "observe"})
                    ),
                ):
                    result = instance.mine([], now=150, preserve_existing=True)
                self.assertEqual(result.candidates[0].lifecycle.state, expected)
                self.assertEqual(result.tombstones, (tombstone,))
                self.assertEqual(
                    result.candidates[0].telemetry["last_evidence_at"],
                    timestamp if expected == "clustered" else 100,
                )

    def test_miner_atomic_replace_failure_preserves_previous_state(self):
        path = self.write_json("state.json", {"previous": True})
        with patch.object(miner.os, "replace", side_effect=OSError("disk unavailable")) as move:
            with self.assertRaisesRegex(OSError, "disk unavailable"):
                miner._write_json_atomic(path, {"replacement": True})
        move.assert_called_once()
        self.assertEqual(json.loads(path.read_text()), {"previous": True})
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_miner_tick_line_cli_names_actionable_failures(self):
        status = self.write_json(
            "status.json",
            {
                "mining_health": {
                    "state": "rejecting",
                    "summary": "1 rejected",
                    "complete_episode_count": 0,
                    "candidate_count": 0,
                    "actionable": True,
                    "detail": "missing attempt",
                }
            },
        )
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(miner.main(["tick-line", "--status", str(status)]), 0)
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "  MINING: rejecting — 1 rejected | episodes=0 candidates=0",
                "  MINING-ACTIONABLE: missing attempt",
            ],
        )

    def test_backlog_owner_questions_skip_duplicates_and_continue_after_failure(self):
        entries = {
            "owner/repo#1": {"await_human": False},
            "owner/repo#2": {"await_human": True, "reason": " x " * 200},
            "owner/repo#3": {"await_human": True},
            "owner/repo#4": {"await_human": True},
        }
        fresh = {"id": "question:one", "deduped": False}
        record = Mock(side_effect=[OSError("offline"), {"deduped": True}, fresh])
        with (
            patch.object(backlog, "expired_scoped_blockers", return_value=entries),
            patch.object(feedback, "record_owner_question", record),
        ):
            self.assertEqual(backlog.raise_expired_blocker_questions(now=100), [fresh])
        self.assertEqual(record.call_count, 3)
        first, _, last = record.call_args_list
        self.assertTrue(first.args[0].endswith(entries["owner/repo#2"]["reason"].strip()[:280]))
        self.assertEqual(
            last.kwargs,
            {"target": "owner/repo#4", "repo": "owner/repo", "expires_days": 7.0},
        )

    def test_backlog_owner_questions_tolerate_unavailable_feedback_dependency(self):
        original_import = builtins.__import__

        def without_feedback(name, *args, **kwargs):
            if name == "feedback":
                raise ImportError("feedback unavailable")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=without_feedback):
            self.assertEqual(backlog.raise_expired_blocker_questions(now=100), [])

    def test_backlog_unreadable_sentinel_is_distinct_from_no_blockers(self):
        with patch.object(backlog, "SENTINEL", self.root):
            self.assertEqual(backlog.scoped_blocker_source(), "unreadable")
            self.assertEqual(backlog.scoped_blocker_entries(), {})

    def test_backlog_research_excludes_scoped_issue_without_hiding_eligible_work(self):
        issues = [
            {"number": number, "labels": ["priority:normal"], "title": f"issue {number}"}
            for number in (1, 2)
        ]
        feed = backlog.build_research_items(
            ["owner/repo"],
            lambda repo: issues,
            lambda repo: [],
            scoped={"owner/repo#1"},
        )
        self.assertEqual([item["target"] for item in feed["items"]], ["owner/repo#2"])
        self.assertEqual(feed["counts"], {"open_issues": 2, "scoped_blocker": 1, "eligible": 1})

    def test_backlog_strict_gh_distinguishes_failure_from_empty_list(self):
        results = [
            (SimpleNamespace(returncode=1, stderr="", stdout=""), "gh failed"),
            (SimpleNamespace(returncode=0, stderr="", stdout="{}"), "gh returned no list"),
            (SimpleNamespace(returncode=0, stderr="", stdout=""), "gh returned no list"),
        ]
        for result, message in results:
            with self.subTest(message=message, stdout=result.stdout):
                with patch.object(subprocess, "run", return_value=result) as run:
                    with self.assertRaisesRegex(RuntimeError, message):
                        backlog._gh_strict(["issue", "list"])
                run.assert_called_once_with(
                    ["gh", "issue", "list"], capture_output=True, text=True, timeout=60
                )
        result = SimpleNamespace(returncode=0, stdout="[]")
        with patch.object(subprocess, "run", return_value=result):
            self.assertEqual(backlog._gh_strict(["issue", "list"]), [])

    def test_backlog_legacy_gh_read_failures_return_empty_list(self):
        for fetch, kind in ((backlog.live_fetch_issues, "issue"), (backlog.live_fetch_prs, "pr")):
            for result in (
                SimpleNamespace(returncode=1),
                SimpleNamespace(returncode=0, stdout="not JSON"),
            ):
                with self.subTest(kind=kind, result=result):
                    with patch.object(subprocess, "run", return_value=result) as run:
                        self.assertEqual(fetch("owner/repo"), [])
                    self.assertEqual(
                        run.call_args.args[0][:5], ["gh", kind, "list", "-R", "owner/repo"]
                    )
            with patch.object(subprocess, "run", side_effect=OSError("gh unavailable")):
                self.assertEqual(fetch("owner/repo"), [])

    def test_backlog_research_cli_atomically_publishes_feed(self):
        feed = {"counts": {"eligible": 1, "open_issues": 2}, "repos_read": 1, "repos_failed": []}
        with (
            patch.object(backlog, "SUPPORTED_REPOS", ["owner/repo"]),
            patch.object(backlog, "build_research_items", return_value=feed) as build,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(backlog.main(["--research"]), 0)
        build.assert_called_once_with(
            ["owner/repo"], backlog.strict_fetch_issues, backlog.strict_fetch_prs, scoped=set()
        )
        self.assertTrue(backlog.RESEARCH_BACKLOG_JSON.exists())
        self.assertEqual(json.loads(backlog.RESEARCH_BACKLOG_JSON.read_text()), feed)
        self.assertFalse(backlog.RESEARCH_BACKLOG_JSON.with_suffix(".json.tmp").exists())
        self.assertIn("1 eligible of 2 open issues; 1 repos read, 0 failed", output.getvalue())

    def test_backlog_dry_run_is_read_only_and_live_persists_the_same_items(self):
        items = [{"target": "owner/repo#1", "lane": "opener"}]
        questions = Mock(return_value=[{"id": "question:one"}])
        with (
            patch.object(backlog, "build_backlog", return_value=items) as build,
            patch.object(backlog, "raise_expired_blocker_questions", questions),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(backlog.main(["--dry-run"]), 0)
            dry = json.loads(output.getvalue())
            questions.assert_not_called()
            self.assertFalse(backlog.BACKLOG_JSON.exists())
            output.seek(0)
            output.truncate()
            self.assertEqual(backlog.main(["--live"]), 0)
            live = json.loads(output.getvalue())
        self.assertEqual(build.call_count, 2)
        questions.assert_called_once_with()
        self.assertEqual(dry["items"], items)
        self.assertEqual(live["items"], items)
        self.assertEqual(dry["owner_questions_raised"], 0)
        self.assertEqual(live["owner_questions_raised"], 1)
        self.assertEqual(json.loads(backlog.BACKLOG_JSON.read_text()), live)

    def test_ir_validation_refuses_invalid_candidates(self):
        candidate = self.candidate()
        occurrence = candidate.source_occurrences[0]
        invalid = [
            ({"schema": "foreign"}, "unsupported capability IR schema"),
            ({"version": -1}, "unsupported capability IR schema"),
            ({"capability_id": "one"}, "invalid capability_id"),
            ({"fingerprint": "one"}, "invalid capability fingerprint"),
            ({"lifecycle": ir.Lifecycle(state="active")}, "may not claim generated or active"),
            ({"kind_proposal": "executable"}, "invalid capability kind"),
            ({"owner_proposal": "anyone"}, "invalid capability owner"),
            ({"telemetry": {"distinct_subject_count": 2}}, "telemetry does not match"),
            ({"source_occurrences": ()}, "requires source occurrences"),
            (
                {"graph": {"phase_order": list(reversed(miner.PHASES))}},
                "canonical completion graph",
            ),
            (
                {"source_occurrences": (replace(occurrence, event_refs=("event:one",)),)},
                "all seven event refs",
            ),
            (
                {"source_occurrences": (replace(occurrence, event_refs=("event:one",) * 7),)},
                "all seven event refs",
            ),
        ]
        for changes, message in invalid:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, message):
                    replace(candidate, **changes).to_dict()
        self.assertEqual(candidate.to_dict()["capability_id"], "capability:one")

    def test_ir_persisted_objects_refuse_foreign_versions(self):
        raw = self.candidate().to_dict()
        tombstone = ir.CandidateTombstone("sha256:one", "capability:one", "expired", 200).to_dict()
        for restore, valid in (
            (ir.CapabilityIR.from_dict, raw),
            (ir.CandidateTombstone.from_dict, tombstone),
        ):
            for changes in ({"schema": "foreign"}, {"version": -1}):
                with self.subTest(restore=restore, changes=changes):
                    with self.assertRaisesRegex(ValueError, "unsupported persisted capability"):
                        restore({**valid, **changes})

    def test_redirect_json_cli_accepts_wrapped_history_and_bare_report(self):
        report = {"state": "stalled", "hints": [{"kind": "auth"}]}
        wrapped = {"report": report, "attempt_history": [report, report]}
        for payload, action in ((report, "redirect"), (wrapped, "decompose")):
            with self.subTest(action=action):
                with (
                    patch("sys.stdin", io.StringIO(json.dumps(payload))),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    self.assertEqual(redirect.main(["--json"]), 0)
                decision = json.loads(output.getvalue())
                self.assertEqual(decision["action"], action)
                self.assertTrue(decision["advisory"])
                self.assertEqual(
                    decision["history_summary"]["prior_attempts"],
                    2 if action == "decompose" else 0,
                )

    def test_redirect_heartbeat_failure_keeps_policy_available(self):
        self.heartbeat.side_effect = OSError("ledger unavailable")
        decision = redirect.decide({"state": "progress"})
        self.assertEqual(decision["action"], "wait")
        self.heartbeat.assert_called_once_with(
            "redirect-policy", "invocation", ref="redirect_policy.main"
        )

    def test_redirect_first_drift_requires_inspection_before_decomposition(self):
        decision = redirect.decide(
            {"state": "stalled", "drift": {"findings": [{"kind": "unexpected_paths"}]}}
        )
        self.assertEqual(decision["action"], "inspect")
        self.assertEqual(
            decision["reason"], "stalled with drift; inspect against acceptance criteria"
        )
        self.assertEqual(decision["confidence"], "high")
        self.assertFalse(decision["escalated"])

    def test_trigger_rejects_non_numeric_measured_coverage(self):
        for value in (None, "unknown", []):
            with self.subTest(value=value):
                report = self.write_json("coverage.json", {"totals": {"percent_covered": value}})
                percent, status = trigger.coverage_from_report(report)
                self.assertIsNone(percent)
                self.assertIn("non-numeric percent_covered", status)

    def test_trigger_cli_survives_report_disappearing_before_ranking(self):
        self.use_patch(patch.dict(os.environ, {trigger.KILL_SWITCH: "1"}))
        report = self.root / "disappeared.json"
        with (
            patch.object(trigger, "coverage_from_report", return_value=(80.0, "ok")),
            patch.object(
                trigger,
                "rank_targets",
                return_value=(["src/pattern_miner.py"], "ranked 1 candidate(s)"),
            ) as rank,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                trigger.main(
                    [
                        "--coverage-json",
                        str(report),
                        "--repo-path",
                        str(self.root),
                        "--targets",
                        "1",
                        "--json",
                    ]
                ),
                0,
            )
        rank.assert_called_once_with(str(self.root), {}, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["targets"], ["src/pattern_miner.py"])
        self.assertEqual(payload["action"], "write_tests")
        self.assertIn("ranked 1 candidate(s)", payload["reason"])

    def test_trigger_cli_ranks_measured_report_and_prints_crossing_warning(self):
        self.use_patch(patch.dict(os.environ, {trigger.KILL_SWITCH: "1"}))
        measured = {"totals": {"percent_covered": 80.0}}
        report = self.write_json("coverage.json", measured)
        with (
            patch.object(
                trigger,
                "rank_targets",
                return_value=(["src/pattern_miner.py"], "ranked 1 candidate(s)"),
            ) as rank,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                trigger.main(
                    [
                        "--repo",
                        "owner/repo",
                        "--coverage-json",
                        str(report),
                        "--repo-path",
                        str(self.root),
                        "--previous",
                        "90",
                    ]
                ),
                0,
            )
        rank.assert_called_once_with(str(self.root), measured, 3)
        self.assertIn("owner/repo: write_tests", output.getvalue())
        self.assertIn("ranked 1 candidate(s)", output.getvalue())
        self.assertIn("WARN: this repository fell below the warning threshold", output.getvalue())

    def test_trigger_enabled_cli_requires_a_coverage_report(self):
        self.use_patch(patch.dict(os.environ, {trigger.KILL_SWITCH: "1"}))
        with redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(SystemExit) as caught:
                trigger.main([])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--coverage-json is required", errors.getvalue())

    def test_trigger_heartbeat_failure_does_not_hide_a_measured_decision(self):
        self.use_patch(patch.dict(os.environ, {trigger.KILL_SWITCH: "1"}))
        self.heartbeat.side_effect = OSError("ledger unavailable")
        report = self.write_json("coverage.json", {"totals": {"percent_covered": 91.0}})
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(trigger.main(["--coverage-json", str(report), "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["action"], "none")
        self.heartbeat.assert_called_once_with(
            "coverage-testgen-trigger", "invocation", ref="coverage_testgen_trigger.main"
        )

    def test_readonly_clis_reject_unknown_arguments_without_creating_artifacts(self):
        for module in (backlog, redirect):
            with self.subTest(module=module.__name__):
                with redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(module.main(["--unknown"]), 2)
                self.assertIn(f"usage: {module.__name__}.py", errors.getvalue())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_paths_pin_nested_alias_even_when_child_does_not_exist(self):
        physical = self.root / "release" / "src"
        physical.mkdir(parents=True)
        alias = self.root / "live"
        alias.symlink_to(physical, target_is_directory=True)
        with (
            patch.object(paths, "MODULE_DIR", physical),
            patch.object(paths, "_MODULE_ALIAS", physical),
        ):
            self.assertEqual(
                paths._pin_import_path(str(alias / "future")), str(physical / "future")
            )
            unrelated = str(self.root / "unrelated")
            self.assertEqual(paths._pin_import_path(unrelated), unrelated)

    def test_paths_checkout_alias_stays_bound_after_publication(self):
        physical = self.root / "old" / "src"
        logical = self.root / "live" / "src"
        with (
            patch.object(paths, "MODULE_DIR", physical),
            patch.object(paths, "_MODULE_ALIAS", logical),
        ):
            self.assertEqual(paths.checkout_root(logical), physical.parent)
            self.assertEqual(paths.fleet_root(logical), physical.parent.parent)
            self.assertEqual(paths.checkout_root(self.root / "flat"), self.root / "flat")

    def test_paths_runtime_and_cli_resolve_private_layout(self):
        self.assertEqual(
            paths.runtime_state_path("nested", "state.json"), self.root / "nested" / "state.json"
        )
        (self.root / "orchestrate.sh").write_text("# fixture\n", encoding="utf-8")
        with (
            patch.object(paths, "REPO_ROOT", self.root),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(paths.main([]), 0)
        self.assertIn(
            f"orchestrate.sh = {self.root / 'orchestrate.sh'} (exists: True)", output.getvalue()
        )


if __name__ == "__main__":
    unittest.main()
