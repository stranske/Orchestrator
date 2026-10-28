"""Real local Git mechanics; no network, provider, original branch/index write."""

import copy
import hashlib
import os
import subprocess

import pytest
from test_closer_coding_publication import TEST_POLICY, values
from test_closer_coding_publication import custody as custody

import closer_coding_publication as pub


def anchor_policy():
    return {
        **copy.deepcopy(TEST_POLICY),
        "schema": pub.ANCHOR_POLICY_SCHEMA,
        "baseline_limits": {
            "schema": "closer-publication-baseline-limits/v1",
            **pub._BASELINE_CAPS,
        },
    }


def fixture(tmp_path, packed=True):
    repo = tmp_path.resolve() / "owned"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        HOME=str(tmp_path.resolve()),
        GIT_AUTHOR_NAME="Synthetic",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Synthetic",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )

    def git(*args, raw=None, selected=None):
        return subprocess.run(
            ["/usr/bin/git", *args],
            cwd=repo,
            env=selected or env,
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=10,
        ).stdout

    git("init")
    (repo / "src").mkdir()
    (repo / "src/fix.py").write_bytes(b"before\n")
    (repo / "unchanged.bin").write_bytes(b"unchanged\0binary\xff")
    (repo / ".gitattributes").write_bytes(b"*.txt text\n")
    git("add", ".")
    git("commit", "-m", "original")
    if packed:
        git("gc", "--prune=now")
    head = git("rev-parse", "HEAD").strip().decode()
    commit = git("cat-file", "commit", head)
    policy = anchor_policy()
    tree = pub._tree_records(git("ls-tree", "-rz", "--full-tree", head), policy)
    rows = pub._physical_inventory(
        repo, policy, lambda: None, original_paths=set(tree), allow_new=False
    )
    baseline = {
        "schema": "closer-publication-baseline-data/v2",
        "base_oid": "b" * 40,
        "head_oid": head,
        "tree_oid": pub._tree_oid(rows),
        "files": rows,
    }
    pub.validate_baseline_data(baseline, policy)
    (repo / "src/fix.py").write_bytes(b"after\n")
    old = next(r["blob"] for r in rows if r["path"] == "src/fix.py")
    import base64

    after = {
        "mode": 100644,
        "git_oid": pub.blob_oid(b"after\n"),
        "sha256": hashlib.sha256(b"after\n").hexdigest(),
        "bytes_base64": base64.b64encode(b"after\n").decode(),
    }
    newrows = [
        {
            "path": r["path"],
            "blob": (
                {**{k: after[k] for k in ("mode", "git_oid", "sha256")}, "size": 6}
                if r["path"] == "src/fix.py"
                else r["blob"]
            ),
        }
        for r in rows
    ]
    candidate = {
        "base_oid": baseline["base_oid"],
        "old_head_oid": head,
        "old_tree_oid": baseline["tree_oid"],
        "tree_oid": pub._tree_oid(newrows),
        "unchanged_tree_sha256": pub.digest([r for r in rows if r["path"] != "src/fix.py"]),
        "files": [
            {
                "path": "src/fix.py",
                "before": {k: old[k] for k in ("mode", "git_oid", "sha256")},
                "after": after,
            }
        ],
    }
    pr = values()[0]["pr"]
    pr.update(head_oid=head, base_oid=baseline["base_oid"])
    return repo, git, env, baseline, candidate, pr, commit, policy


