"""Private filesystem and process controls for the shared registry boundary."""

import hashlib
import json
import multiprocessing
import os
from pathlib import Path

import pytest

import mutable_registry as registry


def validate(value):
    if not isinstance(value, dict) or set(value) != {"records"}:
        raise registry.RegistryError("unsupported schema")
    if not isinstance(value["records"], list):
        raise registry.RegistryError("unsupported records")


def default():
    return {"records": []}


def _worker(path, worker):
    registry.initialize(path, default, validate)
    for number in range(12):

        def append(value):
            value["records"].append([worker, number])

        registry.update(path, default, validate, append)


def test_read_is_noncreating_and_deep_copies_defaults(tmp_path):
    seed = {"records": [[1]]}
    path = tmp_path / "missing" / "state.json"
    result = registry.read(path, lambda: seed, validate)
    result["records"][0].append(2)
    assert seed == {"records": [[1]]}
    assert not path.parent.exists()


def test_initialize_is_idempotent_and_update_detaches_seed(tmp_path):
    path = tmp_path / "private" / "state.json"
    seed = {"records": [[1]]}
    value = registry.update(path, lambda: seed, validate, lambda value: value["records"].append(2))
    assert seed == {"records": [[1]]}
    assert value == {"records": [[1], 2]}
    before = path.stat().st_mtime_ns
    assert registry.initialize(path, default, validate) == value
    assert path.stat().st_mtime_ns == before
    value["records"].append(3)
    assert registry.read(path, default, validate) == {"records": [[1], 2]}


@pytest.mark.parametrize(
    "raw",
    ['{"records":', '{"new_schema": []}', '{"records": [], "records": [1]}', '{"records": [NaN]}'],
)
@pytest.mark.parametrize("operation", ["read", "initialize", "write", "update"])
def test_malformed_existing_never_reseeded_or_overwritten(tmp_path, raw, operation):
    path = tmp_path / "state.json"
    path.write_text(raw)

    def action():
        if operation == "write":
            return registry.write(path, default(), validate)
        if operation == "update":
            return registry.update(path, default, validate, lambda value: value)
        return getattr(registry, operation)(path, default, validate)

    with pytest.raises(registry.RegistryError):
        action()
    assert path.read_text() == raw
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "kind", ["repository", "module", "mirror", "resolved_mirror", "old_generation"]
)
def test_immutable_destination_refused_before_any_creation(tmp_path, monkeypatch, kind):
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    target = tmp_path / "physical-generation"
    target.mkdir()
    mirror = codex / "orchestrator-mirror"
    mirror.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    repo = tmp_path / "repository"
    module = tmp_path / "modules"
    monkeypatch.setattr(registry.paths, "REPO_ROOT", repo)
    monkeypatch.setattr(registry.paths, "MODULE_DIR", module)
    roots = {
        "repository": repo,
        "module": module,
        "mirror": mirror,
        "resolved_mirror": target,
        "old_generation": codex / "orchestrator-mirror.generation-old",
    }
    path = roots[kind] / "absent" / "registry.json"
    with pytest.raises(registry.RegistryError, match="immutable"):
        registry.initialize(path, default, validate)
    assert not path.parent.exists()


def test_legacy_immutable_file_can_be_read_without_lock_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(registry.paths, "REPO_ROOT", tmp_path)
    path = tmp_path / "state.json"
    path.write_text(json.dumps(default()))
    assert registry.read(path, default, validate) == default()
    assert not path.with_name(path.name + ".lock").exists()
    with pytest.raises(registry.RegistryError):
        registry.write(path, default(), validate)


@pytest.mark.parametrize("kind", ["ancestor", "leaf", "lock", "hardlink", "lock_hardlink", "fifo"])
def test_symlinks_hardlinks_and_special_files_refused_without_mutation(tmp_path, kind):
    actual = tmp_path / "actual"
    actual.mkdir()
    master = actual / "master.json"
    master.write_text(json.dumps(default()))
    path = tmp_path / "state.json"
    if kind == "ancestor":
        alias = tmp_path / "alias"
        alias.symlink_to(actual, target_is_directory=True)
        path = alias / "new" / "state.json"
    elif kind == "leaf":
        path.symlink_to(master)
    elif kind == "lock":
        path.with_name(path.name + ".lock").symlink_to(master)
    elif kind == "hardlink":
        os.link(master, path)
    elif kind == "lock_hardlink":
        os.link(master, path.with_name(path.name + ".lock"))
    else:
        os.mkfifo(path)
    before = master.read_bytes()
    with pytest.raises((registry.RegistryError, OSError)):
        registry.write(path, default(), validate)
    assert master.read_bytes() == before
    assert not (actual / "new").exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_parent_traversal_refused_before_creation(tmp_path):
    path = tmp_path / "absent" / ".." / "state.json"
    with pytest.raises(registry.RegistryError, match="traversal"):
        registry.write(path, default(), validate)
    assert not (tmp_path / "absent").exists()


