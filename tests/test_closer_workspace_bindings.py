"""Prospective workspace routing never rewrites an old unknown callback's cwd."""

import hashlib
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
import test_closer_quarantine_closure as fixture

import closer_campaign as campaign
import closer_dispatcher as dispatcher
import closer_pipeline as pipeline
import closer_quarantine as quarantine
import closer_resources as resources


@pytest.mark.parametrize(
    "first_kind,second_kind", [("core", "core"), ("core", "legacy"), ("legacy", "core")]
)
@pytest.mark.parametrize("reverse", [False, True])
def test_concurrent_parent_child_admission_never_double_acts(
    tmp_path, monkeypatch, first_kind, second_kind, reverse
):
    """Different repo keys cannot race past inventory before either publishes intent."""
    monkeypatch.setattr(resources, "RESOURCE_WAIT_SECONDS", 0.12)
    root = tmp_path / "state.scoped"
    parent = tmp_path / "checkout"
    child = parent / "nested"
    child.mkdir(parents=True)
    parent, child = parent.resolve(), child.resolve()
    first_path, second_path = (child, parent) if reverse else (parent, child)
    admitted, release = threading.Event(), threading.Event()
    effects, checks = [], []

    def attempt(kind, path, number, hold):
        def scope_check(_keys):
            checks.append(number)
            if hold:
                admitted.set()
                assert release.wait(3)

        kwargs = dict(
            target=f"owner/repo{number}#{number}", owner=f"worker{number}", deadline=time.time() + 3
        )
        manager = resources.reserve if kind == "core" else resources.legacy_reserve
        if kind == "core":
            kwargs["scope_check"] = scope_check
        try:
            with manager(root, [f"repo:owner/repo{number}", f"worktree:{path}"], **kwargs):
                effects.append(number)
                if hold and kind == "legacy":
                    admitted.set()
                    assert release.wait(3)
            return "acted"
        except resources.ResourceBlocked:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attempt, first_kind, first_path, 1, True)
        try:
            assert admitted.wait(3)
            second = pool.submit(attempt, second_kind, second_path, 2, False)
            assert second.result(timeout=3) == "blocked"
            assert 2 not in effects and 2 not in checks
        finally:
            release.set()
        assert first.result(timeout=3) == "acted"
    # Unlocking the callback does not erase the original uncertain intent.
    assert attempt(second_kind, second_path, 2, False) == "blocked"
    assert effects == [1]


def test_sibling_workspace_effects_remain_concurrent(tmp_path):
    root = tmp_path / "state.scoped"
    siblings = [tmp_path / "checkouts" / name for name in ("one", "two")]
    for path in siblings:
        path.mkdir(parents=True)
    both_entered = threading.Barrier(2)

    def effect(number):
        with resources.reserve(
            root,
            [f"repo:owner/repo{number}", f"worktree:{siblings[number].resolve()}"],
            target=f"owner/repo{number}#1",
            owner=str(number),
            deadline=time.time() + 3,
        ):
            both_entered.wait(timeout=2)
        return number

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(effect, (0, 1)))
    assert results == [0, 1]


@pytest.mark.parametrize("nested", [False, True])
def test_physical_case_alias_blocks_live_and_retained_workspace(tmp_path, monkeypatch, nested):
    monkeypatch.setattr(resources, "RESOURCE_WAIT_SECONDS", 0.08)
    original = tmp_path / "OriginalCheckout"
    original.mkdir()
    alias = tmp_path / "oRIGINALcHECKOUT"
    if not alias.exists():
        pytest.skip("filesystem distinguishes directory name case")
    candidate = original
    if nested:
        candidate = original / "nested"
        candidate.mkdir()
    assert original.samefile(alias)
    old_key, new_key = f"worktree:{alias}", f"worktree:{candidate}"
    assert quarantine.resource_overlap(old_key, new_key)
    root = tmp_path / "state.scoped"
    with resources.reserve(
        root, [old_key], target="owner/first#1", owner="first", deadline=time.time() + 2
    ):
        with pytest.raises(resources.ResourceBlocked):
            with resources.reserve(
                root, [new_key], target="owner/second#2", owner="second", deadline=time.time() + 2
            ):
                pytest.fail("physical case alias entered a simultaneous effect")
    retained = resources.unknown_reservations(root)
    assert old_key in retained
    with pytest.raises(resources.ResourceBlocked, match="unresolved effect"):
        with resources.reserve(
            root, [new_key], target="owner/second#2", owner="second", deadline=time.time() + 2
        ):
            pytest.fail("physical case alias bypassed retained original intent")
    assert resources.unknown_reservations(root) == retained


