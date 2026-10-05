"""Two measurement gaps in one loop, closed together (Orchestrator#421).

1. `detect()` proposed demoting observers and tick-cadence rails it cannot trigger: on 2026-10-01 the
   live output held 54 demotions, most against cadence-run observers offered at `tick:*` phases
   where nothing can take an offer up, and the two real proposals were buried in them. Those
   capabilities are now withheld from both proposal lists and NAMED under `not_selected_by_design`
   with the count, so the drain is a number rather than a silence.
2. `record_usefulness` refused every id not minted by the advisor, so a real production role run
   (`role:decomposer:gemini:1791125304336206000`, 2026-10-04) had no path to a verdict unless a
   consult preceded it; the only workaround was a post-hoc consult, which fabricated an offer. A
   `role:`/`run:` id is now accepted when the caller declares `source=production_run`, the verdict
   is stamped with that source, and `capabilities.usage_report` counts it in the production
   column. A bare id — neither prefix — is still refused under either declaration.

Deliberate-break -> revert (run and recorded with the PR): removing the observer skip in
`detect()` fails the first test; dropping the prefix check in `verdict_source` fails the third.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import capabilities
import capability_advisor
import capability_propensity as cp

SURFACE = "fixture-surface"
EVIDENCE = "the decomposer split the epic into four bounded tasks the runner then completed"


def _cap(cap_id: str, **fields) -> dict:
    cap = capabilities._blank_capability(cap_id)
    cap["status"] = "generated"
    cap.update(fields)
    return cap


def _ledger(tmp_path: Path, *caps: dict) -> Path:
    path = tmp_path / "capabilities.json"
    capabilities.save({c["capability_id"]: c for c in caps}, path)
    return path


def _proposal(cap_id: str, action: str) -> dict:
    return {"capability_id": cap_id, "surface": SURFACE, "action": action, "reason": "fixture"}


def test_detect_skips_observers_and_cadence_rails_and_names_them(tmp_path, monkeypatch):
    ledger = _ledger(
        tmp_path,
        # An observer: a cadence step that emits a report. `is_observer()` is True.
        _cap("obs-report", matcher={"kind": "tick_phase", "name": "route-weights-export"}),
        # A rail that is not an observer by matcher kind but runs on the clock: its cadence names
        # a tick step (`switch-review` is a `cadence_registry` key) as a whole token.
        _cap(
            "clock-rail",
            matcher={"kind": "task_type", "equals": "review"},
            trigger_cadence="weekly switch-review",
        ),
        # A real lane capability: selectable, so pressure on its binding is a statement about it.
        _cap(
            "lane-cap",
            matcher={"kind": "task_type", "equals": "implement"},
            trigger_cadence="per dispatch",
        ),
    )
    # Confine detect() to one fixture surface and feed it proposals against all three capabilities;
    # the live surface readers walk lane memory files and the Brain, which this test must not touch.
    monkeypatch.setattr(cp, "SURFACE_RECORD_GLOBS", {})
    monkeypatch.setattr(capability_advisor, "SURFACE_BINDINGS", {})
    monkeypatch.setattr(cp, "observed_surfaces", lambda *, path=None, **_: {SURFACE})
    monkeypatch.setattr(cp, "surface_records", lambda surface: ["one record"])
    monkeypatch.setattr(cp, "finds", lambda *, path=None, **_: [])
    monkeypatch.setattr(cp, "surface_fact_missing_total", lambda surface, *, path=None, **_: 0)
    monkeypatch.setattr(
        capability_advisor, "binding_for", lambda surface, *, path=None, promoted=None: {}
    )
    monkeypatch.setattr(
        cp,
        "surface_decline_counts",
        lambda surface, *, path=None, **_: {
            "declined": {},
            "declined_demotable": {},
            "declines_by_kind": {},
        },
    )
    monkeypatch.setattr(
        cp,
        "propose_bindings",
        lambda surface, records, *, path=None: [_proposal("obs-report", "promote")],
    )
    monkeypatch.setattr(
        cp,
        "propose_demotions",
        lambda surface, *, path=None, **_: [
            _proposal("obs-report", "demote"),
            _proposal("clock-rail", "demote"),
            _proposal("lane-cap", "demote"),
        ],
    )

    rep = cp.detect(path=ledger)

    assert [d["capability_id"] for d in rep["demotions"]] == ["lane-cap"], rep["demotions"]
    assert rep["promotions"] == [], rep["promotions"]
    nsd = rep["not_selected_by_design"]
    assert nsd["count"] == 2 and nsd["withheld"] == 3, nsd
    assert nsd["capabilities"] == {
        "clock-rail": {
            "reason": "tick_cadence",
            "surfaces": [SURFACE],
            "promotions_withheld": 0,
            "demotions_withheld": 1,
        },
        "obs-report": {
            "reason": "observer",
            "surfaces": [SURFACE],
            "promotions_withheld": 1,
            "demotions_withheld": 1,
        },
    }, nsd
    # The reason helpers are the rule, stated once: a cadence that merely CONTAINS a step's letters
    # is not a tick step, and "daily" names none.
    assert cp.names_tick_step("weekly switch-review") is True
    assert cp.names_tick_step("daily") is False
    assert cp.names_tick_step("switch-reviewer runs this") is False
    assert cp.not_selected_by_design_reason(None) is None


def _outcomes(ledger: Path, cap_id: str) -> list[dict]:
    row = capabilities.load(ledger, create=False)[cap_id]
    return [e for e in row.get("event_history") or [] if e.get("type") == "outcome"]


def test_record_usefulness_accepts_a_role_run_id_with_source_production_run(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path, _cap("role-decomposer"), _cap("offload"))
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    role_run = "role:decomposer:gemini:1791125304336206000"

    assert cp.record_usefulness(
        "role-decomposer",
        role_run,
        useful=True,
        evidence=EVIDENCE,
        provenance=cp.PROVENANCE_DEFAULT,
        source=cp.PRODUCTION_RUN_SOURCE,
        path=ledger,
    )
    (verdict,) = _outcomes(ledger, "role-decomposer")
    assert verdict["ref"] == role_run
    assert verdict["metadata"]["source"] == "production_run", verdict
    assert verdict["metadata"][cp.USEFUL_KEY] is True
    assert verdict["idempotency_key"] == f"useful:role-decomposer:{role_run}"

    # The `run:` prefix is the other accepted shape, and the CLI reaches the same writer with
    # `--source production_run` — the lanes are bash, so a Python-only path would be unreachable.
    rc = cp.main(
        [
            "useful",
            "--capability",
            "offload",
            "--experiment",
            "run:keepalive:o/r#7:codex",
            "--evidence",
            "the offloaded review found the missing edge before merge",
            "--provenance",
            cp.PROVENANCE_DEFAULT,
            "--source",
            cp.PRODUCTION_RUN_SOURCE,
            "--ledger",
            str(ledger),
        ]
    )
    assert rc == 0
    (cli_verdict,) = _outcomes(ledger, "offload")
    assert cli_verdict["metadata"]["source"] == "production_run", cli_verdict

    # A consult-trial verdict keeps its existing stamp: the two populations stay distinguishable.
    assert cp.record_usefulness(
        "offload",
        "advice:0123456789ab",
        useful=True,
        evidence=EVIDENCE,
        provenance=cp.PROVENANCE_DEFAULT,
        path=ledger,
    )
    stamps = sorted(e["metadata"]["source"] for e in _outcomes(ledger, "offload"))
    assert stamps == ["capability_propensity", "production_run"], stamps

    # The production column counts the stamp, per capability and rolled up, and the digest says
    # so in words beside the consult-verdict line it must never be merged with.
    monkeypatch.setattr(capabilities, "_fleet_edge_counts", lambda *, conn=None: None)
    usage = capabilities.usage_report(capabilities.summary(ledger), path=ledger)
    by_id = {row["capability_id"]: row for row in usage["rows"]}
    assert by_id["role-decomposer"]["production_run_verdicts"] == 1
    assert by_id["offload"]["production_run_verdicts"] == 1
    assert usage["capabilities_with_production_run_verdict"] == 2, usage
    # `summary()` reconciles the declared capability table into the ledger, so the denominator is
    # the full population, not the two rows this test wrote; read it rather than assume it.
    text = capabilities.format_usage_report(usage)
    assert (
        f"production runs: 2 of {usage['total']} carry a verdict recorded from an un-offered "
        "production run (source=production_run)"
    ) in text, text
    assert "| Prod verdicts |" in text
    assert re.search(
        r"\| role-decomposer \| \S+ \| \S+ \| [\d.]+ \| unavailable \| 1 \|", text
    ), text
    assert re.search(r"\| offload \| \S+ \| \S+ \| [\d.]+ \| unavailable \| 1 \|", text), text
    json.dumps(usage)  # the report stays serialisable for the tick's JSON artifact


def test_record_usefulness_still_refuses_an_unprefixed_id(tmp_path):
    ledger = _ledger(tmp_path, _cap("offload"))
    bare = "1791125304336206000"
    common = dict(useful=True, evidence=EVIDENCE, provenance=cp.PROVENANCE_DEFAULT, path=ledger)

    with pytest.raises(ValueError, match="must start with 'advice:'"):
        cp.record_usefulness("offload", bare, **common)
    # Declaring a production run does not launder a bare id either.
    with pytest.raises(ValueError, match="role:"):
        cp.record_usefulness("offload", bare, source=cp.PRODUCTION_RUN_SOURCE, **common)
    # A role run id WITHOUT the declaration is still refused: the source is a statement the
    # caller makes, never something inferred from the shape of the id.
    with pytest.raises(ValueError, match="production_run"):
        cp.record_usefulness("offload", "role:decomposer:gemini:1", **common)
    # And an unknown source is refused rather than coerced, like an unknown decline kind.
    with pytest.raises(ValueError, match="unknown verdict source"):
        cp.record_usefulness("offload", "role:decomposer:gemini:1", source="shadow", **common)
    assert _outcomes(ledger, "offload") == [], "a refused verdict must write nothing"


@pytest.mark.parametrize(
    ("source", "experiment", "caller_source", "expected_source", "production_count"),
    [
        ("production_run", "role:decomposer:1", "capability_propensity", "production_run", 1),
        ("", "advice:0123456789ab", "production_run", "capability_propensity", 0),
    ],
)
def test_caller_metadata_cannot_change_validated_verdict_source(
    tmp_path, source, experiment, caller_source, expected_source, production_count
):
    ledger = _ledger(tmp_path, _cap("role-decomposer"))
    assert cp.record_usefulness(
        "role-decomposer",
        experiment,
        useful=True,
        evidence=EVIDENCE,
        provenance=cp.PROVENANCE_DEFAULT,
        source=source,
        metadata={"source": caller_source, "verdict_kind": "delivery"},
        path=ledger,
    )
    (verdict,) = _outcomes(ledger, "role-decomposer")
    assert verdict["metadata"]["source"] == expected_source
    assert verdict["metadata"]["verdict_kind"] == "delivery"
    row = capabilities.load(ledger, create=False)["role-decomposer"]
    assert capabilities.production_run_verdicts(row) == production_count


def test_detect_refuses_apply_when_classification_ledger_is_unreadable(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path, _cap("obs-report"))
    calls = []

    def transient_failure(reader):
        failed = False

        def read(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("classification ledger temporarily unavailable")
            return reader(*args, **kwargs)

        return read

    monkeypatch.setattr(
        capabilities, "load_declared", transient_failure(capabilities.load_declared)
    )
    monkeypatch.setattr(capabilities, "load", transient_failure(capabilities.load))
    monkeypatch.setattr(cp, "finds", lambda **kwargs: [])
    monkeypatch.setattr(cp, "SURFACE_RECORD_GLOBS", {})
    monkeypatch.setattr(capability_advisor, "SURFACE_BINDINGS", {})
    monkeypatch.setattr(cp, "observed_surfaces", lambda **kwargs: {SURFACE})
    monkeypatch.setattr(cp, "surface_records", lambda surface: ["one record"])
    monkeypatch.setattr(
        cp,
        "propose_bindings",
        lambda *args, **kwargs: calls.append("proposal") or [_proposal("obs-report", "promote")],
    )
    monkeypatch.setattr(
        cp, "record_promotion", lambda *args, **kwargs: calls.append("write") or True
    )
    with pytest.raises(RuntimeError, match="classification ledger"):
        cp.detect(path=ledger, apply_promotions=True)
    assert calls == [], "no proposal evaluation or promotion may run without classification"
