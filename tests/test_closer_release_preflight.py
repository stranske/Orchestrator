from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

import closer_release_preflight as preflight


def git_repo(path: Path, files: dict[str, str]) -> tuple[Path, str]:
    path.mkdir()
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "preflight fixture",
        ],
        check=True,
    )
    sha = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    return path, sha


def fixture_config(tmp_path: Path, *, core: Path | None = None, python: Path | None = None):
    source, source_sha = git_repo(
        tmp_path / "source",
        {
            "scripts/verify_before_sync.sh": "#!/usr/bin/env bash\n",
            "scripts/install_verified_snapshot.py": "print('installer')\n",
            ".verify-floor.json": json.dumps({"collected": 1, "passed": 1}),
            "tests/test_fixture.py": "def test_fixture():\n    assert True\n",
        },
    )
    if core is None:
        core, core_sha = git_repo(
            tmp_path / "core", {"scripts/closer_runtime.py": "# required runtime\n"}
        )
    else:
        core_sha = "a" * 40
    return preflight.ReleasePreflight(
        source=source,
        expected_source_sha=source_sha,
        mirror=tmp_path / "mirror",
        core=core,
        expected_core_sha=core_sha,
        python=python or Path(sys.executable),
        journal=tmp_path / "release.json",
        owner="closer",
        workers=1,
    )


def test_missing_core_fails_before_a_journal_or_install_can_exist(tmp_path):
    config = fixture_config(tmp_path, core=tmp_path / "missing-core")
    with pytest.raises(preflight.PreflightError) as raised:
        preflight.install_after_preflight(config)
    assert raised.value.code == "core_missing"
    assert not config.journal.exists()
    assert not config.mirror.exists()


def test_missing_selected_python_fails_fast_without_release_state(tmp_path):
    config = fixture_config(tmp_path, python=tmp_path / "missing-python")
    with pytest.raises(preflight.PreflightError) as raised:
        preflight.install_after_preflight(config)
    assert raised.value.code == "python_missing"
    assert not config.journal.exists()


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("a" * 41, "source_sha_invalid"),
        (True, "workers_invalid"),
        (float("inf"), "timeout_invalid"),
    ],
)
def test_preflight_rejects_ambiguous_sha_and_nonfinite_or_boolean_controls(tmp_path, value, code):
    config = fixture_config(tmp_path)
    if code == "source_sha_invalid":
        config = preflight.ReleasePreflight(**{**config.__dict__, "expected_source_sha": value})
    elif code == "workers_invalid":
        config = preflight.ReleasePreflight(**{**config.__dict__, "workers": value})
    else:
        config = preflight.ReleasePreflight(**{**config.__dict__, "timeout": value})
    with pytest.raises(preflight.PreflightError) as raised:
        config.normalized()
    assert raised.value.code == code


def test_preflight_pins_python3_for_fake_home_and_sets_release_bindings(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("CLOSER_CORE_PACKAGE", "/stale/core")
    config = fixture_config(tmp_path)
    checked, environment, receipt = preflight.checked_environment(config)
    assert checked.python == Path(sys.executable).absolute()
    assert receipt["child_python"]["executable"] == str(Path(sys.executable).resolve())
    assert receipt["child_python"]["fake_home"] is True
    assert environment["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).absolute().parent)
    assert environment["PYTHON"] == str(Path(sys.executable).absolute())
    assert environment["CLOSER_CORE_SOURCE"] == str(checked.core)
    assert environment["CLOSER_CORE_SOURCE_REVISION"] == checked.expected_core_sha
    assert environment["VERIFY_PYTEST_WORKERS"] == "1"
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" not in environment
    assert "CLOSER_CORE_PACKAGE" not in environment
    assert receipt["collection_floor"] == {"collected": 1, "floor": 1}
    assert "--numprocesses" in subprocess.check_output(
        [str(checked.python), "-m", "pytest", "--help", "--color=no"],
        env=environment,
        text=True,
        timeout=20,
    )


def test_collection_floor_preflight_accepts_an_exact_collection(tmp_path, monkeypatch):
    config = fixture_config(tmp_path)
    calls = []

    def collect(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "1 test collected in 0.01s\n", "")

    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        collect,
    )
    assert preflight._collection_floor_preflight(config, {}) == {"collected": 1, "floor": 1}
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == [
        str(config.python),
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    assert kwargs["cwd"] == config.source
    assert Path(kwargs["env"]["ORCH_STATE_DIR"]).name == "state"
    assert Path(kwargs["env"]["ORCH_LOCAL_RUNTIME"]).name == "runtime"
    assert (
        Path(kwargs["env"]["ORCH_STATE_DIR"]).parent
        == Path(kwargs["env"]["ORCH_LOCAL_RUNTIME"]).parent
    )


@pytest.mark.parametrize("value", [None, [], True, 1.5])
def test_collection_floor_preflight_rejects_non_object_or_noninteger_floor_counters(
    tmp_path, monkeypatch, value
):
    config = fixture_config(tmp_path)
    floor = config.source / ".verify-floor.json"
    floor.write_text(
        json.dumps(value if value in (None, []) else {"collected": value, "passed": 1})
    )
    monkeypatch.setattr(
        preflight.subprocess, "run", lambda *a, **kw: pytest.fail("must reject before collection")
    )
    with pytest.raises(preflight.PreflightError) as raised:
        preflight._collection_floor_preflight(config, {})
    assert raised.value.code == "floor_invalid"


def test_collection_floor_preflight_refuses_a_persisted_floor_mismatch(tmp_path, monkeypatch):
    config = fixture_config(tmp_path)
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "2 tests collected in 0.01s\n", ""),
    )
    with pytest.raises(preflight.PreflightError) as raised:
        preflight._collection_floor_preflight(config, {})
    assert raised.value.code == "collection_floor_mismatch"
    assert "normal main CI reconcile-floor job" in str(raised.value)


