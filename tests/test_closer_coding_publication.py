"""Inert DATA contracts and real private custody; never native delivery authority.

No Git/API/credential/provider process is run. ACK/readback values are mock data.
The source issuer and protected Git/config/helper context remain unavailable.
"""

import base64
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_coding_publication as pub
import closer_correction_controller as ctl

TEST_POLICY = {
    "allowed_path_prefixes": ["src", "tests"],
    "consumer_id": "coding-owner",
    "git_executable": {
        "path": "/source-owned/git",
        "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    },
    "max_ack_bytes": 16384,
    "max_file_bytes": 4096,
    "max_files": 8,
    "max_native_io_seconds": 15,
    "max_push_submissions": 1,
    "max_total_bytes": 8192,
    "mode": "owned_existing_pr",
    "request_timeout_seconds": 120,
    "schema": "closer-owned-pr-publication-policy/v1",
    "trusted_context_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
}

COMMAND_TEMPLATE = [
    "/source-owned/git",
    "push",
    "--porcelain",
    "--verify",
    "--force-with-lease=refs/heads/correction/owned:<H>",
    "https://github.com/acme/repo.git",
    "<C>:refs/heads/correction/owned",
]


def values():
    policy = copy.deepcopy(TEST_POLICY)
    original = {
        "repository_id": 17,
        "epoch": "synthetic",
        "effect_key": "a" * 64,
        "request_sha256": "b" * 64,
        "consumer_id": "coding-owner",
    }
    pr = {
        "repository": "acme/repo",
        "repository_id": 17,
        "pr_number": 19,
        "pr_node_id": "PR_19",
        "issue_node_id": "I_9",
        "owner_login": "owner",
        "owner_id": 23,
        "writer_login": "owner",
        "writer_id": 23,
        "head_repo_id": 17,
        "base_repo_id": 17,
        "head_ref": "correction/owned",
        "head_oid": "a" * 40,
        "base_ref": "main",
        "base_oid": "b" * 40,
        "body_sha256": "c" * 64,
        "state": "open",
        "is_draft": False,
        "is_merged": False,
        "closing_issue_node_ids": ["I_9"],
    }
    raw = b"fixed bytes\n"
    candidate = {
        "base_oid": pr["base_oid"],
        "old_head_oid": pr["head_oid"],
        "old_tree_oid": "c" * 40,
        "tree_oid": "d" * 40,
        "unchanged_tree_sha256": "d" * 64,
        "files": [
            {
                "path": "src/fix.py",
                "before": {"mode": 100644, "git_oid": "e" * 40, "sha256": "e" * 64},
                "after": {
                    "mode": 100644,
                    "git_oid": pub.blob_oid(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes_base64": base64.b64encode(raw).decode(),
                },
            }
        ],
    }
    provenance = dict.fromkeys(
        [
            "baseline_sha256",
            "handoff_sha256",
            "terminal_sha256",
            "source_scope_sha256",
            "original_admission_sha256",
            "fresh_io_admission_sha256",
            "current_operation_sha256",
        ],
        "f" * 64,
    )
    intent = pub.prepare_intent(original, policy, pr, candidate, provenance)
    commit = {
        "commit_oid": "f" * 40,
        "tree_oid": candidate["tree_oid"],
        "parent_oids": [pr["head_oid"]],
        "candidate_sha256": pub.digest(candidate),
        "returncode": 0,
        "group_terminal": True,
    }
    ack = f"To https://github.com/acme/repo.git\n \t{'f'*40}:refs/heads/correction/owned\taaaaaaa..fffffff\nDone\n".encode()
    updated = {**pr, "head_oid": commit["commit_oid"]}
    ref = {
        "repository_id": 17,
        "ref": "refs/heads/correction/owned",
        "oid": commit["commit_oid"],
        "object_type": "commit",
    }
    native_commit = {
        "repository_id": 17,
        "oid": commit["commit_oid"],
        "tree_oid": commit["tree_oid"],
        "parent_oids": commit["parent_oids"],
    }
    return intent, commit, ack, (updated, ref, native_commit)


@pytest.fixture
def custody(tmp_path):
    root = tmp_path.resolve()
    state = root / "new-data-only-state.json"
    state.write_text("{}")
    state.chmod(0o600)
    evidence = Path(str(state) + ".evidence")
    evidence.mkdir(mode=0o700)
    (evidence / "publication-data").mkdir(mode=0o700)
    store = ctl._Custody(SimpleNamespace(state_path=str(state), namespace="publication-data"))
    try:
        yield store
    finally:
        store.close()


def started(custody):
    intent, commit, ack, readback = values()
    ledger = pub.ContractLedger(custody)
    key = ledger.begin(intent)
    ledger.commit_started(key)
    ledger.commit_ack(key, commit)
    return ledger, key, intent, commit, ack, readback


def test_policy_is_explicit_detached_data_and_cannot_grant_authority():
    intent, _, _, _ = values()
    result = pub.validate_policy(intent["policy"])
    result["max_files"] = 1
    assert intent["policy"]["max_files"] == 8
    for flag in pub._AUTHORITY:
        assert intent[flag] is False
    for field in pub._BOUNDS:
        bad = copy.deepcopy(intent["policy"])
        del bad[field]
        with pytest.raises(pub.PublicationHeld):
            pub.validate_policy(bad)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_files", True),
        ("max_total_bytes", 0),
        ("request_timeout_seconds", 121),
        ("max_native_io_seconds", 16),
        ("max_push_submissions", 2),
        ("mode", "new_pr"),
        ("permit", True),
    ],
)
def test_policy_unknown_keys_numeric_alias_or_new_mode_hold(field, value):
    intent, _, _, _ = values()
    with pytest.raises(pub.PublicationHeld):
        pub.validate_policy({**intent["policy"], field: value})


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/outside",
        "src/.git/config",
        "src/.env",
        "src/.gitattributes",
        "src/a\0b",
        "src/back\\slash",
        "unowned/fix.py",
    ],
)
def test_changed_bytes_must_have_safe_source_selected_paths(path):
    intent, _, _, _ = values()
    candidate = copy.deepcopy(intent["candidate"])
    candidate["files"][0]["path"] = path
    with pytest.raises(pub.PublicationHeld):
        pub.validate_candidate(candidate, intent["policy"])


