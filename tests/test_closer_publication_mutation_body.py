"""Necessary detached backend mechanics, never a source-issued publisher positive.

The source front door is tested with its genuine missing issuers. For sequence
mechanics only, compile the actual body in a detached namespace with its two
external Git/API effect names bound to inert DATA ports. No source validator,
qualification, writer or genuine issuer is replaced with success.
"""

import ast
import base64
import hashlib
from pathlib import Path

import pytest
from test_closer_coding_publication import baseline_data, candidate_data, values
from test_closer_coding_publication import custody as custody

import closer_coding_publication as pub


def inputs():
    intent, _, _, _ = values()
    baseline = baseline_data()
    candidate = candidate_data(baseline)
    pr = {**intent["pr"], "head_oid": baseline["head_oid"], "base_oid": baseline["base_oid"]}
    intent = pub.prepare_intent(
        intent["original"], intent["policy"], pr, candidate, intent["provenance"]
    )
    raw = (
        "tree "
        + candidate["tree_oid"]
        + "\nparent "
        + pr["head_oid"]
        + "\nauthor Source <source@example.invalid> 1 +0000\ncommitter Source <source@example.invalid> 1 +0000\n\nApply authenticated correction\n"
    ).encode()
    oid = hashlib.sha1(b"commit " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    return {"intent": intent, "baseline": baseline}, raw, oid


def detached_body(git, api):
    tree = ast.parse(Path(pub.__file__).read_text())
    function = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_execute_prepared_backend"
    )
    namespace = dict(pub.__dict__)
    namespace.update(_backend_phase_transport=git, _native_publication_readback=api)
    exec(
        compile(
            ast.Module(body=[function], type_ignores=[]), "<detached-backend-mechanics>", "exec"
        ),
        namespace,
    )
    return namespace[function.name]


def ports(prepared, raw_commit, oid, fail=None):
    calls = []
    intent = prepared["intent"]

    def git(controller, store, operation, selection, host, phase, argv, raw):
        calls.append(phase)
        if phase == fail:
            raise TimeoutError("inert effect port")
        if phase == "hash_blob":
            return (pub.blob_oid(raw) + "\n").encode()
        if phase == "write_tree":
            return (intent["candidate"]["tree_oid"] + "\n").encode()
        if phase == "head":
            return (oid + "\n").encode()
        if phase == "commit_object":
            return raw_commit
        if phase == "push":
            return (
                "To https://github.com/acme/repo.git\n \t"
                + oid
                + ":refs/heads/correction/owned\t"
                + intent["pr"]["head_oid"][:7]
                + ".."
                + oid[:7]
                + "\nDone\n"
            ).encode()
        return b""

    def api(controller, store, operation, selection, host, commit):
        calls.append("native_readback")
        if fail == "native_readback":
            raise pub.PublicationHeld("inert missing readback")
        pr = {**intent["pr"], "head_oid": oid}
        return (
            pr,
            {
                "repository_id": pr["repository_id"],
                "ref": "refs/heads/" + pr["head_ref"],
                "oid": oid,
                "object_type": "commit",
            },
            {
                "repository_id": pr["repository_id"],
                "oid": oid,
                "tree_oid": commit["tree_oid"],
                "parent_oids": commit["parent_oids"],
            },
        )

    return git, api, calls


def test_detached_sequence_ack_readback_is_contract_only(custody):
    prepared, raw, oid = inputs()
    git, api, calls = ports(prepared, raw, oid)
    result = detached_body(git, api)(
        None, custody, {}, {"intent": prepared["intent"]}, None, prepared
    )
    assert calls == [
        "read_tree",
        "hash_blob",
        "update_index",
        "write_tree",
        "commit",
        "head",
        "commit_object",
        "push",
        "native_readback",
    ]
    assert result["contract_consistent"] is True
    assert all(result[k] is False for k in pub._AUTHORITY)
    assert pub.ContractLedger(custody).read(result["slot_key"])["state"] == "readback"


@pytest.mark.parametrize(
    "phase",
    [
        "read_tree",
        "hash_blob",
        "update_index",
        "write_tree",
        "commit",
        "head",
        "commit_object",
        "push",
        "native_readback",
    ],
)
def test_owned_failure_never_restarts_mutation(custody, phase):
    prepared, raw, oid = inputs()
    git, api, calls = ports(prepared, raw, oid, phase)
    body = detached_body(git, api)
    with pytest.raises((TimeoutError, pub.PublicationHeld)):
        body(None, custody, {}, {"intent": prepared["intent"]}, None, prepared)
    ledger = pub.ContractLedger(custody)
    key = prepared["intent"]["slot_key"]
    assert ledger.read(key)["state"] == "unknown"
    before = list(calls)
    with pytest.raises(pub.PublicationHeld):
        body(None, custody, {}, {"intent": prepared["intent"]}, None, prepared)
    assert calls == before


