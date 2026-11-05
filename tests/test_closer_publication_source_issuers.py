"""Synthetic physical-byte and fixed-port controls, no issued native success.

Real callback/history/writer/HostGit checks are never replaced with successes.
No native Git/GitHub/provider command or credential/config file is observed.
"""

import ast
import copy
import inspect
import os
from types import SimpleNamespace

import pytest
from test_closer_coding_publication import TEST_POLICY, values

import closer_coding_publication as publication
import closer_coding_worker as worker


def test_config_observation_is_closed_nonsecret_name_inventory():
    raw = b"local\0file:/synthetic/repo/.git/config\0core.bare\0"
    assert publication._configuration_names(raw, 1000) == [
        ("local", "/synthetic/repo/.git/config", "core.bare")
    ]
    for key in (
        b"include.path",
        b"http.https://github.com.extraheader",
        b"core.hookspath",
        b"core.fsmonitor",
        b"credential.username",
        b"filter.raw.smudge",
    ):
        with pytest.raises(publication.PublicationHeld):
            publication._configuration_names(b"local\0file:/synthetic/config\0" + key + b"\0", 1000)


@pytest.mark.parametrize(
    "raw",
    [
        b"global\0command line:\0core.bare\0",
        b"worktree\0file:/synthetic/config\0core.bare\0",
        b"global\0file:/synthetic/config\0core.bare",
        b"\xff\0\0\0",
    ],
)
def test_unknown_config_origins_and_wire_formats_hold(raw):
    with pytest.raises(publication.PublicationHeld):
        publication._configuration_names(raw, 1000)


def test_synthetic_file_descriptor_detects_raw_and_named_changes(tmp_path):
    path = tmp_path / "synthetic-config"
    path.write_bytes(b"nonsecret synthetic bytes")
    path.chmod(0o600)
    item = publication._PhysicalFile(path, 100)
    try:
        assert item.raw() == b"nonsecret synthetic bytes"
        item.check()
        replacement = tmp_path / "replacement"
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(path)
        with pytest.raises(publication.PublicationHeld):
            item.check()
    finally:
        item.close()


def test_metadata_only_descriptor_does_not_read_file_bytes(tmp_path):
    path = tmp_path / "synthetic-metadata-only"
    path.write_bytes(b"not an actual credential file")
    path.chmod(0o600)
    item = publication._PhysicalFile(path, 100, metadata_only=True)
    try:
        assert item.check()["sha256"] is None
        with pytest.raises(publication.PublicationHeld):
            item.raw()
    finally:
        item.close()


@pytest.mark.parametrize("change", ["bytes", "mode", "hardlink", "symlink"])
def test_synthetic_file_custody_refuses_changes(tmp_path, change):
    path = tmp_path / "synthetic"
    path.write_bytes(b"original")
    path.chmod(0o600)
    item = publication._PhysicalFile(path, 100)
    try:
        if change == "bytes":
            path.write_bytes(b"modified")
        if change == "mode":
            path.chmod(0o644)
        if change == "hardlink":
            os.link(path, tmp_path / "extra")
        if change == "symlink":
            path.unlink()
            path.symlink_to(tmp_path / "missing")
        with pytest.raises(publication.PublicationHeld):
            item.check()
    finally:
        item.close()


def test_fixed_helper_data_never_claims_native_principal():
    route = publication._fixed_helper_route(
        b"!gh auth git-credential\0",
        "/source/gh",
        {"GH_CONFIG_DIR": "/synthetic/gh", "GH_TOKEN": "synthetic-opaque-value"},
    )
    assert "synthetic-opaque-value" not in repr(route)
    assert route["credential_slot"] == "GH_TOKEN"
    assert route["native_principal_proven"] is False
    with pytest.raises(publication.PublicationHeld, match="actual helper-route"):
        publication._observe_helper_api_route(object(), route, object(), [], {})