def test_changed_byte_hashes_and_total_bounds_are_enforced():
    intent, _, _, _ = values()
    candidate = copy.deepcopy(intent["candidate"])
    candidate["files"][0]["after"]["bytes_base64"] = base64.b64encode(b"changed").decode()
    with pytest.raises(pub.PublicationHeld, match="byte identity"):
        pub.validate_candidate(candidate, intent["policy"])
    policy = {**intent["policy"], "max_file_bytes": 1, "max_total_bytes": 1}
    with pytest.raises(pub.PublicationHeld):
        pub.validate_candidate(intent["candidate"], policy)


def test_stable_slot_excludes_new_pr_head_worker_campaign_and_policy_dimensions():
    intent, _, _, _ = values()
    key = pub.stable_slot(intent["original"])
    assert key == intent["slot_key"]
    for dimension in (
        "pr_number",
        "head_oid",
        "worker_id",
        "campaign_id",
        "operation_id",
        "policy_version",
    ):
        with pytest.raises(pub.PublicationHeld):
            pub.stable_slot({**intent["original"], dimension: "cannot-reset"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("is_draft", 0),
        ("is_merged", True),
        ("writer_id", 24),
        ("head_repo_id", 18),
        ("head_ref", "main"),
        ("closing_issue_node_ids", ["I_9", "I_foreign"]),
    ],
)
def test_only_exact_owned_corrective_pr_shape_is_supported(field, value):
    intent, _, _, _ = values()
    with pytest.raises(pub.PublicationHeld):
        pub.validate_owned_pr({**intent["pr"], field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("parent_oids", ["0" * 40]),
        ("parent_oids", ["a" * 40, "0" * 40]),
        ("tree_oid", "0" * 40),
        ("candidate_sha256", "0" * 64),
        ("returncode", False),
        ("group_terminal", 1),
    ],
)
def test_single_parent_exact_tree_not_a_lease_alone(field, value):
    intent, commit, _, _ = values()
    with pytest.raises(pub.PublicationHeld):
        pub.validate_commit_data(intent, {**commit, field: value})


def test_exact_explicit_lease_command_is_data_not_permission():
    intent, commit, _, _ = values()
    command = pub.push_command_data(intent, commit)
    expected = COMMAND_TEMPLATE
    expected = [arg.replace("<H>", "a" * 40).replace("<C>", "f" * 40) for arg in expected]
    assert command["argv"] == expected
    assert command["git_execution_authorized"] is False
    assert "--force" not in command["argv"] and "--no-verify" not in command["argv"]


