"""Real production default callers preserve deployed inventory, bytes and modes."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import paths

MODULES = (
    "paths",
    "mutable_registry",
    "features",
    "feature_scan",
    "capabilities",
    "research_scheduler",
    "periodic_report",
)


def inventory(root):
    return {
        str(p.relative_to(root)): (
            stat.S_IMODE(p.stat().st_mode),
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "directory",
        )
        for p in root.rglob("*")
    }


def environment(tmp_path, modules, explicit=False):
    runtime = tmp_path / "runtime"
    env = dict(
        os.environ,
        ORCH_LOCAL_RUNTIME=str(runtime),
        ORCH_STATE_DIR=str(runtime),
        PYTHONDONTWRITEBYTECODE="1",
        ORCH_MODEL_PROBE="0",
        ORCH_GEMINI_MODEL_PROBE="0",
    )
    for name in (
        "ORCH_FEATURES_PATH",
        "ORCH_HYP_PATH",
        "ORCH_FEEDBACK_DB",
        "ORCH_CAPABILITIES_PATH",
    ):
        env.pop(name, None)
    if explicit:
        env["ORCH_FEATURES_PATH"] = str(tmp_path / "explicit/features.json")
        env["ORCH_HYP_PATH"] = str(tmp_path / "explicit/hypotheses.json")
    for name in (
        "ORCH_CODEX_SESSIONS_DIR",
        "ORCH_CLAUDE_PROJECTS_DIR",
        "ORCH_CURSOR_CHATS_DIR",
        "ORCH_VIBE_SESSIONS_DIR",
        "ORCH_AGY_HOME",
    ):
        p = tmp_path / "empty-probes" / name
        p.mkdir(parents=True, exist_ok=True)
        env[name] = str(p)
    env["PYTHONPATH"] = os.pathsep.join((str(modules), str(paths.MODULE_DIR)))
    return env


def child(tmp_path, layout, explicit=False, generation=0, record=False):
    tree = tmp_path / f"generation-{generation}"
    modules = tree / "src" if layout == "src" else tree
    modules.mkdir(parents=True)
    for name in MODULES:
        shutil.copy2(paths.MODULE_DIR / f"{name}.py", modules / f"{name}.py")
    # Retained optional snapshots never become implicit write destinations.
    (tree / "experiments").mkdir(exist_ok=True)
    (tree / "experiments/source-snapshot.json").write_text('{"historical": "preserve"}')
    before = inventory(tree)
    env = environment(tmp_path, modules, explicit)
    code = """
import json, features, capabilities, feature_scan, research_scheduler, periodic_report
features._capability_heartbeat = lambda *a: None
feature_scan._capability_heartbeat = lambda *a: None
research_scheduler._capability_heartbeat = lambda *a: None
features_before = features.REG.exists()
hypotheses_before = research_scheduler.HYP_PATH.exists()
features.load()
research_scheduler.load_hypotheses()
feature_scan.scan()
research_scheduler.spare_capacity = lambda snapshot: {}
plan = research_scheduler.build_research_plan([], {}, learned={})
periodic_report._hypothesis_summary()
assert features.REG.exists() == features_before
assert research_scheduler.HYP_PATH.exists() == hypotheses_before
if RECORD:
    features.record_use('operator-feature', 'original-task', problem='preserve edits')
    research_scheduler.update_hypotheses(lambda items: items.append({'id':'operator-hypothesis', 'claim':'preserve edits'}))
registry = features.load()
hypotheses = research_scheduler.load_hypotheses()
source, provenance = capabilities._feature_snapshot(capabilities.FEATURES_REG)
print(json.dumps({'features': str(features.REG), 'features_reader': str(capabilities.FEATURES_REG),
 'hypotheses': str(research_scheduler.HYP_PATH), 'hypotheses_reader': str(periodic_report.HYPOTHESES_JSON),
 'operator_feature': registry.get('operator-feature'), 'operator_hypothesis': any(x['id']=='operator-hypothesis' for x in hypotheses),
 'migration_source':provenance, 'migration_records':list(source), 'plan_status':plan['status']}))
