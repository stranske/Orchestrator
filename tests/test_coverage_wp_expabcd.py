"""Hermetic regression tests covering missing statements in src/exp_abcd.py."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import types

import pytest

import dispatcher
import exp_abcd
import experiment_producer
import redirect_sweep
import research_usage_guard
import spec_provenance
import strategy_experiment
import synthesis_promotion

SPEC_TEXT = "spec requirement\n"
spec_text = SPEC_TEXT


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    """Every test runs against a fresh isolated runtime directory."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))


def test_experiment_members_validation():
    # line 155: non-dict member
    with pytest.raises(ValueError, match="experiment member metadata must be objects"):
        exp_abcd.experiment_members({"members": ["not-a-dict"]})

    # line 160: missing arm_id/member_id/agent
    with pytest.raises(ValueError, match="v2 experiment member lacks arm_id/member_id/agent"):
        exp_abcd.experiment_members({"members": [{"arm_id": "a1", "member_id": "m1"}]})
    with pytest.raises(ValueError, match="v2 experiment member lacks arm_id/member_id/agent"):
        exp_abcd.experiment_members({"members": [{"arm_id": "a1", "agent": "claude"}]})
    with pytest.raises(ValueError, match="v2 experiment member lacks arm_id/member_id/agent"):
        exp_abcd.experiment_members({"members": [{"member_id": "m1", "agent": "claude"}]})


def test_member_run_id_legacy():
    # line 196: legacy member run_id
    run_id = exp_abcd._member_run_id("exp-legacy", {"legacy": True, "agent": "claude"})
    assert run_id == "exp-legacy:claude"

    v2_id = exp_abcd._member_run_id("exp-legacy", {"legacy": False, "member_id": "mem-1"})
    assert v2_id == "exp-legacy:member:mem-1"


def test_completion_cmd_options(tmp_path):
    # lines 434, 445, 447: profile and causal context additions
    log = tmp_path / "test.log"
    cmd = exp_abcd._completion_cmd(
        agent="claude",
        mode="full",
        run_id="run-1",
        target="target-1",
        task_type="implement",
        log=log,
        started_ts=123456,
        profile={"profile_id": "prof-1", "requested_model": "model-1"},
        causal_context={"subject_id": "sub-42", "arm_id": "arm-alpha"},
    )
    assert "--selected-profile-id prof-1" in cmd
    assert "--requested-model model-1" in cmd
    assert "--subject-id sub-42" in cmd
    assert "--arm-id arm-alpha" in cmd


def test_spawn_profile_and_error_handling(tmp_path, monkeypatch):
    # lines 531, 565-567, 574, 585
    log = tmp_path / "spawn.log"
    cwd = tmp_path / "worktree"
    cwd.mkdir()

    attempts: list[dict] = []
    completes: list[dict] = []
    monkeypatch.setattr(
        exp_abcd.feedback,
        "record_execution_attempt",
        lambda *args, **kwargs: attempts.append(kwargs),
    )
    monkeypatch.setattr(
        exp_abcd.feedback,
        "complete_profile_attempt_unresolved",
        lambda *args, **kwargs: completes.append(kwargs),
    )
    monkeypatch.setattr(exp_abcd, "_record_execution_start", lambda *args, **kwargs: 100)
    monkeypatch.setattr(exp_abcd, "_record_execution_complete", lambda *args, **kwargs: None)

    profile = {
        "profile_id": "prof-xyz",
        "provider": "anthropic",
        "requested_model": "claude-3-5-sonnet",
        "agent": "claude",
    }
    monkeypatch.setattr(
        exp_abcd.execution_profiles,
        "get_profile",
        lambda pid: profile if pid == "prof-xyz" else None,
    )

    def fail_popen(*args, **kwargs):
        raise OSError("failed to launch agent process")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    with pytest.raises(OSError, match="failed to launch agent process"):
        exp_abcd._spawn(
            agent="claude",
            mode="full",
            prompt="do something",
            cwd=cwd,
            log=log,
            profile_id="prof-xyz",
        )

    assert len(attempts) == 1
    assert attempts[0]["profile_id"] == "prof-xyz"
    assert attempts[0]["requested_provider"] == "anthropic"
    assert len(completes) == 1
    assert completes[0]["fallback_reason"] == "profile_process_start_failed"
    assert completes[0]["selected_profile_id"] == "prof-xyz"


def test_prepare_legacy(tmp_path, monkeypatch):
    # lines 597-603, 608, 611, 623-629, 642-648, 672, 684, 695
    canon = tmp_path / "canon"
    canon.mkdir()
    exp_dir = tmp_path / "experiments"
    exp_dir.mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text("Test specification requirement.")

    monkeypatch.setattr(exp_abcd, "EXP_DIR", exp_dir)
    monkeypatch.setattr(exp_abcd.provision, "WORKTREES_DIR", tmp_path / "worktrees")
    monkeypatch.setattr(exp_abcd.provision, "ensure_canonical", lambda repo: canon)
    monkeypatch.setattr(exp_abcd.provision, "base_branch", lambda repo: "main")

    class FakeResult:
        stdout = "sha-base-123\n"
        returncode = 0

    monkeypatch.setattr(exp_abcd.provision, "_run", lambda *args, **kwargs: FakeResult())
    monkeypatch.setattr(exp_abcd.feedback, "record_run", lambda *args, **kwargs: None)

    spawns: list[dict] = []
    monkeypatch.setattr(exp_abcd, "_spawn", lambda *args, **kwargs: spawns.append(kwargs) or 4242)

    prof = {"profile_id": "p1", "requested_model": "m1", "agent": "claude"}
    monkeypatch.setattr(
        exp_abcd.execution_profiles,
        "get_profile",
        lambda p: prof,
    )

    res = exp_abcd.prepare(
        repo="o/r",
        spec_file=str(spec),
        exp_id="exp-legacy",
        agents=["claude"],
        task_type="implement",
        profiles={"claude": "p1"},
    )

    assert res["exp_id"] == "exp-legacy"
    assert len(res["launched"]) == 1
    assert res["launched"][0]["agent"] == "claude"
    assert res["launched"][0]["pid"] == 4242
    assert res["launched"][0]["profile_id"] == "p1"

    meta_path = exp_dir / "exp-legacy" / "meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text())
    assert meta["base_sha"] == "sha-base-123"
    assert meta["agents"] == ["claude"]


