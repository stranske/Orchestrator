"""Version-skew and pre-import trust regressions; synthetic proofs are not business evidence."""

import copy
import fcntl
import hashlib
import json
import stat
import sys
from pathlib import Path

import pytest

import closer_distribution_package as distribution
import closer_native_source_upgrade as upgrade

FIXTURE = Path(__file__).parent / "fixtures/closer_source_upgrade/f858-runtime-package.json"


def _metadata(descriptor):
    return {
        **{
            k: descriptor[k]
            for k in ("candidate_sha", "modules", "package_digest", "state_versions")
        },
        "public_runtime_descriptor": "closer-runtime-package.json",
    }


def test_reviewed_191_module_descriptor_is_old_only():
    raw = FIXTURE.read_bytes()
    old = json.loads(raw)
    assert len(old["modules"]) == 191
    assert "closer_native_source_upgrade.py" not in old["modules"]
    assert upgrade._reviewed_old_metadata(_metadata(old), raw) == old["modules"]
    assert "modules set does not match pinned runtime modules" in (
        distribution.validate_public_runtime_descriptor(old)
    )

    current = distribution.build_public_runtime_descriptor(
        Path(upgrade.__file__).parent,
        candidate_sha="a" * 40,
        state_versions=old["state_versions"],
        supported_core=old["supported_core"],
    )
    assert len(current["modules"]) == 192
    assert not distribution.validate_public_runtime_descriptor(current)
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._reviewed_old_metadata(_metadata(current), json.dumps(current).encode())


@pytest.mark.parametrize(
    "change", ["candidate", "module", "missing_module", "package", "state", "path", "descriptor"]
)
def test_reviewed_old_metadata_rejects_forged_identity_before_execution(monkeypatch, change):
    raw = FIXTURE.read_bytes()
    doc = copy.deepcopy(_metadata(json.loads(raw)))
    monkeypatch.setattr(
        upgrade.subprocess, "run", lambda *a, **k: pytest.fail("untrusted old code executed")
    )
    if change == "candidate":
        doc["candidate_sha"] = "b" * 40
    elif change == "module":
        doc["modules"]["closer_campaign.py"] = "0" * 64
    elif change == "missing_module":
        doc["modules"].pop("closer_launcher.py")
    elif change == "package":
        doc["package_digest"] = "0" * 64
    elif change == "state":
        doc["state_versions"] = {"campaign": 999}
    elif change == "path":
        doc["public_runtime_descriptor"] = "../closer-runtime-package.json"
    else:
        raw = raw.replace(b'"owner": "Orchestrator"', b'"owner": "Other"')
        assert raw != FIXTURE.read_bytes()
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._reviewed_old_metadata(doc, raw)


