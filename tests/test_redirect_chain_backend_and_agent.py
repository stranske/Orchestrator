"""Real sweep/role/plan/Brain boundaries; all judgment and state remain offline."""

import json
from pathlib import Path

import pytest

import feedback
import redirect_plan
import redirect_shadow
import redirect_sweep
import roles
import watch

FIXTURES = Path(__file__).parent / "fixtures" / "redirect_stalls"


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.delenv("ORCH_REDIRECT_SWEEP_BACKEND", raising=False)
    monkeypatch.setattr(
        roles.router, "load_capacity", lambda: {"agents": {"codex": {"state": "ok"}}}
    )


def report(name="auth"):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def proposal(action="redirect", agent=None):
    return {
        "action": action,
        "switch_agent": agent,
        "reason": "bounded retry",
        "confidence": "high",
        "corrected_prompt": "Repair the scoped defect; run focused tests, commit, push and open a PR.",
    }


@pytest.mark.parametrize("backend", [None, "", "auto", "AUTO"])
def test_sweep_backend_is_router_chosen_when_unset_or_auto(tmp_path, monkeypatch, backend):
    picked = []
    monkeypatch.setattr(
        roles, "route_role", lambda name, **kw: picked.append(name) or {"agent": "gemini"}
    )
    monkeypatch.setattr(
        roles.dispatcher,
        "offload",
        lambda agent, *a, **kw: {
            "exit": 0,
            "run_id": "offline",
            "output": json.dumps(proposal(agent="codex")),
        },
    )
    result = redirect_sweep.record_shadow_candidates(
        {"actionable": [report()]},
        corpus_path=tmp_path / "corpus.jsonl",
        dispatch=True,
        backend=backend,
    )
    assert result["recorded_count"] == 1, result
    assert picked == ["redirect"]
    row = json.loads((tmp_path / "corpus.jsonl").read_text())
    assert row["backend"] == "gemini"


@pytest.mark.parametrize(
    "backend,env,expected",
    [(None, "codex", "codex"), ("auto", "cursor", None), ("vibe", "cursor", "vibe")],
)
def test_explicit_backend_precedence(monkeypatch, backend, env, expected):
    monkeypatch.setenv("ORCH_REDIRECT_SWEEP_BACKEND", env)
    assert redirect_sweep._selected_backend(backend) == expected


@pytest.mark.parametrize("action", ["redirect", "decompose"])
@pytest.mark.parametrize("fixture", ["auth", "exited", "drift"])
def test_a_redirect_naming_no_agent_gets_a_router_agent_and_no_placeholder(
    monkeypatch, action, fixture
):
    calls = []

    def choose(task_type, cap, **kw):
        calls.append((task_type, kw["only"]))
        return {"agent": "codex"}

    monkeypatch.setattr(roles.router, "select_agent", choose)
    result = roles.run_redirect_agent(
        report(fixture), "focused gate", backend="gemini", proposal_json=proposal(action)
    )
    plan = result["plan"]
    command = plan["steps"][-1]["commands"][0]
    assert command[command.index("--agent") + 1] == "codex"
    assert not redirect_plan._has_placeholder(command)
    assert plan["agent_source"] == "router"
    assert calls[0][0] == "implement"
    assert "claude" not in calls[0][1] and "aider" not in calls[0][1]
    assert result["mutates_state"] is False and plan["dry_run_only"] is True


@pytest.mark.parametrize("action", ["redirect", "decompose"])
@pytest.mark.parametrize("agent", [None, "", " ", "<next-agent>"])
def test_plan_refuses_an_applyable_action_without_an_agent(action, agent):
    r = report()
    r["policy_decision"]["action"] = action
    with pytest.raises(ValueError, match="concrete next agent"):
        redirect_plan.plan(r, next_agent=agent)


