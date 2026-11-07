"""Hermetic unit tests for strategy_experiment arm normalization, cost gaps, and CLI guards."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

import capabilities
import exp_abcd
import feedback
import strategy_experiment as se


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    exp_dir = tmp_path / "experiments"
    state.mkdir()
    runtime.mkdir()
    exp_dir.mkdir()

    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.delenv("ORCH_STRATEGY_EXPERIMENT", raising=False)

    monkeypatch.setattr(exp_abcd, "EXP_DIR", exp_dir)
    monkeypatch.setattr(feedback, "DB_PATH", runtime / "feedback.db")
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(capabilities, "production_heartbeat", lambda *a, **k: None)

    attempted: list[tuple] = []

    def forbidden(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("strategy_experiment tests must not spawn processes or sockets")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    yield tmp_path
    assert attempted == [], f"unexpected external calls: {attempted}"


def test_normalize_arm_enforces_single_parallel_and_pair_agent_arity() -> None:
    with pytest.raises(ValueError, match="single strategy must name exactly one agent"):
        se.normalize_arm({"strategy": "single", "agents": ["claude", "cursor"]})

    with pytest.raises(ValueError, match="parallel strategy must name at least two agents"):
        se.normalize_arm({"strategy": "parallel", "agents": ["claude"]})

    with pytest.raises(ValueError, match="pair strategy must name implementer and reviewer"):
        se.normalize_arm({"strategy": "pair", "agents": ["claude"]})

    pair = se.normalize_arm(
        {
            "strategy": "pair",
            "agents": ["codex", "gemini"],
            "member_profiles": {"codex": "codex-high", "gemini": "gemini-pro"},
        },
        index=1,
    )
    assert pair["arm_id"] == "arm-02-pair-codex-gemini"
    assert pair["cost_basis"] == "sum_agent_runs"
    assert pair["members"][0]["role"] == "implement"
    assert pair["members"][0]["profile_id"] == "codex-high"
    assert pair["members"][1]["role"] == "review"
    assert pair["members"][1]["profile_id"] == "gemini-pro"
    assert pair["members"][1]["review_of_member_id"] == pair["members"][0]["member_id"]


@pytest.mark.parametrize("invalid_cost", [float("nan"), -1.0, "invalid"])
def test_strategy_arm_costs_withholds_total_when_any_attempt_is_unmeasured(
    isolated_runtime: Path,
    invalid_cost: float | str,
) -> None:
    spec = isolated_runtime / "spec.md"
    spec.write_text("frozen experiment spec\n", encoding="utf-8")
    plan = se.build_strategy_plan(
        "stranske/Workflows",
        str(spec),
        "exp-cost-check",
        [
            {"strategy": "single", "agents": ["claude"]},
            {"strategy": "parallel", "agents": ["codex", "cursor"], "synthesize": True},
        ],
        exp_dir=isolated_runtime / "experiments",
    )
    complete_source = next(iter(feedback.COMPLETE_COST_SOURCES))
    parallel_arm = plan["arms"][1]
    attempt_ids = parallel_arm["attempt_run_ids"]
    assert len(attempt_ids) == 3

    partial = se.strategy_arm_costs(
        plan,
        [
            {
                "run_id": attempt_ids[0],
                "tokens_in": 100,
                "tokens_out": 40,
                "cost_usd": 1.25,
                "source": complete_source,
                "latency_s": 3.0,
            },
            {
                "run_id": attempt_ids[1],
                "tokens_in": 80,
                "tokens_out": 20,
                "cost_usd": 0.75,
                "source": "estimated_incomplete",
                "latency_s": 2.0,
            },
        ],
    )[parallel_arm["arm_id"]]
    assert partial["cost_usd"] is None
    assert partial["missing_attempt_run_ids"] == [attempt_ids[2]]
    assert partial["unmeasured_attempt_run_ids"] == [attempt_ids[1], attempt_ids[2]]

    complete = se.strategy_arm_costs(
        plan,
        [
            {
                "run_id": rid,
                "tokens_in": 50,
                "tokens_out": 10,
                "cost_usd": 0.5,
                "source": complete_source,
                "latency_s": 1.5,
            }
            for rid in attempt_ids
        ],
    )[parallel_arm["arm_id"]]
    assert complete["cost_usd"] == 1.5
    assert complete["unmeasured_attempt_run_ids"] == []

    invalid = se.strategy_arm_costs(
        plan,
        [
            {
                "run_id": rid,
                "cost_usd": invalid_cost if rid == attempt_ids[0] else 0.5,
                "source": complete_source,
            }
            for rid in attempt_ids
        ],
    )[parallel_arm["arm_id"]]
    assert invalid["cost_usd"] is None
    assert attempt_ids[0] in invalid["unmeasured_attempt_run_ids"]


def test_main_guards_active_prepare_and_formats_readonly_plan(
    isolated_runtime: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = isolated_runtime / "spec.md"
    spec.write_text("frozen spec\n", encoding="utf-8")
    arms_json = json.dumps(
        [{"strategy": "single", "agents": ["claude"]}, {"parallel": ["codex", "cursor"]}]
    )

    called: list[tuple] = []

    def fake_prepare(repo: str, spec_file: str, exp_id: str, arms: list[dict]) -> dict:
        called.append((repo, spec_file, exp_id, len(arms)))
        return {"exp_id": exp_id, "repo": repo, "launched": []}

    monkeypatch.setattr(exp_abcd, "prepare_arms", fake_prepare)
    monkeypatch.setitem(se.prepare_strategy_experiment.__kwdefaults__, "prepare_fn", fake_prepare)

    rc = se.main(
        [
            "--repo",
            "stranske/Workflows",
            "--spec-file",
            str(spec),
            "--exp-id",
            "exp-cli-1",
            "--arms-json",
            arms_json,
        ]
    )
    assert rc == 0
    assert "Strategy experiment plan: exp-cli-1" in capsys.readouterr().out
    assert called == []

    refused = se.main(
        [
            "--repo",
            "stranske/Workflows",
            "--spec-file",
            str(spec),
            "--exp-id",
            "exp-cli-1",
            "--arms-json",
            arms_json,
            "--prepare",
        ]
    )
    assert refused == 2
    assert "--prepare requires --confirm-strategy" in capsys.readouterr().err
    assert called == []

    monkeypatch.setenv("ORCH_STRATEGY_EXPERIMENT", "1")
    active = se.main(
        [
            "--repo",
            "stranske/Workflows",
            "--spec-file",
            str(spec),
            "--exp-id",
            "exp-cli-1",
            "--arms-json",
            arms_json,
            "--prepare",
            "--confirm-strategy",
            "--json",
        ]
    )
    assert active == 0
    payload = json.loads(capsys.readouterr().out)
    assert called == [("stranske/Workflows", str(spec.resolve()), "exp-cli-1", 2)]
    assert Path(payload["metadata_written"]).is_file()
