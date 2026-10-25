"""Admission is a locked state transition, never qualification or launch authority."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone

import pytest

NOW = 1792965600.0
PACKAGE = {
    "package_digest": "a" * 64,
    "manifest_sha256": "b" * 64,
    "qualification_digest": "c" * 64,
}


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


@pytest.fixture
def case():
    doc = {
        "schema_version": 1,
        "campaign_id": "campaign",
        "owner": "existing-owner",
        "generation": 2,
        "revision": 4,
        "release_pin": PACKAGE["package_digest"],
        "phase": "work",
        "hold": None,
        "workers": [],
    }
    assignment = {
        "generation": 8,
        "generated_at": iso(NOW - 1),
        "errors": [],
        "targets": [{"target": "Org/Repo#1", "due": iso(NOW - 1)}],
    }
    raw = json.dumps(assignment, indent=2).encode() + b"\n"
    request = {
        "schema": "closer-demand-request/v1",
        "campaign_id": "campaign",
        "owner": "existing-owner",
        "campaign_generation": 2,
        "expected_revision": 4,
        "assignment_generation": 8,
        "assignment_sha256": hashlib.sha256(raw).hexdigest(),
        **PACKAGE,
        "not_before": NOW - 1,
        "deadline": NOW + 120,
        "targets": ["Org/Repo#1"],
    }
    return doc, assignment, raw, request


def call(case, **kwargs):
    from closer_demand_admission import admit_in_transaction

    doc, assignment, raw, request = case
    return admit_in_transaction(
        doc, assignment_bytes=raw, request=request, qualified_package=PACKAGE, now=NOW, **kwargs
    )


def reencode(case):
    doc, assignment, _, request = case
    raw = json.dumps(assignment, indent=4).encode() + b"\n"
    request["assignment_sha256"] = hashlib.sha256(raw).hexdigest()
    return doc, assignment, raw, request


def test_new_exact_request_records_only_admission(case):
    result = call(case)
    assert result["status"] == "admitted" and result["changed"] is True
    assert case[0]["demand_admission"] == result["record"]
    assert result["record"]["accepted_at"] == NOW
    assert result["record"]["deadline"] == NOW + 120
    assert result["record"]["admission_count"] == 1
    assert not any(
        key in result["record"] for key in ("native_entry", "launch_authority", "lease_token")
    )
    assert case[0]["revision"] == 4  # existing Campaign.update owns its revision increment


def test_replay_preserves_timestamp_deadline_count_and_state(case):
    from closer_demand_admission import admit_in_transaction

    first = call(case)
    case[0]["revision"] += 1
    before = copy.deepcopy(case[0])
    replay = admit_in_transaction(
        case[0], assignment_bytes=case[2], request=case[3], qualified_package=PACKAGE, now=NOW + 121
    )
    assert replay["status"] == "replay" and replay["changed"] is False
    assert replay["record"] == first["record"] and case[0] == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_revision", 3),
        ("campaign_generation", 1),
        ("owner", "foreign"),
        ("campaign_id", "foreign"),
        ("package_digest", "d" * 64),
        ("manifest_sha256", "d" * 64),
        ("qualification_digest", "d" * 64),
        ("assignment_generation", 7),
        ("assignment_sha256", "d" * 64),
        ("not_before", NOW + 1),
        ("deadline", NOW),
        ("targets", ["Org/Repo#2"]),
    ],
)
def test_stale_foreign_due_scope_and_pin_mismatch_refuse_without_mutation(case, field, value):
    case[3][field] = value
    before = copy.deepcopy(case[0])
    result = call(case)
    assert result["status"] == "refused" and result["changed"] is False
    assert case[0] == before


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(errors=["incomplete"]),
        lambda d: d.update(targets=[]),
        lambda d: d.update(generated_at=iso(NOW + 1)),
        lambda d: d.update(generated_at=iso(NOW - 601)),
        lambda d: d.update(generation=True),
        lambda d: d["targets"][0].update(due=iso(NOW + 1)),
        lambda d: d["targets"][0].update(not_before=NOW + 1),
        lambda d: d["targets"].append({"target": "org/repo#1"}),
    ],
)
def test_invalid_or_not_yet_due_assignment_refuses(case, change):
    change(case[1])
    case = reencode(case)
    before = copy.deepcopy(case[0])
    assert call(case)["status"] == "refused"
    assert case[0] == before


@pytest.mark.parametrize(
    "raw",
    [
        b'{"generation":8,"generation":8}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b"[]",
        b"\xff",
        b"{",
        b" " * 2097153,
    ],
)
def test_bounded_strict_json_rejects_ambiguity_nonfinite_or_malformed(case, raw):
    doc, assignment, _, request = case
    request["assignment_sha256"] = hashlib.sha256(raw).hexdigest()
    before = copy.deepcopy(doc)
    assert call((doc, assignment, raw, request))["status"] == "refused"
    assert doc == before


def test_exact_raw_digest_accepts_ordinary_serialization_and_rejects_byte_drift(case):
    assert call(case)["status"] == "admitted"
    doc, assignment, raw, request = case
    assert call((doc, assignment, raw + b" ", request))["status"] == "refused"


@pytest.mark.parametrize("state", ["launched", "active", "unknown"])
def test_existing_worker_blocks_new_admission(case, state):
    case[0]["workers"] = [{"state": state}]
    assert call(case)["status"] == "refused"


def test_existing_hold_blocks_new_admission(case):
    case[0]["hold"] = "owner hold"
    assert call(case)["status"] == "refused"


def test_older_assignment_cannot_replace_current_generation(case):
    assert call(case)["status"] == "admitted"
    case[0]["revision"] += 1
    case[3]["expected_revision"] += 1
    case[1]["generation"] = 7
    case[3]["assignment_generation"] = 7
    case = reencode(case)
    before = copy.deepcopy(case[0])
    assert call(case)["status"] == "refused" and case[0] == before


def test_changed_same_generation_cannot_replace_accepted_request(case):
    assert call(case)["status"] == "admitted"
    case[0]["revision"] += 1
    case[3]["expected_revision"] += 1
    case[3]["deadline"] += 1
    before = copy.deepcopy(case[0])
    assert call(case)["status"] == "refused" and case[0] == before


def test_wrapper_uses_existing_campaign_transaction_and_no_write_on_replay(tmp_path, case):
    import closer_campaign
    from closer_demand_admission import admit_assignment

    campaign = closer_campaign.Campaign(tmp_path / "c.campaign.json")
    doc, assignment, raw, request = case
    campaign.update(lambda value: value.update(doc), create={})
    request["expected_revision"] = campaign.read()["revision"]
    first = admit_assignment(
        campaign, assignment_bytes=raw, request=request, qualified_package=PACKAGE, now=NOW
    )
    assert first["status"] == "admitted"
    before = campaign.path.read_bytes()
    replay = admit_assignment(
        campaign, assignment_bytes=raw, request=request, qualified_package=PACKAGE, now=NOW + 1
    )
    assert replay["status"] == "replay" and campaign.path.read_bytes() == before
    request["owner"] = "foreign"
    assert (
        admit_assignment(
            campaign, assignment_bytes=raw, request=request, qualified_package=PACKAGE, now=NOW
        )["status"]
        == "refused"
    )
    assert campaign.path.read_bytes() == before


def test_two_contenders_share_existing_campaign_lock_and_admit_once(tmp_path, case):
    import concurrent.futures

    import closer_campaign
    from closer_demand_admission import admit_assignment

    campaign = closer_campaign.Campaign(tmp_path / "c.campaign.json")
    doc, _, raw, request = case
    campaign.update(lambda value: value.update(doc), create={})
    request["expected_revision"] = campaign.read()["revision"]

    def enter():
        return admit_assignment(
            campaign, assignment_bytes=raw, request=request, qualified_package=PACKAGE, now=NOW
        )["status"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: enter(), range(2)))
    assert sorted(outcomes) == ["admitted", "replay"]
    assert campaign.read()["demand_admission"]["admission_count"] == 1


@pytest.mark.parametrize(
    "worker",
    [
        {"state": "denied"},
        {"state": "terminal", "outcome": "unknown"},
        {"state": "terminal", "outcome": "denied"},
        {"state": "terminal"},
    ],
)
def test_terminal_label_cannot_erase_denial_or_unknown_outcome(case, worker):
    case[0]["workers"] = [worker]
    assert call(case)["status"] == "refused"


def test_new_generation_after_known_terminal_outcome_uses_exact_revision(case):
    assert call(case)["status"] == "admitted"
    case[0]["revision"] += 1
    case[3]["expected_revision"] += 1
    case[0]["workers"] = [{"state": "terminal", "outcome": "ok"}]
    case[1]["generation"] += 1
    case[3]["assignment_generation"] += 1
    case = reencode(case)
    assert call(case)["status"] == "admitted"
    assert case[0]["demand_admission"]["admission_count"] == 2


def test_result_copy_cannot_change_accepted_record(case):
    result = call(case)
    before = copy.deepcopy(case[0])
    result["record"]["deadline"] += 10
    assert case[0] == before


def test_tampered_accepted_receipt_refuses_replay(case):
    assert call(case)["status"] == "admitted"
    case[0]["demand_admission"]["deadline"] += 1
    before = copy.deepcopy(case[0])
    assert call(case)["status"] == "refused" and case[0] == before


def test_wrapper_storage_uncertainty_propagates(case):
    from closer_demand_admission import admit_assignment

    class BrokenStorage:
        def update(self, mutate):
            mutate(copy.deepcopy(case[0]))
            raise OSError("uncertain write")

    with pytest.raises(OSError, match="uncertain write"):
        admit_assignment(
            BrokenStorage(),
            assignment_bytes=case[2],
            request=case[3],
            qualified_package=PACKAGE,
            now=NOW,
        )