@pytest.mark.parametrize("source", ["redirect-sweep-live", "live-dispatch", "historical-replay"])
def test_role_run_records_source_and_report_state(tmp_path, source):
    result = redirect_shadow.record_redirect(
        report(),
        "focused gate",
        backend="codex",
        dispatch=True,
        proposal_json=proposal(agent="codex"),
        source=source,
        corpus_path=tmp_path / "corpus.jsonl",
    )
    rid = result["entry"]["role_run_id"]
    with feedback._conn() as conn:
        raw = conn.execute("SELECT decomposition FROM runs WHERE run_id=?", (rid,)).fetchone()[0]
    assert json.loads(raw)["source"] == source
    assert json.loads(raw)["report_state"] == "stalled"
    assert result["entry"]["source"] == source


def test_no_worker_capacity_returns_inspection_without_apply_commands(monkeypatch):
    monkeypatch.setattr(roles.router, "select_agent", lambda *a, **kw: None)
    result = roles.run_redirect_agent(report(), "gate", backend="codex", proposal_json=proposal())
    assert result["errors"] and result["proposal"]
    assert result["plan"]["action"] == "inspect"
    assert result["plan"]["blocked_action"] == "redirect"
    assert not result["plan"]["apply_supported"] and not result["plan"]["mutates_state"]


def test_explicit_worker_is_preserved(monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("explicit worker must not be rerouted")

    monkeypatch.setattr(roles.router, "select_agent", forbidden)
    result = roles.run_redirect_agent(
        report(), "gate", backend="codex", proposal_json=proposal(agent="vibe")
    )
    assert result["plan"]["agent_source"] == "proposal"
    assert "vibe" in result["plan"]["steps"][-1]["commands"][0]


def test_watch_classification_preserves_redirect_without_placeholder():
    result = watch._finish_report(report(), [])
    assert result["policy_decision"]["action"] == "redirect"
    assert result["redirect_plan"]["blocked_action"] == "redirect"
    assert not result["redirect_plan"]["apply_supported"]


@pytest.mark.parametrize("backend", ["auto", "AUTO"])
def test_experiment_auto_reaches_final_router_despite_cursor_environment(
    tmp_path, monkeypatch, backend
):
    monkeypatch.setenv("ORCH_REDIRECT_SWEEP_BACKEND", "cursor")
    (tmp_path / "codex.log").write_text("synthetic auth failure")
    picked = []
    monkeypatch.setattr(
        roles, "route_role", lambda name, **kw: picked.append(name) or {"agent": "gemini"}
    )
    monkeypatch.setattr(
        roles.dispatcher,
        "offload",
        lambda agent, *a, **kw: {
            "exit": 0,
            "run_id": "offline",
            "output": json.dumps(proposal(agent="codex")),
        },
    )
    corpus = tmp_path / "experiment-corpus.jsonl"
    result = redirect_sweep.record_experiment_candidates(
        "test-exp",
        {"repo": "owner/repo", "agents": ["codex"]},
        tmp_path,
        corpus_path=corpus,
        backend=backend,
        classify_fn=lambda **kw: report(),
    )
    assert result["recorded_count"] == 1, result
    assert picked == ["redirect"]
    assert json.loads(corpus.read_text())["backend"] == "gemini"


@pytest.mark.parametrize("backend", [" ", "\t"])
def test_whitespace_backend_uses_role_router(monkeypatch, backend):
    picked = []
    monkeypatch.setattr(
        roles, "route_role", lambda name, **kw: picked.append(name) or {"agent": "gemini"}
    )
    assert redirect_sweep._selected_backend(backend) is None
    result = roles.run_redirect_agent(
        report(), "gate", backend=backend, proposal_json=proposal(agent="codex")
    )
    assert picked == ["redirect"]
    assert result["backend"] == "gemini"


@pytest.mark.parametrize("next_agent", [" ", "\t"])
def test_whitespace_caller_worker_uses_worker_router(monkeypatch, next_agent):
    picked = []
    monkeypatch.setattr(
        roles.router, "select_agent", lambda *a, **kw: picked.append(a[0]) or {"agent": "codex"}
    )
    result = roles.run_redirect_agent(
        report(), "gate", backend="codex", proposal_json=proposal(), next_agent=next_agent
    )
    assert picked == ["implement"]
    assert result["plan"]["action"] == "redirect"
    assert result["plan"]["agent_source"] == "router"
    assert "codex" in result["plan"]["steps"][-1]["commands"][0]