def bindings(old, new, target="owner/repo#9"):
    return {
        "schema": quarantine.WORKSPACE_SCHEMA,
        "historical": {"owner/repo": str(old)},
        "execution": {target: str(new)},
    }


def original_remote_with_explicit_workspace(tmp_path):
    path, raw, snap, row, doc, *_ = fixture.prepared(tmp_path)
    old = tmp_path / "original checkout"
    old.mkdir()
    row["operation"] = {
        "action": "merge",
        "status": "uncertain",
        "binding": {
            "pr_target": snap["pr_target"],
            "head": snap["head"],
            "merge": snap["merge"],
            "source_targets": [source["target"] for source in snap["sources"]],
        },
    }
    row["resource_keys"] = ["worktree:" + str(old)]
    fixture.write(path, doc)
    return path, old


def candidate_keys(target, workspace, *extra):
    return resources._scope_keys(
        target,
        {},
        lambda *_: [
            "pr:" + target,
            "branch:owner/repo@other-branch",
            *extra,
        ],
        workspace,
    )


def test_effective_target_override_does_not_readd_unused_repository_checkout(tmp_path):
    path, old = original_remote_with_explicit_workspace(tmp_path)
    new = tmp_path / "isolated checkout"
    new.mkdir()
    mapping = bindings(old, new)
    before = path.read_bytes()
    snapshot = quarantine.scope_snapshot(path, workspaces=mapping)
    assert "worktree:" + str(old) in snapshot["quarantine_keys"]
    assert "repo:owner/repo" not in snapshot["quarantine_keys"]
    keys = candidate_keys("owner/repo#9", mapping)
    assert "worktree:" + str(new) in keys
    assert "worktree:" + str(old) not in keys
    assert quarantine.scope_problem(snapshot, "owner/repo#9", keys) is None
    for alias in ("pr:owner/repo#2", "issue:owner/repo#1", "branch:owner/repo@feature-one"):
        assert quarantine.scope_problem(snapshot, "owner/repo#9", [*keys, alias])
    assert path.read_bytes() == before


@pytest.mark.parametrize("mode", ["same", "symlink", "child", "parent"])
def test_original_explicit_worktree_fence_includes_physical_and_writable_overlap(tmp_path, mode):
    path, old = original_remote_with_explicit_workspace(tmp_path)
    selected = old
    if mode == "symlink":
        selected = tmp_path / "alias"
        selected.symlink_to(old, target_is_directory=True)
    elif mode == "child":
        selected = old / "nested"
        selected.mkdir()
    elif mode == "parent":
        selected = tmp_path
    mapping = bindings(old, selected)
    snapshot = quarantine.scope_snapshot(path, workspaces=mapping)
    assert quarantine.scope_problem(
        snapshot, "owner/repo#9", candidate_keys("owner/repo#9", mapping)
    )


def test_new_map_never_becomes_original_cwd_proof_even_with_default_and_override(tmp_path):
    path, *_ = fixture.prepared(tmp_path)
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    original = path.read_bytes()
    for mapping in (
        {"owner/repo": str(old), "owner/repo#1": str(new), "owner/repo#9": str(new)},
        bindings(old, new),
        {
            "schema": quarantine.WORKSPACE_SCHEMA,
            "historical": {"owner/repo": str(new)},
            "execution": {"owner/repo#9": str(new)},
        },
    ):
        snapshot = quarantine.scope_snapshot(path, workspaces=mapping)
        assert snapshot["items"][0]["closure"] == "repository_fallback"
        assert snapshot["items"][0]["closure_reason"] == "original_execution_workspace_unavailable"
        assert "repo:owner/repo" in snapshot["quarantine_keys"]
        assert quarantine.scope_problem(
            snapshot, "owner/repo#9", candidate_keys("owner/repo#9", mapping)
        )
    assert path.read_bytes() == original