@pytest.mark.parametrize(
    "helper,env",
    [
        (b"!echo unknown\0", {"GH_CONFIG_DIR": "/synthetic/gh"}),
        (b"!gh auth git-credential\0!other\0", {"GH_CONFIG_DIR": "/synthetic/gh"}),
        (b"!gh auth git-credential\0", {}),
        (
            b"!gh auth git-credential\0",
            {
                "GH_CONFIG_DIR": "/synthetic/gh",
                "GH_TOKEN": "never-used",
                "GITHUB_TOKEN": "ambiguous",
            },
        ),
        (b"!gh auth git-credential\0", {"GH_CONFIG_DIR": "/synthetic/gh", "GH_HOST": "foreign"}),
    ],
)
def test_unsupported_helper_or_environment_never_becomes_identity(helper, env):
    with pytest.raises(publication.PublicationHeld):
        publication._fixed_helper_route(helper, "/source/gh", env)


def test_only_closed_readonly_git_argv_is_derived():
    assert publication._read_command("tree", {"head_oid": "a" * 40}) == [
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        "a" * 40,
    ]
    assert publication._read_command("config_names") == [
        "config",
        "--null",
        "--show-scope",
        "--show-origin",
        "--name-only",
        "--list",
    ]
    for key in ("push", "commit", "fetch", "clone", "config_set", "arbitrary"):
        with pytest.raises(publication.PublicationHeld):
            publication._read_command(key)


def test_tree_and_index_raw_records_are_identical_regular_inventory():
    oid = "a" * 40
    tree = ("100644 blob " + oid + "\tsrc/a.py\0").encode()
    index = ("100644 " + oid + " 0\tsrc/a.py\0").encode()
    assert publication._tree_records(tree, TEST_POLICY) == publication._index_records(
        index, TEST_POLICY
    )
    with pytest.raises(publication.PublicationHeld):
        publication._index_records(index.replace(b" 0\t", b" 1\t"), TEST_POLICY)
    with pytest.raises(publication.PublicationHeld):
        publication._tree_records(tree.replace(b"100644", b"120000"), TEST_POLICY)


def test_native_pr_projection_preserves_actual_author_and_head():
    intent, *_ = values()
    pr = intent["pr"]
    config = SimpleNamespace(
        repository=pr["repository"],
        repository_id=pr["repository_id"],
        writer_id=pr["writer_id"],
        writer_login=pr["writer_login"],
    )
    pull = dict(
        number=pr["pr_number"],
        node_id=pr["pr_node_id"],
        user={"id": pr["writer_id"], "login": pr["writer_login"]},
        head={
            "sha": pr["head_oid"],
            "ref": pr["head_ref"],
            "repo": {"id": pr["repository_id"], "full_name": pr["repository"]},
        },
        base={"sha": pr["base_oid"], "ref": pr["base_ref"], "repo": {"id": pr["repository_id"]}},
        body="native synthetic body",
        state="open",
        draft=False,
        merged=False,
    )
    snap = dict(
        pr_target=pr["repository"] + "#" + str(pr["pr_number"]),
        head=pr["head_oid"],
        base=pr["base_oid"],
        head_branch=pr["head_ref"],
        head_repository=pr["repository"],
        body=pull["body"],
        state="OPEN",
        draft=False,
    )
    result = publication._owned_pr_projection(pull, snap, pr["issue_node_id"], config)
    assert result["owner_id"] == pr["writer_id"]
    pull["user"]["id"] += 1
    with pytest.raises(publication.PublicationHeld):
        publication._owned_pr_projection(pull, snap, pr["issue_node_id"], config)


def test_detached_callback_has_zero_external_observation_ports(monkeypatch):
    import dispatcher

    calls = []
    monkeypatch.setattr(dispatcher, "run_observed_offload", lambda *a, **kw: calls.append((a, kw)))
    with pytest.raises(publication.PublicationHeld):
        publication.retain_baseline(*[object()] * 4)
    assert calls == []


