"""Selected correction ownership preserves source identity while accepting GitHub case spelling."""

from __future__ import annotations

import pytest

import closer_correction_owner as owner
import closer_disposition as disposition
import closer_execution_contracts as execution


def _selected_inputs(*, source_target: str, source_kind: str):
    criterion = "Preserve the original issue."
    raw_id, native_id = "raw-source", "native-source"
    snapshot = {
        "sources": [
            {
                "target": source_target,
                "kind": source_kind,
                "body": "## Tasks\n- [ ] " + criterion,
            }
        ]
    }
    low = {
        "adjudication_request": {"basis": {}, "question": "is it met?", "evidence_refs": []},
        "decision": {"criteria": [{"source": source_target, "criterion": criterion}]},
        "findings": [],
        "classification": "unknown",
        "substantive_needs": [],
        "resolutions": [
            {
                "finding_id": native_id,
                "status": "unresolved_missing_evidence",
                "next_guarded_action": "wait",
                "evidence": [],
                "proofs": [],
            }
        ],
    }
    inputs = {
        "source_body": snapshot["sources"][0]["body"],
        "raw_findings": [
            {"id": raw_id, "text": "native quote: " + criterion, "evidence": {"ref": "raw"}}
        ],
        "native_seed_mapping": [{"seed_finding_id": raw_id, "native_finding_id": native_id}],
        "seed_bundle": {
            "seeds": [
                {
                    "target": {"repository": "stranske/orchestrator"},
                    "source_issue": {"number": "983"},
                    "finding": {"id": raw_id},
                }
            ]
        },
    }
    return snapshot, low, inputs, criterion, native_id


def _selected_defect(monkeypatch, *, source_target: str, source_kind: str):
    snapshot, low, inputs, criterion, native_id = _selected_inputs(
        source_target=source_target, source_kind=source_kind
    )
    monkeypatch.setattr(
        execution,
        "build_adjudication_request",
        lambda *_args, **_kwargs: {"criterion": criterion, "basis": {"source": source_target}},
    )
    monkeypatch.setattr(
        execution,
        "validate_adjudication_response",
        lambda *_args, **_kwargs: {"outcome": "unsatisfied"},
    )
    monkeypatch.setattr(
        execution,
        "merge_selected_response",
        lambda source_low, *_args: {
            key: source_low[key] for key in ("classification", "substantive_needs", "resolutions")
        },
    )
    monkeypatch.setattr(disposition, "_expected", lambda _snapshot: [(source_target, criterion)])
    monkeypatch.setattr(
        disposition,
        "pending_nonpass_findings",
        lambda _snapshot, _packet: [{"finding_id": native_id}],
    )
    return owner._selected_defect(
        snapshot=snapshot,
        packet={},
        packet_ref={},
        root=None,
        low=low,
        strong={},
        inputs=inputs,
    )


def test_selected_defect_accepts_case_variant_of_canonical_issue_repository(monkeypatch):
    seed, required, _request = _selected_defect(
        monkeypatch, source_target="stranske/Orchestrator#983", source_kind="issue"
    )
    assert seed["target"]["repository"] == "stranske/orchestrator"
    assert required[0]["kind"] == "required_test_spec"


@pytest.mark.parametrize(
    ("source_target", "source_kind"),
    [
        ("stranske/Other#983", "issue"),
        ("stranske/Orchestrator#984", "issue"),
        ("stranske/Orchestrator#983", "pull"),
    ],
)
def test_selected_defect_rejects_distinct_repository_number_or_source_type(
    monkeypatch, source_target, source_kind
):
    with pytest.raises(owner.OwnerDecisionUnavailable):
        _selected_defect(monkeypatch, source_target=source_target, source_kind=source_kind)