def test_invalid_new_document_does_not_create_anything(tmp_path):
    path = tmp_path / "new" / "state.json"
    with pytest.raises(registry.RegistryError):
        registry.write(path, {"unknown": []}, validate)
    assert not path.parent.exists()


def test_failed_replace_preserves_old_document_and_removes_temporary(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    registry.write(path, default(), validate)
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise PermissionError("private injected replacement failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(PermissionError):
        registry.update(path, default, validate, lambda value: {"records": [1]})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


def test_lock_replacement_during_mutator_refuses_publication(tmp_path):
    path = tmp_path / "state.json"
    registry.write(path, default(), validate)
    before = path.read_bytes()

    def replace_lock(value):
        lock = path.with_name(path.name + ".lock")
        lock.unlink()
        lock.write_text("replacement")
        value["records"].append(1)

    with pytest.raises(registry.RegistryError, match="lock changed"):
        registry.update(path, default, validate, replace_lock)
    assert path.read_bytes() == before


@pytest.mark.parametrize("round_number", range(8))
def test_concurrent_initialization_and_updates_conserve_all_records(tmp_path, round_number):
    path = tmp_path / "private" / "state.json"
    context = multiprocessing.get_context("fork")
    processes = [context.Process(target=_worker, args=(path, worker)) for worker in range(4)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
    value = registry.read(path, default, validate)
    assert sorted(value["records"]) == [
        [worker, number] for worker in range(4) for number in range(12)
    ]


def test_preflight_accepts_regular_snapshot_unlinked_by_atomic_replacement(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"records": ["old"]}))
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps({"records": ["new"]}))
    original_lstat = Path.lstat
    old_fd = os.open(path, os.O_RDONLY)
    observed = []

    def raced_lstat(selected):
        if selected == path and not observed:
            os.replace(replacement, path)
            snapshot = os.fstat(old_fd)
            assert snapshot.st_nlink == 0
            observed.append(snapshot.st_ino)
            return snapshot
        return original_lstat(selected)

    monkeypatch.setattr(Path, "lstat", raced_lstat)
    try:
        assert registry.initialize(path, default, validate) == {"records": ["new"]}
    finally:
        os.close(old_fd)
    assert len(observed) == 1


def test_read_accepts_opened_unlinked_snapshot_without_reseeding(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"records": ["old"]}))
    old_identity = path.stat().st_ino
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps({"records": ["new"]}))
    original_fstat = os.fstat
    observed = []

    def raced_fstat(fd):
        info = original_fstat(fd)
        if info.st_ino == old_identity and not observed:
            os.replace(replacement, path)
            info = original_fstat(fd)
            assert info.st_nlink == 0
            observed.append(info.st_ino)
        return info

    monkeypatch.setattr(os, "fstat", raced_fstat)
    assert registry.read(path, default, validate) == {"records": ["old"]}
    assert json.loads(path.read_text()) == {"records": ["new"]}
    assert len(observed) == 1
    assert not path.with_name(path.name + ".lock").exists()


def test_read_only_existing_preserved(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(default()))
    path.chmod(0o400)
    with pytest.raises(PermissionError):
        registry.write(path, {"records": [1]}, validate)
    assert json.loads(path.read_text()) == default()
    assert not path.with_name(path.name + ".lock").exists()


