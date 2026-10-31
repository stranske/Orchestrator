import hashlib
import json

import pytest

import closer_failed_predecessor_readback as reader


def _fixture(tmp_path, monkeypatch):
    root = tmp_path / "runtime-root"
    runtime = root / "runtime"
    cache = runtime / "__pycache__"
    campaigns = root / "campaigns"
    obs = root / "native-observation" / reader.NONCE
    for p in (cache, campaigns, obs):
        p.mkdir(parents=True, exist_ok=True)
    (runtime / "one.py").write_text("x=1\n")
    (cache / "one.cpython-312.pyc").write_bytes(b"pyc")
    (runtime / "closer-runtime-package.json").write_text("{}")
    manifest = {
        "candidate_sha": reader.CANDIDATE,
        "modules": {"one.py": hashlib.sha256((runtime / "one.py").read_bytes()).hexdigest()},
    }
    state = {
        "phase": "work",
        "targets": {"*round*": "running"},
        "workers": [{"worker_id": reader.WORKER, "state": "active", "pid": 1}],
        "worker_config": {"dispatch_state": str(campaigns / "source984-dispatch-state.json")},
    }
    fields = {
        "result": str(
            campaigns / "public989-readmission.campaign.json.results" / (reader.WORKER + ".json")
        ),
        "state": str(campaigns / "public989-readmission.campaign.json"),
        "target": "*round*",
        "generation": "1",
    }
    binding = {"x": 1}
    detail = {
        "native_execution": {
            "admission_nonce": reader.NONCE,
            "worker": fields,
            "phases": {
                "A": {"exit_code": 0},
                "B": {"exit_code": 1},
                "C": {
                    "exit_code": 65,
                    "cleanup": {
                        "released": True,
                        "released_lease_inode": 567877901,
                        "exact_token_matched": True,
                        "expected_token_sha256": "x",
                        "meta_sha256": "x",
                    },
                },
            },
        }
    }
    result = {
        "worker_id": reader.WORKER,
        "target": "*round*",
        "mechanism": "native-handoff/v1",
        "outcome": "unknown",
        "detail": json.dumps(detail),
    }
    terminal = {
        "nonce": reader.NONCE,
        "passed": True,
        "binding": binding,
        "entry": {"pid": 2},
        "wrapper": {"pid": 3},
    }
    handoff = {"nonce": reader.NONCE, "binding": binding, "fields": fields}
    paths = [
        (campaigns / "pin.manifest.json", manifest),
        (campaigns / "public989-readmission.campaign.json", state),
        (
            campaigns / "public989-readmission.campaign.json.results" / (reader.WORKER + ".json"),
            result,
        ),
        (obs / "terminal.json", {"record": terminal}),
        (obs / ("worker-" + reader.WORKER + ".json"), {"record": handoff}),
    ]
    for p, v in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(v))
    monkeypatch.setattr(reader, "ROOT", root)
    monkeypatch.setattr(reader, "DISPATCH_STATE", campaigns / "source984-dispatch-state.json")
    monkeypatch.setattr(reader, "PYC", {"one.cpython-312.pyc": hashlib.sha256(b"pyc").hexdigest()})
    monkeypatch.setattr(
        reader,
        "MODULE_MAP_SHA256",
        hashlib.sha256(
            json.dumps(manifest["modules"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    monkeypatch.setattr(reader, "DESCRIPTOR_SHA256", hashlib.sha256(b"{}").hexdigest())
    for name, value in [
        ("MANIFEST_SHA256", manifest),
        ("STATE_SHA256", state),
        ("RESULT_SHA256", result),
        ("TERMINAL_SHA256", {"record": terminal}),
        ("HANDOFF_SHA256", {"record": handoff}),
    ]:
        monkeypatch.setattr(reader, name, hashlib.sha256(json.dumps(value).encode()).hexdigest())
    monkeypatch.setattr(
        reader.mechanisms, "verify_signed_record", lambda _root, signed: signed["record"]
    )
    monkeypatch.setattr(reader, "_dead", lambda _pid: None)
    return campaigns / "pin.manifest.json", campaigns / "public989-readmission.campaign.json"


def test_reads_only_the_literal_failed_witness(tmp_path, monkeypatch):
    manifest, state = _fixture(tmp_path, monkeypatch)
    result = reader.read_dd8_fa4_failed_before_pipeline(manifest, state, reader.WORKER)
    assert result["authority"] is False
    assert result["outcome"] == "unknown"


@pytest.mark.parametrize("worker", ["foreign", "5483c1e719d7"])
def test_rejects_foreign_worker(tmp_path, monkeypatch, worker):
    manifest, state = _fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="foreign"):
        reader.read_dd8_fa4_failed_before_pipeline(
            manifest,
            state,
            worker,
        )


def test_rejects_changed_literal_bytecode_inventory(tmp_path, monkeypatch):
    manifest, state = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(reader, "PYC", {"foreign.pyc": "0" * 64})
    with pytest.raises(ValueError, match="bytecode"):
        reader.read_dd8_fa4_failed_before_pipeline(
            manifest,
            state,
            reader.WORKER,
        )


@pytest.mark.parametrize("suffix", [".pipeline.json", ".pipeline.json.scoped"])
def test_rejects_actual_dispatch_pipeline_artifact(tmp_path, monkeypatch, suffix):
    manifest, state = _fixture(tmp_path, monkeypatch)
    path = reader.DISPATCH_STATE.with_name(reader.DISPATCH_STATE.name + suffix)
    path.mkdir() if suffix.endswith("scoped") else path.write_text("{}")
    with pytest.raises(ValueError, match="forbidden pipeline"):
        reader.read_dd8_fa4_failed_before_pipeline(manifest, state, reader.WORKER)


def test_rejects_rehashed_identity_mismatch(tmp_path, monkeypatch):
    manifest, state = _fixture(tmp_path, monkeypatch)
    result_path = state.with_name(state.name + ".results") / (reader.WORKER + ".json")
    result = json.loads(result_path.read_text())
    result["mechanism"] = "foreign"
    raw = json.dumps(result).encode()
    result_path.write_bytes(raw)
    monkeypatch.setattr(reader, "RESULT_SHA256", hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="identity"):
        reader.read_dd8_fa4_failed_before_pipeline(manifest, state, reader.WORKER)