def test_normalize_arm_members_validation():
    # line 705: not dict
    with pytest.raises(ValueError, match="strategy arms must be objects"):
        exp_abcd._normalize_arm_members(["not-a-dict"])

    # line 709: missing arm_id or agents
    with pytest.raises(ValueError, match="each strategy arm requires arm_id and agents"):
        exp_abcd._normalize_arm_members([{"arm_id": "", "agents": ["claude"]}])
    with pytest.raises(ValueError, match="each strategy arm requires arm_id and agents"):
        exp_abcd._normalize_arm_members([{"arm_id": "a1", "agents": []}])
    with pytest.raises(ValueError, match="each strategy arm requires arm_id and agents"):
        exp_abcd._normalize_arm_members([{"arm_id": "a1", "agents": [""]}])

    # line 711: duplicate arm_id
    with pytest.raises(ValueError, match="duplicate arm_id: a1"):
        exp_abcd._normalize_arm_members(
            [
                {"arm_id": "a1", "agents": ["claude"]},
                {"arm_id": "a1", "agents": ["gemini"]},
            ]
        )

    # line 719: duplicate member_id
    with pytest.raises(ValueError, match="duplicate member_id: m_dup"):
        exp_abcd._normalize_arm_members(
            [
                {"arm_id": "a1", "agents": ["claude"], "members": [{"member_id": "m_dup"}]},
                {"arm_id": "a2", "agents": ["gemini"], "members": [{"member_id": "m_dup"}]},
            ]
        )

    # line 741: empty normalized_arms
    with pytest.raises(ValueError, match="at least one strategy arm is required"):
        exp_abcd._normalize_arm_members([])


def test_prepare_arms_producer_and_brief(tmp_path, monkeypatch):
    # lines 809, 811, 868, 876
    canon = tmp_path / "canon"
    canon.mkdir()
    exp_dir = tmp_path / "experiments"
    exp_dir.mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text("Spec text")

    monkeypatch.setattr(exp_abcd, "EXP_DIR", exp_dir)
    monkeypatch.setattr(exp_abcd.provision, "WORKTREES_DIR", tmp_path / "worktrees")
    monkeypatch.setattr(exp_abcd.provision, "ensure_canonical", lambda repo: canon)
    monkeypatch.setattr(exp_abcd.provision, "base_branch", lambda repo: "main")

    class FakeResult:
        stdout = "sha-base-123\n"
        returncode = 0

    monkeypatch.setattr(exp_abcd.provision, "_run", lambda *args, **kwargs: FakeResult())

    # line 809: unknown producer
    with pytest.raises(ValueError, match="unknown producer"):
        exp_abcd.prepare_arms(
            repo="o/r",
            spec_file=str(spec),
            exp_id="exp-prod-bad",
            arms=[{"arm_id": "a1", "agents": ["claude"]}],
            producer_metadata={"producer": "unknown_producer"},
        )

    # line 811: empty spec with producer
    empty_spec = tmp_path / "empty_spec.md"
    empty_spec.write_text("   \n")
    with pytest.raises(
        exp_abcd.MissingSpecNotEvaluableError, match="producer requires frozen issue body"
    ):
        exp_abcd.prepare_arms(
            repo="o/r",
            spec_file=str(empty_spec),
            exp_id="exp-prod-empty",
            arms=[{"arm_id": "a1", "agents": ["claude"]}],
            producer_metadata={"producer": "experiment_producer"},
        )

    # line 868: test_sha256 digest mismatch
    arms = [{"arm_id": "a1", "agents": ["claude"]}]
    _, members = exp_abcd._normalize_arm_members(arms)
    wt = exp_abcd.exp_worktree("o/r", "exp-hash", members[0]["agent"], members[0]["member_id"])
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").mkdir(exist_ok=True)
    (wt / "test_file.py").write_text("actual content")

    prompt_captured: list[str] = []
    monkeypatch.setattr(
        exp_abcd,
        "_spawn",
        lambda agent, mode, prompt, *args, **kwargs: prompt_captured.append(prompt) or 100,
    )
    monkeypatch.setattr(exp_abcd.feedback, "record_run", lambda *args, **kwargs: None)

    fake_producer = types.ModuleType("experiment_producer")
    fake_producer.freeze_test = lambda *args, **kwargs: {
        "named_test": "pytest test_file.py",
        "test_sha256": {"test_file.py": "0" * 64},
        "test_commit": "sha-test-commit",
    }
    monkeypatch.setattr(exp_abcd, "experiment_producer", fake_producer, raising=False)
    monkeypatch.setattr(experiment_producer, "freeze_test", fake_producer.freeze_test)

    with pytest.raises(ValueError, match="frozen test differs in comparison worktree"):
        exp_abcd.prepare_arms(
            repo="o/r",
            spec_file=str(spec),
            exp_id="exp-hash",
            arms=arms,
            producer_metadata={"producer": "experiment_producer"},
        )

    # line 876: arm brief appended to prompt
    arms_brief = [
        {
            "arm_id": "a1",
            "agents": ["claude"],
            "brief": "Focus on high-speed async execution.",
        }
    ]
    exp_abcd.prepare_arms(
        repo="o/r",
        spec_file=str(spec),
        exp_id="exp-brief",
        arms=arms_brief,
    )
    assert any(
        "Alternative brief: Focus on high-speed async execution." in p for p in prompt_captured
    )


def test_prepare_arm_single_wrapper(monkeypatch):
    # line 1160: single arm compatibility wrapper
    called: list[tuple] = []
    monkeypatch.setattr(
        exp_abcd,
        "prepare_arms",
        lambda repo, spec_file, exp_id, arms, task_type: called.append(
            (repo, spec_file, exp_id, arms, task_type)
        )
        or {"status": "ok"},
    )
    arm = {"arm_id": "arm1", "agents": ["claude"]}
    res = exp_abcd.prepare_arm("o/r", "spec.md", "exp-1", arm, task_type="implement")
    assert res == {"status": "ok"}
    assert len(called) == 1
    assert called[0][3] == [arm]


