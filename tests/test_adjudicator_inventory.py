"""Pre-dispatch inventory must preserve source facts without issuing native authority."""

import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest

import adjudicator_retro as retro


@pytest.fixture
def inputs(tmp_path):
    source = tmp_path / "original.json"
    canonical = tmp_path / "canonical.json"
    saved = {
        "population": 299,
        "summary": {"cases": 2, "graded": 0},
        "rows": [
            {
                "case_id": "a",
                "decision": "needs_more_evidence",
                "backend_run_id": "original-paid",
                "retry_attempts": [{"retry_attempts": [{"backend_run_id": "earlier-paid"}]}],
            },
            {"case_id": "b", "decision": "reject_blocker", "backend_run_id": "decided-paid"},
        ],
    }
    source.write_text(json.dumps(saved))
    canonical.write_bytes(source.read_bytes())
    journal = retro.NativeAttemptJournal(tmp_path / "native.sqlite")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    journal.reserve("a", "uncertain-revision", "digest", digest)
    foreign = journal.reserve("foreign", "revision", "digest", "foreign-source")
    journal.persist_raw(
        foreign["attempt_id"], {"run_id": "foreign-paid", "output": "private response"}
    )
    return {"original_report": source, "canonical_path": canonical, "journal_path": journal.path}


def test_inventory_preserves_recursive_paid_history_unknowns_and_foreign_receipts(
    inputs, monkeypatch
):
    def forbidden(*_args, **_kwargs):
        pytest.fail("inventory crossed a mutation or adjudication boundary")

    monkeypatch.setattr(retro.NativeAttemptJournal, "__init__", forbidden)
    monkeypatch.setattr(retro.roles, "run_adjudicator_agent", forbidden)
    monkeypatch.setattr(retro.feedback, "record_role_run", forbidden)
    monkeypatch.setattr(retro, "native_later_truth", forbidden)
    monkeypatch.setattr(retro, "summarize", forbidden)
    before = {key: path.read_bytes() for key, path in inputs.items()}
    result = retro.inventory_native_state(**inputs)
    assert result["original_report"]["case_ids"] == ["a", "b"]
    assert (
        result["original_report"]["sha256"] == hashlib.sha256(before["original_report"]).hexdigest()
    )
    assert result["original_paid_attempts"] == [
        {"case_id": "a", "backend_run_id": "original-paid"},
        {"case_id": "a", "backend_run_id": "earlier-paid"},
        {"case_id": "b", "backend_run_id": "decided-paid"},
    ]
    attempts = {item["case_id"]: item for item in result["native_journal"]["attempts"]}
    assert attempts["a"]["state"] == "in_flight"
    assert attempts["a"]["backend_run_id"] is None
    assert attempts["a"]["matches_original_source"]
    assert attempts["a"]["raw_response_sha256"] is None
    assert not attempts["foreign"]["matches_original_source"]
    assert not attempts["foreign"]["in_original_cohort"]
    assert (
        attempts["foreign"]["raw_response_sha256"]
        == hashlib.sha256(b"private response").hexdigest()
    )
    assert "private response" not in json.dumps(result)
    assert result["paid_identity_summary"]["union_unique"] == 4
    assert result["deployed_code"]["status"] == result["mirror_code"]["status"] == "unavailable"
    assert result["historical_observations"].startswith("unavailable")
    assert not result["dispatch_authorized"]
    assert before == {key: path.read_bytes() for key, path in inputs.items()}


def test_inventory_canonical_claims_are_retained_without_becoming_authenticated_grades(inputs):
    canonical = json.loads(inputs["canonical_path"].read_text())
    canonical["native_publication"] = {
        "source_report_sha256": hashlib.sha256(inputs["original_report"].read_bytes()).hexdigest()
    }
    canonical["summary"]["graded"] = 29
    canonical["rows"][0]["native_projection"] = {"trusted": True, "graded": 1}
    inputs["canonical_path"].write_text(json.dumps(canonical))
    result = retro.inventory_native_state(**inputs)
    assert result["canonical_report"]["retained_summary"]["graded"] == 29
    assert result["canonical_report"]["summary_authority"] == "not_authenticated_by_inventory"
    assert "graded" not in result
    assert not result["dispatch_authorized"]


@pytest.mark.parametrize("change", ["source_hash", "row", "order", "history", "population"])
def test_inventory_refuses_foreign_or_changed_canonical_lineage(inputs, change):
    current = json.loads(inputs["canonical_path"].read_text())
    current["native_publication"] = {
        "source_report_sha256": hashlib.sha256(inputs["original_report"].read_bytes()).hexdigest()
    }
    if change == "source_hash":
        current["native_publication"]["source_report_sha256"] = "foreign"
    elif change == "row":
        current["rows"][0]["decision"] = "reject_blocker"
    elif change == "order":
        current["rows"].reverse()
    elif change == "history":
        current["rows"][0]["retry_attempts"] = []
    else:
        current["population"] += 1
    inputs["canonical_path"].write_text(json.dumps(current))
    with pytest.raises(ValueError, match="foreign|immutable original cohort"):
        retro.inventory_native_state(**inputs)


