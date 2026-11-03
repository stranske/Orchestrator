"""Prospective instruction bytes only; no native comment or execution proof."""

import ast
import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

import closer_correction_handoff as wire
import closer_correction_native_effects as effects
import closer_corrective_verification_instruction as instruction


def inputs():
    request = json.loads(
        (Path(__file__).parent / "fixtures/closer_correction/request.json").read_text()
    )
    text = "Correct CSV quoting, retain café and every original row."
    request["finding"]["evidence"]["sha256"] = hashlib.sha256(text.encode()).hexdigest()
    for test in request["required_tests"]:
        test["source_evidence"] = copy.deepcopy(request["finding"]["evidence"])
    request["request_sha256"] = wire.digest(
        {key: value for key, value in request.items() if key != "request_sha256"}
    )
    other = "Other original concern remains a claim"
    raw = [
        {**copy.deepcopy(request["finding"]), "text": text},
        {
            "id": "other-1",
            "text": other,
            "evidence": {
                "ref": "evaluation.json#/concerns/1",
                "sha256": hashlib.sha256(other.encode()).hexdigest(),
            },
        },
        {
            "id": "other-2",
            "text": other,
            "evidence": {
                "ref": "evaluation.json#/concerns/2",
                "sha256": hashlib.sha256(other.encode()).hexdigest(),
            },
        },
    ]
    return request, text, raw


def test_exact_prospective_suffix_roundtrips_complete_unmodified_scope_and_claims():
    request, text, raw = inputs()
    human = effects._payload(request, "instruction_publication", text)["body"]
    body = effects._payload(request, "instruction_publication", text, raw_findings=raw)["body"]
    expected = instruction.payload(request, text, raw)
    suffix = (
        instruction.PREFIX
        + base64.b64encode(wire.canonical_json(expected).encode()).decode()
        + instruction.SUFFIX
    )
    assert body == human + "\n\n" + suffix
    parsed = instruction.parse(body, max_bytes=effects.MAX_PAYLOAD)
    assert parsed["human_text"] == human
    assert parsed["payload"]["original_request"] == request
    assert (
        parsed["payload"]["raw_findings"] == raw
    )  # Repeated original claims are not deduplicated.
    assert parsed["payload"]["required_tests_sha256"] == wire.digest(request["required_tests"])
    assert parsed["payload"]["original_request"]["target"] == request["target"]
    assert "corrective_pr" not in parsed["payload"]  # Future head does not exist before coding.


@pytest.mark.parametrize("raw", [None, [], {}, "missing"])
def test_prospective_missing_raw_collection_refuses(raw):
    request, text, _ = inputs()
    with pytest.raises(
        instruction.InstructionHeld, match="complete original raw findings required"
    ):
        effects._payload(request, "instruction_publication", text, raw_findings=raw)


def test_legacy_plain_fixture_is_evidence_only_and_issue_creation_bytes_stay_unchanged():
    request, text, raw = inputs()
    plain = effects._payload(request, "instruction_publication", text)["body"]
    with pytest.raises(
        instruction.InstructionHeld, match="structured native instruction suffix missing"
    ):
        instruction.parse(plain, max_bytes=effects.MAX_PAYLOAD)
    assert effects._payload(request, "issue_creation", text) == effects._payload(
        request, "issue_creation", text, raw_findings=raw
    )


@pytest.mark.parametrize(
    "damage",
    [
        "finding",
        "selected_raw",
        "test_digest",
        "changed_test",
        "extra",
        "duplicate_json",
        "noncanonical",
        "trailing",
        "base64",
    ],
)
def test_forged_or_changed_footer_never_becomes_typed_scope(damage):
    request, text, raw = inputs()
    value = instruction.payload(request, text, raw)
    if damage == "finding":
        value["finding_text"] += " changed"
    elif damage == "selected_raw":
        value["raw_findings"][0]["text"] += " changed"
    elif damage == "test_digest":
        value["required_tests_sha256"] = "0" * 64
    elif damage == "changed_test":
        value["original_request"]["required_tests"][0]["specification"] += " different"
    elif damage == "extra":
        value["act_authorized"] = True
    raw_json = wire.canonical_json(value).encode()
    if damage == "duplicate_json":
        raw_json = b'{"schema":"wrong",' + raw_json[1:]
    elif damage == "noncanonical":
        raw_json = json.dumps(value, indent=2).encode()
    encoded = base64.b64encode(raw_json).decode()
    if damage == "base64":
        encoded += " "
    body = "human\n\n" + instruction.PREFIX + encoded + instruction.SUFFIX
    if damage == "trailing":
        body += "\nignored prose"
    with pytest.raises(instruction.InstructionHeld):
        instruction.parse(body, max_bytes=effects.MAX_PAYLOAD)


