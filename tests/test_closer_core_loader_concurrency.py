"""Owned tiny Core packages exercise loader publication without effects or providers."""

import builtins
import json
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import closer_runtime_binding as rb


def make_package(root, sources=None):
    sources = sources or {}
    for name in rb.CORE_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sources.get(name, ""))
    files = {name: rb._hash(root / name) for name in rb.CORE_FILES}
    digest = rb._digest(
        [
            "closer-core",
            "Workflows",
            "1",
            "closer-operation/v1",
            "a" * 40,
            sorted(files.items()),
            sorted(rb.CORE_DEPENDENCIES.items()),
        ]
    )
    descriptor = {
        "schema_version": 1,
        "package_kind": "closer-core",
        "owner": "Workflows",
        "contract": "closer-operation/v1",
        "core_version": "1",
        "source_commit": "a" * 40,
        "entrypoints": {"runtime": "scripts/closer_runtime.py"},
        "files": files,
        "dependencies": rb.CORE_DEPENDENCIES,
        "package_digest": digest,
    }
    (root / "closer-core-package.json").write_text(json.dumps(descriptor))
    return {"package_root": str(root), "package_digest": digest}


def test_concurrent_first_load_waits_for_complete_module(tmp_path, monkeypatch):
    entered, release, second_started, second_done = (threading.Event() for _ in range(4))

    def gate():
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(builtins, "_core_test_gate", gate, raising=False)
    bundle = rb.CoreBundle(
        make_package(
            tmp_path / "core",
            {
                "scripts/check_checks_reported.py": "_core_test_gate()\nEvidence = 'ready'\n",
            },
        )
    )

    def second():
        second_started.set()
        module = bundle.load("check_checks_reported")
        second_done.set()
        return module.Evidence

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(bundle.load, "check_checks_reported")
        try:
            assert entered.wait(5)
            assert "check_checks_reported" not in bundle.loaded
            later = pool.submit(second)
            assert second_started.wait(5)
            assert not second_done.wait(0.05)
        finally:
            release.set()
        assert first.result(5).Evidence == "ready"
        assert later.result(5) == "ready"
        assert bundle.load("check_checks_reported") is first.result()


def test_failed_load_is_not_cached_and_retries_verified_bytes(tmp_path, monkeypatch):
    attempts = []

    def fail_once():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("first initialization failed")

    monkeypatch.setattr(builtins, "_core_fail_once", fail_once, raising=False)
    bundle = rb.CoreBundle(
        make_package(
            tmp_path / "core",
            {
                "scripts/api_client.py": "partial = True\n_core_fail_once()\ncomplete = True\n",
            },
        )
    )
    alias = bundle.namespace + ".api_client"
    before = sys.modules.get(alias)
    with pytest.raises(RuntimeError, match="first initialization"):
        bundle.load("api_client")
    assert "api_client" not in bundle.loaded
    assert not bundle._loading
    assert not hasattr(bundle.package, "api_client")
    assert sys.modules.get(alias) is before
    assert bundle.load("api_client").complete is True
    assert len(attempts) == 2


def test_relocated_bundle_keeps_own_root_and_failed_alias_preserves_other(tmp_path, monkeypatch):
    fail = False

    def gate():
        if fail:
            raise RuntimeError("relocated failure")

    monkeypatch.setattr(builtins, "_core_root_gate", gate, raising=False)
    pin = make_package(
        tmp_path / "first",
        {
            "scripts/api_client.py": "from pathlib import Path\n_core_root_gate()\nroot = Path(__file__).parents[1]\n",
        },
    )
    first = rb.CoreBundle(pin)
    original = first.load("api_client")
    second_root = tmp_path / "second"
    shutil.copytree(tmp_path / "first", second_root)
    second = rb.CoreBundle({**pin, "package_root": str(second_root)})
    fail = True
    with pytest.raises(RuntimeError, match="relocated failure"):
        second.load("api_client")
    assert sys.modules[original.__name__] is original
    assert sys.modules[second.namespace] is second.package
    fail = False
    shutil.rmtree(tmp_path / "first")
    relocated = second.load("api_client")
    assert relocated is not original
    assert relocated.root == second_root
    assert Path(relocated.__file__).is_relative_to(second_root)