@pytest.mark.parametrize("known", [True, False])
def test_actual_worker_finally_keeps_known_data_and_original_exception(known):
    tree = ast.parse(inspect.getsource(worker._execute))
    outer = next(n for n in ast.walk(tree) if isinstance(n, ast.Try) and n.finalbody)
    raw = {"exact": "returned bytes"}
    terminal = object()
    original = copy.deepcopy(raw)

    class CleanupFailure:
        def close_baseline(self, value):
            raise OSError("private cleanup detail")

    retained_reference = object()
    env = dict(
        publication_baseline_ref=object(),
        publication=CleanupFailure(),
        publication_ref=retained_reference,
        publication_held=False,
        result=raw,
        terminal_ref=terminal,
    )
    body = (
        [ast.Pass()]
        if known
        else [
            ast.Raise(
                exc=ast.Call(func=ast.Name(id="ValueError", ctx=ast.Load()), args=[], keywords=[])
            )
        ]
    )
    trial = ast.fix_missing_locations(
        ast.Module(
            body=[ast.Try(body=body, handlers=[], orelse=[], finalbody=outer.finalbody)],
            type_ignores=[],
        )
    )
    if known:
        exec(compile(trial, "<actual-source-cleanup>", "exec"), env)
    else:
        with pytest.raises(ValueError):
            exec(compile(trial, "<actual-source-cleanup>", "exec"), env)
    assert env["publication_held"] is True and env["publication_ref"] is retained_reference
    assert env["result"] is raw and raw == original and env["terminal_ref"] is terminal


def test_commit_object_hash_is_checked_before_using_native_tree():
    import hashlib

    raw = b"tree " + b"a" * 40 + b"\nparent " + b"b" * 40 + b"\n\nnative message\n"
    head = hashlib.sha1(b"commit " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    assert publication._commit_tree(raw, head, TEST_POLICY) == "a" * 40
    with pytest.raises(publication.PublicationHeld):
        publication._commit_tree(raw + b"changed", head, TEST_POLICY)
    with pytest.raises(publication.PublicationHeld):
        publication._index_flags(b"h src/a\0", TEST_POLICY)
    with pytest.raises(publication.PublicationHeld):
        publication._index_flags(b"S src/a\0", TEST_POLICY)
    assert publication._index_flags(b"H src/a\0", TEST_POLICY) == {"src/a"}


def test_synthetic_directory_children_and_named_identity_are_held(tmp_path):
    directory = tmp_path / "synthetic-directory"
    directory.mkdir(mode=0o700)
    item = publication._PhysicalDirectory(directory, immutable_children=True)
    try:
        item.check()
        (directory / "new-gh").write_bytes(b"synthetic")
        with pytest.raises(publication.PublicationHeld):
            item.check()
    finally:
        item.close()


def test_fixed_read_transport_call_expression_only_uses_existing_observer():
    tree = ast.parse(inspect.getsource(publication._fixed_git_read))
    call = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "run_observed_offload"
    )
    seen = []
    observed = SimpleNamespace(operation={"deadline": 112.5}, policy=TEST_POLICY)
    callback = object()
    namespace = dict(
        dispatcher=SimpleNamespace(run_observed_offload=lambda *a, **kw: seen.append((a, kw))),
        command=["/fixed/git", "config", "--name-only", "--list"],
        observed=observed,
        workspace="/synthetic",
        started=callback,
        finished=callback,
        starting=callback,
        source_env={"PATH": "/source/bin"},
        private_input=None,
        kind="config_names",
        _remaining_transport_seconds=publication._remaining_transport_seconds,
        time=SimpleNamespace(time=lambda: 100),
    )
    eval(compile(ast.Expression(call), "<fixed-read-call-data>", "eval"), namespace)
    args, kwargs = seen[0]
    assert args == (["/fixed/git", "config", "--name-only", "--list"],)
    assert kwargs["timeout"] == 12.5 and kwargs["stdin"] is None
    assert kwargs["callback"] is callback and kwargs["finished_callback"] is callback
    assert kwargs["starting_callback"] is callback and kwargs["output_limit"] == 16384
    import dispatcher

    inspect.signature(dispatcher.run_observed_offload).bind(*args, **kwargs)
    assert len(seen) == 1


def test_new_direct_source_calls_match_real_function_signatures():
    tree = ast.parse(inspect.getsource(publication))
    source_names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    checked = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            continue
        name = call.func.id
        if name not in source_names or any(isinstance(a, ast.Starred) for a in call.args):
            continue
        if any(k.arg is None for k in call.keywords):
            continue
        value = getattr(publication, name)
        if isinstance(value, type) and issubclass(value, BaseException):
            continue
        inspect.signature(value).bind(
            *[object() for _ in call.args], **{k.arg: object() for k in call.keywords}
        )
        checked.append(name)
    assert {"_observe_git_context", "_SourceObservation", "_BaselineLease", "_PhysicalFile"} <= set(
        checked
    )


