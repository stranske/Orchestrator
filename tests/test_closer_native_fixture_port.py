"""Registration guards only: mocked Popen, no native child/provider execution."""

import copy
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import closer_inert_dispatcher_support as support
import pytest
from closer_inert_dispatcher_support import InertCaller
from test_closer_native_observer import host as host

import closer_native_observer as native


@pytest.fixture
def registration(host):
    manifest, manifest_path, wrapper = host
    caller = object.__new__(InertCaller)
    caller.world = SimpleNamespace(
        root=manifest_path.parent, cli=manifest_path.parent / "unused-emitter"
    )
    caller.children = {os.getpid()}
    caller.core_head_query = caller.core_head_revision = None
    return caller, manifest, manifest_path, wrapper


def options(caller):
    registered = caller.native_fixture
    path = registered["observation_root"] / ("a" * 32) / "request.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "native-wrapper-qualification-request/v1",
                "nonce": path.parent.name,
                "binding": registered["binding"],
                "action_deadline": time.monotonic() + 30,
                "hard_deadline": time.monotonic() + 35,
            }
        )
    )
    return {
        "env": {"HOME": str(registered["home"]), "CLOSER_NATIVE_QUALIFICATION_REQUEST": str(path)},
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }


def test_only_exact_registered_fixture_forwards_unchanged_to_popen_and_tracks_child(
    registration, monkeypatch
):
    caller, manifest, path, wrapper = registration
    argv = ["/bin/bash", str(wrapper), "closer"]
    assert not caller.allowed(argv)
    caller.register_native_fixture(manifest, path)
    kwargs = options(caller)
    calls = []
    child = SimpleNamespace(pid=424242)

    def popen(command, **received):
        calls.append((command, received))
        return child

    monkeypatch.setattr(support, "REAL_POPEN", popen)
    assert caller.popen(argv, **kwargs) is child
    assert calls == [(argv, kwargs)] and calls[0][0] is argv
    assert child.pid in caller.children
    assert caller.allowed(["ps", "-p", str(child.pid), "-o", "lstart="])
    assert not caller.allowed(["ps", "-p", "424243", "-o", "lstart="])
    assert native.binding(manifest) == caller.native_fixture["binding"]


@pytest.mark.parametrize(
    "fault",
    [
        "lane",
        "extra",
        "shell_command",
        "other_bash",
        "bare_bash",
        "foreign_wrapper",
        "shell",
        "executable",
        "cwd",
        "foreign_home",
        "no_request",
        "foreign_request",
        "wrong_binding",
        "symlink_request",
        "BASH_ENV",
        "ENV",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
    ],
)
def test_other_commands_or_unbound_process_options_never_reach_popen(
    registration, monkeypatch, tmp_path, fault
):
    caller, manifest, path, wrapper = registration
    caller.register_native_fixture(manifest, path)
    argv = ["/bin/bash", str(wrapper), "closer"]
    kwargs = options(caller)
    if fault == "lane":
        argv[-1] = "opener"
    elif fault == "extra":
        argv.append("--extra")
    elif fault == "shell_command":
        argv = ["/bin/bash", "-c", "true"]
    elif fault == "other_bash":
        argv[0] = "bash"
    elif fault == "bare_bash":
        argv = ["/bin/bash"]
    elif fault == "foreign_wrapper":
        argv[1] = str(tmp_path / "foreign.sh")
    elif fault == "shell":
        kwargs["shell"] = True
    elif fault == "executable":
        kwargs["executable"] = "/bin/sh"
    elif fault == "cwd":
        kwargs["cwd"] = str(tmp_path)
    elif fault == "foreign_home":
        kwargs["env"]["HOME"] = str(tmp_path / "foreign-home")
    elif fault == "no_request":
        del kwargs["env"]["CLOSER_NATIVE_QUALIFICATION_REQUEST"]
    elif fault == "foreign_request":
        kwargs["env"]["CLOSER_NATIVE_QUALIFICATION_REQUEST"] = str(tmp_path / "foreign.json")
    elif fault == "wrong_binding":
        request_path = Path(kwargs["env"]["CLOSER_NATIVE_QUALIFICATION_REQUEST"])
        value = json.loads(request_path.read_text())
        value["binding"]["package_digest"] = "f" * 64
        request_path.write_text(json.dumps(value))
    elif fault == "symlink_request":
        request_path = Path(kwargs["env"]["CLOSER_NATIVE_QUALIFICATION_REQUEST"])
        foreign = tmp_path / "foreign-request.json"
        foreign.write_bytes(request_path.read_bytes())
        request_path.unlink()
        request_path.symlink_to(foreign)
    else:
        kwargs["env"][fault] = "/foreign/startup-hook"
    monkeypatch.setattr(support, "REAL_POPEN", lambda *_a, **_kw: pytest.fail("forbidden child"))
    with pytest.raises(AssertionError, match="forbidden subprocess"):
        caller.popen(argv, **kwargs)


