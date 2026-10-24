"""The owner-approved window extends once, from measured per-policy switch counts."""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

import pytest

import exploration_collection
import paths
import route_weights_export as export


def _ts(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


def _brain(path, epsilon, thompson):
    # Adjacent UTC weeks exercise the actual deterministic policy selector, not a mock.
    stamps = {}
    for offset in (0, 7):
        stamp = _ts("2026-10-17") + offset * 86400
        stamps[exploration_collection.choose_mode({}, now=stamp)] = stamp
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_switches "
            "(source TEXT, delegation_source TEXT, switched_ts INTEGER)"
        )
        conn.execute("DELETE FROM agent_switches")
        for mode, n in (("epsilon-greedy", epsilon), ("thompson-hybrid", thompson)):
            conn.executemany(
                "INSERT INTO agent_switches VALUES ('policy', 'route_weights_exploration', ?)",
                [(stamps[mode],)] * n,
            )
        # None of these are exploration policy switches inside the window.
        conn.executemany(
            "INSERT INTO agent_switches VALUES (?, ?, ?)",
            [
                ("label", "route_weights_exploration", _ts("2026-10-17")),
                ("policy", "route_weights", _ts("2026-10-17")),
                ("policy", "route_weights_exploration", _ts("2026-10-16")),
                ("policy", "route_weights_exploration", _ts("2027-01-01")),
            ],
        )


@pytest.mark.parametrize(
    "day,epsilon,thompson,extended",
    [
        ("2026-11-16", 29, 40, False),
        ("2026-11-17", 29, 40, True),
        ("2026-11-18", 40, 29, True),
        ("2026-11-17", 30, 31, False),
    ],
)
def test_extension_threshold(tmp_path, day, epsilon, thompson, extended):
    brain = tmp_path / "brain.db"
    _brain(brain, epsilon, thompson)
    state = export.update_exploration_window(brain, tmp_path, now=_ts(day))
    assert state == json.loads((tmp_path / "exploration-window.json").read_text())
    assert state["n_by_policy"] == {"epsilon-greedy": epsilon, "thompson-hybrid": thompson}
    assert state["extended"] is extended
    assert state["end"] == ("20261215" if extended else "20261117")
    assert state["start"] == "2026-10-17"
    assert state["min_per_policy"] == 30
    assert state["decided_at"].startswith(day)


def test_extended_state_is_sticky(tmp_path):
    brain = tmp_path / "brain.db"
    _brain(brain, 29, 40)
    assert export.update_exploration_window(brain, tmp_path, now=_ts("2026-11-17"))["extended"]
    _brain(brain, 30, 40)
    state = export.update_exploration_window(brain, tmp_path, now=_ts("2026-11-18"))
    assert state["extended"] is True
    assert state["end"] == "20261215"


def test_missing_brain_is_unmeasured_and_is_not_created(tmp_path):
    brain = tmp_path / "absent.db"
    state = export.update_exploration_window(brain, tmp_path, now=_ts("2026-11-17"))
    assert state["n_by_policy"] == {"epsilon-greedy": None, "thompson-hybrid": None}
    assert "unmeasured" in state["errors"][0]
    assert not brain.exists()


@pytest.mark.parametrize("end", [None, "20261215; echo unsafe", "2026121", "20261215\n", []])
def test_shell_falls_back_for_missing_or_malformed_state(tmp_path, end):
    _run_shell(tmp_path, end, [("20261117", "0.20"), ("20261118", "unset")])


def test_shell_honors_extended_end(tmp_path):
    _run_shell(tmp_path, "20261215", [("20261215", "0.20"), ("20261216", "unset")])