def test_launch_pending_reviews_failures(tmp_path, monkeypatch):
    # lines 989, 993-994, 1037-1038, 1070-1071, 1073-1074, 1078-1079
    exp_dir = tmp_path / "experiments"
    edir = exp_dir / "exp-rev"
    edir.mkdir(parents=True)
    monkeypatch.setattr(exp_abcd, "EXP_DIR", exp_dir)
    monkeypatch.setattr(exp_abcd.provision, "WORKTREES_DIR", tmp_path / "worktrees")
    monkeypatch.setattr(exp_abcd.provision, "ensure_canonical", lambda repo: tmp_path / "canon")

    meta = {
        "repo": "o/r",
        "exp_id": "exp-rev",
        "base": "main",
        "base_sha": "base123",
        "members": [
            {"arm_id": "arm1", "member_id": "impl1", "agent": "claude", "legacy": False},
            {
                "arm_id": "arm1",
                "member_id": "rev1",
                "agent": "vibe",
                "legacy": False,
                "role": "review",
                "review_of_member_id": "impl1",
            },
        ],
    }
    (edir / "meta.json").write_text(json.dumps(meta))
    (edir / "spec.md").write_text(spec_text)

    # 1. line 989: reviewer failed (rc != 0)
    done_dir = edir / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    rev_run = "exp-rev:member:rev1"
    (done_dir / f"{rev_run}.json").write_text(
        json.dumps({"run_id": rev_run, "rc_of": "agent", "rc": 1})
    )
    rev_log = edir / exp_abcd.exp_log_path("vibe", "rev1")
    rev_log.write_text("reviewer log content")

    res = exp_abcd.launch_pending_reviews("o/r", "exp-rev")
    assert res["failed"] == [{"member_id": "rev1", "reason": "reviewer-failed"}]
    (done_dir / f"{rev_run}.json").unlink()
    rev_log.unlink()

    # 2. lines 993-994: missing implementer
    meta_bad_impl = dict(meta)
    meta_bad_impl["members"] = [
        {
            "arm_id": "arm1",
            "member_id": "rev1",
            "agent": "vibe",
            "legacy": False,
            "role": "review",
            "review_of_member_id": "nonexistent",
        },
    ]
    (edir / "meta.json").write_text(json.dumps(meta_bad_impl))
    res2 = exp_abcd.launch_pending_reviews("o/r", "exp-rev")
    assert res2["failed"] == [{"member_id": "rev1", "reason": "missing-implementer"}]
    (edir / "meta.json").write_text(json.dumps(meta))
    (edir / "spec.md").write_text(spec_text)

    # Implementer completed with rc=0
    impl_run = "exp-rev:member:impl1"
    (done_dir / f"{impl_run}.json").write_text(
        json.dumps({"run_id": impl_run, "rc_of": "agent", "rc": 0})
    )

    # 3. lines 1037-1038: implementer produced no delta
    class FakeRunEmpty:
        stdout = ""
        returncode = 0

    monkeypatch.setattr(exp_abcd.provision, "_run", lambda *args, **kwargs: FakeRunEmpty())
    res3 = exp_abcd.launch_pending_reviews("o/r", "exp-rev")
    assert res3["failed"] == [{"member_id": "rev1", "reason": "implementer-produced-no-delta"}]

    class FakeRunDiff:
        stdout = "diff --git a/file b/file\n+new line\n"
        returncode = 0

    # 4. lines 1070-1071, 1073-1074: seed_receipt exists but has corrupt json
    monkeypatch.setattr(exp_abcd.provision, "_run", lambda *args, **kwargs: FakeRunDiff())
    seed_receipt = edir / f"review-seed-{exp_abcd._artifact_identity('vibe', 'rev1')}.json"
    seed_receipt.write_text("invalid json content")
    res4 = exp_abcd.launch_pending_reviews("o/r", "exp-rev")
    assert res4["failed"] == [{"member_id": "rev1", "reason": "review-seed-proof-mismatch"}]
    seed_receipt.unlink()

    # 5. lines 1078-1079: seed apply fails
    class FakeRunApplyFail:
        stdout = "patch does not apply"
        returncode = 1

    def fake_run_step(argv, *args, **kwargs):
        if "diff" in argv:
            return FakeRunDiff()
        if "apply" in argv:
            return FakeRunApplyFail()
        return FakeRunDiff()

    monkeypatch.setattr(exp_abcd.provision, "_run", fake_run_step)
    res5 = exp_abcd.launch_pending_reviews("o/r", "exp-rev")
    assert res5["failed"] == [{"member_id": "rev1", "reason": "review-seed-apply-failed"}]


def test_collect_recovery_and_gemini_scratch(tmp_path, monkeypatch):
    # lines 1253-1254, 1281
    exp_dir = tmp_path / "experiments"
    edir = exp_dir / "exp-col"
    edir.mkdir(parents=True)
    monkeypatch.setattr(exp_abcd, "EXP_DIR", exp_dir)
    monkeypatch.setattr(exp_abcd.provision, "WORKTREES_DIR", tmp_path / "worktrees")

    meta = {
        "repo": "o/r",
        "exp_id": "exp-col",
        "base": "main",
        "members": [
            {"arm_id": "arm1", "member_id": "mem_gemini", "agent": "gemini", "legacy": False},
        ],
    }
    (edir / "meta.json").write_text(json.dumps(meta))
    (edir / "spec.md").write_text(spec_text)

    fake_recovery = types.ModuleType("experiment_recovery")
    fake_recovery.arm_diff = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("git recovery failed")
    )
    monkeypatch.setitem(sys.modules, "experiment_recovery", fake_recovery)

    class FakeGit:
        stdout = ""

    monkeypatch.setattr(exp_abcd.worktree_git, "run", lambda *args, **kwargs: FakeGit())

    scratch_calls: list[tuple] = []
    monkeypatch.setattr(
        exp_abcd,
        "_gemini_scratch_diff",
        lambda log, base: scratch_calls.append((log, base)) or "recovered gemini diff\n",
    )

    res = exp_abcd.collect("o/r", "exp-col")
    assert len(scratch_calls) == 1
    assert res["diffs"]["mem_gemini"]["bytes"] > 0
    diff_file = edir / exp_abcd.exp_diff_path("gemini", "mem_gemini")
    assert diff_file.read_text() == "recovered gemini diff\n"


def test_gemini_scratch_diff_recovery(tmp_path, monkeypatch):
    # lines 1308-1320, 1324-1326
    missing_log = tmp_path / "nonexistent.log"
    assert exp_abcd._gemini_scratch_diff(missing_log, "main") == ""

    log_file = tmp_path / "gemini.log"
    log_file.write_text("No scratch references here.\n")
    assert exp_abcd._gemini_scratch_diff(log_file, "main") == ""

    # Path simulation to match r"(/Users/[^\s`]+/antigravity-cli/scratch/[^\s`]+)"
    orig_path = exp_abcd.Path

    class FakePath(orig_path):
        def exists(self):
            return "has_git" in str(self)

    monkeypatch.setattr(exp_abcd, "Path", FakePath)
    log_file.write_text(
        "Checked `/Users/fake/antigravity-cli/scratch/no_git` and "
        "`/Users/fake/antigravity-cli/scratch/has_git` for experimentation.\n"
    )

    class FakeDiffResult:
        stdout = "diff --git a/file b/file\n+recovered from scratch\n"

    monkeypatch.setattr(exp_abcd.provision, "_run", lambda *args, **kwargs: FakeDiffResult())

    diff = exp_abcd._gemini_scratch_diff(log_file, "main")
    assert "recovered from scratch" in diff