def test_packed_existing_objects_private_commit_no_unchanged_copy(custody, tmp_path):
    repo, git, env, baseline, candidate, pr, commit, policy = fixture(tmp_path)
    anchor = pub._ExistingObjects(repo / ".git/objects", baseline, commit, policy)
    stage = None
    before_head, before_index, before_refs = (
        git("rev-parse", "HEAD"),
        (repo / ".git/index").read_bytes(),
        git("show-ref"),
    )
    try:
        assert (
            git(
                "cat-file",
                "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                raw=b"".join((oid + "\n").encode() for oid in sorted(anchor.expected)),
            )
            == anchor.expected_output()
        )
        stage = pub._build_staging_data(
            custody,
            "a" * 64,
            baseline,
            candidate,
            pr,
            policy,
            anchor,
            b"[core]\n repositoryformatversion = 0\n bare = false\n",
            {},
        )
        selected = {
            **env,
            "GIT_DIR": str(stage.path / "repo.git"),
            "GIT_WORK_TREE": str(stage.path / "worktree"),
            "GIT_INDEX_FILE": str(stage.path / "repo.git/index"),
            "GIT_OBJECT_DIRECTORY": str(anchor.path),
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "TMPDIR": str(stage.path / "native-tmp"),
        }
        git("read-tree", baseline["tree_oid"], selected=selected)
        oid = git("hash-object", "-w", "--stdin", raw=b"after\n", selected=selected).strip()
        git(
            "update-index",
            "-z",
            "--index-info",
            raw=b"100644 " + oid + b"\tsrc/fix.py\0",
            selected=selected,
        )
        tree = git("write-tree", selected=selected).strip().decode()
        assert tree == candidate["tree_oid"]
        git("commit", "--file=-", raw=b"Correction\n", selected=selected)
        new = git("rev-parse", "HEAD", selected=selected).strip().decode()
        actual = git("cat-file", "commit", new, selected=selected)
        assert actual.startswith(
            ("tree " + tree + "\nparent " + baseline["head_oid"] + "\n").encode()
        )
        assert (
            git("cat-file", "blob", new + ":.gitattributes", selected=selected) == b"*.txt text\n"
        )
        assert (
            git("cat-file", "blob", new + ":unchanged.bin", selected=selected)
            == b"unchanged\0binary\xff"
        )
        assert git("rev-parse", "HEAD") == before_head
        assert (repo / ".git/index").read_bytes() == before_index
        assert git("show-ref") == before_refs
        assert list((stage.path / "repo.git/objects").iterdir()) == []
        assert list((stage.path / "worktree").iterdir()) == []
        assert not (stage.path / "repo.git/objects/info/alternates").exists()
        assert b"bytes_base64" not in pub._bytes(baseline)
        assert stage.check()["git_execution_authorized"] is False
    finally:
        if stage:
            stage.close()
        anchor.close()


@pytest.mark.parametrize(
    "change", ["missing_pack", "pack_tamper", "index_tamper", "odb_alias", "alternates"]
)
def test_selected_existing_objects_hold_after_loss_or_tamper(tmp_path, change):
    repo, _git, _env, baseline, _candidate, _pr, commit, policy = fixture(tmp_path)
    odb = repo / ".git/objects"
    anchor = pub._ExistingObjects(odb, baseline, commit, policy)
    try:
        if change == "missing_pack":
            next((odb / "pack").glob("*.pack")).unlink()
        elif change in {"pack_tamper", "index_tamper"}:
            target = next((odb / "pack").glob("*.pack" if change == "pack_tamper" else "*.idx"))
            target.chmod(0o644)
            with target.open("r+b") as stream:
                stream.write(b"changed")
        elif change == "odb_alias":
            other = odb.with_name("objects-moved")
            odb.rename(other)
            odb.symlink_to(other, target_is_directory=True)
        else:
            (odb / "info/alternates").write_text("/must-not-use\n")
        with pytest.raises((pub.PublicationHeld, OSError)):
            anchor.check()
    finally:
        anchor.close()


def test_missing_native_object_is_not_a_fetch_or_repair(tmp_path):
    repo, git, env, baseline, _candidate, _pr, commit, policy = fixture(tmp_path, packed=False)
    anchor = pub._ExistingObjects(repo / ".git/objects", baseline, commit, policy)
    try:
        oid = baseline["files"][0]["blob"]["git_oid"]
        (repo / ".git/objects" / oid[:2] / oid[2:]).unlink()
        with pytest.raises((pub.PublicationHeld, OSError)):
            anchor.check()
        response = git(
            "cat-file",
            "--batch-check",
            raw=(oid + "\n").encode(),
            selected={**env, "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1"},
        )
        assert response == (oid + " missing\n").encode()
    finally:
        anchor.close()


def test_closed_v2_original_admission_and_immutable_control_scope():
    v2 = anchor_policy()
    assert (
        pub.validate_consumer_mode(
            v2["consumer_id"], {"publication_v2": v2}, {"publication_v2": v2}
        )
        == v2
    )
    for current, original in [
        ({"publication_v2": v2}, {"publication_v1": TEST_POLICY}),
        ({"publication_v1": TEST_POLICY, "publication_v2": v2}, {}),
        ({}, {"publication_v2": v2}),
        ({"publication_v2": v2}, {}),
    ]:
        with pytest.raises(pub.PublicationHeld):
            pub.validate_consumer_mode(v2["consumer_id"], current, original)
    assert pub._inventory_path(".gitattributes", v2) == ".gitattributes"
    with pytest.raises(pub.PublicationHeld):
        pub._path(".gitattributes")
    with pytest.raises(pub.PublicationHeld):
        pub._physical_path_scope("src/.gitattributes", set(), v2, True)


