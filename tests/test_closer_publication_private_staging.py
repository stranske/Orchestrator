"""Private physical staging DATA controls, not genuine HostGit/IO issuance.

All object/config/hook bytes are NEW synthetic fixtures. No Git process, API,
credentials, model or source validator is invoked or replaced with success.
"""

import base64
import hashlib
import os
import zlib

import pytest
from test_closer_coding_publication import TEST_POLICY, baseline_data, candidate_data, values
from test_closer_coding_publication import custody as custody

import closer_coding_publication as pub
import closer_correction_controller as ctl


def framed(kind, raw):
    return kind.encode() + b" " + str(len(raw)).encode() + b"\0" + raw


def seed_inputs():
    baseline = baseline_data()
    objects = {}
    children = []
    for row in baseline["files"]:
        folder, name = row["path"].split("/")
        raw = base64.b64decode(row["blob"]["bytes_base64"])
        blob = framed("blob", raw)
        objects[hashlib.sha1(blob).hexdigest()] = blob
        tree = framed(
            "tree", b"100644 " + name.encode() + b"\0" + bytes.fromhex(row["blob"]["git_oid"])
        )
        oid = hashlib.sha1(tree).hexdigest()
        objects[oid] = tree
        children.append((folder, oid))
    tree = framed(
        "tree",
        b"".join(
            b"40000 " + name.encode() + b"\0" + bytes.fromhex(oid) for name, oid in sorted(children)
        ),
    )
    tree_oid = hashlib.sha1(tree).hexdigest()
    assert tree_oid == baseline["tree_oid"]
    objects[tree_oid] = tree
    commit = framed(
        "commit",
        b"tree "
        + tree_oid.encode()
        + b"\nauthor Fixture <fixture@example.invalid> 1 +0000\ncommitter Fixture <fixture@example.invalid> 1 +0000\n\nfixture\n",
    )
    head = hashlib.sha1(commit).hexdigest()
    objects[head] = commit
    baseline["head_oid"] = head
    candidate = candidate_data(baseline)
    pr = {**values()[0]["pr"], "head_oid": head, "base_oid": baseline["base_oid"]}
    config = b"[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tfilemode = true\n"
    return baseline, candidate, pr, objects, config


def build(custody, key="a" * 64):
    baseline, candidate, pr, objects, config = seed_inputs()
    return pub._build_staging_data(
        custody, key, baseline, candidate, pr, TEST_POLICY, objects, config, {}
    )


def test_exact_private_objects_candidate_and_metadata(custody):
    baseline, candidate, pr, objects, config = seed_inputs()
    original = custody.path / "original-do-not-mutate"
    original.write_bytes(b"original refs/index marker")
    stage = pub._build_staging_data(
        custody, "a" * 64, baseline, candidate, pr, TEST_POLICY, objects, config, {}
    )
    try:
        assert stage.path.parent == custody.path
        assert stage.path.stat().st_mode & 0o777 == 0o700
        actual_config = (stage.path / "repo.git/config").read_bytes()
        assert actual_config.startswith(config)
        assert actual_config.endswith(
            (
                "\n[credential]\n\thelper =\n\thelper = !"
                + pub._SUPPORTED_GH_PROTOCOL["path"]
                + " auth git-credential\n"
            ).encode()
        )
        assert stage.marker["original_config_sha256"] == hashlib.sha256(config).hexdigest()
        assert (stage.path / "repo.git/HEAD").read_text() == "ref: refs/heads/" + pr[
            "head_ref"
        ] + "\n"
        assert (stage.path / "repo.git/refs/heads" / pr["head_ref"]).read_text() == pr[
            "head_oid"
        ] + "\n"
        for oid, raw in objects.items():
            assert (
                zlib.decompress((stage.path / "repo.git/objects" / oid[:2] / oid[2:]).read_bytes())
                == raw
            )
        assert (stage.path / "worktree/src/a.py").read_bytes() == b"changed\0exact bytes"
        assert (stage.path / "worktree/tests/test_a.py").read_bytes() == b"retained tests"
        assert stage.check()["git_execution_authorized"] is False
        assert original.read_bytes() == b"original refs/index marker"
    finally:
        stage.close()