def test_extract_helpers_and_capacity(tmp_path, monkeypatch):
    # lines 1348-1349 (_extract_json error handling)
    # lines 1371 (_extract_evidence_gaps empty gap continue)
    # line 1382 (_extract_cited_evidence_types non-dict input)
    # lines 1471-1475 (_capacity_map loading and exception handling)
    bad_json_text = '{"scores": {"A": 10}} {"invalid": {unquoted}}'
    parsed = exp_abcd._extract_json(bad_json_text, required_key="scores")
    assert parsed == {"scores": {"A": 10}}

    gaps = exp_abcd._extract_evidence_gaps({"evidence_gaps": ["", "  ", {"gap": ""}, "valid gap"]})
    assert gaps == ["valid gap"]

    assert exp_abcd._extract_cited_evidence_types(None) == []
    assert exp_abcd._extract_cited_evidence_types("not-a-dict") == []

    # _capacity_map valid
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    monkeypatch.setenv("HANDOFF_DIR", str(handoff))
    cap_file = handoff / "capacity.json"
    cap_file.write_text(json.dumps({"agents": {"gemini": {"policy": "drain"}}}))
    assert exp_abcd._capacity_map() == {"gemini": {"policy": "drain"}}

    # _capacity_map exception fallback
    cap_file.write_text("unreadable json")
    assert exp_abcd._capacity_map() == {}


def test_normalize_evaluator_specs_validation():
    # lines 1561, 1564, 1593
    with pytest.raises(ValueError, match="invalid evaluator: 999"):
        exp_abcd._normalize_evaluator_specs([999], default_agents=[], implementer_agents=[])

    with pytest.raises(ValueError, match="evaluator requires agent"):
        exp_abcd._normalize_evaluator_specs(
            [{"agent": ""}], default_agents=[], implementer_agents=[]
        )

    with pytest.raises(ValueError, match="evaluator identities must be unique"):
        exp_abcd._normalize_evaluator_specs(
            [
                {"agent": "claude", "evaluator_id": "dup"},
                {"agent": "gemini", "evaluator_id": "dup"},
            ],
            default_agents=[],
            implementer_agents=[],
        )


def test_evaluate_execution_and_still_running(tmp_path, monkeypatch):
    # lines 1727, 1730-1731, 1767-1771, 1782-1786, 1790-1791, 1841-1843
    edir = tmp_path / "exp-eval"
    edir.mkdir()
    meta = {
        "repo": "o/r",
        "exp_id": "exp-eval",
        "base": "main",
        "members": [{"arm_id": "a1", "member_id": "m1", "agent": "claude", "legacy": False}],
    }
    (edir / "meta.json").write_text(json.dumps(meta))
    (edir / "spec.md").write_text(spec_text)
    spec_path = edir / "spec.md"
    spec_path.write_text("spec text")
    (edir / exp_abcd.exp_diff_path("claude", "m1")).write_text("diff content\n")
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path)

    class Proc1:
        poll = None

        def wait(self, timeout=None):
            out_file = edir / "eval-out-codex.txt"
            out_file.write_text(json.dumps({"scores": {"Z": 10, "A": "not-a-number"}}))
            if timeout == 0:
                raise subprocess.TimeoutExpired("cmd", 0)
            raise RuntimeError("kill wait error")

        def kill(self):
            out_file = edir / "eval-out-codex.txt"
            out_file.write_text(json.dumps({"scores": {"Z": 10, "A": "not-a-number"}}))

    class Proc2:
        poll = None

        def wait(self, timeout=None):
            if timeout is not None:
                raise TypeError("no timeout supported")
            return 0

    procs = [Proc1(), Proc2()]
    monkeypatch.setattr(exp_abcd, "_eval_command", lambda *args, **kwargs: "echo eval")
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: procs.pop(0))

    for ev in ["codex", "claude"]:
        out_file = edir / f"eval-out-{ev}.txt"
        out_file.write_text(json.dumps({"scores": {"Z": 10, "A": "not-a-number", "B": 8.0}}))

    monkeypatch.setattr(
        strategy_experiment,
        "write_evaluation_result",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("strategy result broken")),
    )

    evaluators = [
        {"agent": "codex", "evaluator_id": "codex"},
        {"agent": "claude", "evaluator_id": "claude"},
    ]
    res = exp_abcd.evaluate(
        "o/r",
        str(spec_path),
        "exp-eval",
        evaluators=evaluators,
        timeout=-1,
    )
    assert res.get("strategy_result_error") == "strategy result broken"
    assert (edir / "strategy-result-error.json").exists()


