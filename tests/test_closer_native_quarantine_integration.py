"""Actual named-resource validator consumed by native continuation; no providers."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
import test_closer_quarantine_integration as fixture

import closer_campaign as campaign
import closer_pipeline as pipeline_module
import closer_resources as resources
import closer_runtime_binding as binding


@pytest.mark.parametrize("damage", [None, "unknown", "receipt", "assignment", "lane"])
def test_native_continuation_consumes_real_scoped_validation(tmp_path, damage):
    raw, pipeline, held = fixture.original_state(tmp_path)
    original = {p: p.read_bytes() for p in (raw, pipeline)}
    target = "owner/disjoint#7"
    assignment = tmp_path / "assignment.json"
    fixture.write(assignment, {"targets": [{"target": t} for t in (held[0], target)]})
    candidate = fixture.run(
        pipeline,
        [held[0], target],
        fixture.callbacks(Counter()),
        lambda t, _c: fixture.inventory(t),
    )
    verdict = resources.validate_stop(pipeline, candidate["round_id"], [held[0], target])
    assert verdict["admitted_domain_valid"] is True
    assert verdict["global_stop_valid"] is False
    assert verdict["held_targets"] == [held[0]]
    cfg = {
        "mechanism": "native-handoff/v1",
        "dispatch_state": str(raw),
        "assignment": str(assignment),
        "handoff_dir": str(tmp_path / "handoff"),
        "native_hooks": {
            "postrun_dependencies": {
                "assignment": {
                    "path": str(assignment),
                    "sha256": hashlib.sha256(assignment.read_bytes()).hexdigest(),
                }
            }
        },
    }
    if damage == "unknown":
        state = resources.shard_dir(resources.scoped_root(pipeline), target) / "state.json"
        doc = json.loads(state.read_bytes())
        doc["runs"][candidate["run_ids"][target]]["targets"][target].update(
            status="uncertain", stage="act", deferred_next_trigger="reconcile_before_retry"
        )
        fixture.write(state, doc)
    elif damage == "receipt":
        with open(candidate["receipt_path"], "a") as stream:
            stream.write("\n")
    elif damage == "assignment":
        assignment.write_text("{}")
    elif damage == "lane":
        (tmp_path / "handoff/.lane-closer.lock").mkdir(parents=True)
    proof = binding.scoped_continuation_proof(cfg, candidate)
    if damage is None:
        assert proof["run_ids"] == {target: candidate["run_ids"][target]}
        assert proof["held_targets"] == [held[0]]
        assert proof["global_stop_valid"] is False and proof["accepted"] is False
        assert binding.scoped_continuation_proof(cfg, proof) == proof
    else:
        assert proof is None
    assert {p: p.read_bytes() for p in original} == original


def test_wrapper_exporter_runs_from_flat_package_without_pythonpath(tmp_path):
    root = Path(__file__).resolve().parents[1]
    modules = root / "src" if (root / "src").is_dir() else root
    flat = tmp_path / "flat"
    (flat / "scripts").mkdir(parents=True)
    for module in modules.glob("*.py"):
        shutil.copy2(module, flat / module.name)
    script = flat / "scripts/render_closer_native_entry.py"
    shutil.copy2(root / "scripts" / script.name, script)
    source, manifest, preview = (
        tmp_path / name for name in ("source.sh", "host.json", "preview.sh")
    )
    source.write_text(
        '#!/bin/bash\nLANE="$1"\ncase "$LANE" in closer) ;; esac\n'
        'export PATH="$HOME/.local/bin:$HOME/.cursor/bin:$PATH"\n'
    )
    fixture.write(
        manifest,
        {"runtime": {"python": sys.executable}, "module_path": str(flat / "closer_campaign.py")},
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--source",
            str(source),
            "--manifest",
            str(manifest),
            "--campaign-root",
            str(tmp_path / "campaign"),
            "--out",
            str(preview),
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["installed"] is False
    assert "CLOSER PINNED NATIVE ENTRY v1" in preview.read_text()


def test_native_dispatch_selects_exact_reviewed_scoped_contract_before_action_a(tmp_path):
    pin = pipeline_module.scoped_capability()
    cfg = {
        "assignment": str(tmp_path / "assignment.json"),
        "dispatch_state": str(tmp_path / "queue.json"),
        "apply": True,
        "runtime_manifest": str(tmp_path / "manifest.json"),
        "coordinator_mode": "resource_scoped",
        "coordinator_contract_pin": pin,
    }
    command = campaign.dispatch_command(cfg, sys.executable, tmp_path)
    assert command[command.index("--coordinator-mode") + 1] == "resource_scoped"
    assert json.loads(command[command.index("--coordinator-contract-pin") + 1]) == pin
    for malformed in (None, {**pin, "layout": 999}):
        with pytest.raises(ValueError, match="exact current coordinator pin"):
            campaign.dispatch_command(
                {**cfg, "coordinator_contract_pin": malformed}, sys.executable, tmp_path
            )


@pytest.mark.parametrize("damage", [None, "missing", "callback_started"])
def test_fully_fenced_round_requires_positive_no_callback_proof(tmp_path, damage):
    raw, state, held = fixture.original_state(tmp_path)
    original = {p: p.read_bytes() for p in (raw, state)}
    target = held[0]
    assignment = tmp_path / "assignment.json"
    fixture.write(assignment, {"targets": [{"target": target}]})
    events = Counter()
    candidate = fixture.run(
        state, [target], fixture.callbacks(events), lambda t, _c: fixture.inventory(t)
    )
    assert events == Counter() and candidate["run_ids"] == {}
    cfg = {
        "mechanism": "native-handoff/v1",
        "dispatch_state": str(raw),
        "assignment": str(assignment),
        "handoff_dir": str(tmp_path / "handoff"),
        "native_hooks": {
            "postrun_dependencies": {
                "assignment": {
                    "path": str(assignment),
                    "sha256": hashlib.sha256(assignment.read_bytes()).hexdigest(),
                }
            }
        },
    }
    gate = candidate["targets"][target]["scope_proof"]
    if damage == "missing":
        Path(gate["path"]).unlink()
    elif damage == "callback_started":
        proof = json.loads(Path(gate["path"]).read_bytes())
        proof["callback_started"] = True
        fixture.write(Path(gate["path"]), proof)
        recorded = json.loads(Path(candidate["receipt_path"]).read_bytes())
        recorded["targets"][target]["scope_proof"]["sha256"] = hashlib.sha256(
            Path(gate["path"]).read_bytes()
        ).hexdigest()
        fixture.write(Path(candidate["receipt_path"]), recorded)
        candidate["receipt_sha256"] = hashlib.sha256(
            Path(candidate["receipt_path"]).read_bytes()
        ).hexdigest()
    verdict = resources.validate_stop(state, candidate["round_id"], [target])
    assert verdict["admitted_domain_valid"] is False and verdict["global_stop_valid"] is False
    proof = binding.scoped_continuation_proof(cfg, candidate)
    if damage is None:
        assert verdict["not_started_domain_valid"] is True
        assert proof["domain_outcome"] == "not_started" and proof["accepted"] is False
    else:
        assert verdict["not_started_domain_valid"] is False and proof is None
    assert {p: p.read_bytes() for p in original} == original