def test_v2_source_original_snapshot_and_current_policy_are_exact():
    import json

    from test_closer_coding_session_v2 import consumers

    import closer_coding_session_v2 as session

    configured = consumers()
    key = next(iter(configured))
    policy = {**anchor_policy(), "consumer_id": key}
    configured[key]["publication_v2"] = policy
    draft = session.prepare_admission_policy(configured)
    original = json.loads(draft.snapshot_utf8)
    assert original["publication_v2"] == policy
    assert session.validate_admission_snapshot(original) == original
    assert pub.validate_consumer_mode(key, configured[key], original) == policy
    bad = copy.deepcopy(original)
    bad["publication_v1"] = TEST_POLICY
    with pytest.raises(pub.PublicationHeld):
        session.validate_admission_snapshot(bad)


def test_v2_pending_campaign_selector_is_selected_and_unissued_io_still_holds(monkeypatch):
    import ast
    import inspect

    import closer_campaign as campaign
    import closer_coding_publication_io as io
    import closer_native_observer as native

    # Evaluate the exact production pending-reconciliation selector, not a
    # mirror of its spelling. No native admission or writer is fabricated.
    expression = next(
        n.test
        for n in ast.walk(ast.parse(inspect.getsource(campaign.tick)))
        if isinstance(n, ast.If) and "configured_consumers.values" in ast.unparse(n.test)
    )
    selected = eval(
        compile(ast.Expression(expression), "actual-campaign-selector", "eval"),
        {"configured_consumers": {"c": {"publication_v2": anchor_policy()}}},
    )
    assert selected is True
    monkeypatch.setattr(native, "correction_cleanup_enabled", lambda _: False)
    manifest = {
        "policy": {
            "correction_controller_installation": {
                "config": {"consumers": {"c": {"publication_v2": anchor_policy()}}}
            }
        }
    }
    with pytest.raises(pub.PublicationHeld, match="original terminal reconciliation"):
        io.run_if_selected(
            None, {"worker_config": {}}, manifest, "/no-native-manifest", {"kind": "reconcile"}
        )


def test_inventory_full_checks_bracket_readonly_loop_and_failure(tmp_path):
    root = tmp_path.resolve()
    (root / "src").mkdir()
    (root / "src/fix.py").write_bytes(b"data\n")
    calls = []

    def full():
        calls.append("full")

    def lifetime():
        calls.append("lifetime")

    pub._physical_inventory(
        root,
        anchor_policy(),
        lifetime,
        full_check=full,
        original_paths={"src/fix.py"},
        allow_new=False,
    )
    assert calls == ["full", "lifetime", "lifetime", "full"]
    calls.clear()
    (root / "src/fix.py").unlink()
    (root / "src/fix.py").symlink_to("/not-readable")
    with pytest.raises(pub.PublicationHeld):
        pub._physical_inventory(
            root,
            anchor_policy(),
            lifetime,
            full_check=full,
            original_paths={"src/fix.py"},
            allow_new=False,
        )
    assert calls[0] == calls[-1] == "full"


def test_loose_heavy_existing_head_has_constant_held_descriptor_count(tmp_path):
    repo, git, _env, _baseline, _candidate, _pr, _commit, policy = fixture(tmp_path, packed=False)
    for index in range(1000):
        (repo / "src" / ("file-" + str(index))).write_text("unique loose object " + str(index))
    git("add", ".")
    # Git2.55 defaults to geometric auto-maintenance at100 loose objects.
    # This control measures loose-object descriptor ownership, not repacking.
    # Keep its setup genuinely loose without changing production ODB admission.
    git("-c", "maintenance.auto=false", "commit", "-m", "loose-heavy")
    assert list((repo / ".git/objects/pack").iterdir()) == []
    head = git("rev-parse", "HEAD").strip().decode()
    native = pub._tree_records(git("ls-tree", "-rz", "--full-tree", head), policy)
    rows = pub._physical_inventory(
        repo, policy, lambda: None, original_paths=set(native), allow_new=False
    )
    baseline = {
        "schema": "closer-publication-baseline-data/v2",
        "base_oid": "b" * 40,
        "head_oid": head,
        "tree_oid": pub._tree_oid(rows),
        "files": rows,
    }
    before = len(os.listdir("/dev/fd"))
    anchor = pub._ExistingObjects(
        repo / ".git/objects", baseline, git("cat-file", "commit", head), policy
    )
    try:
        assert len(anchor.identities) >= 1000
        assert len(os.listdir("/dev/fd")) - before < 40
        anchor.check()
        assert len(os.listdir("/dev/fd")) - before < 40
    finally:
        anchor.close()
    assert len(os.listdir("/dev/fd")) == before