def _run_shell(tmp_path, end, expectations):
    if end is not None:
        (tmp_path / "exploration-window.json").write_text(json.dumps({"end": end}))
    text = (paths.REPO_ROOT / "orchestrate.sh").read_text()
    start = text.index("# ORCH-ANCHOR: exploration-window-start")
    snippet = text[start : text.index("# ORCH-ANCHOR: exploration-window-end", start)]
    fake_date = tmp_path / "date"
    fake_date.write_text(
        '#!/bin/bash\n[[ "$*" == "-u +%Y%m%d" ]] || exit 2\nprintf "%s\\n" "$FAKE_DATE"\n'
    )
    fake_date.chmod(0o755)
    env = dict(
        os.environ,
        ORCH_STATE_DIR=str(tmp_path),
        PATH=f"{tmp_path}:{os.path.dirname(sys.executable)}:{os.environ['PATH']}",
    )
    env.pop("ORCH_EXPLORATION_RATE", None)
    for day, expected in expectations:
        result = subprocess.run(
            ["/bin/bash", "-euc", snippet + '\nprintf "%s" "${ORCH_EXPLORATION_RATE-unset}"'],
            env=dict(env, FAKE_DATE=day),
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout == expected


def test_export_refreshes_window_even_when_publication_is_unchanged(tmp_path, monkeypatch):
    brain = tmp_path / "brain.db"
    _brain(brain, 29, 40)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_ROUTE_WEIGHTS_PUBLISH", "1")
    monkeypatch.setattr(
        sys, "argv", ["route_weights_export.py", "--feedback-db", str(brain), "--publish"]
    )
    monkeypatch.setattr(export, "build_document", lambda *a, **kw: {"source_version": 1})
    monkeypatch.setattr(export.capabilities, "daily_heartbeat", lambda *a, **kw: None)
    published = []

    def publish(document, **kwargs):
        # The real CLI writes the decision before attempting remote publication.
        published.append(json.loads((tmp_path / "exploration-window.json").read_text()))
        return False

    monkeypatch.setattr(export, "publish_document", publish)
    assert export.main() == 0
    assert len(published) == 1
    assert published[0]["start"] == "2026-10-17"


@pytest.mark.parametrize("invocation", ["inspection", "real-file"])
@pytest.mark.parametrize(
    "day,epsilon,thompson,already_extended,override,expected",
    [
        ("2026-11-18", 29, 40, False, None, "0.20"),
        ("2026-11-18", 30, 31, False, None, "unset"),
        ("2026-11-18", 30, 31, True, None, "0.20"),
        ("2026-11-18", 29, 40, False, "0", "0"),
        ("2026-11-18", 29, 40, False, "0.35", "0.35"),
        ("2026-12-16", 30, 31, True, None, "unset"),
    ],
)
def test_late_first_tick_refreshes_window_before_default_rate(
    tmp_path, day, epsilon, thompson, already_extended, override, expected, invocation
):
    brain = tmp_path / "brain.db"
    _brain(brain, epsilon, thompson)
    if already_extended:
        (tmp_path / "exploration-window.json").write_text(
            json.dumps({"extended": True, "end": "20261215"})
        )
    state = tmp_path / "exploration-window.json"
    before = state.read_bytes() if state.exists() else None
    brain_before = brain.read_bytes()
    text = (paths.REPO_ROOT / "orchestrate.sh").read_text()
    start = text.index("# ORCH-ANCHOR: exploration-window-start")
    snippet = text[start : text.index("# ORCH-ANCHOR: exploration-window-end", start)]
    fake_date = tmp_path / "date"
    fake_date.write_text(
        '#!/bin/bash\ncase "$*" in\n'
        f'"-u +%Y%m%d") echo {day.replace("-", "")};;\n'
        f'"-u +%s") echo {_ts(day)};;\n'
        "*) exit 2;;\nesac\n"
    )
    fake_date.chmod(0o755)
    env = dict(
        os.environ,
        ORCH=str(paths.REPO_ROOT / "src"),
        ORCH_STATE_DIR=str(tmp_path),
        ORCH_FEEDBACK_DB=str(brain),
        PATH=f"{tmp_path}:{os.path.dirname(sys.executable)}:{os.environ['PATH']}",
    )
    env.pop("ORCH_EXPLORATION_RATE", None)
    if override is not None:
        env["ORCH_EXPLORATION_RATE"] = override
    script = snippet + '\nprintf "%s" "${ORCH_EXPLORATION_RATE-unset}"'
    if invocation == "real-file":
        tick = tmp_path / "tick.sh"
        tick.write_text(script)
        command = ["/bin/bash", "-eu", str(tick)]
    else:
        command = ["/bin/bash", "-euc", script]
    result = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == expected
    if invocation == "inspection":
        assert (state.read_bytes() if state.exists() else None) == before
    else:
        window = json.loads(state.read_text())
        assert window["extended"] is (already_extended or min(epsilon, thompson) < 30)
        assert window["decided_at"].startswith(day)
    assert brain.read_bytes() == brain_before


def test_export_decides_window_before_build_at_one_instant(tmp_path, monkeypatch):
    brain = tmp_path / "brain.db"
    _brain(brain, 29, 40)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ORCH_EXPLORATION_RATE", raising=False)
    monkeypatch.setattr(sys, "argv", ["route_weights_export.py", "--feedback-db", str(brain)])
    monkeypatch.setattr(export, "_now", lambda: "2026-11-18T00:00:00Z")
    monkeypatch.setattr(export.capabilities, "daily_heartbeat", lambda *a, **kw: None)
    built = []

    def build(*args, **kwargs):
        window = json.loads((tmp_path / "exploration-window.json").read_text())
        built.append((window, kwargs.get("now"), os.environ.get("ORCH_EXPLORATION_RATE")))
        return {"source_version": 1}

    monkeypatch.setattr(export, "build_document", build)
    assert export.main() == 0
    assert built[0][0]["extended"] is True
    assert built[0][0]["decided_at"] == "2026-11-18T00:00:00Z"
    assert built[0][1:] == (_ts("2026-11-18"), "0.20")
    assert "ORCH_EXPLORATION_RATE" not in os.environ


@pytest.mark.parametrize(
    "day,epsilon,thompson,override,expected",
    [
        ("2026-11-17", 30, 31, None, 0.20),
        ("2026-11-18", 30, 31, None, 0.05),
        ("2026-11-18", 29, 40, None, 0.20),
        ("2026-12-15", 29, 40, None, 0.20),
        ("2026-12-16", 29, 40, None, 0.05),
        ("2026-11-18", 29, 40, "0", 0.0),
        ("2026-11-18", 29, 40, "0.35", 0.35),
        ("2026-12-16", 29, 40, "0.35", 0.35),
    ],
)
def test_real_export_rate_matches_decision_and_preserves_override(
    tmp_path,
    monkeypatch,
    day,
    epsilon,
    thompson,
    override,
    expected,
):
    brain = tmp_path / "brain.db"
    _brain(brain, epsilon, thompson)
    export._fixture_db(brain)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "route_weights_export.py",
            "--feedback-db",
            str(brain),
            "--state-dir",
            str(tmp_path),
            "--as-of",
            str(_ts(day)),
        ],
    )
    if override is None:
        monkeypatch.delenv("ORCH_EXPLORATION_RATE", raising=False)
    else:
        monkeypatch.setenv("ORCH_EXPLORATION_RATE", override)
    monkeypatch.setattr(export.capabilities, "daily_heartbeat", lambda *a, **kw: None)
    original_build = export.build_document
    monkeypatch.setattr(
        export, "build_document", lambda *a, **kw: original_build(*a, **kw, include_capacity=False)
    )
    assert export.main() == 0
    document = json.loads((tmp_path / "route-weights-export.json").read_text())
    window = json.loads((tmp_path / "exploration-window.json").read_text())
    assert document["exploration"]["rate"] == expected
    assert document["generated_at"] == window["decided_at"]
    assert os.environ.get("ORCH_EXPLORATION_RATE") == override


