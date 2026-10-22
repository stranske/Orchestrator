"""Issue #853: hermetic coverage for conservative planning-lane edge paths."""

from __future__ import annotations

import json
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import cross_repo_lane
import dispatcher
import epic_lane
import feedback
import gh_capacity
import research_scheduler

_REAL_FEEDBACK_CONN = feedback._conn
OUT_OF_BOUND_SENTINEL_NAME = "issue853-out-of-bound-sentinel.db"


def _assert_feedback_path_owned(owned_root: Path) -> None:
    selected = Path(feedback.DB_PATH).expanduser().resolve()
    try:
        selected.relative_to(owned_root.resolve())
    except ValueError:
        raise AssertionError(
            f"feedback DB {selected} is outside owned test root {owned_root.resolve()}"
        ) from None


def _guarded_feedback_conn(owned_root: Path, *, timeout: float = 5.0):
    _assert_feedback_path_owned(owned_root)
    return _REAL_FEEDBACK_CONN(timeout=timeout)


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep #853 tests off the live Brain and block process/network/agent side effects."""
    owned = tmp_path / "owned"
    state = owned / "state"
    runtime = owned / "runtime"
    db = runtime / "feedback" / "orchestrator.db"
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv(feedback.DB_PATH_ENV, str(db))
    monkeypatch.setattr(feedback, "DB_PATH", db)
    monkeypatch.setattr(
        feedback,
        "_conn",
        lambda *, timeout=5.0: _guarded_feedback_conn(owned, timeout=timeout),
    )
    attempted: list[object] = []

    def forbidden(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError(
            "issue #853 coverage test started an external process, agent, or network call"
        )

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(gh_capacity, "gh_run", forbidden)
    monkeypatch.setattr(dispatcher, "delegate", forbidden)
    monkeypatch.setattr(dispatcher, "offload", forbidden)
    yield
    assert attempted == []


def test_feedback_containment_rejects_out_of_bound_sentinel_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Named regression: refuse sentinel paths outside the owned root before SQLite opens."""
    owned = tmp_path / "owned"
    owned.mkdir()
    sentinel = (tmp_path / OUT_OF_BOUND_SENTINEL_NAME).resolve()
    assert sentinel.parent == tmp_path.resolve()
    with pytest.raises(ValueError):
        sentinel.relative_to(owned.resolve())

    open_attempts: list[Path] = []

    def forbid_sqlite_open(*, timeout: float = 5.0):
        open_attempts.append(Path(feedback.DB_PATH).expanduser().resolve())
        raise AssertionError("SQLite open attempted before containment check")

    monkeypatch.setitem(globals(), "_REAL_FEEDBACK_CONN", forbid_sqlite_open)
    monkeypatch.setattr(feedback, "DB_PATH", sentinel)
    with pytest.raises(AssertionError, match="outside owned test root"):
        feedback._conn()
    assert open_attempts == [], "containment must reject before any SQLite open"
    assert not sentinel.exists()
    assert not sentinel.parent.joinpath(f"{sentinel.name}-journal").exists()


def test_isolation_binding_deliberate_break_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safe break/restore: without ownership, opener-spy fires; with it, reject before open.

    Never opens a real runtime database — the spy replaces SQLite and the sentinel lives under
    tmp_path only.
    """
    owned = tmp_path / "owned"
    owned.mkdir()
    sentinel = (tmp_path / OUT_OF_BOUND_SENTINEL_NAME).resolve()
    monkeypatch.setattr(feedback, "DB_PATH", sentinel)
    open_attempts: list[Path] = []

    def opener_spy(*, timeout: float = 5.0):
        open_attempts.append(Path(feedback.DB_PATH).expanduser().resolve())
        raise AssertionError("SQLite open attempted before containment check")

    # BREAK: remove the ownership binding; opener-spy must fire (no real SQLite).
    monkeypatch.setitem(globals(), "_REAL_FEEDBACK_CONN", opener_spy)
    monkeypatch.setattr(
        feedback,
        "_conn",
        lambda *, timeout=5.0: _REAL_FEEDBACK_CONN(timeout=timeout),
    )
    with pytest.raises(AssertionError, match="SQLite open attempted before containment check"):
        feedback._conn()
    assert open_attempts == [sentinel]
    assert not sentinel.exists()

    # The named containment expectation ("outside owned test root") fails while unbound.
    open_attempts.clear()
    unbound_failed = False
    try:
        with pytest.raises(AssertionError, match="outside owned test root"):
            feedback._conn()
    except AssertionError:
        unbound_failed = True
    assert unbound_failed, "containment regression must fail when isolation binding is removed"
    assert open_attempts == [sentinel]
    assert not sentinel.exists()

    # RESTORE: ownership check rejects the sentinel before the opener-spy runs.
    open_attempts.clear()
    monkeypatch.setattr(
        feedback,
        "_conn",
        lambda *, timeout=5.0: _guarded_feedback_conn(owned, timeout=timeout),
    )
    with pytest.raises(AssertionError, match="outside owned test root"):
        feedback._conn()
    assert open_attempts == []
    assert not sentinel.exists()


def test_cross_repo_validation_rejects_malformed_plans_and_cli_stays_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise rejection paths without creating branches, PRs, or external work."""
    monkeypatch.setattr(cross_repo_lane, "_capability_heartbeat", lambda: None)
    assert cross_repo_lane.validate_coordination([]) == ["coordination plan must be a JSON object"]
    assert "plan.contract_change is required" in cross_repo_lane.validate_coordination({})

    malformed = cross_repo_lane._valid_plan()
    malformed["coordination"].update(
        {
            "id": "",
            "title": "",
            "goal": "",
            "source_repo": "not-a-repo",
            "constraints": "x",
            "definition_of_done": [],
        }
    )
    malformed["contract_change"].update(
        {
            "summary": "",
            "changed_interfaces": [],
            "source_files": "x",
            "compatibility": "no",
            "migration_notes": [],
        }
    )
    malformed["consumers"] = [
        {
            "repo": "bad",
            "reason": "",
            "sync_manifest_refs": "x",
            "required_changes": [],
            "validation": [],
            "risk_level": "no",
        }
    ]
    malformed["rollout"].update(
        {
            "strategy": "no",
            "branch_prefix": "",
            "pr_mode": "no",
            "merge_order": [],
            "barrier_checks": [],
            "rollback_plan": [],
        }
    )
    malformed["prompts"].update(
        {"source_prompt": "", "consumer_prompt_template": "nope", "review_prompt": ""}
    )
    errors = cross_repo_lane.validate_coordination(malformed)
    assert "coordination.id must be a non-empty string" in errors
    assert "consumers[0].repo must look like owner/repo" in errors
    assert "prompts.consumer_prompt_template must contain '{repo}'" in errors

    valid = cross_repo_lane._valid_plan()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(valid))
    assert cross_repo_lane.main(["--validate", str(plan), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert cross_repo_lane.main(["--plan", str(plan), "--json", "--emit-dispatch-prompts"]) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["dispatch_ready_prompts"][0]["item_id"] == "source"

    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert cross_repo_lane.main(["--validate", str(bad)]) == 2
    assert "Could not read or parse coordination JSON" in capsys.readouterr().err

    goal_context = tmp_path / "context.txt"
    goal_context.write_text("Keep the API compatible.")
    assert (
        cross_repo_lane.main(
            [
                "--goal",
                "Update API",
                "--consumer",
                "owner/consumer",
                "--context-file",
                str(goal_context),
                "--json",
            ]
        )
        == 0
    )
    assert "Keep the API compatible" in json.loads(capsys.readouterr().out)["prompt"]


def test_cross_repo_barriers_and_rollout_dependencies_are_deterministic() -> None:
    assert cross_repo_lane._dependencies_for("source_first", "consumer:one", ["consumer:one"]) == [
        "source"
    ]
    assert cross_repo_lane._dependencies_for("consumers_first", "source", ["consumer:one"]) == [
        "consumer:one"
    ]
    assert cross_repo_lane._dependencies_for("manual", "source", ["consumer:one"]) == []
    errors = cross_repo_lane.validate_deterministic_barriers(
        ["source", "consumer"],
        [
            {
                "id": "duplicate",
                "after": "missing",
                "before": "missing",
                "condition_id": "",
                "extra": True,
            },
            {"id": "duplicate", "after": "source", "before": "source", "condition_id": "ok"},
            "not-a-barrier",
        ],
    )
    assert "duplicate barrier ID" in errors
    assert "barriers[1] cannot order a step against itself" in errors
    assert "barriers[2] must be an object" in errors


def test_research_scheduler_conservatively_handles_unavailable_or_invalid_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        research_scheduler.feedback,
        "current_weights",
        lambda _task: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    assert research_scheduler.route_n_obs("implement", ["codex"]) == {"codex": 0}
    assert research_scheduler.route_n_obs("implement", []) == {}
    assert research_scheduler.route_n_obs(
        "implement", ["codex"], weights=[{"agent": "codex", "n_obs": "bad"}]
    ) == {"codex": 0}
    assert research_scheduler.data_hunger("implement", []) == 0.0
    # Controlled temp SQLite (fixture-owned): real _agent_acquisition_scores, not a stub.
    owned_scores = research_scheduler._agent_acquisition_scores(
        "implement", ["codex"], n_obs={"codex": "bad"}
    )
    assert owned_scores["codex"] >= 0
    assert Path(feedback.DB_PATH).resolve().is_relative_to((tmp_path / "owned").resolve())

    # Intentional offline connection failure at the _conn boundary (still not a stub).
    def offline_conn(*, timeout: float = 5.0):
        raise OSError("feedback offline for issue853 isolation proof")

    monkeypatch.setattr(feedback, "_conn", offline_conn)
    offline_scores = research_scheduler._agent_acquisition_scores(
        "implement", ["codex"], n_obs={"codex": 0}
    )
    assert offline_scores["codex"] >= 0
    with pytest.raises(ValueError, match="identified"):
        research_scheduler._validate_hypotheses([{}])
    with pytest.raises(ValueError, match="duplicate"):
        research_scheduler._validate_hypotheses([{"id": "same"}, {"id": "same"}])
    assert research_scheduler.learned_posteriors(
        "implement", ["codex"], {"implement": {"codex": {"posterior": "bad"}}}
    ) == {"codex": 0.5}
    assert research_scheduler._hypothesis_uncertainty({"evidence": {"posterior": "bad"}}) == 1.0
    assert "--arms-json" in research_scheduler._strategy_experiment_command_template(None)


def test_research_candidate_skip_reasons_preserve_no_launch_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(research_scheduler, "should_test", lambda *_args, **_kwargs: None)
    skipped: list[dict] = []
    jobs = research_scheduler.research_job_candidates(
        [
            {"target": "o/r#1", "lane": "closer"},
            {"target": "bad", "lane": "opener"},
            {"target": "o/r#2", "lane": "opener"},
        ],
        {"codex": 2},
        [],
        excluded_targets={"o/r#2"},
        skipped=skipped,
    )
    assert jobs == []
    assert [row["reason"] for row in skipped] == [
        "not_opener",
        "invalid_target",
        "production_reserved",
    ]


def test_epic_lane_rejects_invalid_graphs_and_cli_only_emits_plans(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(epic_lane, "_capability_heartbeat", lambda: None)
    assert epic_lane.validate_plan([]) == ["plan must be a JSON object"]
    assert "plan.subtasks is required" in epic_lane.validate_plan({})
    valid = epic_lane._valid_plan()
    broken = json.loads(json.dumps(valid))
    broken["epic"].update(
        {"title": "", "goal": "", "repo": 3, "constraints": "bad", "definition_of_done": []}
    )
    broken["subtasks"] = [
        {
            "id": "",
            "title": "",
            "lane": "bad",
            "task_type": "bad",
            "scope": "",
            "files": "bad",
            "acceptance": [],
            "validation": [],
            "dependencies": ["missing"],
            "dispatch_prompt": "",
            "redecompose_if": [],
        }
    ]
    broken["integration"].update(
        {"order": ["missing", "missing"], "risks": "bad", "final_verification": []}
    )
    broken["re_decomposition_triggers"] = []
    errors = epic_lane.validate_plan(broken)
    assert "epic.title must be a non-empty string" in errors
    assert "subtasks[0].lane must be one of" in "\n".join(errors)
    assert "integration.order must not contain duplicate subtask IDs" in errors

    assert epic_lane.validate_dependency_dag([{"id": "a", "depends_on": ["a"]}]) == [
        "cycle detected"
    ]
    assert "duplicate DAG node ID" in epic_lane.validate_dependency_dag(
        [{"id": "a", "depends_on": []}, {"id": "a", "depends_on": []}]
    )
    assert epic_lane.dependency_order(
        [{"id": "b", "depends_on": ["a"]}, {"id": "a", "depends_on": []}]
    ) == ["a", "b"]
    with pytest.raises(ValueError, match="invalid plan"):
        epic_lane.build_dispatch_prompts(broken)

    plan = tmp_path / "epic.json"
    plan.write_text(json.dumps(valid))
    assert epic_lane.main(["--validate", str(plan), "--json", "--emit-dispatch-prompts"]) == 0
    assert json.loads(capsys.readouterr().out)["dispatch_prompts"][0]["id"] == "E1"
    assert epic_lane.main(["--goal", "Improve reporting", "--subtask-count", "2", "--json"]) == 0
    assert "Target subtask count: 2" in json.loads(capsys.readouterr().out)["prompt"]
    assert epic_lane._positive_int("1") == 1
    with pytest.raises(Exception, match="must be >= 1"):
        epic_lane._positive_int("0")