""".replace("RECORD", repr(record))
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert inventory(tree) == before
    return json.loads(result.stdout.splitlines()[-1]), env, modules


@pytest.mark.parametrize("layout", ["flat", "src"])
@pytest.mark.parametrize("explicit", [False, True])
def test_scan_report_planner_and_readers_preserve_generation(tmp_path, layout, explicit):
    result, env, modules = child(tmp_path, layout, explicit, record=True)
    root = tmp_path / ("explicit" if explicit else "runtime/experiments")
    assert result["features"] == result["features_reader"] == str(root / "features.json")
    assert result["hypotheses"] == result["hypotheses_reader"] == str(root / "hypotheses.json")
    assert result["migration_source"]["source_path"] == result["features"]
    assert "operator-feature" in result["migration_records"]
    assert result["plan_status"] == "no_spare"


@pytest.mark.parametrize("layout", ["flat", "src"])
def test_first_report_only_cli_never_initializes_registry_or_touches_generation(tmp_path, layout):
    tree = tmp_path / "generation"
    modules = tree / "src" if layout == "src" else tree
    modules.mkdir(parents=True)
    for name in ("paths", "mutable_registry", "features", "feature_scan"):
        shutil.copy2(paths.MODULE_DIR / f"{name}.py", modules / f"{name}.py")
    env = environment(tmp_path, modules)
    before = inventory(tree)
    result = subprocess.run(
        [sys.executable, str(modules / "feature_scan.py"), "--json"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert inventory(tree) == before
    assert not (tmp_path / "runtime/experiments/features.json").exists()


def test_generations_conserve_operator_records_and_edits(tmp_path):
    first, _, _ = child(tmp_path, "src", generation=0, record=True)
    second, _, _ = child(tmp_path, "flat", generation=1)
    assert first["features"] == second["features"]
    assert first["operator_feature"] == second["operator_feature"]
    assert second["operator_feature"]["problem"] == "preserve edits"
    assert second["operator_hypothesis"] is True


def test_default_runtime_path_does_not_create_anything(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCH_LOCAL_RUNTIME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    expected = tmp_path / "home/.codex/orchestrator/experiments/features.json"
    assert paths.runtime_state_path("experiments", "features.json") == expected
    assert not expected.parent.exists()


@pytest.mark.parametrize("kind", ["features", "hypotheses"])
@pytest.mark.parametrize("layout", ["flat", "src"])
def test_legacy_operator_records_hold_before_initialization(tmp_path, kind, layout):
    tree = tmp_path / "legacy-generation"
    modules = tree / "src" if layout == "src" else tree
    modules.mkdir(parents=True)
    for name in MODULES:
        shutil.copy2(paths.MODULE_DIR / f"{name}.py", modules / f"{name}.py")
    legacy = modules / "experiments" / f"{kind}.json"
    legacy.parent.mkdir()
    value = (
        {"operator-custom": {"maturity": "reused", "uses": ["original-task"], "count": 1}}
        if kind == "features"
        else [{"id": "operator-custom", "claim": "retain edits"}]
    )
    legacy.write_text(json.dumps(value))
    before = inventory(tree)
    env = environment(tmp_path, modules)
    code = """
import features, research_scheduler
loader = features.load if KIND == 'features' else research_scheduler.load_hypotheses
for create in (False, True):
    try:
        loader(create=create)
    except ValueError as exc:
        assert 'reviewed migration required' in str(exc), str(exc)
        assert 'canonical_json_sha256' in str(exc), str(exc)
    else:
        raise AssertionError('legacy operator state was hidden by a seed')
""".replace("KIND", repr(kind))
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert inventory(tree) == before
    assert not (tmp_path / "runtime").exists()


def _record_concurrently(path, hypotheses, worker):
    import features
    import research_scheduler

    for number in range(8):
        features.record_use("shared-feature", f"{worker}:{number}", path=path)
        identifier = f"operator-{worker}-{number}"
        research_scheduler.update_hypotheses(
            lambda rows: rows.append({"id": identifier, "claim": "conserve"}), path=hypotheses
        )


def test_actual_feature_and_hypothesis_updates_conserve_concurrent_operator_edits(tmp_path):
    import multiprocessing

    import features
    import research_scheduler

    path = tmp_path / "features.json"
    hypotheses = tmp_path / "hypotheses.json"
    ctx = multiprocessing.get_context("fork")
    workers = [
        ctx.Process(target=_record_concurrently, args=(path, hypotheses, n)) for n in range(4)
    ]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(20)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join()
    result = features.load(path)["shared-feature"]
    assert result["count"] == 32
    assert set(result["uses"]) == {
        f"{worker}:{number}" for worker in range(4) for number in range(8)
    }
    actual = {row["id"] for row in research_scheduler.load_hypotheses(hypotheses)}
    assert {f"operator-{worker}-{number}" for worker in range(4) for number in range(8)} <= actual