def test_producer_followup_phases_and_errors(tmp_path, monkeypatch):
    # lines 1911, 1936, 1938, 1940, 1966, 1976, 1980-1983, 1986, 1998, 2030-2032
    edir = tmp_path / "exp-prod"
    edir.mkdir()
    spec_text = "spec requirement\n"
    meta = {
        "repo": "o/r",
        "exp_id": "exp-prod",
        "subject": "sub1",
        "spec_provenance": "issue_body",
        "spec_sha256": hashlib.sha256(spec_text.encode()).hexdigest(),
        "members": [{"arm_id": "a1", "member_id": "m1", "agent": "claude", "legacy": False}],
    }
    (edir / "meta.json").write_text(json.dumps(meta))
    (edir / "spec.md").write_text(spec_text)

    # 1. PR url and synthesis promotion state exists (lines 1911, 1936, 1940)
    (edir / "producer-delivery.json").write_text(
        json.dumps({"pr_url": "http://pr", "pr_number": 123})
    )
    (edir / "synthesis-promotion.json").write_text("{}")
    monkeypatch.setattr(
        synthesis_promotion,
        "reconcile_producer_delivery",
        lambda edir, gh_fn=None: {"state": {"canonical_state": "merged"}},
    )
    res1 = exp_abcd.producer_followup(
        edir,
        meta,
        now=1000.0,
        min_idle_s=120,
        eval_timeout=1500,
        collect_fn=exp_abcd.collect,
        evaluate_fn=exp_abcd.evaluate,
        gh_fn=lambda *a: {"ok": True},
    )
    assert res1.get("status") == "merged"
    assert res1.get("delivery_link") is not None

    # 2. PR url with phase discarded (line 1938)
    (edir / "producer-delivery.json").write_text(
        json.dumps({"pr_url": "http://pr", "pr_number": 123})
    )
    monkeypatch.setattr(
        synthesis_promotion,
        "reconcile_producer_delivery",
        lambda edir, gh_fn=None: {"state": {"delivery_phase": "discarded"}},
    )
    res2 = exp_abcd.producer_followup(
        edir,
        meta,
        now=1000.0,
        min_idle_s=120,
        eval_timeout=1500,
        collect_fn=exp_abcd.collect,
        evaluate_fn=exp_abcd.evaluate,
        gh_fn=lambda *a: {"ok": True},
    )
    assert res2.get("status") == "discarded"
    (edir / "producer-delivery.json").unlink()
    (edir / "synthesis-promotion.json").unlink()

    # 3. missing logs -> still_running (line 1966)
    res3 = exp_abcd.producer_followup(
        edir,
        meta,
        now=1000.0,
        min_idle_s=120,
        eval_timeout=1500,
        collect_fn=exp_abcd.collect,
        evaluate_fn=exp_abcd.evaluate,
    )
    assert res3.get("status") == "still_running"

    log = edir / exp_abcd.exp_log_path("claude", "m1")
    log.write_text("done")
    os.utime(log, (100, 100))

    # 4. allow_evaluate is False -> per_call_cap (line 1976)
    res4 = exp_abcd.producer_followup(
        edir,
        meta,
        now=10000.0,
        min_idle_s=120,
        eval_timeout=1500,
        collect_fn=exp_abcd.collect,
        evaluate_fn=exp_abcd.evaluate,
        allow_evaluate=False,
        push_check_fn=lambda *a: None,
    )
    assert res4.get("status") == "per_call_cap"

    # 5. empty candidate diffs -> no_winner (lines 1980-1983)
    res5 = exp_abcd.producer_followup(
        edir,
        meta,
        now=10000.0,
        min_idle_s=120,
        eval_timeout=1500,
        collect_fn=lambda *a: {"diffs": {}},
        evaluate_fn=exp_abcd.evaluate,
        allow_evaluate=True,
        push_check_fn=lambda *a: None,
    )
    assert res5.get("status") == "no_winner"
    assert res5.get("reason") == "no_diffs"
    (edir / "producer-delivery.json").unlink(missing_ok=True)

    # 6. insufficient neutral judges (line 1986)
    meta_all_evals = dict(meta)
    meta_all_evals["members"] = [
        {"arm_id": f"a{i}", "member_id": f"m{i}", "agent": agent, "legacy": False}
        for i, agent in enumerate(exp_abcd.EVALUATOR_TOPUP_ORDER[:-1])
    ]
    (edir / "meta.json").write_text(json.dumps(meta_all_evals))
    for m in meta_all_evals["members"]:
        mlog = edir / exp_abcd.exp_log_path(m["agent"], m["member_id"])
        mlog.write_text("done")
        os.utime(mlog, (100, 100))

    res6 = exp_abcd.producer_followup(
        edir,
        meta_all_evals,
        now=10000.0,
        min_idle_s=120,
        eval_timeout=1500,
        collect_fn=lambda *a: {"diffs": {"m0": {"diff": "something"}}},
        evaluate_fn=exp_abcd.evaluate,
        allow_evaluate=True,
        push_check_fn=lambda *a: None,
    )
    assert res6.get("status") == "insufficient_neutral_judges"
    (edir / "meta.json").write_text(json.dumps(meta))
    (edir / "spec.md").write_text(spec_text)

    # 7. opp not eligible -> opp decision (line 1998)
    monkeypatch.setattr(
        research_usage_guard,
        "assess_and_record_opportunity",
        lambda **k: {"eligible": False, "decision": "budget_exceeded"},
    )
    res7 = exp_abcd.producer_followup(
        edir,
        meta,
        now=10000.0,
        min_idle_s=120,
        eval_timeout=1500,
        collect_fn=lambda *a: {"diffs": {"m1": {"diff": "something"}}},
        evaluate_fn=exp_abcd.evaluate,
        allow_evaluate=True,
        push_check_fn=lambda *a: None,
    )
    assert res7.get("status") == "budget_exceeded"

    # 8. evaluate_fn raises -> update outcome failed and re-raise (lines 2030-2032)
    monkeypatch.setattr(
        research_usage_guard,
        "assess_and_record_opportunity",
        lambda **k: {"eligible": True, "opportunity_id": "opp-1"},
    )
    outcome_updates: list[tuple] = []
    monkeypatch.setattr(
        research_usage_guard,
        "update_opportunity_outcome",
        lambda opp_id, outcome: outcome_updates.append((opp_id, outcome)),
    )
    with pytest.raises(RuntimeError, match="eval fail"):
        exp_abcd.producer_followup(
            edir,
            meta,
            now=10000.0,
            min_idle_s=120,
            eval_timeout=1500,
            collect_fn=lambda *a: {"diffs": {"m1": {"diff": "something"}}},
            evaluate_fn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("eval fail")),
            allow_evaluate=True,
            push_check_fn=lambda *a: None,
        )
    assert outcome_updates == [("opp-1", "failed")]