def test_same_staging_slot_is_never_recreated(custody):
    stage = build(custody)
    stage.close()
    with pytest.raises(pub.PublicationHeld, match="already owned"):
        build(custody)


@pytest.mark.parametrize("mutation", ["missing", "unbound", "hash", "framing", "count"])
def test_seed_graph_requires_exact_bounded_native_head(mutation):
    baseline, _, pr, objects, _ = seed_inputs()
    if mutation == "missing":
        objects.pop(baseline["tree_oid"])
    elif mutation == "unbound":
        raw = framed("blob", b"not reachable")
        objects[hashlib.sha1(raw).hexdigest()] = raw
    elif mutation == "hash":
        objects[pr["head_oid"]] += b"bad"
    elif mutation == "framing":
        raw = b"commit 999\0bad"
        head = hashlib.sha1(raw).hexdigest()
        objects = {head: raw}
        pr["head_oid"] = head
    else:
        objects = {str(i).zfill(40): b"x" for i in range(TEST_POLICY["max_files"] + 1)}
    with pytest.raises(pub.PublicationHeld):
        pub._validate_seed_data(objects, pr["head_oid"], TEST_POLICY)


@pytest.mark.parametrize("kind", ["config", "hook", "root_alias", "hooks_alias"])
def test_private_control_or_ancestor_change_refuses(custody, kind):
    stage = build(custody)
    try:
        if kind == "config":
            (stage.path / "repo.git/config").write_bytes(b"changed")
        elif kind == "hook":
            (stage.path / "repo.git/hooks/pre-commit").write_bytes(b"must not execute")
        else:
            target = stage.path if kind == "root_alias" else stage.path / "repo.git/hooks"
            moved = target.with_name(target.name + "-retained")
            target.rename(moved)
            target.symlink_to(moved, target_is_directory=True)
        with pytest.raises(pub.PublicationHeld):
            stage.check()
    finally:
        stage.close()