def test_incumbent_lock_is_opened_without_create_after_exclusive_claim(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    lock = path.with_name(path.name + ".lock")
    lock.write_text("")
    identity = lock.stat().st_ino
    original_open = os.open
    lock_flags = []

    def observe_open(name, flags, *args, **kwargs):
        if name == lock.name:
            lock_flags.append(flags)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", observe_open)
    registry.initialize(path, default, validate)
    assert len(lock_flags) == 2
    assert lock_flags[0] & os.O_CREAT and lock_flags[0] & os.O_EXCL
    assert not lock_flags[1] & os.O_CREAT
    assert all(flags & os.O_NOFOLLOW for flags in lock_flags)
    assert lock.stat().st_ino == identity


def test_missing_lock_open_error_is_not_retried_or_given_another_lock(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    original_open = os.open
    attempts = []

    def fail_lock_open(name, flags, *args, **kwargs):
        if name == "state.json.lock":
            attempts.append(name)
            raise FileNotFoundError("private injected lock admission failure")
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_lock_open)
    with pytest.raises(FileNotFoundError, match="admission failure"):
        registry.initialize(path, default, validate)
    assert attempts == ["state.json.lock"]
    assert list(tmp_path.iterdir()) == []


def test_custom_legacy_seed_guard_holds_before_initialization_creates_anything(tmp_path):
    legacy = tmp_path / "legacy.json"
    prior = {"records": ["operator record"]}
    legacy.write_text(json.dumps(prior))
    current = tmp_path / "runtime" / "state.json"
    before = legacy.read_bytes()
    digest = hashlib.sha256(
        json.dumps(prior, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(registry.RegistryError, match="reviewed migration required") as caught:
        registry.initialize(
            current, lambda: registry.seed_default(current, legacy, default, validate), validate
        )
    assert str(legacy) in str(caught.value)
    assert digest in str(caught.value)
    assert not current.parent.exists()
    assert legacy.read_bytes() == before


@pytest.mark.parametrize(
    "raw", ['{"records":', '{"new_schema": []}', '{"records":[],"records":[]}']
)
def test_invalid_legacy_is_not_silently_replaced_by_virtual_seed(tmp_path, raw):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(raw)
    current = tmp_path / "absent" / "state.json"
    with pytest.raises(registry.RegistryError):
        registry.seed_default(current, legacy, default, validate)
    assert legacy.read_text() == raw
    assert not current.parent.exists()
    assert not legacy.with_name(legacy.name + ".lock").exists()


def test_pristine_legacy_allows_pure_seed_without_import_or_filesystem_changes(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(default()))
    current = tmp_path / "absent" / "state.json"
    before = legacy.read_bytes()
    assert registry.seed_default(current, legacy, default, validate) == default()
    assert legacy.read_bytes() == before
    assert not current.parent.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["legacy.json"]


def test_existing_runtime_is_authoritative_even_with_invalid_legacy(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text("corrupt legacy")
    current = tmp_path / "state.json"
    current.write_text(json.dumps({"records": ["runtime record"]}))
    assert registry.seed_default(current, legacy, default, validate) == default()
    assert registry.read(current, default, validate) == {"records": ["runtime record"]}
    current.write_text("corrupt runtime")
    # seed_default does not purport to validate the authoritative current store.
    assert registry.seed_default(current, legacy, default, validate) == default()
    with pytest.raises(registry.RegistryError):
        registry.read(current, default, validate)
    assert legacy.read_text() == "corrupt legacy"


@pytest.mark.parametrize("dangling", [False, True])
def test_legacy_symlink_is_refused_without_creating_runtime(tmp_path, dangling):
    target = tmp_path / "target.json"
    if not dangling:
        target.write_text(json.dumps(default()))
    legacy = tmp_path / "legacy.json"
    legacy.symlink_to(target)
    current = tmp_path / "absent" / "state.json"
    with pytest.raises(registry.RegistryError, match="symlink"):
        registry.seed_default(current, legacy, default, validate)
    assert not current.parent.exists()


def test_legacy_normalization_preserves_original_documents(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"records": [{"uses": ["first"], "first_seen": 99}]}))
    seed = {"records": [{"uses": ["first"], "first_seen": 0, "count": 1}]}
    before = legacy.read_bytes()

    def normalize(value):
        for item in value["records"]:
            item.pop("first_seen", None)
            item.setdefault("count", len(item["uses"]))
        return value

    current = tmp_path / "absent" / "state.json"
    result = registry.seed_default(current, legacy, lambda: seed, validate, normalize=normalize)
    assert result == seed
    result["records"][0]["uses"].append("second")
    assert seed["records"][0]["uses"] == ["first"]
    assert legacy.read_bytes() == before
    assert not current.parent.exists()


def test_legacy_comparison_is_json_exact_and_does_not_equate_true_with_one(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"records": [True]}))
    with pytest.raises(registry.RegistryError, match="reviewed migration required"):
        registry.seed_default(tmp_path / "new.json", legacy, lambda: {"records": [1]}, validate)


def test_missing_or_same_legacy_path_never_creates_a_store(tmp_path):
    path = tmp_path / "absent" / "state.json"
    assert registry.seed_default(path, path, default, validate) == default()
    assert registry.seed_default(path, tmp_path / "missing.json", default, validate) == default()
    assert not path.parent.exists()