def _synthetic_old_runtime(tmp_path, monkeypatch):
    """Tiny external-service simulation solely to observe which code is imported."""
    root = tmp_path / "old"
    root.mkdir(mode=0o700)
    target = "test/repo#1"
    receipt = tmp_path / "round.json"
    receipt.write_text(
        json.dumps(
            {
                "targets": {
                    target: {
                        "results": {
                            "exception": {"correction": {}},
                            "act": {"queue_receipt": {}},
                        }
                    }
                }
            }
        )
    )
    proof = {
        "accepted": False,
        "receipt_path": str(receipt),
        "round_id": "test-round",
        "finite_frontier": {"targets": [target]},
        "row_sha256": {target: "a" * 64},
    }
    campaign = (
        "import fcntl,pathlib\n"
        "class Campaign:\n"
        " def __init__(self, path): self.path=pathlib.Path(path)\n"
        " def read(self):\n"
        "  with self.path.with_name(self.path.name+'.lock').open('a+b') as stream:\n"
        "   fcntl.flock(stream.fileno(),fcntl.LOCK_EX)\n"
        "   return {'workers':[{'worker_id':'test-worker','target':'test/repo#1'}]}\n"
        "def validate_manifest(m): return []\n"
        "def verify_modules(m): return []\n"
        f"def _cohort_worker_evidence(*a): return {{'result':{{'simulation_only':True}}}}, {{'dispatch_state':{str(tmp_path / 'dispatch')!r}}}, {{'completed_round':{proof!r}}}, {{'completed':False}}\n"
    )
    code = {
        "closer_campaign.py": campaign,
        "closer_native_observer.py": f"def completed_round_proof(*a): return {proof!r}\n",
        "closer_resources.py": "def validate_stop(*a): return {'valid':True,'global_stop_valid':True}\n",
        "closer_mechanisms.py": "def mechanism(m): return None\n",
    }
    for name, body in code.items():
        (root / name).write_text(body)
    modules = {name: hashlib.sha256(body.encode()).hexdigest() for name, body in code.items()}
    descriptor = {
        "candidate_sha": upgrade.REVIEWED_OLD_CANDIDATE,
        "modules": modules,
        "package_digest": "a" * 64,
        "state_versions": {"campaign": 1},
    }
    raw = json.dumps(descriptor).encode()
    (root / "closer-runtime-package.json").write_bytes(raw)
    (root / "closer_role_selection.json").write_text("{}")
    interpreter = Path(sys.executable).resolve()
    interpreter_sha = hashlib.sha256(interpreter.read_bytes()).hexdigest()
    monkeypatch.setattr(
        upgrade,
        "REVIEWED_OLD_MODULES_SHA256",
        hashlib.sha256(
            json.dumps(modules, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    monkeypatch.setattr(upgrade, "REVIEWED_OLD_DESCRIPTOR_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(upgrade, "REVIEWED_OLD_INTERPRETER_SHA256", interpreter_sha)
    doc = {
        **_metadata(descriptor),
        "module_dir": str(root),
        "module_path": str(root / "closer_campaign.py"),
        "mechanism_contract": {
            "interpreter": {"path": str(interpreter), "sha256": interpreter_sha}
        },
    }
    manifest = root / "host-manifest.json"
    manifest.write_text(json.dumps(doc))
    state = tmp_path / "campaign.json"
    state.write_text("{}")
    return root, manifest, state, interpreter


@pytest.mark.parametrize(
    "tamper",
    ["module", "extra_code", "cached_code", "entrypoint", "interpreter", "missing_interpreter"],
)
def test_old_files_are_rejected_before_subprocess(tmp_path, monkeypatch, tamper):
    root, manifest, state, interpreter = _synthetic_old_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        upgrade.subprocess, "run", lambda *a, **k: pytest.fail("untrusted old code executed")
    )
    if tamper == "module":
        (root / "closer_campaign.py").write_text("raise SystemExit('substituted')")
    elif tamper == "extra_code":
        (root / "unexpected.py").write_text("raise SystemExit('extra')")
    elif tamper == "cached_code":
        cache = root / "__pycache__"
        cache.mkdir()
        (cache / "closer_campaign.cpython-312.pyc").write_bytes(b"untrusted bytecode")
    elif tamper == "entrypoint":
        doc = json.loads(manifest.read_text())
        doc["module_path"] = str(root / "unexpected.py")
        manifest.write_text(json.dumps(doc))
    elif tamper == "interpreter":
        interpreter = tmp_path / "fake-python"
        interpreter.write_text("unreviewed executable")
    else:
        interpreter = tmp_path / "missing-python"
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._old_diagnostic(manifest, state, "test-worker", interpreter=interpreter)


@pytest.mark.parametrize("contract", [None, [], {"interpreter": None}, {"interpreter": []}])
def test_malformed_old_interpreter_contract_holds(tmp_path, monkeypatch, contract):
    _root, manifest, _state, _interpreter = _synthetic_old_runtime(tmp_path, monkeypatch)
    doc = json.loads(manifest.read_text())
    doc["mechanism_contract"] = contract
    manifest.write_text(json.dumps(doc))
    with pytest.raises(upgrade.SourceUpgradeHeld, match="old interpreter"):
        upgrade._manifest(manifest, old=True)


def test_old_diagnostic_imports_captured_bytes_when_original_is_swapped(tmp_path, monkeypatch):
    root, manifest, state, interpreter = _synthetic_old_runtime(tmp_path, monkeypatch)
    sentinel = tmp_path / "UNTRUSTED_EXECUTED"
    actual_run = upgrade.subprocess.run
    observed = []

    def swap_original(argv, **kwargs):
        stage = Path(argv[-1])
        observed.append(stage)
        assert stat.S_IMODE(stage.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in stage.glob("*.py"))
        (root / "closer_campaign.py").write_text(
            f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n"
            "raise RuntimeError('untrusted original imported')\n"
        )
        result = actual_run(argv, **kwargs)
        assert result.returncode == 0, result.stderr
        return result

    monkeypatch.setattr(upgrade.subprocess, "run", swap_original)
    with pytest.raises(upgrade.SourceUpgradeHeld, match="old manifest module differs"):
        upgrade._old_diagnostic(manifest, state, "test-worker", interpreter=interpreter)
    assert observed
    assert not sentinel.exists()
    assert not observed[0].exists()


def test_old_diagnostic_reads_snapshot_while_parent_holds_real_campaign_lock(tmp_path, monkeypatch):
    """The apply fence and old reader must not deadlock across processes."""
    _root, manifest, state, interpreter = _synthetic_old_runtime(tmp_path, monkeypatch)
    lock_path = state.with_name(state.name + ".lock")
    with lock_path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        answer = upgrade._old_diagnostic(manifest, state, "test-worker", interpreter=interpreter)
    assert answer["state_sha256"] == hashlib.sha256(state.read_bytes()).hexdigest()
    assert answer["result"]["simulation_only"] is True


def test_old_diagnostic_refuses_original_campaign_drift_after_snapshot(tmp_path, monkeypatch):
    _root, manifest, state, interpreter = _synthetic_old_runtime(tmp_path, monkeypatch)
    actual_run = upgrade.subprocess.run

    def change_original(argv, **kwargs):
        state.write_text('{"revision":2}')
        return actual_run(argv, **kwargs)

    monkeypatch.setattr(upgrade.subprocess, "run", change_original)
    with pytest.raises(upgrade.SourceUpgradeHeld, match="old campaign changed"):
        upgrade._old_diagnostic(manifest, state, "test-worker", interpreter=interpreter)