def test_exact_commands_preserve_hooks_and_single_ref_lease():
    prepared, raw, oid = inputs()
    intent = prepared["intent"]
    assert pub._backend_command_data(intent, "commit") == [
        "/source-owned/git",
        "commit",
        "--file=-",
    ]
    commit = pub._backend_commit_data(intent, oid, raw)
    push = pub.push_command_data(intent, commit)["argv"]
    assert "--verify" in push
    assert "--force-with-lease=refs/heads/correction/owned:" + intent["pr"]["head_oid"] in push
    assert "--force" not in push and "--no-verify" not in push
    for name in ("config", "init", "checkout", "fetch", "commit-tree", "reset"):
        with pytest.raises(pub.PublicationHeld):
            pub._backend_command_data(intent, name)


def test_second_parent_or_wrong_tree_refused():
    prepared, _, _ = inputs()
    intent = prepared["intent"]
    for raw in (
        (
            "tree "
            + intent["candidate"]["tree_oid"]
            + "\nparent "
            + intent["pr"]["head_oid"]
            + "\nparent "
            + "a" * 40
            + "\n\nx\n"
        ).encode(),
        ("tree " + "f" * 40 + "\nparent " + intent["pr"]["head_oid"] + "\n\nx\n").encode(),
    ):
        oid = hashlib.sha1(b"commit " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        with pytest.raises(pub.PublicationHeld):
            pub._backend_commit_data(intent, oid, raw)


def test_actual_staging_and_ref_issuers_hold():
    with pytest.raises(pub.PublicationHeld, match="actual publication IO scope absent"):
        pub._prepare_mutation_backend(None, None, None, None, None)
    with pytest.raises(pub.PublicationHeld, match="actual publication IO scope absent"):
        pub._native_publication_readback(None, None, None, None, None, None)


def test_staging_raw_payload_is_exact():
    prepared, _, _ = inputs()
    staging = pub.staging_payload_data(
        prepared["baseline"], prepared["intent"]["candidate"], prepared["intent"]["policy"]
    )
    blob = staging["blobs"][0]
    raw = base64.b64decode(blob["bytes_base64"], validate=True)
    assert b"\0" in raw
    assert pub.blob_oid(raw) == blob["git_oid"]
    assert (
        hashlib.sha256(base64.b64decode(staging["index_bytes_base64"], validate=True)).hexdigest()
        == staging["index_sha256"]
    )


def test_actual_front_door_has_zero_effect_ports(custody, monkeypatch):
    import closer_correction_controller as ctl
    import dispatcher

    calls = []
    monkeypatch.setattr(dispatcher, "run_observed_offload", lambda *a, **kw: calls.append("Git"))
    # Exact types are necessary; this detached controller still cannot issue
    # selection and is refused by the real source-owned IO scope producer.
    controller = object.__new__(ctl.IndexController)
    with pytest.raises(pub.PublicationHeld, match="actual publication IO scope absent"):
        pub.execute_existing_update(controller, custody, {}, {})
    assert calls == []


def test_actual_unissued_phase_reuses_only_immutable_payload_data(custody, monkeypatch):
    import dispatcher

    prepared, _, _ = inputs()
    calls = []
    monkeypatch.setattr(dispatcher, "run_observed_offload", lambda *a, **kw: calls.append("Git"))
    command = pub._backend_command_data(prepared["intent"], "hash_blob")
    for _ in range(2):
        with pytest.raises(pub.PublicationHeld, match="transport descriptor unavailable"):
            pub._backend_phase_transport(
                None,
                custody,
                {},
                {"intent": prepared["intent"]},
                None,
                "hash_blob",
                command,
                b"same payload",
            )
    assert calls == []


@pytest.mark.parametrize("failure", ["missing_ack", "wrong_readback"])
def test_remote_write_without_exact_ack_or_readback_remains_unknown(custody, failure):
    prepared, raw, oid = inputs()
    git, api, calls = ports(prepared, raw, oid)

    def remote_git(*args):
        result = git(*args)
        return b"" if args[5] == "push" and failure == "missing_ack" else result

    def remote_api(*args):
        pr, ref, commit = api(*args)
        if failure == "wrong_readback":
            ref["oid"] = "f" * 40
        return pr, ref, commit

    with pytest.raises(pub.PublicationHeld):
        detached_body(remote_git, remote_api)(
            None, custody, {}, {"intent": prepared["intent"]}, None, prepared
        )
    assert calls.count("push") == 1
    assert pub.ContractLedger(custody).read(prepared["intent"]["slot_key"])["state"] == "unknown"