@pytest.mark.parametrize("state_bytes", [b"not json", b"\xff", b"[]"])
def test_corrupt_window_state_recovers_only_from_measured_counts(tmp_path, state_bytes):
    brain = tmp_path / "brain.db"
    _brain(brain, 30, 31)
    (tmp_path / "exploration-window.json").write_bytes(state_bytes)
    window = export.update_exploration_window(brain, tmp_path, now=_ts("2026-11-18"))
    assert window["extended"] is False
    assert window["n_by_policy"] == {"epsilon-greedy": 30, "thompson-hybrid": 31}


def test_corrupt_brain_keeps_extended_state_without_inventing_counts(tmp_path):
    brain = tmp_path / "brain.db"
    brain.write_bytes(b"not sqlite")
    (tmp_path / "exploration-window.json").write_text(json.dumps({"extended": True}))
    window = export.update_exploration_window(brain, tmp_path, now=_ts("2026-11-18"))
    assert window["extended"] is True
    assert window["n_by_policy"] == {"epsilon-greedy": None, "thompson-hybrid": None}
    assert "unmeasured" in window["errors"][0]
    assert brain.read_bytes() == b"not sqlite"


@pytest.mark.parametrize("existing", [False, True])
def test_real_preview_cli_does_not_write_or_run_export(tmp_path, existing):
    brain = tmp_path / "brain.db"
    _brain(brain, 29, 40)
    state_dir = tmp_path / "state"
    if existing:
        state_dir.mkdir()
        (state_dir / "exploration-window.json").write_text('{"end":"20261117","extended":false}')
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    result = subprocess.run(
        [
            sys.executable,
            str(paths.REPO_ROOT / "src" / "route_weights_export.py"),
            "--preview-exploration-window",
            "--feedback-db",
            str(brain),
            "--state-dir",
            str(state_dir),
            "--as-of",
            str(_ts("2026-11-18")),
        ],
        env=dict(
            os.environ,
            HOME=str(tmp_path),
            ORCH_STATE_DIR=str(state_dir),
            ORCH_LOCAL_RUNTIME=str(tmp_path),
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "20261215 20261118\n"
    assert result.stderr == ""
    assert {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    } == before
    assert state_dir.exists() is existing


@pytest.mark.parametrize("existing", [False, True])
def test_real_recurrence_prologue_does_not_write_window(tmp_path, monkeypatch, existing):
    import capability_recurrence_check as recurrence

    brain = tmp_path / "brain.db"
    _brain(brain, 29, 40)
    state_dir = tmp_path / "state"
    if existing:
        state_dir.mkdir()
        (state_dir / "exploration-window.json").write_text('{"end":"20261215","extended":true}')
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ORCH_DIR", str(paths.REPO_ROOT))
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(brain))
    monkeypatch.setenv("ORCH_STATE_DIR", str(state_dir))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    monkeypatch.delenv("ORCH_EXPLORATION_RATE", raising=False)
    monkeypatch.setattr(recurrence, "ORCHESTRATE", paths.REPO_ROOT / "orchestrate.sh")
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    env, status = recurrence._tick_env_attempt()
    assert status["reason"] == "ok"
    assert env["ORCH_EXPLORATION_RATE"] == "0.20"
    assert {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    } == before
    assert state_dir.exists() is existing


def test_real_tick_extension_survives_samples_improving_before_export(tmp_path, monkeypatch):
    brain = tmp_path / "brain.db"
    _brain(brain, 29, 40)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ORCH_EXPLORATION_RATE", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "route_weights_export.py",
            "--feedback-db",
            str(brain),
            "--refresh-exploration-window",
            "--as-of",
            str(_ts("2026-11-18")),
        ],
    )
    monkeypatch.setattr(export.capabilities, "daily_heartbeat", lambda *a, **kw: None)
    assert export.main() == 0
    assert json.loads((tmp_path / "exploration-window.json").read_text())["extended"] is True
    _brain(brain, 30, 40)
    export._fixture_db(brain)
    build = export.build_document
    monkeypatch.setattr(
        export, "build_document", lambda *a, **kw: build(*a, **kw, include_capacity=False)
    )
    monkeypatch.setattr(export, "_now", lambda: "2026-11-18T00:01:00Z")
    monkeypatch.setattr(sys, "argv", ["route_weights_export.py", "--feedback-db", str(brain)])
    assert export.main() == 0
    window = json.loads((tmp_path / "exploration-window.json").read_text())
    document = json.loads((tmp_path / "route-weights-export.json").read_text())
    assert window["extended"] is True
    assert window["n_by_policy"] == {"epsilon-greedy": 30, "thompson-hybrid": 40}
    assert window["decided_at"] == document["generated_at"] == "2026-11-18T00:01:00Z"
    assert document["exploration"]["rate"] == 0.20


