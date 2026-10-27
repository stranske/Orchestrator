"""Causal test-harness guards; private files only, no providers or network."""

import os
import pwd
from pathlib import Path
from types import SimpleNamespace

import pytest
from closer_inert_dispatcher_support import InertCaller


def guard_caller(root):
    caller = object.__new__(InertCaller)
    caller.world = SimpleNamespace(root=root)
    return caller


def test_private_pytest_root_may_live_below_account_home(tmp_path, monkeypatch):
    account = tmp_path.resolve() / "account-home"
    root = account / "project/.tmp/pytest-case"
    root.mkdir(parents=True)
    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(account)))
    monkeypatch.setenv("HOME", str(root / "private-home"))
    caller = guard_caller(root)
    guard = caller.temporary_campaign_guard()
    assert guard == root / "private-home/.codex/automations/model-allocation/assess.py"
    assert guard.is_file()
    assert caller.temporary_campaign_guard() == guard
    assert not (account / ".codex").exists()


@pytest.mark.parametrize("location", ["home", ".codex", ".aws", ".config", ".agents"])
def test_campaign_guard_refuses_account_or_protected_root(tmp_path, monkeypatch, location):
    account = tmp_path.resolve() / "account-home"
    root = account if location == "home" else account / location
    root.mkdir(parents=True)
    before = sorted(root.rglob("*"))
    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(account)))
    monkeypatch.setenv("HOME", str(root / "private-home"))
    with pytest.raises(AssertionError):
        guard_caller(root).temporary_campaign_guard()
    assert sorted(root.rglob("*")) == before


def test_campaign_guard_refuses_symlink_and_existing_foreign_file(tmp_path, monkeypatch):
    account = tmp_path.resolve() / "account-home"
    root = tmp_path.resolve() / "pytest-case"
    root.mkdir()
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    home = root / "private-home"
    home.mkdir()
    (home / ".codex").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(account)))
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(AssertionError):
        guard_caller(root).temporary_campaign_guard()
    assert list(outside.iterdir()) == []
    (home / ".codex").unlink()
    guard = home / ".codex/automations/model-allocation/assess.py"
    guard.parent.mkdir(parents=True)
    guard.write_bytes(b"foreign retained bytes")
    with pytest.raises(FileExistsError):
        guard_caller(root).temporary_campaign_guard()
    assert guard.read_bytes() == b"foreign retained bytes"


def core_guard_caller(source, monkeypatch):
    from closer_inert_dispatcher_support import fixture_core_head_binding

    monkeypatch.delenv("CLOSER_CORE_PACKAGE", raising=False)
    monkeypatch.setenv("CLOSER_CORE_SOURCE", str(source))
    monkeypatch.setenv("CLOSER_CORE_SOURCE_REVISION", "a" * 40)
    caller = guard_caller(source)
    caller.world.cli = source / "unused-native-emitter"
    caller.children = {os.getpid()}
    caller.core_head_query, caller.core_head_revision = fixture_core_head_binding()
    return caller


@pytest.fixture
def bound_core(tmp_path, monkeypatch):
    source = tmp_path.resolve() / "core-source"
    (source / "scripts").mkdir(parents=True)
    (source / "scripts/closer_runtime.py").write_text("# Binding-only fixture, never executed.\n")
    (source / ".git").mkdir()
    return core_guard_caller(source, monkeypatch)


def test_core_query_is_bound_once_to_physical_configured_source(bound_core, tmp_path, monkeypatch):
    source = bound_core.world.root
    query = ["git", "-C", str(source), "rev-parse", "HEAD"]
    assert bound_core.allowed(query)
    assert bound_core.core_head_revision == "a" * 40
    foreign = tmp_path.resolve() / "foreign"
    foreign.mkdir()
    monkeypatch.setenv("CLOSER_CORE_SOURCE", str(foreign))
    monkeypatch.setenv("CLOSER_CORE_SOURCE_REVISION", "f" * 40)
    assert bound_core.allowed(query)
    assert bound_core.core_head_revision == "a" * 40
    assert not bound_core.allowed(["git", "-C", str(foreign), "rev-parse", "HEAD"])
    alias = tmp_path.resolve() / "source-alias"
    alias.symlink_to(source, target_is_directory=True)
    assert not bound_core.allowed(["git", "-C", str(alias), "rev-parse", "HEAD"])


