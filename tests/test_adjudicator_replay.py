import copy
import hashlib
import subprocess
from pathlib import Path

import pytest
from test_adjudicator_qualified_input import qualified_input as _qualified_input

import adjudicator_replay as replay
import adjudicator_retro as retro
import roles

RUNNER = "scripts/replay.py"
TESTS = ("alpha", "beta", "gamma")
CASE = "case-478"


@pytest.fixture
def replay_qualified_input(tmp_path):
    return _qualified_input.__wrapped__(tmp_path)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path, runner: str | None = None) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    text = (
        runner
        or """import json\nprint(json.dumps([{\"test\": n, \"runs\": [{\"phase\": \"baseline\", \"exit\": 0, \"output\": \"pass\"}, {\"phase\": \"broken\", \"exit\": 1, \"output\": \"AssertionError\"}, {\"phase\": \"restored\", \"exit\": 0, \"output\": \"pass\"}]} for n in [\"alpha\", \"beta\", \"gamma\"]]))\n"""
    )
    (repo / "scripts").mkdir()
    (repo / RUNNER).write_text(text)
    (repo / "tracked.txt").write_text("unchanged")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    sha = _git(repo, "rev-parse", "HEAD")
    return repo, sha, hashlib.sha256(text.encode()).hexdigest()


def _registry(monkeypatch, digest: str, sha: str):
    monkeypatch.setattr(
        replay,
        "REVIEWED_RUNNERS",
        {RUNNER: replay.ReviewedRunner(RUNNER, digest, TESTS, CASE, sha, ())},
    )


def test_collects_fresh_private_replay_without_mutating_checkout(tmp_path, monkeypatch):
    repo, sha, digest = _repo(tmp_path)
    _registry(monkeypatch, digest, sha)
    token = replay.collect_replay(repo, CASE, sha, RUNNER, digest, TESTS)
    receipt = replay.snapshot_receipt(token)
    assert receipt["evaluated_sha"] == sha
    assert [row["test"] for row in receipt["controls"]] == list(TESTS)
    assert receipt["historical_execution"].startswith("UNKNOWN")
    assert (repo / "tracked.txt").read_text() == "unchanged"
    assert not list(repo.rglob("baseline.xml"))


@pytest.mark.parametrize("change", ["hash", "tests", "path", "sha"])
def test_refuses_unreviewed_caller_expectations(tmp_path, monkeypatch, change):
    repo, sha, digest = _repo(tmp_path)
    _registry(monkeypatch, digest, sha)
    path, supplied, tests, revision = RUNNER, digest, TESTS, sha
    if change == "hash":
        supplied = "0" * 64
    if change == "tests":
        tests = TESTS[:2]
    if change == "path":
        path = "scripts/other.py"
    if change == "sha":
        revision = "0" * 40
    with pytest.raises(replay.ReplayRefused):
        replay.collect_replay(repo, CASE, revision, path, supplied, tests)


@pytest.mark.parametrize(
    "runner",
    [
        "print('not json')\n",
        "import json\nprint(json.dumps([]))\n",
        "import sys\nsys.exit(3)\n",
    ],
)
def test_refuses_malformed_or_incomplete_runner_proof(tmp_path, monkeypatch, runner):
    repo, sha, digest = _repo(tmp_path, runner)
    _registry(monkeypatch, digest, sha)
    with pytest.raises(replay.ReplayRefused):
        replay.collect_replay(repo, CASE, sha, RUNNER, digest, TESTS)


@pytest.mark.parametrize("field", ["stdout", "original_case_id", "controls"])
def test_snapshot_rejects_serialized_or_nested_full_payload_tampering(tmp_path, monkeypatch, field):
    repo, sha, digest = _repo(tmp_path)
    _registry(monkeypatch, digest, sha)
    token = replay.collect_replay(repo, CASE, sha, RUNNER, digest, TESTS)
    with pytest.raises(replay.ReplayRefused):
        replay.snapshot_receipt({})
    if field == "controls":
        token._payload["controls"][0]["runs"][0]["output"] = "substituted"
    else:
        token._payload[field] = "substituted"
    with pytest.raises(replay.ReplayRefused):
        replay.snapshot_receipt(token)


def test_scrubs_ambient_git_directory(tmp_path, monkeypatch):
    repo, sha, digest = _repo(tmp_path)
    _registry(monkeypatch, digest, sha)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "foreign-git"))
    receipt = replay.snapshot_receipt(replay.collect_replay(repo, CASE, sha, RUNNER, digest, TESTS))
    assert receipt["evaluated_sha"] == sha