def test_fixed_object_reader_uses_native_regular_stdin_without_copy(custody, tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace

    repo, _git, _env, baseline, _candidate, _pr, _commit, policy = fixture(tmp_path)
    policy["git_executable"]["path"] = "/usr/bin/git"
    # Read-only DATA observation only. This cannot construct a coding Root,
    # source installation, admitted operation or mutation descriptor.
    observed = SimpleNamespace(
        policy=policy, custody=custody, operation={"deadline": time.time() + 15}, check=lambda: None
    )
    for key in list(os.environ):
        if key.startswith(("GIT_", "DYLD_", "LD_")) or key in {
            "BASH_ENV",
            "ENV",
            "SHELLOPTS",
            "BASHOPTS",
            "CDPATH",
            "IFS",
        }:
            monkeypatch.delenv(key)
    oid = baseline["head_oid"]
    raw = pub._fixed_git_read(
        observed, repo, "object_inventory", object_input=(oid + "\n").encode()
    )
    assert raw.startswith((oid + " commit ").encode())


def test_cold_staging_reopen_rejects_refreshed_pack_fingerprint(custody, tmp_path):
    repo, _git, _env, baseline, candidate, pr, commit, policy = fixture(tmp_path)
    original = pub._ExistingObjects(repo / ".git/objects", baseline, commit, policy)
    stage = pub._build_staging_data(
        custody, "b" * 64, baseline, candidate, pr, policy, original, b"[core]\n bare = false\n", {}
    )
    path, marker = stage.path, copy.deepcopy(stage.marker)
    stage.close()
    original.close()
    pack = next((repo / ".git/objects/pack").glob("*.pack"))
    meta = pack.stat()
    os.utime(pack, ns=(meta.st_atime_ns, meta.st_mtime_ns + 1000000))
    fresh = pub._ExistingObjects(repo / ".git/objects", baseline, commit, policy)
    reopened = pub._StagingData(path, marker)
    try:
        assert fresh.summary()["physical_sha256"] != marker["existing_objects"]["physical_sha256"]
        reopened.object_anchor = fresh
        with pytest.raises(pub.PublicationHeld, match="ODB binding differs"):
            reopened.check()
    finally:
        reopened.close()
        fresh.close()


def test_new_unrelated_git_objects_do_not_change_original_anchor(tmp_path):
    repo, git, _env, baseline, _candidate, _pr, commit, policy = fixture(tmp_path)
    original = pub._ExistingObjects(repo / ".git/objects", baseline, commit, policy)
    summary = original.summary()
    try:
        git("hash-object", "-w", "--stdin", raw=b"new independent normal Git object")
        original.check()
        fresh = pub._ExistingObjects(repo / ".git/objects", baseline, commit, policy)
        try:
            assert original.summary() == fresh.summary() == summary
        finally:
            fresh.close()
    finally:
        original.close()


def test_complete_capture_baseline_reads_tracked_attributes_but_candidate_cannot_edit_it(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    repo, git, _env, _before, _candidate, _pr, _commit, policy = fixture(tmp_path)
    (repo / "src/fix.py").write_bytes(b"before\n")
    head = git("rev-parse", "HEAD").strip().decode()
    branch = git("symbolic-ref", "HEAD").strip().decode().removeprefix("refs/heads/")
    host = SimpleNamespace(
        workspace=repo,
        observed=SimpleNamespace(policy=policy),
        pr={"head_oid": head, "base_oid": head, "head_ref": branch},
        check=lambda: None,
        read_lifetime=lambda: None,
        object_anchor=None,
    )
    reads = []

    def read(kind):
        reads.append(kind)
        return git(*pub._read_command(kind, host.pr))

    host.read = read

    def data_borrow(actual, baseline):
        assert actual is host
        # DATA only: full capture branch/flags/index/inventory remains actual.
        # A source Root/HostGit admission is not manufactured by this test.
        return pub._ExistingObjects(repo / ".git/objects", baseline, read("commit"), policy)

    monkeypatch.setattr(pub, "_borrow_existing_objects", data_borrow)
    baseline = pub._capture_baseline(host)
    try:
        assert reads[:6] == ["head", "branch", "commit", "tree", "index_flags", "index"]
        assert any(row["path"] == ".gitattributes" for row in baseline["files"])
        (repo / "src/fix.py").write_bytes(b"after\n")
        candidate = pub._capture_candidate(host, baseline)
        assert [row["path"] for row in candidate["files"]] == ["src/fix.py"]
        original = (repo / ".gitattributes").read_bytes()
        (repo / ".gitattributes").write_bytes(original + b"*.py text\n")
        with pytest.raises(pub.PublicationHeld, match="protected control data"):
            pub._capture_candidate(host, baseline)
    finally:
        host.object_anchor.close()


@pytest.mark.parametrize("packed", [False, True])
def test_native_git_freshening_keeps_original_fingerprint_and_detects_same_inode_tamper(
    tmp_path, packed, monkeypatch
):
    import time

    repo, git, _env, baseline, _candidate, _pr, commit, policy = fixture(tmp_path, packed=packed)
    oid = next(row["blob"]["git_oid"] for row in baseline["files"] if row["path"] == "src/fix.py")
    obj = (
        next((repo / ".git/objects/pack").glob("*.pack"))
        if packed
        else repo / ".git/objects" / oid[:2] / oid[2:]
    )
    os.utime(obj, (time.time() - 3600, time.time() - 3600))
    anchor = pub._ExistingObjects(repo / ".git/objects", baseline, commit, policy)
    calls = []
    actual = anchor._file_content

    def read(*args, **kwargs):
        calls.append(args[0])
        return actual(*args, **kwargs)

    monkeypatch.setattr(anchor, "_file_content", read)
    try:
        original = anchor.summary()
        for _ in range(3):
            anchor.check()
        assert calls == []  # No content hashing at unchanged barriers.
        old = obj.stat()
        assert git("hash-object", "-w", "--stdin", raw=b"before\n").strip().decode() == oid
        assert (obj.stat().st_mtime_ns, obj.stat().st_ctime_ns) != (
            old.st_mtime_ns,
            old.st_ctime_ns,
        )
        anchor.check()
        assert len(calls) == 1
        assert anchor.summary() == original
        assert len(calls) == 1  # Accepted times cached; original proof unchanged.
        cold = pub._ExistingObjects(repo / ".git/objects", baseline, commit, policy, prior=original)
        try:
            assert cold.summary() == original
        finally:
            cold.close()
        metadata = obj.stat()
        mode = metadata.st_mode & 0o777
        obj.chmod(0o600)
        with obj.open("r+b") as stream:
            stream.seek(10)
            byte = stream.read(1)
            stream.seek(10)
            stream.write(bytes([byte[0] ^ 1]))
        obj.chmod(mode)
        assert (obj.stat().st_ino, obj.stat().st_size, obj.stat().st_mode & 0o777) == (
            metadata.st_ino,
            metadata.st_size,
            mode,
        )
        with pytest.raises(pub.PublicationHeld, match="content differs"):
            anchor.check()
        with pytest.raises(pub.PublicationHeld, match="content differs"):
            pub._ExistingObjects(repo / ".git/objects", baseline, commit, policy, prior=original)
    finally:
        anchor.close()


def test_changed_index_sidecar_is_not_timestamp_freshening(tmp_path):
    repo, _git, _env, baseline, _candidate, _pr, commit, policy = fixture(tmp_path)
    anchor = pub._ExistingObjects(repo / ".git/objects", baseline, commit, policy)
    original = anchor.summary()
    path = next((repo / ".git/objects/pack").glob("*.idx"))
    mode = path.stat().st_mode & 0o777
    try:
        path.chmod(0o600)
        with path.open("r+b") as stream:
            stream.seek(10)
            value = stream.read(1)
            stream.seek(10)
            stream.write(bytes([value[0] ^ 1]))
        path.chmod(mode)
        with pytest.raises(pub.PublicationHeld, match="sidecar content differs"):
            anchor.check()
        with pytest.raises(pub.PublicationHeld, match="sidecar content differs"):
            pub._ExistingObjects(repo / ".git/objects", baseline, commit, policy, prior=original)
    finally:
        anchor.close()


@pytest.mark.parametrize("bad", [None, {"publication_v1": {}, "publication_v2": {}}])
def test_malformed_publication_is_held_per_campaign_and_next_campaign_continues(bad):
    import ast
    import copy
    import inspect
    from types import SimpleNamespace

    import closer_campaign as campaign

    tree = ast.parse(inspect.getsource(campaign.tick))
    pending = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and ast.unparse(node.test) == "pending.get('kind') != 'launch'"
    )
    boundary = next(
        node
        for node in pending.body
        if isinstance(node, ast.Try) and "publication_key" in ast.unparse(node)
    )
    follow = next(
        node
        for node in pending.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "publication is not None"
    )
    calls, report = [], []
    # The exact source try and held/continue block execute twice. No tick
    # qualification/Root/authority is manufactured by this control.
    body = [
        ast.Assign(
            targets=[ast.Name(id="configured_consumers", ctx=ast.Store())],
            value=ast.Subscript(
                value=ast.Name(id="cases", ctx=ast.Load()),
                slice=ast.Name(id="label", ctx=ast.Load()),
                ctx=ast.Load(),
            ),
        ),
        copy.deepcopy(boundary),
        copy.deepcopy(follow),
        ast.Expr(
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="calls", ctx=ast.Load()), attr="append", ctx=ast.Load()
                ),
                args=[ast.Name(id="label", ctx=ast.Load())],
                keywords=[],
            )
        ),
    ]
    loop = ast.For(
        target=ast.Name(id="label", ctx=ast.Store()),
        iter=ast.List(elts=[ast.Constant("bad"), ast.Constant("next")], ctx=ast.Load()),
        body=body,
        orelse=[],
    )
    env = dict(
        cases={"bad": {"one": bad}, "next": {}},
        calls=calls,
        report=report,
        PublicationHeld=pub.PublicationHeld,
        campaign=None,
        doc={},
        manifest={},
        manifest_path="/no-live-manifest",
        pending={"kind": "reconcile"},
        path=SimpleNamespace(name="bad.campaign.json"),
        publish_if_selected=lambda *_: pytest.fail(
            "malformed configuration must not reach publisher"
        ),
    )
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[])),
            "actual-per-campaign-publication-boundary",
            "exec",
        ),
        env,
    )
    assert calls == ["next"]
    assert len(report) == 1 and report[0]["kind"] == "existing_pr_publication_held"
    assert report[0]["authority"] is False


