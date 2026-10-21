"""Historical feature maturity stays readable without inventing activation."""

import hashlib
import json
import socket
import subprocess

import pytest

import capabilities
import feature_scan
import features


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("legacy registry test attempted a process or network call")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(capabilities, "KNOWN_GATES", {})
    monkeypatch.setattr(capabilities, "declared_row_ids", lambda: set())
    monkeypatch.setattr(capabilities, "summary", lambda **kwargs: {})
    monkeypatch.setattr(feature_scan, "_capability_heartbeat", lambda: None)


@pytest.mark.parametrize("maturity", ["new", "ad-hoc", "reused", "hardened"])
def test_paired_readers_and_both_migrations_preserve_source_and_legacy_evidence(
    tmp_path, monkeypatch, maturity
):
    source = tmp_path / "features.json"
    raw = {"operator-feature": {"maturity": maturity, "module": "operator.py", "note": "keep"}}
    source.write_text(json.dumps(raw))
    before = source.read_bytes()
    parsed, evidence = capabilities._feature_snapshot(source)
    assert features.load(source) == parsed == raw
    assert (
        evidence["source_json_digest"]
        == hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    explicit = capabilities.migrate_features_to_capabilities(
        source, tmp_path / "explicit-ledger.json", now=100
    )["capabilities"]
    monkeypatch.setattr(capabilities, "FEATURES_REG", source)
    initial = capabilities.load(tmp_path / "initial-ledger.json")
    for records in (explicit, initial):
        record = records["operator-feature"]
        assert record["status"] == ("generated" if maturity == "hardened" else "observed")
        event = record["event_history"][0]
        assert event["legacy_maturity"] == maturity
        assert event["source_json_digest"] == evidence["source_json_digest"]
        assert event["activation_inferred"] is False
        assert record["last_invocation"] is None
    assert source.read_bytes() == before


def test_legacy_minimal_rows_report_scan_and_list_without_rewriting(tmp_path, monkeypatch, capsys):
    path = tmp_path / "features.json"
    raw = {"operator-feature": {"maturity": "new", "module": "operator.py"}}
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    assert features.summary(path)["counts_by_maturity"]["ad-hoc"] == 1
    assert features.promotion_candidates(path) == []
    assert feature_scan.scan(path=path, root=tmp_path)["registry_entries"] == 1
    original_load = features.load
    monkeypatch.setattr(features, "load", lambda: original_load(path))
    assert features.main(["list"]) == 0
    assert "operator-feature" in capsys.readouterr().out
    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".lock").exists()


def test_new_is_read_compatibility_only_and_transaction_conserves_operator_fields(tmp_path):
    path = tmp_path / "features.json"
    path.write_text(json.dumps({"operator-feature": {"maturity": "new", "note": "keep"}}))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="invalid maturity"):
        features.record_use("other", "task", path=path, maturity="new")
    assert path.read_bytes() == before
    first = features.record_use("operator-feature", "first-task", path=path)
    assert first["maturity"] == "new" and first["note"] == "keep"
    second = features.record_use("operator-feature", "second-task", path=path)
    assert second["maturity"] == "reused" and second["note"] == "keep"
    assert second["uses"] == ["first-task", "second-task"]


@pytest.mark.parametrize("maturity", ["active", "NEW", "prototype", None, [], {}])
def test_unsupported_maturity_never_creates_a_ledger_or_rewrites_source(tmp_path, maturity):
    source = tmp_path / "features.json"
    source.write_text(json.dumps({"operator-feature": {"maturity": maturity}}))
    before = source.read_bytes()
    ledger = tmp_path / "capabilities.json"
    with pytest.raises(ValueError, match="unsupported feature maturity"):
        features.load(source)
    with pytest.raises(ValueError, match="unsupported feature maturity"):
        capabilities.migrate_features_to_capabilities(source, ledger, now=100)
    assert source.read_bytes() == before
    assert not ledger.exists()
    assert not source.with_name(source.name + ".lock").exists()