def test_collection_floor_preflight_refuses_a_collection_error(tmp_path, monkeypatch):
    config = fixture_config(tmp_path)
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 2, "", "ImportError: missing dependency\n"),
    )
    with pytest.raises(preflight.PreflightError) as raised:
        preflight._collection_floor_preflight(config, {})
    assert raised.value.code == "collection_probe_failed"
    assert "ImportError: missing dependency" in str(raised.value)


@pytest.mark.parametrize(
    "help_text",
    [
        "  -n numprocesses, --numprocesses=numprocesses\n",
        "  -n NUMPROCESSES, --numprocesses=NUMPROCESSES\n",
        "  \x1b[32m-n\x1b[0m NUMPROCESSES, --numprocesses=NUMPROCESSES\n",
    ],
)
def test_xdist_probe_accepts_both_help_metavar_presentations(monkeypatch, help_text):
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, help_text, ""),
    )
    preflight._check_xdist(Path(sys.executable), {})


def test_xdist_probe_still_refuses_missing_worker_option(monkeypatch):
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "  -q quiet\n", ""),
    )
    with pytest.raises(preflight.PreflightError, match="does not advertise"):
        preflight._check_xdist(Path(sys.executable), {})


def test_virtualenv_invocation_path_is_preserved_and_child_keeps_venv_prefix(tmp_path):
    virtualenv = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(virtualenv)
    selected = virtualenv / "bin" / "python"
    resolved = preflight._resolve_executable(selected)
    assert resolved == selected.absolute()
    receipt = preflight._inspect_python(resolved)
    assert receipt["invocation"] == str(selected.absolute())
    assert receipt["prefix"] == str(virtualenv)
    assert receipt["base_prefix"] != receipt["prefix"]


def test_existing_release_wrapper_is_not_called_until_preflight_is_green(tmp_path, monkeypatch):
    config = fixture_config(tmp_path, core=tmp_path / "missing-core")
    called = False

    class UnexpectedRecord:
        def __init__(self, _path):
            nonlocal called
            called = True

    monkeypatch.setattr(preflight, "ReleaseRecord", UnexpectedRecord)
    with pytest.raises(preflight.PreflightError):
        preflight.install_after_preflight(config)
    assert called is False