@pytest.mark.parametrize(
    "variant",
    ["foreign", "extra", "config", "status", "fetch", "origin", "alternate-executable"],
)
@pytest.mark.parametrize("entrypoint", ["run", "popen", "check_output"])
def test_core_query_refuses_foreign_or_broader_git(
    bound_core, tmp_path, monkeypatch, variant, entrypoint
):
    import subprocess

    import closer_inert_dispatcher_support as support

    query = list(bound_core.core_head_query)
    if variant == "foreign":
        query[2] = str(tmp_path.resolve())
    elif variant == "extra":
        query.append("--verify")
    elif variant == "config":
        query[1:1] = ["-c", "core.fsmonitor=arbitrary"]
    elif variant in {"status", "fetch"}:
        query[3:] = [variant]
    elif variant == "origin":
        query[3:] = ["remote", "get-url", "origin"]
    else:
        query[0] = "/usr/bin/git"

    def forbidden(*_args, **_kwargs):
        pytest.fail("rejected Git request reached a real subprocess")

    monkeypatch.setattr(support, "REAL_RUN", forbidden)
    monkeypatch.setattr(support, "REAL_POPEN", forbidden)
    monkeypatch.setattr(subprocess, "run", bound_core.run)
    monkeypatch.setattr(subprocess, "Popen", bound_core.popen)
    invoke = (
        subprocess.check_output if entrypoint == "check_output" else getattr(bound_core, entrypoint)
    )
    with pytest.raises(AssertionError, match="forbidden subprocess"):
        invoke(query, shell=False)


@pytest.mark.parametrize(
    "options",
    [
        {"shell": True},
        {"shell": 1},
        {"shell": None},
        {"cwd": "/foreign"},
        {"env": {"GIT_DIR": "/foreign"}},
        {"executable": "/bin/sh"},
    ],
)
@pytest.mark.parametrize("entrypoint", ["run", "popen", "check_output"])
def test_core_query_refuses_shell_or_effective_repository_overrides(
    bound_core, monkeypatch, options, entrypoint
):
    import subprocess

    import closer_inert_dispatcher_support as support

    def forbidden(*_args, **_kwargs):
        pytest.fail("rejected override reached a real subprocess")

    monkeypatch.setattr(support, "REAL_RUN", forbidden)
    monkeypatch.setattr(support, "REAL_POPEN", forbidden)
    monkeypatch.setattr(subprocess, "run", bound_core.run)
    monkeypatch.setattr(subprocess, "Popen", bound_core.popen)
    invoke = (
        subprocess.check_output if entrypoint == "check_output" else getattr(bound_core, entrypoint)
    )
    with pytest.raises(AssertionError, match="forbidden subprocess"):
        invoke(list(bound_core.core_head_query), **options)


def test_export_descriptor_does_not_enable_git_query(bound_core, monkeypatch):
    from closer_inert_dispatcher_support import fixture_core_head_binding

    source = bound_core.world.root
    (source / "closer-core-package.json").write_text("{}")
    monkeypatch.setenv("CLOSER_CORE_PACKAGE", str(source))
    assert fixture_core_head_binding() == (None, None)