def test_inventory_does_not_create_or_repair_missing_or_foreign_journal(inputs, tmp_path):
    missing = tmp_path / "absent.sqlite"
    inputs["journal_path"] = missing
    with pytest.raises(sqlite3.OperationalError):
        retro.inventory_native_state(**inputs)
    assert not missing.exists()
    with sqlite3.connect(missing) as conn:
        conn.execute("CREATE TABLE unrelated (value TEXT)")
    before = missing.read_bytes()
    with pytest.raises(ValueError, match="existing native journal"):
        retro.inventory_native_state(**inputs)
    assert missing.read_bytes() == before


def test_inventory_aborts_when_source_changes_during_observation(inputs, monkeypatch):
    def observe(_root):
        inputs["original_report"].write_bytes(b"changed")
        return {"status": "unavailable"}

    monkeypatch.setattr(retro, "_inventory_code", observe)
    with pytest.raises(ValueError, match="inputs changed"):
        retro.inventory_native_state(**inputs)


def test_inventory_aborts_when_journal_changes_during_observation(inputs, monkeypatch):
    journal = retro.NativeAttemptJournal.open_existing(inputs["journal_path"])

    def observe(_root):
        journal.reserve("new-case", "new-revision", "digest", "foreign-source")
        return {"status": "unavailable"}

    monkeypatch.setattr(retro, "_inventory_code", observe)
    with pytest.raises(ValueError, match="inputs changed"):
        retro.inventory_native_state(**inputs)


def test_inventory_retains_repeated_original_paid_identities_across_cases(inputs):
    saved = json.loads(inputs["original_report"].read_text())
    saved["rows"][1]["backend_run_id"] = "original-paid"
    inputs["original_report"].write_text(json.dumps(saved))
    inputs["canonical_path"].write_bytes(inputs["original_report"].read_bytes())
    result = retro.inventory_native_state(**inputs)
    assert result["paid_identity_summary"]["original_records"] == 3
    assert result["paid_identity_summary"]["original_unique"] == 2
    assert [item["backend_run_id"] for item in result["original_paid_attempts"]].count(
        "original-paid"
    ) == 2


def test_inventory_observes_checkout_revision_and_dirty_code(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "src").mkdir()
    code = root / "src/adjudicator_retro.py"
    code.write_text("original bytes")
    for command in (
        ["init", "-q"],
        ["add", "src/adjudicator_retro.py"],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(["git", "-C", str(root), *command], check=True, capture_output=True)
    observed = retro._inventory_code(root)
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert observed["git_revision"] == head
    assert observed["tracked_files_clean"]
    code.write_text("modified bytes")
    observed = retro._inventory_code(root)
    assert observed["git_revision"] == head
    assert not observed["tracked_files_clean"]
    assert observed["adjudicator_sha256"] == hashlib.sha256(b"modified bytes").hexdigest()


def test_inventory_pins_flat_mirror_generation_without_inheriting_ancestor_git(inputs, tmp_path):
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    (deployed / "adjudicator_retro.py").write_text("deployed code")
    generation = tmp_path / "mirror.generation-a"
    generation.mkdir()
    (generation / "adjudicator_retro.py").write_text("mirror code")
    mirror = tmp_path / "mirror"
    mirror.symlink_to(generation, target_is_directory=True)
    result = retro.inventory_native_state(**inputs, deployed_root=deployed, mirror_root=mirror)
    assert (
        result["deployed_code"]["adjudicator_sha256"]
        == hashlib.sha256(b"deployed code").hexdigest()
    )
    assert result["mirror_code"]["adjudicator_sha256"] == hashlib.sha256(b"mirror code").hexdigest()
    assert result["mirror_code"]["resolved_path"] == str(generation)
    assert result["mirror_code"]["git_revision"] is None
    assert result["deployed_code"]["live_reader_binding"] == "unverified"


def test_inventory_cli_prints_only_inventory(inputs, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "adjudicator_retro.py",
            "--inventory",
            "--report",
            str(inputs["original_report"]),
            "--canonical-report",
            str(inputs["canonical_path"]),
            "--native-journal",
            str(inputs["journal_path"]),
        ],
    )
    assert retro.main() == 0
    assert json.loads(capsys.readouterr().out)["original_report"]["case_ids"] == ["a", "b"]


@pytest.mark.parametrize("mode", ["--dispatch", "--retry", "--selftest", "--native-cohort"])
def test_inventory_cli_refuses_dispatch_or_other_modes(mode, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["adjudicator_retro.py", "--inventory", mode])
    with pytest.raises(SystemExit) as exc:
        retro.main()
    assert exc.value.code == 2
    assert "read-only" in capsys.readouterr().err


def test_inventory_cli_requires_explicit_mode_for_inventory_paths(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["adjudicator_retro.py", "--canonical-report", "unused"])
    with pytest.raises(SystemExit) as exc:
        retro.main()
    assert exc.value.code == 2
    assert "require --inventory" in capsys.readouterr().err
