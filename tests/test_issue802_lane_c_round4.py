"""Lane C round-4 coverage for Orchestrator #802 CI leftovers.

After rounds 1–3, CI combined coverage left 10 statements on the five targets.
This module covers the reachable ones hermetically:

- exploration_backfill: backup-agent continues (96/240) via stubbed `_route_agents`,
  duplicate-agent skip in `_build_jobs` (299), `_selftest` HANDOFF_DIR restore (823)
- ux_review: argparse `--selftest` branch (1662–63) after the early-argv short-circuit

Unreachable without restructuring (documented in the PR body):
- redirect_apply selftest tripwire bodies (1398/1582–83/1675) — only execute when an
  invariant is already broken
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import exploration_backfill as eb
import feedback
import ux_review as ur


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """Keep state + learner paths inside the test temp dir; never spawn agents."""
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    db = runtime / "feedback" / "orchestrator.db"
    db.parent.mkdir(parents=True)
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(db))
    monkeypatch.setattr(feedback, "DB_PATH", db)
    started: list = []

    def forbid(*a, **k):
        started.append((a, k))
        raise AssertionError(f"subprocess must not start: {a!r}")

    monkeypatch.setattr("subprocess.run", forbid)
    monkeypatch.setattr("subprocess.Popen", forbid)
    yield
    assert started == []


# ---------------------------------------------------------------------------
# exploration_backfill — backup-agent continues + duplicate-agent skip
# ---------------------------------------------------------------------------


def test_exploration_backfill_missing_cells_skips_backup_agents(monkeypatch) -> None:
    """Line 96: `if agent in BACKUP_AGENTS: continue` — defensive after route filter."""
    # Real `_route_agents` already drops backups; stub a list that still includes one so
    # the continue is reachable and discriminating.
    monkeypatch.setattr(
        eb, "_route_agents", lambda task_type, route_table: ["codex", "aider", "claude"]
    )
    coverage = {
        "tasks": [
            {
                "task_type": "implement",
                "ready_for_default_review": False,
                "agent_observations": {"codex": 0, "aider": 0, "claude": 0},
                "observed_agents": 0,
                "needed_total_observations": 5,
                "top_agents": [],
            }
        ]
    }
    cells = eb._missing_cells(coverage, route_table={})
    agents = {c["agent"] for c in cells}
    assert "aider" not in agents
    assert agents == {"codex", "claude"}


def test_exploration_backfill_anchor_skips_backup_then_falls_back(monkeypatch) -> None:
    """Line 240: backup continue inside `_anchor_agent`'s first loop."""
    monkeypatch.setattr(
        eb, "_route_agents", lambda task_type, route_table: ["aider", "muse", "codex"]
    )
    # All preferred agents shed → first loop skips backups then finds none available;
    # second loop still returns a non-backup outside missing_agents.
    anchor = eb._anchor_agent(
        "implement",
        missing_agents=["claude"],
        states={"aider": "ok", "muse": "ok", "codex": "shed"},
        route_table={},
    )
    assert anchor == "codex"


def test_exploration_backfill_build_jobs_skips_duplicate_agent() -> None:
    """Line 299: skip when the same agent already appears in missing_agents."""
    route = {"implement": {"agents": [{"agent": "codex"}, {"agent": "claude"}]}}
    cells = [
        {
            "task_type": "implement",
            "agent": "codex",
            "observations": 0,
            "target_observations": 1,
            "needed_observations": 1,
            "reasons": ["zero_observation_cell"],
        },
        {
            "task_type": "implement",
            "agent": "codex",  # duplicate — must hit `if agent in missing_agents: continue`
            "observations": 0,
            "target_observations": 1,
            "needed_observations": 1,
            "reasons": ["zero_observation_cell"],
        },
        {
            "task_type": "implement",
            "agent": "claude",
            "observations": 0,
            "target_observations": 1,
            "needed_observations": 1,
            "reasons": ["zero_observation_cell"],
        },
    ]
    items = [{"target": "o/r#1", "task_type": "implement", "lane": "opener", "title": "T"}]
    jobs, blocked = eb._build_jobs(
        cells=cells,
        items=items,
        capacity_snapshot={"agents": {"codex": {"state": "ok"}, "claude": {"state": "ok"}}},
        route_table=route,
        max_jobs=1,
    )
    assert blocked == []
    assert len(jobs) == 1
    assert jobs[0]["agents"].count("codex") == 1
    assert "claude" in jobs[0]["agents"]
    assert len(set(jobs[0]["agents"])) == len(jobs[0]["agents"])


def test_exploration_backfill_selftest_restores_prior_handoff_dir(monkeypatch, tmp_path) -> None:
    """Line 823: else branch that writes old HANDOFF_DIR back after selftest."""
    import os

    prior = str(tmp_path / "prior-handoff")
    monkeypatch.setenv("HANDOFF_DIR", prior)
    # Selftest is offline and uses its own temp DB; forbid still guards subprocess.
    eb._selftest()
    assert os.environ.get("HANDOFF_DIR") == prior


# ---------------------------------------------------------------------------
# ux_review — argparse selftest branch after early-argv return
# ---------------------------------------------------------------------------


def test_ux_review_argparse_selftest_branch(monkeypatch) -> None:
    """Lines 1662–63: `if args.selftest` after the `"--selftest" in argv` short-circuit.

    Production always hits the early return for a literal `--selftest` token. Reach the
    argparse branch by omitting that token and stubbing parse_args.
    """
    called: list[str] = []

    def fake_selftest() -> None:
        called.append("selftest")

    monkeypatch.setattr(ur, "_selftest", fake_selftest)

    def fake_parse_args(self, argv=None):  # noqa: ANN001
        return SimpleNamespace(
            selftest=True,
            backfill_panels=False,
            apply=False,
            bundle=None,
            evaluators=None,
            adversary="claude",
            timeout=1500,
        )

    monkeypatch.setattr(ur.argparse.ArgumentParser, "parse_args", fake_parse_args)
    # argv without the literal `--selftest` token so the early check does not return.
    assert ur.main(["--bundle-skip"]) == 0
    assert called == ["selftest"]
