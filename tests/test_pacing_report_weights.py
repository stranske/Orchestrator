"""Report the persisted routing decision separately from the next pacing calculation."""

import json

import pytest

import capacity
import pacing
import pacing_usage

START = pacing.timestamp("2026-10-11T05:00:00Z")
NOW = START + pacing.DAY_SECONDS
END = START + pacing.PERIOD_SECONDS
SHED_NOTE = (
    "shed: weight is informational; router uses the state tier "
    "(shed pools only when no ok/warn pool can take the task)"
)


@pytest.fixture
def report_state(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity, "HANDOFF", tmp_path)
    monkeypatch.setattr(capacity.time, "time", lambda: NOW)
    monkeypatch.setattr(pacing_usage, "POLL_DIR", tmp_path / "polls")
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "resets.json")
    monkeypatch.setattr(pacing_usage, "CALIBRATION_FILE", tmp_path / "calibration.json")
    pacing_usage.RESETS_FILE.write_text(json.dumps(dict(codex=[], claude=[])))
    pacing_usage.CALIBRATION_FILE.write_text(
        json.dumps(dict(codex_period_start=START, claude_period_end=END))
    )
    monkeypatch.setattr(
        pacing_usage,
        "usage",
        lambda *a, **k: dict(used_fraction=0.8, ts=NOW, source="fixture", period_end=END),
    )
    agents = {
        pool: dict(
            state="shed",
            pacing=dict(
                state="shed",
                routing_weight=0.451,
                ts=NOW - 120,  # Observation age differs from the routing tick's age.
                period_start=START,
                period_end=END,
            ),
            pacing_diagnostics=dict(previous_routing_weight=0.5),
        )
        for pool in ("codex", "claude")
    }
    (tmp_path / "capacity.json").write_text(
        json.dumps(dict(generated_at=NOW - 60, agents=agents)) + "\n"
    )
    pacing_usage.append_rows(
        tmp_path / "pacing-usage-samples.ndjson",
        [
            dict(agent=pool, ts=NOW - hours * 3600, used_fraction=used, source="fixture")
            for pool in agents
            for hours, used in ((6, 0.6), (3, 0.7))
        ],
    )
    (tmp_path / "capacity-ledger.ndjson").write_text("")
    return tmp_path


def test_pacing_report_prints_persisted_routed_weight_and_shed_note_read_only(
    report_state, capsys, monkeypatch
):
    paths = list(report_state.iterdir())
    before = {p: p.read_bytes() for p in paths}
    real_read = pacing_usage.read_json
    capacity_reads = []

    def read(path):
        if path == report_state / "capacity.json":
            capacity_reads.append(path)
            assert len(capacity_reads) == 1, "report must use one consistent persisted snapshot"
        return real_read(path)

    monkeypatch.setattr(pacing_usage, "read_json", read)
    assert capacity.main(["pacing-report"]) == 0
    output = capsys.readouterr().out
    for pool in ("codex", "claude"):
        line = next(line for line in output.splitlines() if line.startswith(f"pool {pool}: u="))
        assert "state=shed pacing=measured" in line
        assert (
            f"routed_weight=0.451 (tick ts={NOW - 60}, age=60s) "
            "report_weight=0.201 weight_delta=-0.250 "
            "w_p_prev→w_p=0.451→0.201"
        ) in line
        assert line.endswith("; " + SHED_NOTE)
        print(line)
    assert capacity_reads == [report_state / "capacity.json"]
    assert {p: p.read_bytes() for p in report_state.iterdir()} == before


def test_pacing_report_without_persisted_file_is_unmeasured_read_only(report_state, capsys):
    (report_state / "capacity.json").unlink()
    before = {p: p.read_bytes() for p in report_state.iterdir()}
    assert capacity.main(["pacing-report"]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("pool ")]
    pool_lines = [line for line in lines if ": u=" in line]
    assert len(pool_lines) == 2
    for line in pool_lines:
        assert (
            "routed_weight=unmeasured (tick ts=unmeasured, age=unmeasured) "
            "report_weight=0.750 w_p_prev→w_p=unmeasured→0.750 "
            "(hypothetical step from an unpersisted default)"
        ) in line
        assert "weight_delta=" not in line
    assert {p: p.read_bytes() for p in report_state.iterdir()} == before