def checkout(tmp_path, name="checkout with spaces", remote="https://github.com/owner/repo.git"):
    directory = tmp_path / name
    directory.mkdir()
    subprocess.run(["git", "init", "-q", str(directory)], check=True)
    subprocess.run(["git", "-C", str(directory), "remote", "add", "origin", remote], check=True)
    return directory


def config(tmp_path, mapping):
    path = tmp_path / "workspace bindings with spaces.json"
    path.write_text(json.dumps(mapping))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_native_command_forwards_exact_file_and_hash_without_shell_splitting(tmp_path):
    directory = checkout(tmp_path)
    path, digest = config(tmp_path, bindings(tmp_path / "old", directory))
    cfg = {
        "assignment": "assignment",
        "dispatch_state": "state",
        "apply": True,
        "runtime_manifest": "manifest",
        "workspaces": {"path": str(path), "sha256": digest},
    }
    command = campaign.dispatch_command(cfg, sys.executable, tmp_path)
    assert command[command.index("--workspaces") + 1] == str(path)
    assert command[command.index("--workspaces-sha256") + 1] == digest
    effective, binding = dispatcher.load_workspace_config(
        path, digest, require_pin=True, targets=["owner/repo#9"]
    )
    assert dispatcher.selected_workspace(effective, "Owner/Repo#9") == str(directory)
    assert binding["historical"]["owner/repo"] == str(tmp_path / "old")
    path.write_text(json.dumps(bindings(tmp_path / "old", tmp_path / "elsewhere")))
    with pytest.raises(ValueError, match="admitted.*pin|SHA256"):
        campaign.dispatch_command(cfg, sys.executable, tmp_path)
    with pytest.raises(ValueError, match="SHA256"):
        dispatcher.load_workspace_config(path, digest, require_pin=True, targets=["owner/repo#9"])


@pytest.mark.parametrize(
    "damage", ["missing_pin", "wrong_repo", "symlink_file", "duplicate", "repo_override"]
)
def test_workspace_routing_refuses_unbound_or_forged_execution_config(tmp_path, damage):
    directory = checkout(
        tmp_path,
        remote=(
            "https://example.com/owner/repo.git"
            if damage == "wrong_repo"
            else "https://github.com/owner/repo.git"
        ),
    )
    mapping = bindings(tmp_path / "old", directory)
    if damage == "repo_override":
        mapping["execution"] = {"owner/repo": str(directory)}
    path, digest = config(tmp_path, mapping)
    if damage == "duplicate":
        path.write_text('{"owner/repo":"/tmp/one","owner/repo":"/tmp/two"}')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if damage == "symlink_file":
        alias = tmp_path / "config alias"
        alias.symlink_to(path)
        path = alias
    with pytest.raises(ValueError):
        dispatcher.load_workspace_config(
            path, None if damage == "missing_pin" else digest, require_pin=True
        )


def test_prepared_inventory_binds_prospective_mapping_and_cannot_reuse_other_checkout(tmp_path):
    _path, _raw, snap, *_ = fixture.prepared(tmp_path)
    mapping = bindings(tmp_path / "old", tmp_path / "new", target=snap["target"])
    effective = {**mapping["historical"], **mapping["execution"]}
    original = deepcopy(snap)
    with pytest.raises(resources.ResourceBlocked, match="execution workspace"):
        dispatcher._prepared_resource_keys(
            snap["target"],
            {"revision": snap["revision"]},
            snap,
            effective,
            workspace_bindings=mapping,
        )
    snap["execution_workspace"] = dispatcher._workspace_snapshot_binding(
        snap["target"], effective, mapping
    )
    keys = dispatcher._prepared_resource_keys(
        snap["target"], {"revision": snap["revision"]}, snap, effective, workspace_bindings=mapping
    )
    assert "worktree:" + str(tmp_path / "new") in keys
    changed = deepcopy(mapping)
    changed["execution"][snap["target"]] = str(tmp_path / "another")
    with pytest.raises(resources.ResourceBlocked, match="execution workspace"):
        dispatcher._prepared_resource_keys(
            snap["target"],
            {"revision": snap["revision"]},
            snap,
            {**changed["historical"], **changed["execution"]},
            workspace_bindings=changed,
        )
    assert "execution_workspace" not in original