def test_real_pinned_core_head_query_crosses_all_subprocess_guards(tmp_path, monkeypatch):
    import subprocess

    import closer_inert_dispatcher_support as support
    from closer_core_support import core_source

    source = core_source()
    if not (source / "scripts/closer_runtime.py").is_file() or not (source / ".git").exists():
        pytest.skip("existing configured Git Core source unavailable; export requires no query")
    source = source.resolve(strict=True)
    query = ["git", "-C", str(source), "rev-parse", "HEAD"]
    observed = support.REAL_RUN(query, capture_output=True, text=True, check=True).stdout.strip()
    caller = core_guard_caller(source, monkeypatch)
    monkeypatch.setenv("CLOSER_CORE_SOURCE_REVISION", observed)
    caller.core_head_query, caller.core_head_revision = support.fixture_core_head_binding()
    monkeypatch.setattr(subprocess, "run", caller.run)
    monkeypatch.setattr(subprocess, "Popen", caller.popen)
    assert (
        caller.run(query, capture_output=True, text=True, shell=False, check=True).stdout.strip()
        == observed
    )
    assert subprocess.check_output(query, text=True, shell=False).strip() == observed
    with caller.popen(query, stdout=subprocess.PIPE, text=True, shell=False) as child:
        output, _ = child.communicate(timeout=5)
    assert child.returncode == 0 and output.strip() == observed
    # The existing fixture still hashes actual Core files and invokes CoreBundle
    # validation. The real HEAD result, not a fabricated descriptor, is consumed.
    import json

    from test_closer_native_production_composition import _core

    pin = _core(tmp_path / "git-core-copy")
    descriptor = json.loads((Path(pin["package_root"]) / "closer-core-package.json").read_text())
    assert descriptor["source_commit"] == observed
    with monkeypatch.context() as exported:
        exported.setenv("CLOSER_CORE_PACKAGE", pin["package_root"])

        def forbidden(*_args, **_kwargs):
            pytest.fail("exported Core descriptor must not query Git")

        exported.setattr(support, "REAL_RUN", forbidden)
        export_pin = _core(tmp_path / "export-core-copy")
        assert export_pin["package_digest"] == pin["package_digest"]
    # A mismatched independent pin is never replaced with a synthetic answer.
    caller.core_head_revision = "f" * 40 if observed != "f" * 40 else "e" * 40
    with pytest.raises(AssertionError, match="Core HEAD differs from pin"):
        caller.run(query, capture_output=True, text=True, shell=False, check=True)


def test_campaign_guard_refuses_account_ancestor_and_home_alias(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    account = root / "account-home"
    account.mkdir()
    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(account)))
    monkeypatch.setenv("HOME", str(root / "private-home"))
    with pytest.raises(AssertionError):
        guard_caller(root).temporary_campaign_guard()
    private = root / "project/.tmp/pytest-case"
    (private / "private-home").mkdir(parents=True)
    alias = root / "alias-home"
    alias.symlink_to(private / "private-home", target_is_directory=True)
    monkeypatch.setenv("HOME", str(alias))
    with pytest.raises(AssertionError):
        guard_caller(private).temporary_campaign_guard()
    assert not (private / "private-home/.codex").exists()


@pytest.mark.parametrize(
    "layout", ["project/.tmp/pytest-case", "work/_temp/storage-verify/tmp/pytest-case"]
)
def test_actual_inert_callback_under_ci_account_home(tmp_path, monkeypatch, layout):
    import time

    from test_closer_batch_interpretation import native_callbacks_for, native_delivery
    from test_closer_disposition import TARGET

    account = tmp_path.resolve() / "account-home"
    root = account / layout
    root.mkdir(parents=True)
    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(account)))
    delivery = native_delivery(root, monkeypatch)
    callbacks, context = native_callbacks_for(delivery, monkeypatch)
    context["deadline"] = time.time() + 60
    low = callbacks["interpret"](TARGET, context)
    assert low["integration_status"] == "accepted"
    assert low["actual"]["run_id"] == delivery.caller.results[0]["run_id"]
    assert len(delivery.caller.results) == 1
    guard = root / "private-home/.codex/automations/model-allocation/assess.py"
    assert guard.is_file() and guard.resolve().is_relative_to(root)
    assert not (account / ".codex").exists()
    assert not delivery.guard_calls and not delivery.closure_calls