@pytest.mark.parametrize(
    "change", ["wrong_repo", "forced", "new_branch", "failed", "wrong_ref", "missing", "large"]
)
def test_mock_push_ack_must_be_one_expected_fast_forward(change):
    intent, commit, raw, _ = values()
    if change == "wrong_repo":
        raw = raw.replace(b"acme/repo", b"other/repo")
    elif change == "forced":
        raw = raw.replace(b"\n \t", b"\n+\t")
    elif change == "new_branch":
        raw = raw.replace(b"\n \t", b"\n*\t")
    elif change == "failed":
        raw = raw.replace(b"\n \t", b"\n!\t")
    elif change == "wrong_ref":
        raw = raw.replace(b"correction/owned", b"correction/foreign")
    elif change == "missing":
        raw = b""
    else:
        raw = b"x" * (intent["policy"]["max_ack_bytes"] + 1)
    with pytest.raises(pub.PublicationHeld):
        pub.parse_push_ack_data(intent, commit, raw, 0)


def test_mock_readback_checks_owner_ref_parent_tree_and_typed_json():
    intent, commit, _, (pr, ref, native_commit) = values()
    pub.validate_readback_data(intent, commit, pr, ref, native_commit)
    for bad_pr, bad_ref, bad_commit in [
        ({**pr, "owner_login": "foreign"}, ref, native_commit),
        (pr, {**ref, "oid": "0" * 40}, native_commit),
        (pr, ref, {**native_commit, "parent_oids": ["0" * 40]}),
        (pr, ref, {**native_commit, "repository_id": 17.0}),
    ]:
        with pytest.raises(pub.PublicationHeld):
            pub.validate_readback_data(intent, commit, bad_pr, bad_ref, bad_commit)


def test_mock_effect_contract_journal_can_be_consistent_but_never_native(custody):
    ledger, key, _, _, raw, readback = started(custody)
    command = ledger.push_started(key)
    effects = []
    effects.append(command["argv"])  # A mock DATA effect, never subprocess/API execution.
    ledger.push_ack(key, raw, 0)
    result = ledger.readback(key, *readback)
    assert len(effects) == 1 and result["contract_consistent"] is True
    assert ledger.read(key)["state"] == "readback"
    assert all(result[k] is False for k in pub._AUTHORITY)
    assert result["schema"] != "native-owned-pr-update-origin/v1"


@pytest.mark.parametrize("phase", ["commit_started", "push_started", "push_ack"])
def test_unknown_never_produces_a_second_mock_commit_or_push(custody, phase):
    intent, commit, raw, _ = values()
    ledger = pub.ContractLedger(custody)
    key = ledger.begin(intent)
    ledger.commit_started(key)
    effects = ["mock commit"]
    if phase != "commit_started":
        ledger.commit_ack(key, commit)
        ledger.push_started(key)
        effects.append("mock push")
        if phase == "push_ack":
            ledger.push_ack(key, raw, 0)
    ledger.unknown(key, "transport_timeout")
    count = len(effects)
    with pytest.raises(pub.PublicationHeld):
        ledger.commit_started(key)
        effects.append("second commit")
    with pytest.raises(pub.PublicationHeld):
        ledger.push_started(key)
        effects.append("second push")
    assert len(effects) == count and ledger.read(key)["state"] == "unknown"


def test_lost_ack_with_matching_head_is_not_confirmation_or_push_permission(custody):
    ledger, key, _, _, _, readback = started(custody)
    ledger.push_started(key)
    with pytest.raises(pub.PublicationHeld):
        ledger.readback(key, *readback)
    with pytest.raises(pub.PublicationHeld):
        ledger.push_started(key)
    assert ledger.read(key)["state"] == "push_started"


def test_existing_intent_or_changed_candidate_cannot_restart_slot(custody):
    ledger, key, intent, _, _, _ = started(custody)
    with pytest.raises(pub.PublicationHeld):
        ledger.begin(intent)
    changed = copy.deepcopy(intent)
    changed["provenance"]["current_operation_sha256"] = "0" * 64
    with pytest.raises((pub.PublicationHeld, ctl.ControllerUnknown)):
        ledger.begin(changed)
    assert ledger.read(key)["state"] == "commit_ack"


def test_lost_started_event_does_not_reset_consumed_push_attempt(custody):
    ledger, key, _, _, _, _ = started(custody)
    ledger.push_started(key)
    (custody.path / f"publication-contract-{key}-03.json").unlink()
    with pytest.raises(pub.PublicationHeld, match="attempt already owned"):
        ledger.push_started(key)


