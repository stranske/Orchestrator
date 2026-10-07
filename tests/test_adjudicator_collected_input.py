"""Actual Git-to-role packet transport preserves bytes without granting acceptance."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import adjudicator_retro as retro
import roles


@pytest.fixture
def saved(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "subject.py").write_text("value = 'évaluated'\n", encoding="utf-8")
    (repo / "proof.txt").write_text("Complete proof " + "λ" * 2400, encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "evaluated")
    sha = git("rev-parse", "HEAD")
    (repo / "subject.py").write_text("value = 'decoy'\n")
    row = {
        "case_id": "one",
        "target": "owner/repo#1",
        "packet": {
            "target": "owner/repo#1",
            "disputed_finding": {
                "body": "Required proof was unavailable",
                "decision": {"evaluated_sha": sha},
            },
            "ground_truth_evidence": {
                "merge_sha": sha,
                "diff_summary": "subject and proof",
                "gate_runs": ["gate-run"],
            },
        },
        "collection_requirements": {
            "source_paths": ["subject.py"],
            "acceptance": [{"criterion": "entire proof", "location": "git-path:proof.txt"}],
        },
    }
    report = tmp_path / "saved.json"
    report.write_text(json.dumps({"rows": [row]}))
    return repo, report, tmp_path / "case.json", row


def prepare(saved, **kwargs):
    repo, report, output, _ = saved
    return retro.prepare_collected_case(report, "one", output, repo, **kwargs)


def test_full_evaluated_bytes_reach_actual_role_prompt_without_writes(saved, monkeypatch):
    repo, report, output, row = saved
    before = report.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("preparation must not dispatch or write Brain")

    monkeypatch.setattr(retro.feedback, "record_role_run", forbidden)
    monkeypatch.setattr(roles, "run_adjudicator_agent", forbidden)
    packet = prepare(saved)
    collection = packet["ground_truth_evidence"]["collected_evidence"]
    source = collection["sources"][0]
    assert source["bytes_utf8"] == "value = 'évaluated'\n"
    assert source["sha256"] == hashlib.sha256(source["bytes_utf8"].encode()).hexdigest()
    proof = collection["acceptance_artifacts"][0]["bytes_utf8"]
    assert proof == "Complete proof " + "λ" * 2400
    prompt = roles.ROLE_REGISTRY["adjudicator"].build_prompt({"case": packet})
    prompt_case = json.loads(
        prompt.split("Adjudication case JSON:\n", 1)[1].split(
            "\n\nAdditional orchestrator context:", 1
        )[0]
    )
    assert (
        prompt_case["ground_truth_evidence"]["collected_evidence"]["acceptance_artifacts"][0][
            "bytes_utf8"
        ]
        == proof
    )
    assert roles.adjudication_metadata_only(packet) is True
    assert packet["metadata_only"] is True
    assert collection["inventory_exhaustiveness"] == "unverified"
    assert collection["acceptance_semantics"] == "unassessed"
    assert report.read_bytes() == before
    assert (repo / "subject.py").read_text() == "value = 'decoy'\n"
    assert json.loads(output.read_text()) == packet
    assert retro.summarize([{"packet": packet, "decision": "reject_blocker"}])["adjudicated"] == 0


@pytest.mark.parametrize(
    "defect",
    [
        "missing-source",
        "missing-proof",
        "empty-acceptance",
        "empty-source",
        "wrong-target",
        "duplicate-case",
    ],
)
def test_invalid_or_incomplete_inventory_never_creates_output(saved, defect):
    repo, report, output, row = saved
    if defect == "missing-source":
        row["collection_requirements"]["source_paths"] = ["absent.py"]
    elif defect == "missing-proof":
        row["collection_requirements"]["acceptance"][0]["location"] = "git-path:absent.txt"
    elif defect == "empty-acceptance":
        row["collection_requirements"]["acceptance"] = []
    elif defect == "empty-source":
        row["collection_requirements"]["source_paths"] = []
    elif defect == "wrong-target":
        row["packet"]["target"] = "other/repo#2"
    report.write_text(json.dumps({"rows": [row, row] if defect == "duplicate-case" else [row]}))
    before = report.read_bytes()
    with pytest.raises(ValueError):
        prepare(saved)
    assert not output.exists()
    assert report.read_bytes() == before


@pytest.mark.parametrize("alias", ["same", "hardlink", "symlink", "existing"])
def test_output_cannot_replace_saved_report_or_existing_receipt(saved, alias):
    repo, report, output, row = saved
    if alias == "same":
        output = report
    elif alias == "hardlink":
        os.link(report, output)
    elif alias == "symlink":
        output.symlink_to(report)
    else:
        output.write_bytes(b"existing receipt")
    before = report.read_bytes()
    with pytest.raises(ValueError):
        retro.prepare_collected_case(report, "one", output, repo)
    assert report.read_bytes() == before


def test_total_utf8_budget_rejects_without_truncating_or_publishing(saved):
    repo, report, output, row = saved
    with pytest.raises(ValueError, match="packet byte limit"):
        prepare(saved, packet_byte_limit=6000)
    assert not output.exists()
    packet = prepare(saved)
    assert len(json.dumps(packet, ensure_ascii=False).encode()) > 6000


def test_real_cli_prepares_case_and_rejects_dispatch(saved):
    repo, report, output, row = saved
    argv = [
        sys.executable,
        str(Path(retro.__file__)),
        "--prepare-collected-case",
        "one",
        "--report",
        str(report),
        "--output",
        str(output),
        "--repository",
        str(repo),
    ]
    result = subprocess.run(argv, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["dispatched"] is False
    assert json.loads(output.read_text())["ground_truth_evidence"]["collected_evidence"]["complete"]
    result = subprocess.run([*argv, "--dispatch"], capture_output=True, text=True)
    assert result.returncode != 0 and "cannot collect-only or dispatch" in result.stderr


def test_packet_budget_includes_unicode_expansion_in_actual_role_rendering(saved):
    repo, report, output, row = saved
    packet = prepare(saved)
    stored_size = output.stat().st_size
    role_size = len(
        json.dumps(roles._compact_adjudication_case(packet), indent=2, sort_keys=True).encode()
    )
    assert role_size > stored_size
    output.unlink()
    with pytest.raises(ValueError, match="packet byte limit"):
        prepare(saved, packet_byte_limit=stored_size + 1)
    assert not output.exists()
