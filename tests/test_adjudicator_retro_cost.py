"""Native token transport and honest single-scale shadow cost reporting."""

import json
import sqlite3
import subprocess
import time

import pytest
import test_adjudicator_retro as retro_tests
import test_offload_log_per_run as offload_tests

import adjudicator_retro as retro
import dispatcher
import feedback
import roles

private_brain = retro_tests.private_brain
stores = offload_tests.stores
evidence = retro_tests.evidence
proposal = retro_tests.proposal


def _dispatch(monkeypatch, private_brain, tmp_path, usage=None):
    monkeypatch.setattr(roles, "route_role", lambda *a, **kw: {"agent": "gemini"})
    monkeypatch.setattr(roles, "_role_capability_event", lambda *a, **kw: None)
    monkeypatch.setattr(
        roles.dispatcher,
        "offload",
        lambda *a, **kw: {
            "run_id": "backend-one",
            "model": "native-model",
            "usage": usage or {},
            "output": json.dumps(proposal()),
            "exit": 0,
        },
    )
    path = tmp_path / "report.json"
    return retro.run(dispatch=True, path=path, db=private_brain, evidence_reader=evidence), path


def _seed_pool(agent, costs, *, unmeasured=0, source="ccusage"):
    with feedback._conn() as conn:
        for i, cost in enumerate(costs):
            rid = f"pool:{agent}:{i}"
            conn.execute(
                "INSERT INTO runs(run_id,ts,agent) VALUES (?,?,?)", (rid, int(time.time()), agent)
            )
            conn.execute(
                "INSERT INTO costs(run_id,cost_usd,source,tokens_in) VALUES (?,?,?,3000)",
                (rid, cost, source),
            )
        for i in range(unmeasured):
            conn.execute(
                "INSERT INTO runs(run_id,ts,agent) VALUES (?,?,?)",
                (f"dark:{agent}:{i}", int(time.time()), agent),
            )


def test_dispatch_records_measured_cost_when_the_adapter_reports_usage(
    private_brain, tmp_path, monkeypatch
):
    result, _ = _dispatch(
        monkeypatch, private_brain, tmp_path, {"input_tokens": 4000, "output_tokens": 200}
    )
    rid = result["rows"][0]["role_run_id"]
    with sqlite3.connect(private_brain) as conn:
        metadata = json.loads(
            conn.execute("SELECT decomposition FROM runs WHERE run_id=?", (rid,)).fetchone()[0]
        )
        tokens = conn.execute(
            "SELECT tokens_in,tokens_out,source FROM costs WHERE run_id=?", (rid,)
        ).fetchone()
    assert metadata["cost_tokens_in"] == 4000
    assert metadata["cost_tokens_out"] == 200
    assert metadata["imputed"] is False
    assert tokens == (4000, 200, "ledger")
    # Measured token effort does not invent a dollar price or satisfy complete-source USD.
    assert result["summary"]["cost_measured_cases"] == 0
    assert result["rows"][0]["cost_report"]["status"] == "unmeasured"


def test_missing_usage_is_imputed_and_flagged_never_null(private_brain, tmp_path, monkeypatch):
    _seed_pool("gemini", [2.0, 4.0])
    result, path = _dispatch(monkeypatch, private_brain, tmp_path)
    row = result["rows"][0]
    with sqlite3.connect(private_brain) as conn:
        metadata = json.loads(
            conn.execute(
                "SELECT decomposition FROM runs WHERE run_id=?", (row["role_run_id"],)
            ).fetchone()[0]
        )
    assert metadata["imputed"] is True
    assert row["cost_report"] == {
        "scale": feedback.COST_SCALE,
        "status": "imputed",
        "cost_usd": 3.0,
        "imputed": True,
        "source": "agent_global",
    }
    assert result["summary"]["cost_usd"] is None
    assert result["summary"]["estimated_cost_usd"] == 3.0
    line = retro.weekly_line(path)
    assert "cost measured 0 of 1, imputed 1, unmeasured 0" in line
    assert "null" not in line


def test_no_eligible_pool_names_unmeasured_without_fabricating_free_cost(
    private_brain, tmp_path, monkeypatch
):
    result, path = _dispatch(monkeypatch, private_brain, tmp_path)
    assert result["rows"][0]["cost_report"]["source"] == "no_eligible_pool"
    assert result["summary"]["estimated_cost_usd"] is None
    assert "cost UNKNOWN" in retro.weekly_line(path)
    assert "imputed 0, unmeasured 1" in retro.weekly_line(path)


@pytest.mark.parametrize("source,unmeasured", [("langsmith", 0), ("ledger", 0), ("ccusage", 10)])
def test_partial_and_sparse_pools_do_not_enter_cost_scale(private_brain, source, unmeasured):
    _seed_pool("gemini", [0.01], source=source, unmeasured=unmeasured)
    assert feedback.role_cost_report("missing", "gemini")["status"] == "unmeasured"


def test_imputation_uses_existing_global_median_when_own_pool_is_absent(private_brain):
    _seed_pool("codex", [2.0, 4.0, 6.0])
    report = feedback.role_cost_report("missing", "gemini")
    assert report["cost_usd"] == 4.0
    assert report["source"] == "global_median"