def test_custody_replacement_or_symlink_cannot_be_followed(custody):
    ledger, key, _, _, _, _ = started(custody)
    path = custody.path / f"publication-contract-{key}-02.json"
    path.unlink()
    path.symlink_to("/does-not-exist")
    with pytest.raises((pub.PublicationHeld, ctl.ControllerUnknown, OSError)):
        ledger.read(key)


def test_source_issuers_remain_unavailable_and_module_has_no_executor():
    for function, count in (
        (pub.retain_baseline, 4),
        (pub.retain_handoff, 7),
    ):
        with pytest.raises(pub.PublicationHeld, match="unavailable"):
            function(*[{"permit": True} for _ in range(count)])
    # Absent publication mode is deliberately an ordinary no-op after IO source wiring.
    assert pub.run_if_selected(*[{"permit": True} for _ in range(5)]) is None
    import ast

    tree = ast.parse(Path(pub.__file__).read_text())
    assert not any(
        isinstance(n, ast.Import)
        and any(a.name in {"subprocess", "requests", "urllib"} for a in n.names)
        for n in ast.walk(tree)
    )


@pytest.mark.parametrize(
    "path",
    [
        ".",
        ".GIT/config",
        ".CoDeX/settings.json",
        ".SSH/config",
        "src/.GIT/config",
        "src/.EnV.local",
        "src/.GitAttributes",
        "src/.GitConfig",
        "src/.GitModules",
    ],
)
def test_case_insensitive_control_alias_and_empty_path_parts_are_refused(path):
    with pytest.raises(pub.PublicationHeld, match="protected control"):
        pub._path(path)


def test_public_adapter_missing_issuance_has_zero_external_ports(monkeypatch, custody):
    import dispatcher

    calls = []
    monkeypatch.setattr(dispatcher, "run_observed_offload", lambda *a, **k: calls.append(a))
    for controller in ({"permit": True}, object.__new__(ctl.IndexController)):
        with pytest.raises(pub.PublicationHeld):
            pub.execute_existing_update(controller, custody, {"deadline": 999}, {"permit": True})
    for function, args in (
        (pub._host_git_context, ({"authorized": True},)),
        (pub._selected_transport, (None, None, None, None, None)),
    ):
        with pytest.raises(pub.PublicationHeld, match="actual publication IO scope absent"):
            function(*args)
    assert calls == []


@pytest.mark.parametrize("deadline,now,expected", [(120, 100, 15), (112.5, 100, 12.5)])
def test_transport_uses_original_remaining_minimum(deadline, now, expected):
    assert pub._remaining_transport_seconds({"deadline": deadline}, TEST_POLICY, now) == expected


@pytest.mark.parametrize("deadline,now", [(100, 100), (99, 100), (True, 0), (float("inf"), 0)])
def test_transport_refuses_expired_or_nonfinite_original_clock(deadline, now):
    with pytest.raises(pub.PublicationHeld):
        pub._remaining_transport_seconds({"deadline": deadline}, TEST_POLICY, now)