def test_existing_stored_route_is_data_only_and_empty_environment_slot_still_holds():
    route = publication._fixed_helper_route(
        b"!gh auth git-credential\0", "/source/gh", {"GH_CONFIG_DIR": "/synthetic/gh"}
    )
    assert route["credential_slot"] == "stored_github_cli"
    assert route["native_principal_proven"] is False
    with pytest.raises(publication.PublicationHeld, match="ambiguous"):
        publication._fixed_helper_route(
            b"!gh auth git-credential\0",
            "/source/gh",
            {"GH_CONFIG_DIR": "/synthetic/gh", "GH_TOKEN": ""},
        )


def test_untracked_or_out_of_scope_file_refuses_before_raw_capture():
    with pytest.raises(publication.PublicationHeld, match="before reading"):
        publication._physical_path_scope("private-data", set(), TEST_POLICY, False)
    with pytest.raises(publication.PublicationHeld, match="before reading"):
        publication._physical_path_scope("private-data", set(), TEST_POLICY, True)
    publication._physical_path_scope("src/new.py", set(), TEST_POLICY, True)
    tree = ast.parse(inspect.getsource(publication._physical_inventory))
    calls = sorted(
        (n.lineno, n.func.id)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in {"_physical_path_scope", "_PhysicalFile"}
    )
    assert [name for _, name in calls] == ["_physical_path_scope", "_PhysicalFile"]


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_live_ancestor_symlink_replacement_refuses_same_terminal_inode(tmp_path, kind):
    parent = tmp_path / "original-parent"
    parent.mkdir(mode=0o700)
    if kind == "file":
        target = parent / "held-file"
        target.write_bytes(b"synthetic bytes")
        target.chmod(0o600)
        item = publication._PhysicalFile(target, 100)
    else:
        target = parent / "held-directory"
        target.mkdir(mode=0o700)
        item = publication._PhysicalDirectory(target)
    try:
        item.check()
        inode = os.stat(target).st_ino
        moved = tmp_path / "moved-parent"
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        assert os.stat(target).st_ino == inode
        with pytest.raises(publication.PublicationHeld, match="ancestor"):
            item.check()
    finally:
        item.close()


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_live_ancestor_identity_replacement_without_symlink_refuses(tmp_path, kind):
    parent = tmp_path / "original-parent"
    parent.mkdir(mode=0o700)
    child = parent / "child"
    child.mkdir(mode=0o700)
    target = child / "held"
    if kind == "file":
        target.write_bytes(b"synthetic bytes")
        target.chmod(0o600)
        item = publication._PhysicalFile(target, 100)
    else:
        target.mkdir(mode=0o700)
        item = publication._PhysicalDirectory(target)
    try:
        moved = tmp_path / "moved-parent"
        parent.rename(moved)
        parent.mkdir(mode=0o700)
        (moved / "child").rename(parent / "child")
        # The leaf object and its immediate parent still have the same inodes.
        with pytest.raises(publication.PublicationHeld, match="ancestor"):
            item.check()
    finally:
        item.close()


def test_unqualified_gh_protocol_holds_before_native_identity_or_effect_ports(monkeypatch):
    import closer_dispatcher
    import dispatcher

    calls = []
    monkeypatch.setattr(closer_dispatcher.GitHub, "api", lambda *a, **kw: calls.append("api"))
    monkeypatch.setattr(
        dispatcher, "run_observed_offload", lambda *a, **kw: calls.append("process")
    )
    with pytest.raises(
        publication.PublicationHeld, match="actual fixed helper/API protocol producer"
    ):
        publication._require_fixed_gh_protocol({"caller_hash": "f" * 64}, {"user": "not proof"})
    assert calls == []
    tree = ast.parse(inspect.getsource(publication._observe_helper_api_route))
    gate = next(
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_require_fixed_gh_protocol"
    )
    read = next(
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_native_writer"
    )
    assert gate < read