def test_real_native_command_reaches_dispatcher_backend_with_frozen_workspace_pin(
    tmp_path, monkeypatch
):
    import closer_runtime_binding

    directory = checkout(tmp_path)
    path, digest = config(tmp_path, bindings(tmp_path / "original", directory))
    assignment = tmp_path / "assignment.json"
    assignment.write_text(json.dumps({"targets": [{"target": "owner/repo#9"}]}))
    cfg = {
        "assignment": str(assignment),
        "dispatch_state": str(tmp_path / "queue.json"),
        "apply": True,
        "runtime_manifest": str(tmp_path / "manifest.json"),
        "coordinator_mode": "resource_scoped",
        "coordinator_contract_pin": pipeline.scoped_capability(),
        "workspaces": {"path": str(path), "sha256": digest},
    }
    seen = []

    def staged(args, targets, backend, _deadline):
        seen.append(
            (
                args.workspaces,
                args.workspaces_sha256,
                dispatcher.selected_workspace(backend.workspaces, targets[0]),
                backend.workspace_bindings,
            )
        )
        return {"round_id": "1" * 32}

    monkeypatch.setattr(dispatcher, "_run_staged_pipeline", staged)
    monkeypatch.setattr(dispatcher, "known_workspaces", lambda _: {})
    monkeypatch.setattr(dispatcher, "_scoped_budget_admission", lambda *_: None)
    monkeypatch.setattr(closer_runtime_binding, "binding_from_manifest", lambda _: object())
    monkeypatch.setattr(pipeline, "validate_stop_scoped", lambda *_: {"valid": True})
    command = campaign.dispatch_command(cfg, sys.executable, tmp_path)
    # CLI parsing, native Runtime binding, workspace loading and real Closer
    # construction all run. The staged callback is the no-effects boundary.
    assert dispatcher.main(command[2:]) == 0
    assert seen == [(str(path), digest, str(directory), bindings(tmp_path / "original", directory))]


@pytest.mark.parametrize(
    "damage", ["missing_field", "stale", "storage_subset", "bool_type", "extra"]
)
def test_native_dispatcher_rejects_incomplete_or_stale_capability_before_callbacks(
    tmp_path, monkeypatch, damage
):
    pin = pipeline.scoped_capability()
    if damage == "missing_field":
        pin.pop("resource_keys_callback")
    elif damage == "stale":
        pin["layout"] = 999
    elif damage == "bool_type":
        pin["coordination_gates"] = True
    elif damage == "extra":
        pin["unexpected"] = True
    else:
        pin = {
            "schema": resources.CONTRACT_SCHEMA,
            "layout": resources.LAYOUT,
            "pipeline_schema": pipeline.SCHEMA,
            "coordination_gates": 1,
        }
    monkeypatch.setattr(
        dispatcher,
        "_main_admitted",
        lambda *_: pytest.fail("invalid pin reached callback boundary"),
    )
    with pytest.raises(SystemExit) as exc:
        dispatcher.main(
            [
                "run",
                "--assignment",
                str(tmp_path / "missing-assignment"),
                "--state",
                str(tmp_path / "queue"),
                "--apply",
                "--workers",
                "--pipeline",
                "--coordinator-mode",
                "resource_scoped",
                "--runtime-manifest",
                "manifest",
                "--coordinator-contract-pin",
                json.dumps(pin),
            ]
        )
    assert exc.value.code == 2


def test_workspace_config_access_time_change_is_not_content_mutation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import closer_native_observer as native

    directory = checkout(tmp_path)
    path, digest = config(tmp_path, bindings(tmp_path / "old", directory))
    real = native.os.fstat
    calls = 0

    def atime_only(fd):
        nonlocal calls
        value = real(fd)
        calls += 1
        if calls <= 2:
            fields = {
                key: getattr(value, key)
                for key in (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                    "st_atime_ns",
                )
            }
            fields["st_atime_ns"] += calls
            return SimpleNamespace(**fields)
        return value

    monkeypatch.setattr(native.os, "fstat", atime_only)
    effective, _ = dispatcher.load_workspace_config(
        path, digest, require_pin=True, targets=["owner/repo#9"]
    )
    assert effective["owner/repo#9"] == str(directory)