def test_complete_payload_overflow_holds_without_truncating_any_original_field():
    request, text, raw = inputs()
    before = copy.deepcopy((request, raw))
    with pytest.raises(instruction.InstructionHeld, match="exceeds existing bound"):
        instruction.append("human", request, text, raw, max_bytes=100)
    assert (request, raw) == before


def test_producer_passes_full_protected_collection_before_selected_row_narrowing():
    tree = ast.parse(Path(effects.__file__).read_text())
    function = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "publish_first_instruction_locked"
    )
    calls = [
        n
        for n in ast.walk(function)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_payload"
    ]
    assert len(calls) == 1
    assert any(
        k.arg == "raw_findings" and isinstance(k.value, ast.Name) and k.value.id == "raw_findings"
        for k in calls[0].keywords
    )
    assignments = [n for n in ast.walk(function) if isinstance(n, ast.Assign)]
    full = next(
        n
        for n in assignments
        if any(isinstance(t, ast.Name) and t.id == "raw_findings" for t in n.targets)
    )
    selected = next(
        n for n in assignments if any(isinstance(t, ast.Name) and t.id == "raw" for t in n.targets)
    )
    assert full.lineno < selected.lineno < calls[0].lineno


def test_canonical_native_selector_roundtrips_existing_repeat_material_path():
    import verification_events

    selector = json.loads(
        (
            Path(__file__).parent / "fixtures/closer_corrective_instruction/v1-native-selector.json"
        ).read_text()
    )
    assert instruction.validate_selector(selector) == selector
    assert verification_events.material_selectors([selector]) == [selector]
    value = verification_events.material_selectors([selector])[0]
    value["owner_instruction"]["writer_id"] += 1
    assert value != selector  # No alias to the original protected scope.


@pytest.mark.parametrize(
    "damage", ["missing", "extra", "identity", "head", "tests", "time", "unknown_kind"]
)
def test_typed_selector_refuses_malformed_without_dropping_to_plain_evidence(damage):
    import verification_events

    selector = json.loads(
        (
            Path(__file__).parent / "fixtures/closer_corrective_instruction/v1-native-selector.json"
        ).read_text()
    )
    if damage == "missing":
        del selector["required_tests_sha256"]
    elif damage == "extra":
        selector["act_authorized"] = True
    elif damage == "identity":
        selector["incumbent_assignee"]["id"] = True
    elif damage == "head":
        selector["corrective_pr"]["head_sha"] = "not-a-head"
    elif damage == "tests":
        selector["required_tests_sha256"] = []
    elif damage == "time":
        selector["owner_instruction"]["updated_at"] = "invalid"
    else:
        selector["kind"] = "unknown_corrective_instruction"
    with pytest.raises(ValueError):
        verification_events.material_selectors([selector])


def test_ambiguous_suffix_refuses_instead_of_selecting_one_machine_instruction():
    request, text, raw = inputs()
    body = instruction.append("human", request, text, raw, max_bytes=effects.MAX_PAYLOAD)
    with pytest.raises(instruction.InstructionHeld):
        instruction.parse(body + "\n\n" + body, max_bytes=effects.MAX_PAYLOAD)


def test_first_typed_verification_instruction_holds_no_incumbent_without_changing_legacy_bytes():
    request, text, raw = inputs()
    no_inc = json.loads(
        (Path(__file__).parent / "fixtures/closer_correction/no-incumbent-request.json").read_text()
    )
    request["incumbent"] = no_inc["incumbent"]
    request["request_sha256"] = wire.digest(
        {key: value for key, value in request.items() if key != "request_sha256"}
    )
    wire.validate_request(request)
    with pytest.raises(instruction.InstructionHeld, match="existing incumbent"):
        effects._payload(request, "instruction_publication", text, raw_findings=raw)
    legacy = effects._payload(request, "instruction_publication", text)
    assert instruction.PREFIX not in legacy["body"]  # Legacy evidence-only behavior remains intact.