def test_fd_relative_writer_refuses_symlink_parent(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (root / "alias").symlink_to(outside, target_is_directory=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            pub._stage_write_data(fd, "alias/no-write", b"denied")
        assert not (outside / "no-write").exists()
    finally:
        os.close(fd)


def test_genuine_staging_source_cannot_be_selected_by_data():
    with pytest.raises(pub.PublicationHeld, match="actual known-result staging producer"):
        pub._issue_private_staging({"permit": True}, {}, {})
    with pytest.raises(pub.PublicationHeld, match="source staging constructor"):
        pub._SourceStagingLease({}, {}, {}, {})


def step_fixture(custody):
    import types

    from test_closer_publication_mutation_body import inputs

    prepared, _, _ = inputs()
    intent = prepared["intent"]
    command = pub._backend_command_data(intent, "read_tree")
    reference = pub._retain_step_intent_data(
        custody, intent, prepared["baseline"], {"deadline": 12.5}, 1, "read_tree", command, None
    )
    observation = {
        "source": "trusted-popen-transport/v1",
        "transport_argv": command,
        "pid": 123,
        "process_group": 123,
        "os_started": "synthetic birth",
        "cleanup_terminal": True,
    }
    result = types.SimpleNamespace(returncode=0, stdout=b"")
    return prepared, reference, observation, result


def test_per_command_intent_precedes_result_and_cannot_repeat(custody):
    prepared, reference, observation, result = step_fixture(custody)
    assert not (custody.path / pub._step_data_name(reference["slot_key"], 1, "result")).exists()
    stored = custody.read(reference["name"])
    assert stored["phase"] == "read_tree"
    assert all(stored[k] is False for k in pub._AUTHORITY)
    pub._retain_step_result_data(custody, reference, result, observation, TEST_POLICY)
    with pytest.raises(pub.PublicationHeld, match="already owned"):
        pub._retain_step_intent_data(
            custody,
            prepared["intent"],
            prepared["baseline"],
            {"deadline": 12.5},
            1,
            "read_tree",
            observation["transport_argv"],
            None,
        )
    with pytest.raises(pub.PublicationHeld, match="already retained"):
        pub._retain_step_result_data(custody, reference, result, observation, TEST_POLICY)


def test_next_command_needs_prior_known_result(custody):
    prepared, reference, observation, result = step_fixture(custody)
    staging = pub.staging_payload_data(
        prepared["baseline"], prepared["intent"]["candidate"], TEST_POLICY
    )
    blob = staging["blobs"][0]
    identity = {"fd": 5, "dev": 1, "ino": 2, "size": blob["size"], "sha256": blob["sha256"]}
    command = pub._backend_command_data(prepared["intent"], "hash_blob")
    with pytest.raises(FileNotFoundError):
        pub._retain_step_intent_data(
            custody,
            prepared["intent"],
            prepared["baseline"],
            {"deadline": 12.5},
            2,
            "hash_blob",
            command,
            identity,
        )
    pub._retain_step_result_data(custody, reference, result, observation, TEST_POLICY)
    next_reference = pub._retain_step_intent_data(
        custody,
        prepared["intent"],
        prepared["baseline"],
        {"deadline": 12.5},
        2,
        "hash_blob",
        command,
        identity,
    )
    assert custody.read(next_reference["name"])["previous_result_sha256"] == pub.digest(
        custody.read(pub._step_data_name(reference["slot_key"], 1, "result"))
    )


@pytest.mark.parametrize("mutation", ["argv", "cleanup", "group", "returncode"])
def test_command_result_mismatch_stays_intent_without_result(custody, mutation):
    _, reference, observation, result = step_fixture(custody)
    if mutation == "argv":
        observation["transport_argv"] = ["wrong"]
    elif mutation == "cleanup":
        observation["cleanup_terminal"] = False
    elif mutation == "group":
        observation["process_group"] = 321
    else:
        result.returncode = True
    with pytest.raises(pub.PublicationHeld):
        pub._retain_step_result_data(custody, reference, result, observation, TEST_POLICY)
    assert not (custody.path / pub._step_data_name(reference["slot_key"], 1, "result")).exists()


def test_final_descriptor_keeps_missing_publication_io_authority_held():
    with pytest.raises(pub.PublicationHeld, match="actual publication IO scope absent"):
        pub._require_publication_io_admission(
            None, None, {"action": "correction_coding"}, {"publication_v1": True}, None
        )


@pytest.mark.parametrize("alias", [False, 0.0])
def test_prior_result_type_alias_does_not_authorize_next_step(custody, alias):
    prepared, reference, observation, result = step_fixture(custody)
    pub._retain_step_result_data(custody, reference, result, observation, TEST_POLICY)
    name = pub._step_data_name(reference["slot_key"], 1, "result")
    path = custody.path / name
    import json

    stored = json.loads(path.read_bytes())
    stored["returncode"] = alias
    path.write_bytes(pub._bytes(stored))
    staging = pub.staging_payload_data(
        prepared["baseline"], prepared["intent"]["candidate"], TEST_POLICY
    )
    blob = staging["blobs"][0]
    identity = {"fd": 5, "dev": 1, "ino": 2, "size": blob["size"], "sha256": blob["sha256"]}
    with pytest.raises((pub.PublicationHeld, ctl.ControllerUnknown)):
        pub._retain_step_intent_data(
            custody,
            prepared["intent"],
            prepared["baseline"],
            {"deadline": 12.5},
            2,
            "hash_blob",
            pub._backend_command_data(prepared["intent"], "hash_blob"),
            identity,
        )