def test_recurrence_from_external_cwd_previews_late_window_without_orch_dir(tmp_path, monkeypatch):
    import capability_recurrence_check as recurrence

    brain = tmp_path / "brain.db"
    _brain(brain, 29, 40)
    fake_date = tmp_path / "date"
    fake_date.write_text(
        '#!/bin/bash\ncase "$*" in\n'
        '"-u +%Y%m%d") echo 20261118;;\n'
        f'"-u +%s") echo {_ts("2026-11-18")};;\n'
        '"+%Y-%m-%d") echo 2026-11-18;;\n'
        "*) exit 2;;\nesac\n"
    )
    fake_date.chmod(0o755)
    external = tmp_path / "external-caller"
    external.mkdir()
    monkeypatch.chdir(external)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.path.dirname(sys.executable)}:{os.environ['PATH']}")
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(brain))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "absent-state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "absent-runtime"))
    monkeypatch.delenv("ORCH_DIR", raising=False)
    monkeypatch.delenv("ORCH_EXPLORATION_RATE", raising=False)
    monkeypatch.setattr(recurrence, "ORCHESTRATE", paths.REPO_ROOT / "orchestrate.sh")
    caller_env = dict(os.environ)
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    env, status = recurrence._tick_env_attempt()
    assert status["reason"] == "ok"
    assert env.get("ORCH_EXPLORATION_RATE") == "0.20"
    assert "ORCH_DIR" not in env
    assert dict(os.environ) == caller_env
    assert {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    } == before
    assert not (tmp_path / "absent-state").exists()
    assert not (tmp_path / "absent-runtime").exists()
