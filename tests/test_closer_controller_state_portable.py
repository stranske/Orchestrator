"""Private index data rules only; fixtures confer no custody or execution authority."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_closer_controller_canonical_portable import GOLDEN_BYTES

import closer_correction_controller as ctl


@pytest.fixture
def config():
    return ctl.ControllerConfig(
        state_path="unused",
        repository="stranske/example",
        repository_id=1,
        epoch="test",
        bootstrap_source_sha="a" * 40,
        source_sha="b" * 64,
        writer_id=99,
        writer_login="test",
        writer_type="User",
        bootstrap_anchor_sha256="c" * 64,
        max_bytes=1048576,
        max_entries=10,
        max_subjects=30,
    )


@pytest.fixture
def admission():
    return json.loads(GOLDEN_BYTES["local-admission-selector.json"])


def request(incumbent=False):
    name = "request.json" if incumbent else "no-incumbent-request.json"
    return json.loads((Path(__file__).parent / "fixtures/closer_correction" / name).read_text())


def empty(config):
    return dict(
        schema=ctl.SCHEMA,
        repository=config.repository,
        repository_id=config.repository_id,
        epoch=config.epoch,
        bootstrap_source_sha=config.bootstrap_source_sha,
        revision=0,
        previous_sha256=None,
        last_action=None,
        entries=[],
    )


def enroll_action(admission, incumbent=False):
    req = request(incumbent)
    subjects = [
        dict(kind="pull", number="7", node_id="PR_7"),
        dict(kind="issue", number="1", node_id="I_1"),
    ]
    if incumbent:
        subjects.append(dict(kind="issue", number="8", node_id="I_8"))
    return dict(kind="enroll", request=req, admitted_selector=admission, subjects=subjects)


def enrolled(config, admission, incumbent=False):
    return ctl.transition(empty(config), enroll_action(admission, incumbent), config)


def prepare(data, admission, phase="issue_creation"):
    return dict(
        kind="prepare",
        key=data["entries"][0]["key"],
        phase=phase,
        intent_sha256="d" * 64,
        selector=admission,
    )


def confirm(data, admission, result=None):
    return dict(
        kind="confirm",
        key=data["entries"][0]["key"],
        phase="issue_creation",
        proof_sha256="e" * 64,
        selector=admission,
        result=result or dict(kind="issue", number="9", node_id="I_9"),
    )


def test_enroll_idempotent_and_rebound_refused(config, admission):
    action = enroll_action(admission)
    data = ctl.transition(empty(config), action, config)
    assert ctl.transition(data, action, config) is data
    rebound = copy.deepcopy(action)
    rebound["request"]["original_operation"]["hold_id"] = "different"
    rebound["request"]["request_sha256"] = ctl.wire.digest(
        {k: v for k, v in rebound["request"].items() if k != "request_sha256"}
    )
    with pytest.raises(ctl.ControllerUnknown, match="enrollment rebound"):
        ctl.transition(data, rebound, config)
    assert data["revision"] == 1 and data["entries"][0]["stages"] == []


def test_incumbent_and_confirmed_issue_prepare_guards(config, admission):
    data = enrolled(config, admission, incumbent=True)
    with pytest.raises(ctl.ControllerUnknown, match="existing owner cannot create issue"):
        ctl.transition(data, prepare(data, admission), config)
    data = enrolled(config, admission)
    with pytest.raises(ctl.ControllerUnknown, match="confirmed issue required"):
        ctl.transition(data, prepare(data, admission, "autopilot_dispatch"), config)
    data = ctl.transition(data, prepare(data, admission), config)
    data = ctl.transition(data, confirm(data, admission), config)
    continued = ctl.transition(data, prepare(data, admission, "autopilot_dispatch"), config)
    assert continued["entries"][0]["stages"][1]["state"] == "prepared"


def test_unknown_is_idempotent_and_cannot_erase_confirmed(config, admission):
    data = enrolled(config, admission)
    data = ctl.transition(data, prepare(data, admission), config)
    action = dict(kind="unknown", key=data["entries"][0]["key"], phase="issue_creation")
    data = ctl.transition(data, action, config)
    assert ctl.transition(data, action, config) is data
    data = ctl.transition(data, confirm(data, admission), config)
    with pytest.raises(ctl.ControllerUnknown, match="unknown cannot erase confirmed identity"):
        ctl.transition(data, action, config)


def test_confirm_is_idempotent_and_rebound_refused(config, admission):
    data = enrolled(config, admission)
    data = ctl.transition(data, prepare(data, admission), config)
    action = confirm(data, admission)
    data = ctl.transition(data, action, config)
    assert ctl.transition(data, action, config) is data
    action["proof_sha256"] = "f" * 64
    with pytest.raises(ctl.ControllerUnknown, match="native result rebound"):
        ctl.transition(data, action, config)


def test_creation_cannot_adopt_existing_subject(config, admission):
    data = enrolled(config, admission)
    data = ctl.transition(data, prepare(data, admission), config)
    with pytest.raises(ctl.ControllerUnknown, match="creation cannot adopt existing subject"):
        ctl.transition(data, confirm(data, admission, data["entries"][0]["subjects"][1]), config)


def test_index_rejects_competing_membership_and_unproved_subject(config, admission):
    data = enrolled(config, admission)
    other = copy.deepcopy(data["entries"][0])
    other["request"]["finding"]["id"] = "other"
    other["request"]["finding"]["evidence"]["ref"] = "evaluation.json#/concerns/1"
    other["request"]["required_tests"][0]["finding_id"] = "other"
    other["request"]["required_tests"][0]["source_evidence"] = copy.deepcopy(
        other["request"]["finding"]["evidence"]
    )
    other["request"]["request_sha256"] = ctl.wire.digest(
        {k: v for k, v in other["request"].items() if k != "request_sha256"}
    )
    other["key"] = ctl.effect_key(other["request"])
    other["source_scope_digest"] = ctl.scope_digest(other["request"])
    data["entries"].append(other)
    with pytest.raises(ctl.ControllerUnknown, match="competing native subject membership"):
        ctl.validate_index(data, config)
    data = enrolled(config, admission)
    data["entries"][0]["subjects"].append(dict(kind="pull", number="10", node_id="PR_10"))
    with pytest.raises(ctl.ControllerUnknown, match="additional subject lacks native proof"):
        ctl.validate_index(data, config)


@pytest.mark.parametrize("field", ["number"])
@pytest.mark.parametrize("value", [7, None, [], {}])
def test_subject_nontext_identity_is_unknown(field, value):
    row = dict(kind="issue", number="7", node_id="I_7")
    row[field] = value
    with pytest.raises(ctl.ControllerUnknown, match="native subject identity missing"):
        ctl.subject(row)


@pytest.mark.parametrize("field", ["archive_sha256", "member_sha256"])
def test_seed_selector_rejects_nontext_digest(admission, field):
    row = admission["seed_selector"]
    ctl.seed_selector(row, "stranske/example")
    row[field] = 123
    with pytest.raises(ctl.ControllerUnknown, match="original seed selector differs"):
        ctl.seed_selector(row, "stranske/example")


@pytest.mark.parametrize("field", ["record_sha256", "issue_number", "updated_at", "body_sha256"])
def test_local_selector_rejects_nontext_fields(admission, field):
    ctl.selector(admission, "stranske/example")
    target = admission if field == "record_sha256" else admission["owner_instruction"]
    target[field] = 123
    reason = (
        "protected local record selector required"
        if field == "record_sha256"
        else "native instruction selector differs"
    )
    with pytest.raises(ctl.ControllerUnknown, match=reason):
        ctl.selector(admission, "stranske/example")


def test_index_rejects_nontext_predecessor(config, admission):
    data = enrolled(config, admission)
    data["previous_sha256"] = 1
    with pytest.raises(ctl.ControllerUnknown, match="index predecessor missing"):
        ctl.validate_index(data, config)


@pytest.mark.parametrize("field", ["intent_sha256", "proof_sha256", "run_id", "run_attempt"])
def test_index_rejects_nontext_stage_fields(config, admission, field):
    data = enrolled(config, admission, incumbent=True)
    data = ctl.transition(data, prepare(data, admission, "autopilot_dispatch"), config)
    stage = data["entries"][0]["stages"][0]
    stage.update(
        state="confirmed",
        proof_sha256="e" * 64,
        result_selector=admission,
        result=dict(
            workflow=".github/workflows/agents-auto-pilot.yml",
            run_id="123",
            run_attempt="1",
            issue=data["entries"][0]["subjects"][2],
        ),
    )
    if field in {"run_id", "run_attempt"}:
        stage["result"][field] = 123
    else:
        stage[field] = 123
    with pytest.raises(
        ctl.ControllerUnknown,
        match={"intent_sha256": "invalid stage", "proof_sha256": "native proof missing"}.get(
            field, "native continuation identity missing"
        ),
    ):
        ctl.validate_index(data, config)


def test_binding_nontext_proof_is_unknown(config, admission):
    data = enrolled(config, admission)
    data["entries"][0]["bindings"].append(
        dict(subject=data["entries"][0]["subjects"][0], selector=admission, proof_sha256=123)
    )
    with pytest.raises(ctl.ControllerUnknown, match="subject binding changed"):
        ctl.validate_index(data, config)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.github.com/repos/STRANSKE/Example/issues/7",
        "https://api.github.com/repos/stranske/example/issues/7",
    ],
)
def test_native_api_url_allows_only_repository_case_variance(url):
    assert ctl.native_api_url(url, "stranske/example", "/issues/7")


@pytest.mark.parametrize(
    "url",
    [
        "http://api.github.com/repos/stranske/example/issues/7",
        "https://API.github.com/repos/stranske/example/issues/7",
        "https://api.github.com/repos/stranske/other/issues/7",
        "https://api.github.com/repos/stranske/example/Issues/7",
        "https://api.github.com/repos/stranske/example/issues/7/",
        "https://api.github.com/repos/stranske/example/issues/7?q=1",
        "https://api.github.com/repos/stranske/example/issues/7#part",
        "https://user@api.github.com/repos/stranske/example/issues/7",
    ],
)
def test_native_api_url_refuses_other_uri_variations(url):
    assert not ctl.native_api_url(url, "stranske/example", "/issues/7")


def test_write_intent_key_excludes_owner_but_binds_before_after_action(config, admission):
    data = empty(config)
    action = enroll_action(admission)
    after = ctl.transition(data, action, config)
    before = dict(commit_sha="a" * 40, blob_sha="b" * 40, data=data)
    one = ctl.write_intent(before, after, action, config, {"process_id": 1})
    two = ctl.write_intent(before, after, action, config, {"process_id": 2})
    assert one["transaction_key"] == two["transaction_key"]
    assert one["owner"] != two["owner"]
    changed = dict(before, commit_sha="c" * 40)
    assert (
        ctl.write_intent(changed, after, action, config, {})["transaction_key"]
        != one["transaction_key"]
    )
    assert one["before_sha256"] == ctl.digest(data)
    assert one["after_sha256"] == ctl.digest(after)
    assert one["action_sha256"] == ctl.digest(action)


def test_blob_sha_matches_fixed_git_hash_object_vectors():
    # Git hash-object --stdin outputs for empty content and b"test content\n".
    # Fixed public format vectors avoid invoking Git in this data-only test.
    assert ctl.blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert ctl.blob_sha(b"test content\n") == "d670460b4b4aece5915caf5c68d12f560a9fe3e4"


def test_observe_refuses_wrong_source_before_identity_read(monkeypatch):
    reader = ctl.NativeConsumerReader()
    calls = []
    monkeypatch.setattr(reader, "read_native_identity", lambda: calls.append(1))
    entry = type(reader).__module__ + "." + type(reader).__qualname__
    registration = dict(reader_entrypoint=entry, reader_source_sha256="0" * 64, profile_id="test")
    assert (
        hashlib.sha256(Path(ctl.__file__).read_bytes()).hexdigest()
        != registration["reader_source_sha256"]
    )
    with pytest.raises(ctl.ControllerUnknown, match="installed consumer reader source differs"):
        reader.observe(registration)
    assert calls == []


@pytest.mark.parametrize("kind", ["prepare", "bind", "confirm"])
def test_action_digest_nontext_refuses_through_unknown(config, admission, kind):
    data = enrolled(config, admission)
    if kind == "prepare":
        action = prepare(data, admission)
        action["intent_sha256"] = 123
        reason = "stage exists; reconcile never retry"
    elif kind == "bind":
        action = dict(
            kind="bind",
            key=data["entries"][0]["key"],
            subject=dict(kind="pull", number="10", node_id="PR_10"),
            selector=admission,
            proof_sha256=123,
        )
        reason = "native coding PR proof missing"
    else:
        data = ctl.transition(data, prepare(data, admission), config)
        action = confirm(data, admission)
        action["proof_sha256"] = 123
        reason = "original intent missing"
    with pytest.raises(ctl.ControllerUnknown, match=reason):
        ctl.transition(data, action, config)


def test_nontext_native_repository_and_noncanonical_wire_spelling_refuse():
    assert not ctl.native_repository_name(123, "stranske/example")
    assert not ctl.native_repository_name("stranske/example", "Stranske/example")
    assert not ctl.native_repository_name("stranske/ex ample", "stranske/ex ample")


@pytest.mark.parametrize("value", [[], {}])
def test_subject_unhashable_kind_is_unknown(value):
    with pytest.raises(ctl.ControllerUnknown, match="native subject identity missing"):
        ctl.subject(dict(kind=value, number="7", node_id="I_7"))


@pytest.mark.parametrize("field", ["phase", "state"])
@pytest.mark.parametrize("value", [[], {}])
def test_index_unhashable_stage_fields_are_unknown(config, admission, field, value):
    data = enrolled(config, admission)
    data = ctl.transition(data, prepare(data, admission), config)
    data["entries"][0]["stages"][0][field] = value
    with pytest.raises(ctl.ControllerUnknown, match="invalid stage"):
        ctl.validate_index(data, config)


@pytest.mark.parametrize("value", [[], {}])
def test_action_unhashable_phase_is_unknown(config, admission, value):
    data = enrolled(config, admission)
    action = prepare(data, admission)
    action["phase"] = value
    with pytest.raises(ctl.ControllerUnknown, match="unsupported phase"):
        ctl.transition(data, action, config)