@pytest.mark.parametrize(
    "fault", ["script_bytes", "script_symlink", "home_symlink", "manifest_bytes", "hardlink"]
)
def test_launch_revalidates_physical_script_and_manifest_after_registration(
    registration, monkeypatch, tmp_path, fault
):
    caller, manifest, path, wrapper = registration
    caller.register_native_fixture(manifest, path)
    kwargs = options(caller)
    if fault == "script_bytes":
        wrapper.write_text(wrapper.read_text() + "# changed\n")
    elif fault == "script_symlink":
        foreign = tmp_path / "copied.sh"
        foreign.write_bytes(wrapper.read_bytes())
        wrapper.unlink()
        wrapper.symlink_to(foreign)
    elif fault == "home_symlink":
        home = caller.native_fixture["home"]
        renamed = home.with_name("renamed-home")
        home.rename(renamed)
        home.symlink_to(renamed, target_is_directory=True)
    elif fault == "manifest_bytes":
        path.write_text(path.read_text() + "\n")
    elif fault == "hardlink":
        os.link(wrapper, tmp_path / "linked.sh")
    monkeypatch.setattr(support, "REAL_POPEN", lambda *_a, **_kw: pytest.fail("changed child"))
    with pytest.raises(AssertionError, match="forbidden subprocess"):
        caller.popen(["/bin/bash", str(wrapper), "closer"], **kwargs)


@pytest.mark.parametrize(
    "fault", ["hash", "arbitrary_script", "foreign_manifest", "wrong_home", "protected_root"]
)
def test_registration_does_not_admit_arbitrary_even_self_hashed_wrappers(
    registration, monkeypatch, tmp_path, fault
):
    caller, manifest, path, wrapper = registration
    manifest = copy.deepcopy(manifest)
    if fault == "hash":
        manifest["mechanism_contract"]["wrapper"]["sha256"] = "f" * 64
    elif fault == "arbitrary_script":
        wrapper.write_text("#!/bin/bash\nexit 0\n")
        manifest["mechanism_contract"]["wrapper"]["sha256"] = hashlib.sha256(
            wrapper.read_bytes()
        ).hexdigest()
        path.write_text(json.dumps(manifest))
    elif fault == "foreign_manifest":
        path = tmp_path / "foreign-manifest.json"
    elif fault == "wrong_home":
        monkeypatch.setenv("HOME", str(tmp_path / "wrong-home"))
    elif fault == "protected_root":
        import pwd

        monkeypatch.setattr(
            pwd, "getpwuid", lambda *_: SimpleNamespace(pw_dir=str(caller.world.root))
        )
    with pytest.raises(AssertionError):
        caller.register_native_fixture(manifest, path)
    assert getattr(caller, "native_fixture", None) is None


def observation_case(registration, monkeypatch):
    """Synthetic boundary records, never evidence of actual process ownership."""
    caller, manifest, path, wrapper = registration
    caller.register_native_fixture(manifest, path)
    launch_options = options(caller)
    monkeypatch.setattr(support, "REAL_POPEN", lambda *_a, **_kw: SimpleNamespace(pid=4100))
    caller.popen(["/bin/bash", str(wrapper), "closer"], **launch_options)
    request = Path(launch_options["env"]["CLOSER_NATIVE_QUALIFICATION_REQUEST"])
    birth = "Mon Oct 26 12:00:00 2026"
    startup = {
        "schema": "native-wrapper-lifecycle/v1",
        "nonce": request.parent.name,
        "binding": caller.native_fixture["binding"],
        "actor": "cursor",
        "source": "owned-native-wrapper-process",
        "desktop_qualified": False,
        "scheduler_origin": "unobserved",
        "wrapper": {
            "pid": 4100,
            "started": birth,
            "argv_sha256": caller.native_fixture["wrapper_argv_sha256"],
            "source": "os-byte-argv",
        },
        "entry": {
            "pid": 4101,
            "started": birth,
            "argv_sha256": caller.native_fixture["entry_argv_sha256"],
            "source": "os-byte-argv",
        },
    }
    startup_path = request.parent / "startup.json"
    startup_path.write_text(json.dumps(startup))
    return caller, startup_path, startup, birth


def metadata(pid):
    return ["ps", "-p", str(pid), "-o", "ppid=", "-o", "lstart="]


def argv_reader(caller, pid, birth, seconds="1.25"):
    registered = caller.native_fixture
    return [
        registered["interpreter"],
        "-I",
        str(registered["observer"]),
        "--owned-argv",
        str(pid),
        birth,
        seconds,
    ]


