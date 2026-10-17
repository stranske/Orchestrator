"""Source660 compatibility at real learner, CLI and publication boundaries."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import route_weights_export as export


def test_state_only_learner_writer_and_cli_share_db(tmp_path):
    """A new process must write and export the same Brain, independent of shadow destination."""
    state = tmp_path / "instance"
    output = tmp_path / "shadow"
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_")}
    env.update(
        HOME=str(home),
        ORCH_STATE_DIR=str(state),
        ORCH_MODEL_PROBE="0",
        PYTHONPATH=str(Path(export.__file__).parent),
    )
    script = """
import json, sys
# This boundary needs no provider, Git, or network subprocess.
def deny_subprocess(event, args):
    if event == "subprocess.Popen":
        raise AssertionError(f"unexpected CLI subprocess: {args[:2]}")
sys.addaudithook(deny_subprocess)
import feedback
import route_weights_export as export
with feedback._conn() as connection:
    connection.execute("INSERT INTO route_weights "
        "(version, task_type, agent, posterior, score, n_obs, success_rate) "
        "VALUES (73, 'implement', 'codex', 0.9, 0.9, 25, 0.9)")
export._current_capacity_snapshot = lambda: {"generated_at": 100, "agents": {}}
export.capabilities.daily_heartbeat = lambda *a, **k: None
sys.argv = ["route_weights_export.py", "--state-dir", sys.argv[1]]
assert export.main() == 0
print(json.dumps({"writer": str(feedback.DB_PATH), "reader": str(feedback.resolve_db_path())}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(output)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    expected = state / "feedback" / "orchestrator.db"
    binding = json.loads(result.stdout.splitlines()[-1])
    assert binding == {"writer": str(expected), "reader": str(expected)}, "learner DB binding split"
    document = json.loads((output / "route-weights-export.json").read_text())
    assert document["source_version"] == 73, "CLI shadow destination redirected evidence source"
    assert document["task_types"]["implement"]["evidence_ok"] is True
    assert list(tmp_path.rglob("*.db")) == [expected], "only the selected learner DB may exist"
    assert not (state / "route-weights-export.json").exists()


@pytest.mark.parametrize("age_hours", [0, 24, 240])
def test_partial_regression_never_ages_into_permission(tmp_path, age_hours):
    current = {"source_version": 73, "task_types": {"implement": {"evidence_ok": True}}}
    previous = {"task_types": {"review": {"evidence_ok": True}}}
    hold = tmp_path / "route-weights-regression-hold.json"
    export.write_document(
        hold,
        {
            "first_seen": export.dt.datetime.now(export.dt.timezone.utc).timestamp()
            - age_hours * 3600,
            "task_types": ["review"],
            "reason": "fixture",
        },
    )
    with pytest.raises(export.PublicationRefused, match="--allow-evidence-regression"):
        export._require_publishable(current, previous, state_dir=tmp_path)
    export._require_publishable(current, previous, True, state_dir=tmp_path)
    assert not hold.exists(), "explicit release must clear stale hold diagnostics"


def test_cli_shadow_destination_does_not_select_db(tmp_path, monkeypatch, capsys):
    for name in ("ORCH_STATE_DIR", "ORCH_LOCAL_RUNTIME", export.feedback.DB_PATH_ENV):
        monkeypatch.delenv(name, raising=False)
    source = tmp_path / "selected.db"
    export._fixture_db(source)
    before = source.read_bytes()
    output = tmp_path / "output"
    monkeypatch.setattr(export.feedback, "DB_PATH", source)
    monkeypatch.setattr(
        export, "_current_capacity_snapshot", lambda: {"generated_at": 100, "agents": {}}
    )
    monkeypatch.setattr(export.capabilities, "daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["export", "--state-dir", str(output)])
    assert export.main() == 0
    assert json.loads((output / "route-weights-export.json").read_text())["source_version"] == 2
    assert f"db={source} source_version=2" in capsys.readouterr().err
    assert source.read_bytes() == before
    assert list(tmp_path.rglob("*.db")) == [source]


@pytest.mark.parametrize("allow", [False, True], ids=["aged-refused", "explicit-override"])
def test_real_db_cli_publication_requires_override(tmp_path, monkeypatch, allow):
    """Real DB → CLI build → fetched local publication; age alone cannot release evidence."""

    def git(root, *args):
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Reconciliation Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.invalid")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", str(remote), str(checkout))
    database = tmp_path / "source.db"
    export._fixture_db(database)
    before_db = database.read_bytes()
    previous = export.build_document(database, now=100, capacity_snapshot={"agents": {}})
    previous["task_types"]["review"] = dict(previous["task_types"]["implement"])
    export.write_document(checkout / export.EXPORT_PATH, previous)
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "isolated baseline")
    git(checkout, "push", "origin", f"HEAD:{export.EXPORT_BRANCH}")
    before_head = git(remote, "rev-parse", export.EXPORT_BRANCH)
    state = tmp_path / "output"
    export.write_document(
        state / "route-weights-regression-hold.json",
        {
            "first_seen": 1,
            "task_types": ["review"],
            "reason": "old partial regression",
        },
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_")}
    env.update(
        HOME=str(tmp_path / "home"),
        ORCH_MODEL_PROBE="0",
        ORCH_ROUTE_WEIGHTS_PUBLISH="1",
        ORCH_LOCAL_RUNTIME=str(tmp_path / "runtime"),
        ORCH_STATE_DIR=str(tmp_path / "instance"),
        PYTHONPATH=str(Path(export.__file__).parent),
    )
    script = """
import sys
from pathlib import Path
import route_weights_export as export
checkout = Path(sys.argv[1])
export.provision.ensure_canonical = lambda *a: checkout
export._current_capacity_snapshot = lambda: {"generated_at": 100, "agents": {}}
export.capabilities.daily_heartbeat = lambda *a, **k: None
sys.argv = ["route_weights_export.py", *sys.argv[2:]]
raise SystemExit(export.main())
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(checkout),
            "--feedback-db",
            str(database),
            "--state-dir",
            str(state),
            "--publish",
        ]
        + (["--allow-evidence-regression"] if allow else []),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == (0 if allow else 1), result.stderr
    after_head = git(remote, "rev-parse", export.EXPORT_BRANCH)
    published = json.loads(git(remote, "show", f"{export.EXPORT_BRANCH}:{export.EXPORT_PATH}"))
    assert published["source_version"] == 2
    assert published["task_types"]["implement"]["evidence_ok"] is True
    assert published["task_types"]["review"]["evidence_ok"] is (not allow)
    assert (after_head != before_head) is allow
    assert (
        database.read_bytes() == before_db
    ), "publication must never migrate/write the evidence DB"
    assert json.loads((state / "route-weights-export.json").read_text())["source_version"] == 2
    if allow:
        assert "override --allow-evidence-regression" in result.stderr
        assert not (state / "route-weights-regression-hold.json").exists()
    else:
        assert "--allow-evidence-regression required" in result.stderr
        assert git(checkout, "branch", "--show-current") == "main"