def test_followup_missing_exp_dir_and_producer_meta_error(tmp_path, monkeypatch):
    # lines 2093, 2186-2187, 2211-2212
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path / "nonexistent")
    res_none = exp_abcd.followup()
    assert res_none["processed"] == []
    assert res_none["skipped"] == []

    # unreadable producer meta (lines 2186-2187)
    exp_dir = tmp_path / "experiments"
    exp_dir.mkdir()
    monkeypatch.setattr(exp_abcd, "EXP_DIR", exp_dir)
    edir1 = exp_dir / "exp1"
    edir1.mkdir()
    (edir1 / "meta.json").write_text("corrupt meta")
    (edir1 / "producer-delivery.json").write_text("{}")
    res1 = exp_abcd.followup()
    assert res1["processed"] == [{"exp_id": "exp1", "error": "unreadable producer meta"}]

    # producer_followup raises and producer-delivery.json has corrupt json (lines 2211-2212)
    (edir1 / "meta.json").write_text(
        json.dumps({"producer": "experiment_producer", "launched_ts": 10000000000})
    )
    (edir1 / "producer-delivery.json").write_text("corrupt json")
    monkeypatch.setattr(
        exp_abcd,
        "producer_followup",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    res2 = exp_abcd.followup()
    assert "boom" in res2["processed"][0]["error"]


def test_followup_skips_and_lifecycles(tmp_path, monkeypatch):
    # lines 2298-2299, 2305-2306, 2317, 2320-2321, 2327-2328, 2370-2371, 2392-2393, 2426-2427, 2440, 2453-2454
    exp_dir = tmp_path / "experiments"
    exp_dir.mkdir()
    monkeypatch.setattr(exp_abcd, "EXP_DIR", exp_dir)
    monkeypatch.setattr(exp_abcd.time, "time", lambda: 10000.0)
    monkeypatch.setattr(exp_abcd, "ship_gate_stamp", lambda: tmp_path / "nonexistent_stamp")

    # refresh trial error (lines 2305-2306) and corrupt meta after refresh (lines 2320-2321)
    edir_ref = exp_dir / "exp-ref"
    edir_ref.mkdir()
    (edir_ref / "meta.json").write_text(
        json.dumps({"repo": "o/r", "exp_id": "exp-ref", "agents": ["claude"]})
    )
    (edir_ref / "spec.md").write_text("spec")
    (edir_ref / "eval-maps.json").write_text("{}")

    edir_ca = exp_dir / "exp-ca"
    edir_ca.mkdir()
    (edir_ca / "meta.json").write_text(
        json.dumps({"repo": "o/r", "exp_id": "exp-ca", "agents": ["claude"]})
    )
    (edir_ca / "spec.md").write_text("spec")

    monkeypatch.setattr(
        strategy_experiment, "refreshable_trials", lambda *a, **k: {"exp-ref", "exp-ca"}
    )

    def handle_refresh(name):
        if name == "exp-ref":
            raise RuntimeError("refresh err")
        if name == "exp-ca":
            (edir_ca / "meta.json").write_text("corrupted json now")

    monkeypatch.setattr(strategy_experiment, "refresh_evaluation_result", handle_refresh)

    # pair review failure with side effect error raising OSError (lines 2426-2427)
    edir_pf = exp_dir / "exp-pf"
    edir_pf.mkdir()
    meta_pf = {
        "repo": "o/r",
        "exp_id": "exp-pf",
        "base": "main",
        "members": [
            {"arm_id": "a1", "member_id": "impl1", "agent": "claude", "legacy": False},
            {
                "arm_id": "a1",
                "member_id": "rev1",
                "agent": "vibe",
                "legacy": False,
                "role": "review",
                "review_of_member_id": "impl1",
            },
        ],
    }
    (edir_pf / "meta.json").write_text(json.dumps(meta_pf))
    (edir_pf / "spec.md").write_text("spec")

    monkeypatch.setattr(
        exp_abcd,
        "launch_pending_reviews",
        lambda *a, **k: {
            "failed": [{"reason": "pair-review-failed"}],
            "pending": [],
            "launched": [],
        },
    )

    import fleet_shapes

    orig_write = fleet_shapes.write_json_atomic
    atomic_calls = 0

    def fail_second_write(path, payload):
        nonlocal atomic_calls
        if "followup-skip.json" in str(path):
            atomic_calls += 1
            if atomic_calls == 2:
                raise OSError("disk full on second atomic write")
        return orig_write(path, payload)

    monkeypatch.setattr(fleet_shapes, "write_json_atomic", fail_second_write)

    # old meta (line 2317)
    edir_old = exp_dir / "exp-old"
    edir_old.mkdir()
    (edir_old / "meta.json").write_text(
        json.dumps({"repo": "o/r", "exp_id": "exp-old", "agents": ["claude"]})
    )
    (edir_old / "spec.md").write_text("spec")
    os.utime(edir_old / "meta.json", (100, 100))

    # corrupt meta (lines 2298-2299, 2320-2321)
    edir_bad = exp_dir / "exp-bad"
    edir_bad.mkdir()
    (edir_bad / "meta.json").write_text("corrupt")
    (edir_bad / "spec.md").write_text("spec")

    # invalid members metadata (lines 2392-2393)
    edir_inv = exp_dir / "exp-inv"
    edir_inv.mkdir()
    (edir_inv / "meta.json").write_text(
        json.dumps({"repo": "o/r", "exp_id": "exp-inv", "members": ["not-a-dict"]})
    )
    (edir_inv / "spec.md").write_text("spec")

    # missing logs (line 2440)
    edir_nolog = exp_dir / "exp-nolog"
    edir_nolog.mkdir()
    (edir_nolog / "meta.json").write_text(
        json.dumps({"repo": "o/r", "exp_id": "exp-nolog", "agents": ["claude"]})
    )
    (edir_nolog / "spec.md").write_text("spec")

    exp_abcd.followup(
        max_age_days=0.01,
        subject_lifecycle_fn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("life fail")),
    )
    assert (edir_ref / "strategy-result-error.json").exists()

    # missing spec skip cap (lines 2327-2328)
    exp_dir_miss = tmp_path / "exp_miss"
    exp_dir_miss.mkdir()
    monkeypatch.setattr(exp_abcd, "EXP_DIR", exp_dir_miss)
    monkeypatch.setattr(spec_provenance, "is_missing_spec", lambda m, s: True)
    for i in range(12):
        ed = exp_dir_miss / f"exp-miss-{i}"
        ed.mkdir()
        (ed / "meta.json").write_text(json.dumps({"repo": "o/r", "exp_id": f"exp-miss-{i}"}))
        (ed / "spec.md").write_text("stub")

    res = exp_abcd.followup(
        max_experiments=1,
        subject_lifecycle_fn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("life fail")),
    )
    capped = [s for s in res["skipped"] if s.get("reason") == "missing-spec-skip-cap"]
    assert len(capped) > 0