@pytest.mark.parametrize("damage", ["mutation", "symlink_race", "fifo_race"])
def test_workspace_config_fd_read_refuses_replacement_and_never_blocks_on_fifo(
    tmp_path, monkeypatch, damage
):
    import os

    import closer_native_observer as native

    path, digest = config(tmp_path, {})
    if damage == "mutation":
        real = native.os.fstat
        calls = 0

        def mutate(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                path.write_bytes(b'{"changed":"/tmp/workspace"}')
            return real(fd)

        monkeypatch.setattr(native.os, "fstat", mutate)
    else:
        real_open = native.os.open
        target = tmp_path / "other-config"
        target.write_bytes(path.read_bytes())

        def replace(name, flags, *args, **kwargs):
            if Path(name) == path:
                path.unlink()
                if damage == "symlink_race":
                    path.symlink_to(target)
                else:
                    os.mkfifo(path)
                assert flags & os.O_NONBLOCK
                assert flags & os.O_NOFOLLOW
            return real_open(name, flags, *args, **kwargs)

        monkeypatch.setattr(native.os, "open", replace)
    with pytest.raises(ValueError, match="stable bounded regular"):
        dispatcher.load_workspace_config(path, digest, require_pin=True, targets=["owner/repo#9"])


def test_fallback_keeps_explicit_original_inline_workspace_when_retained_packet_has_no_cwd(
    tmp_path,
):
    path, _raw, _snap, row, doc, *_ = fixture.prepared(tmp_path)
    old, new = tmp_path / "original", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    row["results"]["prepare"]["snapshot"] = {"workspace": str(old)}
    fixture.write(path, doc)
    snapshot = quarantine.scope_snapshot(path, workspaces=bindings(new, new))
    assert "worktree:" + str(old) in snapshot["quarantine_keys"]
    assert snapshot["items"][0]["closure"] == "repository_fallback"


def test_execution_workspace_cannot_hide_shared_git_root_by_naming_subdirectory(tmp_path):
    root = checkout(tmp_path)
    nested = root / "subdirectory"
    nested.mkdir()
    path, digest = config(tmp_path, bindings(tmp_path / "old", nested))
    with pytest.raises(ValueError, match="actual Git worktree root"):
        dispatcher.load_workspace_config(path, digest, require_pin=True, targets=["owner/repo#9"])


def test_native_legacy_flat_mapping_never_routes_wrong_repository(tmp_path):
    wrong = checkout(tmp_path, remote="https://github.com/other/wrong.git")
    path, digest = config(tmp_path, {"owner/repo#9": str(wrong)})
    with pytest.raises(ValueError, match="versioned bindings"):
        dispatcher.load_workspace_config(path, digest, require_pin=True, targets=["owner/repo#9"])


def test_native_selected_historical_fallback_is_identity_checked(tmp_path):
    wrong = checkout(tmp_path, remote="https://github.com/other/wrong.git")
    path, digest = config(
        tmp_path,
        {
            "schema": quarantine.WORKSPACE_SCHEMA,
            "historical": {"owner/repo": str(wrong)},
            "execution": {},
        },
    )
    with pytest.raises(ValueError, match="repository identity"):
        dispatcher.load_workspace_config(path, digest, require_pin=True, targets=["owner/repo#9"])


@pytest.mark.parametrize(
    "remote", ["https://github.com/Owner/Repo.git", "git@github.com:Owner/Repo.git"]
)
def test_native_exact_physical_workspace_accepts_canonical_github_transports(tmp_path, remote):
    root = checkout(tmp_path, remote=remote)
    path, digest = config(tmp_path, bindings(tmp_path / "unused old hint", root))
    selected, _ = dispatcher.load_workspace_config(
        path, digest, require_pin=True, targets=["owner/repo#9"]
    )
    assert selected["owner/repo#9"] == str(root)


def test_native_alias_retarget_cannot_change_admitted_physical_route(tmp_path):
    first = checkout(tmp_path, name="first")
    second = checkout(tmp_path, name="second")
    alias = tmp_path / "routing alias"
    alias.symlink_to(first, target_is_directory=True)
    path, digest = config(tmp_path, bindings(tmp_path / "old", alias))
    for destination in (first, second):
        alias.unlink()
        alias.symlink_to(destination, target_is_directory=True)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        with pytest.raises(ValueError, match="canonical physical"):
            dispatcher.load_workspace_config(
                path, digest, require_pin=True, targets=["owner/repo#9"]
            )
