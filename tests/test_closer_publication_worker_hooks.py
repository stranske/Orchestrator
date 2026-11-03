"""Necessary source callsite controls, not a successful native worker fixture.

No authority validator is replaced. Detached source call blocks prove refusal and
ordering only; real root/callback/HostGit issuance remains unavailable.
"""

import ast
import copy
import hashlib
import inspect
import json
from types import SimpleNamespace

import pytest
from test_closer_coding_publication import TEST_POLICY
from test_closer_coding_session_v2 import consumers

import closer_coding_publication as publication
import closer_coding_session_v2 as session
import closer_coding_worker as worker
import closer_queue as queue


def configured():
    values = consumers()
    consumer_id = next(iter(values))
    policy = {**copy.deepcopy(TEST_POLICY), "consumer_id": consumer_id}
    values[consumer_id]["publication_v1"] = policy
    return values, consumer_id


def test_absent_mode_keeps_exact_original_snapshot_bytes():
    draft = session.prepare_admission_policy(consumers())
    assert hashlib.sha256(draft.snapshot_utf8).hexdigest() == (
        "4c91ee68d501a03d49bb0b05211b030e9338df42c2df814e1d97b4954d80a6a8"
    )
    assert "publication_v1" not in json.loads(draft.snapshot_utf8)
    assert publication.validate_consumer_mode("legacy", {}, {}) is None


def test_new_original_snapshot_retains_explicit_detached_publication_policy():
    values, consumer_id = configured()
    draft = session.prepare_admission_policy(values)
    original = json.loads(draft.snapshot_utf8)
    assert session.validate_admission_snapshot(original) == original
    selected = publication.validate_consumer_mode(consumer_id, values[consumer_id], original)
    assert selected == original["publication_v1"]
    values[consumer_id]["publication_v1"]["max_files"] = 1
    assert original["publication_v1"]["max_files"] == 8
    with pytest.raises(publication.PublicationHeld, match="differs"):
        publication.validate_consumer_mode(consumer_id, values[consumer_id], original)


@pytest.mark.parametrize("change", ["add", "remove", "null", "wrong_consumer", "unknown"])
def test_original_policy_cannot_be_retrofitted_or_reinterpreted(change):
    values, consumer_id = configured()
    registration = values[consumer_id]
    original = json.loads(session.prepare_admission_policy(values).snapshot_utf8)
    if change == "add":
        original.pop("publication_v1")
    if change == "remove":
        registration.pop("publication_v1")
    if change == "null":
        registration["publication_v1"] = None
    if change == "wrong_consumer":
        registration["publication_v1"]["consumer_id"] = "foreign"
    if change == "unknown":
        registration["publication_v1"]["permit"] = True
    with pytest.raises(publication.PublicationHeld):
        publication.validate_consumer_mode(consumer_id, registration, original)


def test_malformed_publication_mode_cannot_get_a_new_original_snapshot():
    values, consumer_id = configured()
    values[consumer_id]["publication_v1"] = {"permit": True}
    assert session.prepare_admission_policy(values).snapshot_utf8 is None


def source_nodes():
    return ast.parse(inspect.getsource(worker._execute)).body[0].body


def test_unissued_selected_baseline_block_has_zero_provider_effects():
    nodes = source_nodes()
    index = next(i for i, node in enumerate(nodes) if isinstance(node, ast.With))
    inner = next(
        n
        for n in ast.walk(nodes[index])
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Compare)
        and isinstance(n.test.left, ast.Name)
        and n.test.left.id == "publication_policy"
        and any(isinstance(c, ast.Attribute) and c.attr == "retain_baseline" for c in ast.walk(n))
    )
    effects = []
    env = dict(
        publication_policy=TEST_POLICY,
        queue=queue,
        publication=publication,
        root=object(),
        controller=object(),
        guard=SimpleNamespace(custody=object()),
        operation={},
        dispatcher=SimpleNamespace(offload=lambda *a, **kw: effects.append("provider")),
    )
    # Execute the exact closed source branch on detached objects. It must refuse
    # before authority/effects; this is not actual _execute or an issued root.
    with pytest.raises(publication.PublicationHeld, match="callback unavailable"):
        exec(
            compile(ast.Module(body=[inner], type_ignores=[]), "<detached-baseline-block>", "exec"),
            env,
        )
    assert effects == []
    # The actual source text places that block before the sole offload call.
    calls = [
        (n.lineno, n.func.attr)
        for n in ast.walk(ast.parse(inspect.getsource(worker._execute)))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"retain_baseline", "offload", "retain_handoff"}
    ]
    ordered = [name for _, name in sorted(calls)]
    assert ordered == ["retain_baseline", "offload", "retain_handoff"]


def test_absent_mode_return_parity_and_exact_reference_identity():
    returned = next(
        n.value
        for n in ast.walk(ast.parse(inspect.getsource(worker._execute)))
        if isinstance(n, ast.Return)
    )
    raw, terminal_ref, handoff = object(), object(), object()
    values = dict(
        result=raw,
        terminal_ref=terminal_ref,
        publication_ref=None,
        publication_held=False,
        _NO_AUTHORITY=worker._NO_AUTHORITY,
    )
    expression = compile(ast.Expression(returned), "<actual-return-data>", "eval")
    result = eval(expression, values)
    assert result == {
        "status": "completed",
        "coding_terminal_reference": terminal_ref,
        "actual": raw,
        **{k: False for k in worker._NO_AUTHORITY},
    }
    values["publication_ref"] = handoff
    assert eval(expression, values)["publication_handoff_reference"] is handoff
    assert eval(expression, values)["actual"] is raw


@pytest.mark.parametrize(
    "error", [publication.PublicationHeld("held"), OSError("private"), RuntimeError("private")]
)
def test_handoff_refusal_preserves_known_result_without_provider_repeat(error):
    node = next(
        n
        for n in ast.walk(ast.parse(inspect.getsource(worker._execute)))
        if isinstance(n, ast.Try)
        and not n.finalbody
        and any(isinstance(c, ast.Attribute) and c.attr == "retain_handoff" for c in ast.walk(n))
    )

    # Mock only the failing handoff effect: no successful source validator stub.
    class Refusal:
        def retain_handoff(self, *args):
            raise error

    raw = {"retained": ["exact raw result"]}
    original = copy.deepcopy(raw)
    values = dict(
        publication=Refusal(),
        root=object(),
        controller=object(),
        guard=SimpleNamespace(custody=object()),
        operation={},
        result=raw,
        publication_baseline_ref=object(),
        terminal_ref=object(),
        publication_ref=None,
    )
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), "<known-result-handoff-refusal>", "exec"),
        values,
    )
    assert values["publication_held"] is True
    assert values["publication_ref"] is None
    assert values["result"] is raw and raw == original
    assert not any(isinstance(n, ast.Attribute) and n.attr == "offload" for n in ast.walk(node))


def test_detached_actual_entrypoint_cannot_claim_callback_custody():
    for function, args in [
        (publication.retain_baseline, [object()] * 4),
        (publication.retain_handoff, [object()] * 7),
    ]:
        with pytest.raises(publication.PublicationHeld, match="callback unavailable"):
            function(*args)