def test_followup_evaluation_and_redirect_corpus(tmp_path, monkeypatch):
    # lines 2483-2484, 2517, 2520, 2523-2527, 2530, 2533, 2540-2541, 2544, 2563-2564, 2571-2572, 2618-2619, 2637-2638, 2640-2641, 2645-2646
    exp_dir = tmp_path / "experiments"
    exp_dir.mkdir()
    monkeypatch.setattr(exp_abcd, "EXP_DIR", exp_dir)
    monkeypatch.setattr(exp_abcd.time, "time", lambda: 10000.0)
    monkeypatch.setattr(exp_abcd, "ship_gate_stamp", lambda: tmp_path / "nonexistent_stamp")

    edir = exp_dir / "exp-eval"
    edir.mkdir()
    meta = {
        "repo": "o/r",
        "exp_id": "exp-eval",
        "base": "main",
        "agents": ["claude"],
    }
    (edir / "meta.json").write_text(json.dumps(meta))
    (edir / "spec.md").write_text(spec_text)
    (edir / "spec.md").write_text("spec content")
    log = edir / exp_abcd.exp_log_path("claude")
    log.write_text("done")
    os.utime(log, (100, 100))

    # 1. No nonempty diffs (lines 2483-2484)
    exp_abcd.followup(
        collect_fn=lambda *a: {"diffs": {}},
        subject_lifecycle_fn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("life fail")),
    )
    assert (edir / "followup-skip.json").exists()
    (edir / "followup-skip.json").unlink()

    # Provide diff file
    def collect_diff(*a):
        return {"diffs": {"claude": {"path": str(edir / "diff.patch"), "bytes": 10}}}

    (edir / "diff.patch").write_text("diff")

    # 2. opp decision == duplicate (lines 2517, 2520, 2523-2527)
    monkeypatch.setattr(
        research_usage_guard,
        "assess_and_record_opportunity",
        lambda **k: {"decision": "duplicate", "eligible": False},
    )
    exp_abcd.followup(
        collect_fn=collect_diff,
        subject_lifecycle_fn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("life fail")),
    )
    assert (edir / "followup-skip.json").exists()
    (edir / "followup-skip.json").unlink()

    # 3. opp decision not eligible (lines 2530, 2533)
    monkeypatch.setattr(
        research_usage_guard,
        "assess_and_record_opportunity",
        lambda **k: {"decision": "quota_exhausted", "eligible": False},
    )
    res_inelig = exp_abcd.followup(collect_fn=collect_diff)
    assert any(s.get("reason") == "quota_exhausted" for s in res_inelig["skipped"])

    # 4. evaluate_fn raises exception (lines 2540-2541, 2544)
    monkeypatch.setattr(
        research_usage_guard,
        "assess_and_record_opportunity",
        lambda **k: {"decision": "admitted", "eligible": True, "opportunity_id": "opp-99"},
    )
    outcomes: list[tuple] = []
    monkeypatch.setattr(
        research_usage_guard,
        "update_opportunity_outcome",
        lambda opp_id, st, **k: outcomes.append((opp_id, st)),
    )
    with pytest.raises(RuntimeError, match="eval error"):
        exp_abcd.followup(
            collect_fn=collect_diff,
            evaluate_fn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("eval error")),
        )
    assert ("opp-99", "failed") in outcomes

    # 5. Successful evaluate, strategy write error, promotion error, redirect corpus (lines 2563-2564, 2571-2572, 2618-2619, 2637-2638, 2640-2641, 2645-2646)
    monkeypatch.setattr(
        strategy_experiment,
        "write_evaluation_result",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("strat write error")),
    )
    monkeypatch.setenv("ORCH_REDIRECT_SWEEP_RECORD_CORPUS", "1")
    # First: successful recording (line 2641) and reconcile error (lines 2618-2619)
    monkeypatch.setattr(
        redirect_sweep,
        "record_experiment_candidates",
        lambda *a, **k: {"recorded_count": 2, "experiment_actionable": 1},
    )
    res_rec = exp_abcd.followup(
        collect_fn=collect_diff,
        evaluate_fn=lambda *a, **k: {"evaluators": ["claude"]},
        promotion_reconcile_fn=lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("reconcile error")
        ),
        subject_lifecycle_fn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("life fail")),
    )
    assert (edir / "strategy-result-error.json").exists()
    proc_entry = res_rec["processed"][-1]
    assert "reconcile error" in proc_entry.get("ship_gate")
    assert proc_entry["redirect_corpus"] == {"recorded": 2, "actionable": 1}

    # Second: error recording in separate dir (lines 2645-2646)
    exp_dir2 = tmp_path / "exp_dir2"
    exp_dir2.mkdir()
    monkeypatch.setattr(exp_abcd, "EXP_DIR", exp_dir2)
    edir2 = exp_dir2 / "e2"
    edir2.mkdir()
    (edir2 / "meta.json").write_text(
        json.dumps({"repo": "o/r", "exp_id": "e2", "agents": ["claude"]})
    )
    (edir2 / "spec.md").write_text("spec")
    log2 = edir2 / exp_abcd.exp_log_path("claude")
    log2.write_text("done")
    os.utime(log2, (100, 100))
    (edir2 / "diff.patch").write_text("diff")

    def collect_diff2(*a):
        return {"diffs": {"claude": {"path": str(edir2 / "diff.patch"), "bytes": 10}}}

    monkeypatch.setattr(
        redirect_sweep,
        "record_experiment_candidates",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sweep err")),
    )
    res_err = exp_abcd.followup(
        collect_fn=collect_diff2,
        evaluate_fn=lambda *a, **k: {"evaluators": ["claude"]},
        subject_lifecycle_fn=lambda *a, **k: None,
    )
    assert "sweep err" in res_err["processed"][-1]["redirect_corpus"]["error"]


def test_winner_and_harvest_reliability_fallback(monkeypatch):
    # lines 2668, 2669
    import judge_reliability

    monkeypatch.setattr(
        judge_reliability,
        "summarize",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("reliability fail")),
    )
    verdicts = {
        "claude": {"scores": {"A": 9.0}},
        "codex": {"scores": {"A": 8.0}},
    }
    maps = {
        "claude": {"A": "cand1"},
        "codex": {"A": "cand1"},
    }
    harvest = exp_abcd._winner_and_harvest(verdicts, maps, reliability=None)
    assert harvest["winner"] == "cand1"
    assert harvest["winner_mean"] == 8.5


def test_usefulness_gate_and_winner_validation(tmp_path, monkeypatch):
    # lines 2731, 2735-2743, 2750, 2751
    exp_dir = tmp_path / "experiments"
    edir = exp_dir / "exp-gate"
    edir.mkdir(parents=True)
    meta = {
        "repo": "o/r",
        "exp_id": "exp-gate",
        "base": "main",
        "members": [{"arm_id": "a1", "member_id": "mem1", "agent": "claude", "legacy": False}],
    }
    (edir / "meta.json").write_text(json.dumps(meta))
    (edir / "spec.md").write_text(spec_text)
    monkeypatch.setattr(exp_abcd, "EXP_DIR", exp_dir)

    harvest_bad = {
        "winner": "unknown_winner",
        "winner_weaknesses": [],
        "alt_strengths": {},
        "ranking": [],
    }
    with pytest.raises(
        ValueError, match="winner identity missing from experiment metadata: unknown_winner"
    ):
        exp_abcd.usefulness_gate("o/r", "exp-gate", harvest_bad)

    harvest_good = {
        "winner": "mem1",
        "winner_weaknesses": ["too slow"],
        "alt_strengths": {"mem2": ["faster"]},
        "ranking": [("mem1", 9.0)],
    }
    monkeypatch.setattr(
        dispatcher,
        "offload",
        lambda agent, prompt, cwd, mode, timeout: {
            "output": json.dumps(
                {
                    "scores": {},
                    "decision": "use",
                    "base": "mem1",
                    "divergence_risk": "low",
                    "rationale": "solid base",
                }
            )
        },
    )

    res = exp_abcd.usefulness_gate("o/r", "exp-gate", harvest_good)
    assert res == {
        "decision": "use",
        "base": "mem1",
        "divergence_risk": "low",
        "rationale": "solid base",
        "judge": "codex",
    }