def test_bound_metadata_candidates_and_exact_reader_forward_without_claiming_ownership(
    registration, monkeypatch
):
    caller, _path, _startup, birth = observation_case(registration, monkeypatch)
    assert 4100 in caller.children and 4101 not in caller.children
    assert not caller.allowed(["ps", "-p", "4101", "-o", "lstart="])
    commands = [
        metadata(4100),
        metadata(4101),
        argv_reader(caller, 4100, birth),
        argv_reader(caller, 4101, birth),
    ]
    forwarded = []

    def popen(command, **kwargs):
        forwarded.append((command, kwargs))
        return SimpleNamespace(pid=4200)

    monkeypatch.setattr(support, "REAL_POPEN", popen)
    opts = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    for command in commands:
        assert caller.popen(command, **opts).pid == 4200
        assert forwarded[-1] == (command, opts) and forwarded[-1][0] is command
    assert 4200 not in caller.children  # Never promotes a helper/entry to model ownership.
    assert not caller.allowed(metadata(4102))


def test_entry_candidate_needs_every_fresh_private_startup_join(registration, monkeypatch):
    caller, path, startup, birth = observation_case(registration, monkeypatch)
    monkeypatch.setattr(
        support, "REAL_POPEN", lambda *_a, **_kw: pytest.fail("unjoined observation")
    )
    for field in (
        "nonce",
        "binding",
        "wrapper_pid",
        "wrapper_argv",
        "entry_argv",
        "source",
        "birth_control",
        "missing",
    ):
        changed = copy.deepcopy(startup)
        if field == "nonce":
            changed["nonce"] = "b" * 32
        elif field == "binding":
            changed["binding"]["package_digest"] = "f" * 64
        elif field == "wrapper_pid":
            changed["wrapper"]["pid"] = 4099
        elif field == "wrapper_argv":
            changed["wrapper"]["argv_sha256"] = "f" * 64
        elif field == "entry_argv":
            changed["entry"]["argv_sha256"] = "f" * 64
        elif field == "source":
            changed["source"] = "caller"
        elif field == "birth_control":
            changed["entry"]["started"] = birth + "\n"
        if field == "missing":
            path.unlink()
        else:
            path.write_text(json.dumps(changed))
        with pytest.raises(AssertionError, match="forbidden subprocess"):
            caller.popen(metadata(4101), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert caller.allowed(
            metadata(4100)
        )  # The actual direct child remains a metadata candidate.
    path.write_text(json.dumps(startup))
    original = path.read_bytes()
    path.unlink()
    foreign = path.parent.parent / "foreign-startup.json"
    foreign.write_bytes(original)
    path.symlink_to(foreign)
    assert not caller.allowed(metadata(4101))


def test_reader_refuses_foreign_flags_birth_budget_and_process_overrides(registration, monkeypatch):
    caller, _path, _startup, birth = observation_case(registration, monkeypatch)
    base = argv_reader(caller, 4101, birth)
    forbidden = []
    for index, value in (
        (0, "/foreign/python"),
        (1, "-c"),
        (2, "/foreign/script.py"),
        (3, "--ancestor-argv"),
        (4, "999999"),
        (5, birth + "\n"),
        (5, "wrong birth"),
        (6, "nan"),
        (6, "inf"),
        (6, "-1"),
        (6, "0"),
        (6, "5.1"),
    ):
        command = base.copy()
        command[index] = value
        forbidden.append(command)
    forbidden.extend(
        [base + ["extra"], ["/bin/bash", "-c", "true"], ["ps", "-p", "4101", "-o", "args="]]
    )
    monkeypatch.setattr(support, "REAL_POPEN", lambda *_a, **_kw: pytest.fail("unbound reader"))
    opts = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    for command in forbidden:
        with pytest.raises(AssertionError, match="forbidden subprocess"):
            caller.popen(command, **opts)
    for extra in (
        {"env": {}},
        {"cwd": "/tmp"},
        {"executable": "/bin/sh"},
        {"shell": True},
        {"timeout": float("nan")},
        {"timeout": 6},
    ):
        with pytest.raises(AssertionError, match="forbidden subprocess"):
            caller.popen(base, **opts, **extra)


def test_expired_request_and_changed_pinned_reader_refuse_without_process(
    registration, monkeypatch
):
    caller, _path, _startup, birth = observation_case(registration, monkeypatch)
    command = argv_reader(caller, 4101, birth)
    original_read = caller._native_fixture_file

    def changed_source(root, path, limit):
        raw, identity = original_read(root, path, limit)
        return (
            raw + b"# changed\n" if path == caller.native_fixture["observer"] else raw
        ), identity

    monkeypatch.setattr(caller, "_native_fixture_file", changed_source)
    assert not caller.allowed(command)
    monkeypatch.setattr(caller, "_native_fixture_file", original_read)
    deadline = caller.native_runs[4100]["action_deadline"]
    monkeypatch.setattr(time, "monotonic", lambda: deadline + 1)
    assert (
        not caller.allowed(command)
        and not caller.allowed(metadata(4100))
        and not caller.allowed(metadata(4101))
    )