def test_existing_release_wrapper_receives_only_the_green_preflight_environment(
    tmp_path, monkeypatch
):
    config = fixture_config(tmp_path)
    calls = []

    class GreenGateRecord:
        def __init__(self, path):
            self.path = path

        def verify_and_install(self, **kwargs):
            calls.append((kwargs, dict(os.environ)))
            return {"stage": "installed"}

    monkeypatch.setattr(preflight, "ReleaseRecord", GreenGateRecord)
    receipt, result = preflight.install_after_preflight(config)
    kwargs, environment = calls[0]
    assert receipt["ok"] is True
    assert result == {"stage": "installed"}
    assert kwargs["candidate_sha"] == config.expected_source_sha
    assert kwargs["source"] == config.source.resolve()
    assert environment["CLOSER_CORE_SOURCE"] == str(config.core.resolve())
    assert environment["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).absolute().parent)


@pytest.mark.parametrize("failure", [False, True])
def test_install_after_preflight_restores_outer_environment_on_success_and_failure(
    tmp_path, monkeypatch, failure
):
    config = fixture_config(tmp_path)
    outer = dict(os.environ)
    inner = {"PATH": "/inner", "PYTHON": "/inner/python", "ONLY_INNER": "yes"}
    monkeypatch.setattr(
        preflight,
        "checked_environment",
        lambda _config: (config, inner, {"ok": True, "phase": "preflight"}),
    )

    class Record:
        def __init__(self, _path):
            pass

        def verify_and_install(self, **_kwargs):
            assert os.environ["ONLY_INNER"] == "yes"
            if failure:
                raise RuntimeError("gate failed")
            return {"stage": "installed"}

    monkeypatch.setattr(preflight, "ReleaseRecord", Record)
    if failure:
        with pytest.raises(RuntimeError, match="gate failed"):
            preflight.install_after_preflight(config)
    else:
        assert preflight.install_after_preflight(config)[1] == {"stage": "installed"}
    assert dict(os.environ) == outer


def test_cli_reports_a_machine_readable_non_authorizing_preflight_error(tmp_path):
    source, source_sha = git_repo(tmp_path / "source", {"README.md": "fixture\n"})
    result = subprocess.run(
        [
            sys.executable,
            "scripts/closer_verified_release.py",
            "preflight",
            "--source",
            str(source),
            "--expected-source-sha",
            source_sha,
            "--mirror",
            str(tmp_path / "mirror"),
            "--core",
            str(tmp_path / "missing-core"),
            "--expected-core-sha",
            "a" * 40,
            "--python",
            sys.executable,
            "--journal",
            str(tmp_path / "release.json"),
            "--owner",
            "closer",
        ],
        cwd=Path(__file__).parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "core_missing"
    assert not (tmp_path / "release.json").exists()


def test_cli_install_returns_nonzero_when_existing_release_does_not_install(
    tmp_path, monkeypatch, capsys
):
    script = Path(__file__).parents[1] / "scripts" / "closer_verified_release.py"
    spec = spec_from_file_location("closer_release_cli_test", script)
    assert spec and spec.loader
    cli = module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(
        cli,
        "install_after_preflight",
        lambda _config: ({"ok": True, "phase": "preflight"}, {"stage": "failed"}),
    )
    args = [
        "install",
        "--source",
        str(tmp_path),
        "--expected-source-sha",
        "a" * 40,
        "--mirror",
        str(tmp_path / "mirror"),
        "--core",
        str(tmp_path),
        "--expected-core-sha",
        "b" * 40,
        "--python",
        sys.executable,
        "--journal",
        str(tmp_path / "release.json"),
        "--owner",
        "closer",
    ]
    assert cli.main(args) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "release_not_installed"


@pytest.mark.parametrize("failure", ["release", "value"])
def test_cli_install_returns_json_receipt_for_release_refusal(
    tmp_path, monkeypatch, capsys, failure
):
    script = Path(__file__).parents[1] / "scripts" / "closer_verified_release.py"
    spec = spec_from_file_location("closer_release_cli_refusal_test", script)
    assert spec and spec.loader
    cli = module_from_spec(spec)
    spec.loader.exec_module(cli)
    exception = (
        cli.ReleaseError("owned by another candidate")
        if failure == "release"
        else ValueError("bad record")
    )
    monkeypatch.setattr(
        cli,
        "install_after_preflight",
        lambda _config: (_ for _ in ()).throw(exception),
    )
    args = [
        "install",
        "--source",
        str(tmp_path),
        "--expected-source-sha",
        "a" * 40,
        "--mirror",
        str(tmp_path / "mirror"),
        "--core",
        str(tmp_path),
        "--expected-core-sha",
        "b" * 40,
        "--python",
        sys.executable,
        "--journal",
        str(tmp_path / "release.json"),
        "--owner",
        "closer",
    ]
    assert cli.main(args) == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": False,
        "phase": "install",
        "error": {"code": "release_refused", "message": str(exception)},
    }


def test_actual_cli_install_returns_nonzero_for_a_failed_existing_gate(tmp_path):
    source, source_sha = git_repo(
        tmp_path / "source",
        {
            "scripts/verify_before_sync.sh": (
                "#!/usr/bin/env bash\n"
                'if [[ "${1:-}" == "--identity" ]]; then printf \'%040d\\n\' 0; exit 0; fi\n'
                "exit 1\n"
            ),
            "scripts/install_verified_snapshot.py": (
                "import sys\n"
                "if '--generation-token' in sys.argv:\n"
                " print('generation-v1:' + 'a' * 64)\n"
            ),
            ".verify-floor.json": json.dumps({"collected": 1, "passed": 1}),
            "tests/test_fixture.py": "def test_fixture():\n    assert True\n",
        },
    )
    (source / "scripts" / "verify_before_sync.sh").chmod(0o755)
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "make verifier executable",
        ],
        check=True,
    )
    source_sha = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    core, core_sha = git_repo(
        tmp_path / "core", {"scripts/closer_runtime.py": "# required runtime\n"}
    )
    result = subprocess.run(
        [
            sys.executable,
            "scripts/closer_verified_release.py",
            "install",
            "--source",
            str(source),
            "--expected-source-sha",
            source_sha,
            "--mirror",
            str(tmp_path / "mirror"),
            "--core",
            str(core),
            "--expected-core-sha",
            core_sha,
            "--python",
            sys.executable,
            "--journal",
            str(tmp_path / "release.json"),
            "--owner",
            "closer",
        ],
        cwd=Path(__file__).parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "release_not_installed"
    assert json.loads((tmp_path / "release.json").read_text())["stage"] == "failed"


def test_operator_script_cannot_shadow_release_record_module(tmp_path):
    root = Path(__file__).parents[1]
    module_root = root / "src" if (root / "src").is_dir() else root
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(root / "scripts"), str(module_root))),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from closer_release import ReleaseRecord; print(ReleaseRecord.__module__)",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "closer_release"