def test_resume_synthesis_promotion_launch(tmp_path, monkeypatch):
    # lines 2793-2799, 2816, 2822, 2832
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()

    state = {
        "experiment_id": "exp-res",
        "repo": "o/r",
        "synthesis": {
            "synth_agent": "claude",
            "worktree": str(wt),
            "log": str(tmp_path / "synth.log"),
            "root_run_id": "exp-res:synth",
            "resume_history": [{"run_id": "prior"}],
        },
    }

    monkeypatch.setattr(exp_abcd, "_spawn", lambda *args, **kwargs: 8888)
    monkeypatch.setattr(exp_abcd.feedback, "record_run", lambda *args, **kwargs: None)

    res = exp_abcd._resume_synthesis_promotion(state)
    assert res["pid"] == 8888
    assert res["run_id"] == "exp-res:synth:resume:2"
    assert res["log"].endswith("synth-resume-2-claude.log")


def test_synthesize_paths_and_errors(tmp_path, monkeypatch):
    # lines 2854-2855, 2888-2889, 2891, 2898-2901, 2905-2908, 2910, 2915-2918, 2922-2923, 2926-2927, 2942-2943, 2963-2964, 2974-2975, 2984-2985, 2989
    exp_dir = tmp_path / "experiments"
    edir = exp_dir / "exp-synth"
    edir.mkdir(parents=True)
    monkeypatch.setattr(exp_abcd, "EXP_DIR", exp_dir)

    meta = {
        "repo": "o/r",
        "exp_id": "exp-synth",
        "base": "main",
        "members": [
            {"arm_id": "a1", "member_id": "m1", "agent": "claude", "legacy": False},
            {"arm_id": "a2", "member_id": "m2", "agent": "claude", "legacy": False},
        ],
    }
    (edir / "meta.json").write_text(json.dumps(meta))
    (edir / "spec.md").write_text(spec_text)
    (edir / "eval-maps.json").write_text(json.dumps({"judge1": {"A": "m1", "B": "m2"}}))
    (edir / "eval-out-judge1.txt").write_text(json.dumps({"scores": {"A": 9, "B": 7}}))

    # 1. Invalid prior launch (lines 2854-2855)
    (edir / "synthesis-launch.json").write_text("not-json")

    # 2. _winner_and_harvest raises ValueError (lines 2888-2889)
    monkeypatch.setattr(
        exp_abcd,
        "_winner_and_harvest",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("no eligible evaluators")),
    )
    res2 = exp_abcd.synthesize("o/r", "exp-synth")
    assert res2.get("blocked") is True
    assert res2.get("reason") == "no eligible evaluators"

    # Restore _winner_and_harvest
    monkeypatch.undo()
    monkeypatch.setattr(exp_abcd, "EXP_DIR", exp_dir)

    # 3. Ambiguous base (matching 2 members with agent 'claude') (line 2910)
    gate_ambig = {"decision": "use", "base": "claude"}
    res3 = exp_abcd.synthesize("o/r", "exp-synth", gate=gate_ambig)
    assert res3.get("blocked") is True
    assert "ambiguous synthesis base: claude" in res3.get("reason", "")

    # 4. Spawn failure (lines 2963-2964, 2974-2985)
    gate_valid = {"decision": "use", "base": "m1"}
    monkeypatch.setattr(exp_abcd.feedback, "record_run", lambda *a, **k: None)
    monkeypatch.setattr(
        exp_abcd,
        "_spawn",
        lambda *a, **k: (_ for _ in ()).throw(OSError("spawn synth failed")),
    )
    with pytest.raises(OSError, match="spawn synth failed"):
        exp_abcd.synthesize("o/r", "exp-synth", gate=gate_valid)
    failed_launch = json.loads((edir / "synthesis-launch.json").read_text())
    assert failed_launch.get("status") == "failed"

    # 5. Spawn success (lines 2942-2943, 2989)
    monkeypatch.setattr(exp_abcd, "_spawn", lambda *a, **k: 7777)
    res5 = exp_abcd.synthesize("o/r", "exp-synth", gate=gate_valid)
    assert res5.get("pid") == 7777
    assert res5.get("base") == "m1"

    # 6. gate is None and unique agent match (lines 2891, 2907-2908)
    edir_single = exp_dir / "exp-single"
    edir_single.mkdir()
    meta_single = {
        "repo": "o/r",
        "exp_id": "exp-single",
        "base": "main",
        "members": [{"arm_id": "a1", "member_id": "m1", "agent": "claude", "legacy": False}],
    }
    (edir_single / "meta.json").write_text(json.dumps(meta_single))
    (edir_single / "eval-maps.json").write_text(json.dumps({"judge1": {"A": "m1"}}))
    (edir_single / "eval-out-judge1.txt").write_text(json.dumps({"scores": {"A": 9}}))
    monkeypatch.setattr(
        exp_abcd, "usefulness_gate", lambda *a, **k: {"decision": "use", "base": "claude"}
    )
    monkeypatch.setattr(exp_abcd, "_spawn", lambda *a, **k: 6666)
    res6 = exp_abcd.synthesize("o/r", "exp-single", gate=None)
    assert res6.get("pid") == 6666
    assert res6.get("base") == "m1"


def test_main_cli_commands(monkeypatch):
    # lines 3006-3007, 3010-3012, 3014-3016, 3019, 3023, 3025-3028, 3033, 3048-3049, 3051-3061, 3063-3064, 3071
    assert exp_abcd.main([]) == 2

    monkeypatch.setattr(exp_abcd, "synthesize", lambda repo, exp_id, judge: {"synth": True})
    assert exp_abcd.main(["synthesize", "o/r", "exp1", "codex"]) == 0

    monkeypatch.setattr(
        exp_abcd,
        "prepare",
        lambda repo, spec_file, exp_id, agents, task_type: {"prep": True},
    )
    assert exp_abcd.main(["prepare", "o/r", "spec.md", "exp1", "claude,vibe", "implement"]) == 0

    monkeypatch.setattr(
        exp_abcd,
        "prepare_arms",
        lambda repo, spec_file, exp_id, arms, task_type: {"prep_arms": True},
    )
    assert (
        exp_abcd.main(
            [
                "prepare-arms",
                "o/r",
                "spec.md",
                "exp1",
                '[{"arm_id": "a1", "agents": ["claude"]}]',
                "implement",
            ]
        )
        == 0
    )

    monkeypatch.setattr(exp_abcd, "collect", lambda repo, exp_id: {"collect": True})
    assert exp_abcd.main(["collect", "o/r", "exp1"]) == 0

    monkeypatch.setattr(
        exp_abcd,
        "evaluate",
        lambda repo, spec, exp_id, evs, timeout: {"eval": True},
    )
    assert (
        exp_abcd.main(["evaluate", "o/r", "spec.md", "exp1", "claude,gemini", "--timeout", "600"])
        == 0
    )

    with pytest.raises(SystemExit, match="unknown evaluate argument"):
        exp_abcd.main(["evaluate", "o/r", "spec.md", "exp1", "claude", "extra_arg"])