def test_late_native_dollars_replace_estimate_without_redispatch(
    private_brain, tmp_path, monkeypatch
):
    _seed_pool("gemini", [3.0])
    result, path = _dispatch(monkeypatch, private_brain, tmp_path)
    assert result["summary"]["cost_imputed_cases"] == 1
    with sqlite3.connect(private_brain) as conn:
        conn.execute("UPDATE costs SET cost_usd=1.25,source='ccusage' WHERE run_id='backend-one'")
    result = retro.run(dispatch=False, limit=0, path=path, db=private_brain)
    assert result["rows"][0]["cost_report"]["imputed"] is False
    assert result["summary"]["cost_usd"] == 1.25
    assert result["summary"]["cost_imputed_cases"] == 0
    assert result["summary"]["cost_measured_cases"] == 1


def test_record_only_retry_preserves_richer_role_receipt(private_brain):
    feedback.record_role_run(
        "role-one", "adjudicator", "o/r#1", "gemini", cost_tokens_in=10, cost_tokens_out=2
    )
    with feedback._conn() as conn:
        conn.execute(
            "UPDATE costs SET cost_usd=7,source='ccusage',tokens_in=8000 WHERE run_id='role-one'"
        )
    feedback.record_role_run(
        "role-one", "adjudicator", "o/r#1", "gemini", cost_tokens_in=10, cost_tokens_out=2
    )
    with sqlite3.connect(private_brain) as conn:
        assert conn.execute(
            "SELECT cost_usd,source,tokens_in FROM costs WHERE run_id='role-one'"
        ).fetchone() == (7.0, "ccusage", 8000)


@pytest.mark.parametrize("value", [-1, True, 1.5, "12"])
def test_invalid_token_counts_are_rejected_before_recording(private_brain, value):
    with pytest.raises(ValueError):
        feedback.record_role_run("bad", "adjudicator", "o/r#1", "gemini", cost_tokens_in=value)
    with sqlite3.connect(private_brain) as conn:
        assert not conn.execute("SELECT 1 FROM runs WHERE run_id='bad'").fetchone()


@pytest.mark.parametrize(
    "agent,event_type,argv",
    [
        ("cursor", "result", ["cursor-agent", "--output-format", "stream-json"]),
        ("codex", "turn.completed", ["codex", "exec", "--json"]),
    ],
)
def test_offload_preserves_native_terminal_usage_before_result_reduction(
    stores, monkeypatch, tmp_path, agent, event_type, argv
):
    monkeypatch.setattr(dispatcher.adapters, "build_command", lambda *a, **kw: argv)
    events = [
        {"type": "system", "subtype": "init", "model": "native-model"},
        {"type": "assistant", "usage": {"input_tokens": 99999}},
        {
            "type": event_type,
            "result": "done",
            "usage": {"input_tokens": 4000, "output_tokens": 200},
        },
    ]
    if agent == "codex":
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            events[-1],
        ]
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[:2] == ["bash", "-lc"]:
            return subprocess.CompletedProcess(
                cmd, 0, "\n".join(json.dumps(x, separators=(",", ":")) for x in events), ""
            )
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = dispatcher.offload(agent, "Review a bounded case", cwd=str(tmp_path))
    assert result["usage"] == {"input_tokens": 4000, "output_tokens": 200}
    assert ("agent_message" in result["output"]) if agent == "codex" else result["output"] == "done"


@pytest.mark.parametrize("use_pool", [False, True])
def test_zero_dollar_receipt_is_not_measured(private_brain, use_pool):
    if use_pool:
        _seed_pool("gemini", [3.0])
    feedback.record_role_run("zero", "adjudicator", "o/r#1", "gemini")
    with feedback._conn() as conn:
        conn.execute(
            "INSERT INTO costs VALUES (?,?,?,?,?,?,?)",
            ("zero", 10, 2, 0.0, 0.0, "ccusage", int(time.time())),
        )
    report = feedback.role_cost_report("zero", "gemini", db=private_brain)
    assert report["status"] == ("imputed" if use_pool else "unmeasured")
    assert report["cost_usd"] == (3.0 if use_pool else None)


@pytest.mark.parametrize("agent", ["codex", "claude", "cursor", "gemini"])
def test_plain_deliverable_json_is_not_native_usage(stores, monkeypatch, tmp_path, agent):
    text = json.dumps({"type": "result", "usage": {"input_tokens": 4000, "output_tokens": 200}})
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[:2] == ["bash", "-lc"]:
            return subprocess.CompletedProcess(cmd, 0, text, "")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = dispatcher.offload(agent, "Review a bounded case", cwd=str(tmp_path))
    assert result["usage"] == {}


def test_native_usage_accumulates_across_executed_retry(stores, monkeypatch, tmp_path):
    monkeypatch.setattr(
        dispatcher.adapters, "build_command", lambda *a, **kw: ["codex", "exec", "--json"]
    )
    monkeypatch.setenv("ORCH_OFFLOAD_NETWORK_RETRIES", "1")
    monkeypatch.setenv("ORCH_OFFLOAD_RETRY_BACKOFF_S", "0")
    real_run = subprocess.run
    attempts = []

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[:2] == ["bash", "-lc"]:
            attempts.append(cmd)
            first = len(attempts) == 1
            text = json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 100 if first else 40,
                        "output_tokens": 10 if first else 4,
                    },
                }
            )
            if not first:
                text += "\n" + json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}
                )
            return subprocess.CompletedProcess(
                cmd, 1 if first else 0, text, "connection reset by peer" if first else ""
            )
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = dispatcher.offload("codex", "Review a bounded case", cwd=str(tmp_path))
    assert len(attempts) == 2
    assert result["usage"] == {"input_tokens": 140, "output_tokens": 14}