def test_recursive_owned_import_and_dataclass_initialization(tmp_path):
    bundle = rb.CoreBundle(
        make_package(
            tmp_path / "core",
            {
                "scripts/closer_runtime.py": "from dataclasses import dataclass\n@dataclass\nclass Evidence:\n    value: int\n",
                "scripts/closer_operation_github.py": "from scripts.closer_runtime import Evidence\n",
                "scripts/api_client.py": "from scripts import reusable_ci_scope\nready = reusable_ci_scope.ready\n",
                "scripts/reusable_ci_scope.py": "ready = True\n",
            },
        )
    )
    assert bundle.github.Evidence is bundle.runtime.Evidence
    assert bundle.load("api_client").ready


def test_failed_constructor_restores_prior_bundle_aliases(tmp_path, monkeypatch):
    should_fail = False

    def gate():
        if should_fail:
            raise RuntimeError("constructor failure")

    monkeypatch.setattr(builtins, "_core_constructor_gate", gate, raising=False)
    pin = make_package(
        tmp_path / "core",
        {
            "scripts/closer_operation_github.py": "_core_constructor_gate()\n",
        },
    )
    original = rb.CoreBundle(pin)
    before = {
        key: value
        for key, value in sys.modules.items()
        if key == original.namespace or key.startswith(original.namespace + ".")
    }
    should_fail = True
    with pytest.raises(RuntimeError, match="constructor failure"):
        rb.CoreBundle(pin)
    assert {
        key: value
        for key, value in sys.modules.items()
        if key == original.namespace or key.startswith(original.namespace + ".")
    } == before


def test_waiting_thread_retries_after_failed_first_load(tmp_path, monkeypatch):
    entered, release, waiting = (threading.Event() for _ in range(3))
    attempts = []

    def gate():
        attempts.append(1)
        if len(attempts) == 1:
            entered.set()
            assert release.wait(5)
            raise RuntimeError("failed first thread")

    monkeypatch.setattr(builtins, "_core_wait_retry", gate, raising=False)
    bundle = rb.CoreBundle(
        make_package(
            tmp_path / "core",
            {
                "scripts/api_client.py": "_core_wait_retry()\ncomplete = True\n",
            },
        )
    )

    def retry():
        waiting.set()
        return bundle.load("api_client")

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(bundle.load, "api_client")
        try:
            assert entered.wait(5)
            second = pool.submit(retry)
            assert waiting.wait(5)
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="failed first thread"):
            first.result(5)
        assert second.result(5).complete is True
    assert len(attempts) == 2


def test_concurrent_bundle_initialization_is_serial_and_root_bound(tmp_path, monkeypatch):
    entered, release, second_started, second_entered = (threading.Event() for _ in range(4))
    calls = []

    def gate():
        calls.append(threading.get_ident())
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        else:
            second_entered.set()

    monkeypatch.setattr(builtins, "_core_bundle_gate", gate, raising=False)
    pin = make_package(
        tmp_path / "first",
        {
            "scripts/closer_runtime.py": "_core_bundle_gate()\npath = __file__\n",
            "scripts/closer_operation_github.py": "from scripts.closer_runtime import path\n",
        },
    )
    second_root = tmp_path / "second"
    shutil.copytree(tmp_path / "first", second_root)

    def second():
        second_started.set()
        return rb.CoreBundle({**pin, "package_root": str(second_root)})

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(rb.CoreBundle, pin)
        try:
            assert entered.wait(5)
            later = pool.submit(second)
            assert second_started.wait(5)
            assert not second_entered.wait(0.05)
        finally:
            release.set()
        a, b = first.result(5), later.result(5)
    assert a.runtime is not b.runtime
    assert a.github.path == str(tmp_path / "first/scripts/closer_runtime.py")
    assert b.github.path == str(second_root / "scripts/closer_runtime.py")