def test_digest_is_semantic_and_changes_with_proof(tmp_path, monkeypatch):
    repo, sha, digest = _repo(tmp_path)
    _registry(monkeypatch, digest, sha)
    one = replay.snapshot_receipt(replay.collect_replay(repo, CASE, sha, RUNNER, digest, TESTS))
    two = replay.snapshot_receipt(replay.collect_replay(repo, CASE, sha, RUNNER, digest, TESTS))
    assert one["material_digest"] == two["material_digest"]
    changed = dict(one["material_evidence"])
    changed["controls"] = list(changed["controls"])
    changed["controls"][0] = dict(changed["controls"][0])
    changed["controls"][0]["test"] = "substituted"
    assert replay._digest(changed) != one["material_digest"]


def _issued_replay_token(case_id: str, evaluated_sha: str, *, stdout: str, executed_at: float):
    """Issue distinct full packets with one authenticated stable replay core."""
    controls = [
        {
            "test": "control",
            "runs": [
                {"phase": "baseline", "exit": 0, "output": "pass"},
                {"phase": "broken", "exit": 1, "output": "AssertionError"},
                {"phase": "restored", "exit": 0, "output": "pass"},
            ],
        }
    ]
    stable = {
        "schema": "adjudicator-replay-material/v1",
        "original_case_id": case_id,
        "evaluated_sha": evaluated_sha,
        "runner": {"path": "scripts/runner.py", "sha256": "a" * 64},
        "tests": ["control"],
        "controls": replay._stable_controls(controls),
        "historical_execution": "UNKNOWN; fresh replay does not establish native-attempt history",
    }
    payload = {
        "schema": "adjudicator-replay/v1",
        "original_case_id": case_id,
        "evaluated_sha": evaluated_sha,
        "runner": stable["runner"],
        "tests": stable["tests"],
        "controls": controls,
        "stdout": stdout,
        "stderr": "",
        "executed_at": executed_at,
        "checkout": "/private/replay",
        "historical_execution": "UNKNOWN; this is fresh replay evidence, not native-attempt history",
        "material_evidence": stable,
    }
    return replay.ReplayEvidence(
        replay._ISSUER,
        payload,
        replay._digest(stable),
        hashlib.sha256(replay._canonical(payload)).hexdigest(),
    )


def test_qualified_replay_uses_stable_material_not_full_packet_noise(replay_qualified_input):
    repo, row, qualification, _ = replay_qualified_input
    # The replay contributes to the two original acceptance mappings, so this
    # private fixture supplies a second explicit criterion with the same
    # immutable proof location.
    body = "## Acceptance Criteria\n- Entire proof is inspected.\n- Control proof is replayed.\n"
    row = copy.deepcopy(row)
    qualification = copy.deepcopy(qualification)
    row["inventory_provenance"] = {
        "source_criterion_tasks": body,
        "pr_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    qualification["snapshot"]["body"] = body
    qualification["criterion_mapping"].append(
        {
            "criterion": "Control proof is replayed.",
            "evidence_locations": ["git-path:proof.txt"],
            "unknowns": [],
        }
    )
    sha = qualification["snapshot"]["headRefOid"]
    first = _issued_replay_token("one", sha, stdout="first raw output", executed_at=1.0)
    second = _issued_replay_token("one", sha, stdout="second raw output", executed_at=2.0)
    first_receipt, second_receipt = replay.snapshot_receipt(first), replay.snapshot_receipt(second)
    assert first_receipt["receipt_identity"] != second_receipt["receipt_identity"]
    assert first_receipt["material_digest"] == second_receipt["material_digest"]
    first_case = retro.qualify_collected_case(row, repo, qualification, replay_receipt=first)
    second_case = retro.qualify_collected_case(row, repo, qualification, replay_receipt=second)
    assert first_case._digest != second_case._digest
    assert retro._native_substantive_revision(first_case) == retro._native_substantive_revision(
        second_case
    )
    replay_record = roles._collected_snapshot(first_case)["ground_truth_evidence"][
        "collected_evidence"
    ]["execution_replays"][0]
    assert replay_record["historical_execution"].startswith("UNKNOWN")
    first._payload["original_case_id"] = "forged"
    with pytest.raises(replay.ReplayRefused):
        retro.qualify_collected_case(row, repo, qualification, replay_receipt=first)