@pytest.mark.parametrize(
    "routed,computed,delta",
    [(0.451, 0.5, "+0.049"), (0.451, 0.451, None), (0.0, 0.0, None), (0.5, 0.51, None)],
)
def test_pacing_report_exact_line_and_difference_threshold(tmp_path, routed, computed, delta):
    row = dict(
        state="shed", measurement="measured", routing_weight=computed, ts=NOW, reason="fixture"
    )
    current = dict(agents=dict(claude=dict(state="shed", pacing=row)))
    previous = dict(
        generated_at=NOW - 60,
        agents=dict(claude=dict(state="shed", pacing=dict(routing_weight=routed))),
    )
    output = pacing_usage.report(tmp_path, now=NOW, current=current, previous=previous)
    line = next(line for line in output.splitlines() if line.startswith("pool claude: u="))
    difference = "" if delta is None else f" weight_delta={delta}"
    assert line == (
        "pool claude: u=unmeasured ũ=unmeasured ũ_eff=unmeasured "
        "span_h=unmeasured c=unmeasured C=unmeasured n_usable=unmeasured "
        "s=unmeasured ḃ_short=unmeasured ḃ_period=unmeasured ḃ_eff=unmeasured "
        "w_p_target=unmeasured state=shed pacing=measured "
        f"routed_weight={routed:.3f} (tick ts={NOW - 60}, age=60s) "
        f"report_weight={computed:.3f}{difference} w_p_prev→w_p=unmeasured→{computed:.3f} "
        f"resets=unknown source=none T1=None reset_expiries=None ts={NOW} stale=False; "
        f"fixture; {SHED_NOTE}"
    )


@pytest.mark.parametrize("persisted_weight", [None, 0.0])
def test_pacing_report_partial_snapshot_keeps_missing_values_unmeasured(tmp_path, persisted_weight):
    previous = dict(agents=dict(claude=dict(pacing=dict(routing_weight=persisted_weight))))
    current = dict(agents=dict(claude=dict(pacing=dict(routing_weight=None, state="unknown"))))
    text = pacing_usage.report(tmp_path, now=NOW, current=current, previous=previous)
    line = next(line for line in text.splitlines() if line.startswith("pool claude: u="))
    expected = "unmeasured" if persisted_weight is None else "0.000"
    assert (
        f"routed_weight={expected} (tick ts=unmeasured, age=unmeasured) "
        "report_weight=unmeasured "
    ) in line
    assert "weight_delta=" not in line
    assert SHED_NOTE not in line


def test_pacing_report_rounds_step_only_preserving_diagnostic_precision(report_state):
    path = report_state / "capacity.json"
    previous = json.loads(path.read_text())
    prior = 0.6753499299389598
    for agent in previous["agents"].values():
        agent["pacing"]["routing_weight"] = prior
    path.write_text(json.dumps(previous))
    agents = {pool: dict(state="ok", reason="fixture") for pool in previous["agents"]}
    pacing_usage.integrate(agents, now=NOW, handoff=report_state, previous=previous)
    current = dict(agents=agents)
    before = json.dumps(current, sort_keys=True)
    text = pacing_usage.report(report_state, now=NOW, current=current, previous=previous)
    assert "w_p_prev→w_p=0.675→0.425" in text
    assert "0.4253499299389598" not in text.split("w_p_prev→w_p=")[1].split(" ")[0]
    assert agents["codex"]["pacing_diagnostics"]["previous_routing_weight"] == prior
    assert agents["codex"]["pacing"]["routing_weight"] == prior - pacing.MAX_WEIGHT_CHANGE
    assert json.dumps(current, sort_keys=True) == before


def test_pacing_report_uses_persisted_diagnostic_prior_when_routed_weight_missing(tmp_path):
    prior = 0.4253499299389598
    previous = dict(agents=dict(codex=dict(pacing_diagnostics=dict(previous_routing_weight=prior))))
    current = dict(
        agents=dict(
            codex=dict(
                pacing=dict(routing_weight=prior + 0.25),
                pacing_diagnostics=dict(previous_routing_weight=prior),
            )
        )
    )
    text = pacing_usage.report(tmp_path, now=NOW, current=current, previous=previous)
    line = next(line for line in text.splitlines() if line.startswith("pool codex: u="))
    assert "w_p_prev→w_p=0.425→0.675" in line
    assert "hypothetical" not in line