@pytest.mark.parametrize("expire_mid_read", [False, True])
def test_stream_verification_scopes_once_per_file_and_keeps_original_deadline(
    tmp_path, monkeypatch, expire_mid_read
):
    import time

    repo, _git, _env, baseline, _candidate, _pr, commit, policy = fixture(tmp_path)
    keep = next((repo / ".git/objects/pack").glob("*.pack")).with_suffix(".keep")
    keep.write_bytes(b"bounded metadata" * 20000)
    checks, reads = [], []
    original_clock = time.time()
    deadline = original_clock + 60
    anchor = pub._ExistingObjects(
        repo / ".git/objects",
        baseline,
        commit,
        policy,
        read_context=lambda: checks.append("scope"),
        deadline=deadline,
    )
    checks.clear()
    original_read = os.read
    clock = [original_clock]

    def read(fd, bound):
        raw = original_read(fd, bound)
        reads.append(len(raw))
        if expire_mid_read and raw:
            clock[0] = deadline + 1
        return raw

    os.utime(keep, ns=(keep.stat().st_atime_ns, keep.stat().st_mtime_ns + 1000000))
    monkeypatch.setattr(pub.time, "time", lambda: clock[0])
    monkeypatch.setattr(pub.os, "read", read)
    try:
        if expire_mid_read:
            with pytest.raises(
                pub.PublicationHeld, match="original object read deadline exhausted"
            ):
                anchor.check()
            assert len(reads) == 1
        else:
            anchor.check()
            assert len(reads) > 3
        assert checks == ["scope", "scope"]
    finally:
        anchor.close()
