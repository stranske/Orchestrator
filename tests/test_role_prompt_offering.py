"""Batch authoring reaches its callers without scoring wrong-moment offers as failures."""

import json

import pytest
from test_role_prompt_batch import BODY, proposal

import capabilities
import capability_advisor as advisor
import capability_propensity as propensity
import dispatcher
import feedback
import roles


def ledger(tmp_path):
    path = tmp_path / "capabilities.json"
    row = capabilities._blank_capability("role-prompt")
    row["status"] = "generated"
    capabilities.save({"role-prompt": row}, path)
    return path


def test_research_program_surface_is_declared_and_binds_role_prompt(tmp_path):
    path = ledger(tmp_path)
    assert "research-program" in advisor.consult_keys()
    assert advisor.CONSULT_SITES["research-program"]["caller"].endswith("driver.py")
    for surface in ("research-program", "repo-audit:phase-4"):
        result = advisor.advise(
            "Author a batch of three issue bodies from verified findings",
            surface=surface,
            path=path,
            record=False,
        )
        offered = {row["capability_id"]: row for row in result["capabilities"]}
        assert "role-prompt" in offered
        assert "role-prompt" in advisor.binding_for(surface, path=path)


def test_wrong_moment_declines_never_demote(tmp_path, monkeypatch):
    path = ledger(tmp_path)
    rows = capabilities.load(path, create=False)
    reasons = ("single-body moment", "one-prompt request", "no-batch work")
    # Old events remain byte-identical: only their read-time interpretation changes.
    for index in range(propensity.DEMOTION_MIN_DECLINES + 1):
        rows["role-prompt"]["event_history"].append(
            {
                "type": "match",
                "ref": f"advice:old-{index}",
                "timestamp": capabilities._now(),
                "metadata": {
                    "source": propensity.DECLINE_SOURCE,
                    "surface": "orchestrate",
                    "decline_kind": "scope_too_small",
                    propensity.DECLINE_REASON_KEY: reasons[index % len(reasons)],
                },
            }
        )
    capabilities.save(rows, path)
    before = path.read_bytes()
    monkeypatch.setattr(propensity, "surface_records", lambda *a, **kw: [])
    report = propensity.detect(path=path)
    assert not [r for r in report["demotions"] if r["capability_id"] == "role-prompt"]
    counts = report["surfaces"]["orchestrate"]
    assert counts["declines_by_kind"]["role-prompt"] == {
        "wrong_moment": propensity.DEMOTION_MIN_DECLINES + 1
    }
    assert counts["declines_demotable"] == {}
    assert path.read_bytes() == before
    assert propensity.DECLINE_KINDS["wrong_moment"]["demotable"] is False
    assert propensity.record_decline(
        "role-prompt",
        "advice:new",
        reason="One body is cheaper by hand",
        kind="scope_too_small",
        surface="orchestrate",
        path=path,
    )
    trial = next(t for t in propensity.experiments(path=path) if t["experiment_id"] == "advice:new")
    assert trial["decline_kinds"]["role-prompt"] == "wrong_moment"
    assert trial["declined_demotable"] == []


def test_a_batch_counts_once_against_the_cycle_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(roles, "_role_capability_event", lambda *a, **kw: None)
    calls = []
    monkeypatch.setattr(
        roles.dispatcher,
        "offload",
        lambda *a, **kw: calls.append(a)
        or {
            "output": json.dumps(proposal()),
            "exit": 0,
            "run_id": f"fake:{len(calls)}",
        },
    )
    roles.reset_role_invocation_counts()
    items = [{"target": f"owner/repo#{i}", "goal": f"Issue {i}"} for i in range(3)]
    kwargs = {"output": "issue_body", "backend": "cursor", "dispatch": True, "env": {}}
    try:
        result = roles.run_prompt_batch(items, **kwargs)
        assert result["selector"]["invocation_ordinal"] == 1
        assert result["selector"]["max_invocations"] == 1
        assert len(calls) == 3
        assert all(i["issue_body"] == BODY and not i["errors"] for i in result["items"])
        assert len({i["role_run_id"] for i in result["items"]}) == 3
        blocked = roles.run_prompt_batch(items, **kwargs)
        assert blocked["selector"]["reason"] == "per_cycle_invocation_cap"
        assert blocked["selector"]["invocation_ordinal"] == 1
        assert len(calls) == 3
        assert all(i["errors"] == ["per_cycle_invocation_cap"] for i in blocked["items"])
        manifest = roles.write_prompt_batch(blocked, tmp_path / "blocked")
        assert all(not i["valid"] and i["body_file"] is None for i in manifest["items"])
        roles.reset_role_invocation_counts()
        assert roles.run_prompt_batch(items, **kwargs)["selector"]["invoked"]
        assert len(calls) == 6
    finally:
        roles.reset_role_invocation_counts()


@pytest.mark.parametrize("setting", [None, "0", "1"])
def test_delegate_logs_unset_role_activation_once_without_changing_default(
    monkeypatch, capsys, setting
):
    monkeypatch.delenv("ORCH_ROLE_SHADOW", raising=False)
    if setting is not None:
        monkeypatch.setenv("ORCH_ROLE_SHADOW", setting)
    monkeypatch.setattr(dispatcher, "delegate", lambda *a, **kw: {"ok": True})
    assert (
        dispatcher.main(
            ["delegate", "--agent", "codex", "--target", "owner/repo#1", "--prompt", "Work"]
        )
        == 0
    )
    assert capsys.readouterr().err == (
        "role activation off (ORCH_ROLE_SHADOW unset)\n" if setting is None else ""
    )
    assert roles._shadow_gate(None) is (setting == "1")