def test_exact_observed_transport_call_expression_only():
    """Detached call DATA: does not enter a source validator or pretend issuance."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(pub._observed_transport))
    call = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "run_observed_offload"
    )
    seen = []
    port = SimpleNamespace(run_observed_offload=lambda *a, **kw: seen.append((a, kw)))
    started, finished, starting = object(), object(), object()
    values = dict(
        dispatcher=port,
        _transport_stdin=pub._transport_stdin,
        command=("/fixed/git", "push"),
        started=started,
        finished=finished,
        starting=starting,
        observation={},
        cwd="/protected",
        timeout=7.5,
        stdin=None,
        environment={"PATH": "/protected/bin"},
        policy={"max_ack_bytes": 100},
    )
    eval(compile(ast.Expression(call), "<mechanical-call-data>", "eval"), values)
    args, kw = seen[0]
    assert args == (["/fixed/git", "push"],)
    assert kw == dict(
        callback=started,
        observation=values["observation"],
        cwd="/protected",
        timeout=7.5,
        stdin=None,
        finished_callback=finished,
        starting_callback=starting,
        env=values["environment"],
        output_limit=100,
    )
    # Bind to the actual adapter signature without executing it.
    import dispatcher

    inspect.signature(dispatcher.run_observed_offload).bind(*args, **kw)
    assert len(seen) == 1


@pytest.mark.parametrize("change", ["argv", "source", "pid", "group", "birth"])
def test_transport_observation_refuses_changed_command_or_identity(change):
    value = dict(
        source="trusted-popen-transport/v1",
        transport_argv=["/fixed/git"],
        pid=42,
        process_group=42,
        os_started="source-birth",
    )
    pub._transport_observation(["/fixed/git"], value)
    field, replacement = {
        "argv": ("transport_argv", ["/other"]),
        "source": ("source", "json"),
        "pid": ("pid", True),
        "group": ("process_group", 43),
        "birth": ("os_started", ""),
    }[change]
    value[field] = replacement
    with pytest.raises(pub.PublicationHeld):
        pub._transport_observation(["/fixed/git"], value)


def raw_blob(raw=b"original\0bytes", mode=100644):
    return dict(
        mode=mode,
        git_oid=pub.blob_oid(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        bytes_base64=base64.b64encode(raw).decode(),
    )


def baseline_data():
    rows = [
        {"path": "src/a.py", "blob": raw_blob()},
        {"path": "tests/test_a.py", "blob": raw_blob(b"retained tests")},
    ]
    return dict(
        schema="closer-publication-baseline-data/v1",
        base_oid="a" * 40,
        head_oid="b" * 40,
        tree_oid=pub._tree_oid(rows),
        files=rows,
    )


def candidate_data(baseline):
    before = {k: v for k, v in baseline["files"][0]["blob"].items() if k != "bytes_base64"}
    after = raw_blob(b"changed\0exact bytes")
    rows = [{"path": "src/a.py", "blob": after}, baseline["files"][1]]
    return dict(
        base_oid=baseline["base_oid"],
        old_head_oid=baseline["head_oid"],
        old_tree_oid=baseline["tree_oid"],
        tree_oid=pub._tree_oid(rows),
        unchanged_tree_sha256=pub.digest([baseline["files"][1]]),
        files=[{"path": "src/a.py", "before": before, "after": after}],
    )


def test_actual_private_baseline_and_exact_candidate_bytes(custody):
    baseline = baseline_data()
    ref = pub.retain_baseline_data(custody, baseline, TEST_POLICY)
    assert custody.read(ref["name"]) == baseline
    candidate = candidate_data(baseline)
    assert pub.validate_candidate_against_baseline(baseline, candidate, TEST_POLICY) == candidate
    with pytest.raises(pub.PublicationHeld, match="already"):
        pub.retain_baseline_data(custody, baseline, TEST_POLICY)


@pytest.mark.parametrize("mutation", ["before", "tree", "unchanged", "head", "bytes"])
def test_candidate_cannot_replace_baseline_or_unrecorded_bytes(mutation):
    baseline = baseline_data()
    candidate = candidate_data(baseline)
    if mutation == "before":
        candidate["files"][0]["before"]["sha256"] = "0" * 64
    if mutation == "tree":
        candidate["tree_oid"] = "0" * 40
    if mutation == "unchanged":
        candidate["unchanged_tree_sha256"] = "0" * 64
    if mutation == "head":
        candidate["old_head_oid"] = "0" * 40
    if mutation == "bytes":
        candidate["files"][0]["after"]["bytes_base64"] = "YQ=="
    with pytest.raises(pub.PublicationHeld):
        pub.validate_candidate_against_baseline(baseline, candidate, TEST_POLICY)


@pytest.mark.parametrize("paths", [["src/a", "src/a/b"], ["src/A", "SRC/b"], ["src/A", "src/a"]])
def test_baseline_case_and_prefix_aliases_refused(paths):
    baseline = baseline_data()
    baseline["files"] = [{"path": p, "blob": raw_blob()} for p in sorted(paths)]
    with pytest.raises(pub.PublicationHeld):
        pub.validate_baseline_data(baseline, TEST_POLICY)


def test_git_empty_and_single_blob_tree_encoding():
    # Published empty Git tree and direct object-byte construction, no Git command.
    assert pub._tree_oid([]) == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    blob = raw_blob(b"hi\n")
    raw = b"100644 a\0" + bytes.fromhex(blob["git_oid"])
    expected = hashlib.sha1(b"tree " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    assert pub._tree_oid([{"path": "a", "blob": blob}]) == expected


def test_payload_descriptor_exact_raw_bytes_and_mocked_effect(custody):
    raw = b"nul\0and\nraw\xffbytes"
    ref = pub.retain_payload_data(custody, raw, TEST_POLICY)
    payload = pub._PayloadData(custody, ref, TEST_POLICY)
    try:
        identity = payload.check()
        assert identity["sha256"] == hashlib.sha256(raw).hexdigest()
        calls = []

        def effect_port(*, stdin):
            calls.append(stdin.read())

        effect_port(stdin=pub._transport_stdin(payload))
        assert calls == [raw]
        # A consumed descriptor cannot silently be reused as a fresh input.
        with pytest.raises(pub.PublicationHeld, match="origin"):
            payload.check()
    finally:
        payload.close()


@pytest.mark.parametrize("mutation", ["same_size", "offset", "mode", "closed"])
def test_payload_descriptor_mutation_refused(custody, mutation):
    ref = pub.retain_payload_data(custody, b"original", TEST_POLICY)
    payload = pub._PayloadData(custody, ref, TEST_POLICY)
    try:
        if mutation == "same_size":
            payload.stream.write(b"modified")
            payload.stream.flush()
            payload.stream.seek(0)
        if mutation == "offset":
            payload.stream.seek(1)
        if mutation == "mode":
            __import__("os").fchmod(payload.stream.fileno(), 0o644)
        if mutation == "closed":
            payload.close()
        with pytest.raises(pub.PublicationHeld):
            payload.check()
    finally:
        payload.close()


def test_arbitrary_stdin_and_forged_payload_reference_refused(custody):
    import io

    with pytest.raises(pub.PublicationHeld):
        pub._transport_stdin(io.BytesIO(b"caller bytes"))
    ref = pub.retain_payload_data(custody, b"original", TEST_POLICY)
    ref["sha256"] = "0" * 64
    with pytest.raises(pub.PublicationHeld):
        pub._PayloadData(custody, ref, TEST_POLICY)


def test_staging_inputs_bind_raw_blob_and_nul_index_records(custody):
    baseline = baseline_data()
    ref = pub.retain_baseline_data(custody, baseline, TEST_POLICY)
    assert pub.read_baseline_data(custody, ref, TEST_POLICY) == baseline
    candidate = candidate_data(baseline)
    data = pub.staging_payload_data(baseline, candidate, TEST_POLICY)
    blob = candidate["files"][0]["after"]
    expected = b"100644 " + blob["git_oid"].encode() + b"\tsrc/a.py\0"
    assert base64.b64decode(data["index_bytes_base64"]) == expected
    assert data["index_sha256"] == hashlib.sha256(expected).hexdigest()
    assert data["blobs"][0]["bytes_base64"] == blob["bytes_base64"]
    assert data["git_execution_authorized"] is False
    forged = {**ref, "name": "publication-baseline-data-" + "0" * 64 + ".json"}
    with pytest.raises(pub.PublicationHeld):
        pub.read_baseline_data(custody, forged, TEST_POLICY)


def test_staging_delete_uses_zero_mode_and_oid():
    baseline = baseline_data()
    candidate = candidate_data(baseline)
    candidate["files"][0]["after"] = None
    candidate["tree_oid"] = pub._tree_oid([baseline["files"][1]])
    data = pub.staging_payload_data(baseline, candidate, TEST_POLICY)
    assert base64.b64decode(data["index_bytes_base64"]) == b"0 " + b"0" * 40 + b"\tsrc/a.py\0"
    assert data["blobs"] == []


@pytest.mark.parametrize(
    "paths",
    [
        ["src/é.py", "src/e\u0301.py"],
        ["src/é/a.py", "src/e\u0301/b.py"],
        ["src/É.py", "src/e\u0301.py"],
        ["src/é", "src/e\u0301/child.py"],
    ],
)
def test_unicode_inventory_aliases_refused_without_renaming(paths):
    baseline = baseline_data()
    baseline["files"] = [{"path": p, "blob": raw_blob()} for p in sorted(paths)]
    baseline["tree_oid"] = pub._tree_oid(baseline["files"])
    before = copy.deepcopy(baseline)
    with pytest.raises(pub.PublicationHeld):
        pub.validate_baseline_data(baseline, TEST_POLICY)
    assert baseline == before


@pytest.mark.parametrize("path", ["src/é.py", "src/e\u0301.py", "src/ordinary.py"])
def test_unicode_inventory_preserves_raw_git_path_bytes(path):
    baseline = baseline_data()
    baseline["files"] = [{"path": path, "blob": raw_blob()}]
    baseline["tree_oid"] = pub._tree_oid(baseline["files"])
    validated = pub.validate_baseline_data(baseline, TEST_POLICY)
    assert validated == baseline
    assert validated["files"][0]["path"].encode("utf-8") == path.encode("utf-8")
    if path == "src/e\u0301.py":
        other = [{"path": "src/é.py", "blob": raw_blob()}]
        assert pub._tree_oid(other) != baseline["tree_oid"]
