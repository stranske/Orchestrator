import copy
import hashlib
import json
import subprocess
import sys
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


def test_native_publish_cli_dispatches_manifest_replay_to_opaque_qualification(
    replay_qualified_input, tmp_path, monkeypatch
):
    """A replay-bearing manifest must not reach publication without its token."""
    repo, row, qualification, _ = replay_qualified_input
    sha = qualification["snapshot"]["headRefOid"]
    runner_path = "scripts/private-runner.py"
    runner = replay.ReviewedRunner(runner_path, "b" * 64, ("control",), "one", sha, ())
    monkeypatch.setattr(replay, "REVIEWED_RUNNERS", {runner_path: runner})
    token = _issued_replay_token("one", sha, stdout="cli proof", executed_at=3.0)
    observed = {}

    def collect(repository, case_id, evaluated_sha, path, expected_hash, expected_tests, **_kwargs):
        observed["collect"] = (
            Path(repository),
            case_id,
            evaluated_sha,
            path,
            expected_hash,
            expected_tests,
        )
        return token

    def qualify(candidate, repository, candidate_qualification, **kwargs):
        observed["receipt"] = kwargs.get("replay_receipt")
        assert candidate == row
        assert Path(repository) == repo
        assert candidate_qualification == qualification
        assert observed["receipt"] is token  # RED if the CLI omits replay dispatch.
        return "issued-case"

    def publish(**kwargs):
        assert kwargs["cases"] == {"one": "issued-case"}
        return {"native_summary": {"graded": 1, "unknown": 0}}

    monkeypatch.setattr(replay, "collect_replay", collect)
    monkeypatch.setattr(retro, "qualify_collected_case", qualify)
    monkeypatch.setattr(retro, "publish_native_cohort", publish)
    monkeypatch.setattr(retro.NativeAttemptJournal, "open_existing", lambda _path: object())
    report, output, journal, canonical, manifest = (
        tmp_path / name
        for name in (
            "original.json",
            "output.json",
            "journal.sqlite",
            "canonical.json",
            "manifest.json",
        )
    )
    report.write_text(json.dumps({"rows": [row]}))
    output.write_text("{}")
    journal.write_text("fixture")
    canonical.write_text("{}")
    qualification_path = tmp_path / "qualification.json"
    enriched_path = tmp_path / "enriched.json"
    qualification_path.write_text(json.dumps(qualification))
    enriched_path.write_text(json.dumps({"rows": [row]}))
    manifest.write_text(
        json.dumps(
            [
                {
                    "case_id": "one",
                    "input_report": enriched_path.name,
                    "repository": str(repo),
                    "qualification": qualification_path.name,
                    "replay_runner": runner_path,
                }
            ]
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "adjudicator_retro",
            "--native-publish",
            str(canonical),
            "--report",
            str(report),
            "--output",
            str(output),
            "--native-journal",
            str(journal),
            "--expected-source-hash",
            hashlib.sha256(report.read_bytes()).hexdigest(),
            "--expected-canonical-hash",
            hashlib.sha256(canonical.read_bytes()).hexdigest(),
            "--cohort-manifest",
            str(manifest),
        ],
    )
    assert retro.main() == 0
    assert observed["collect"] == (repo, "one", sha, runner_path, runner.sha256, runner.tests)
    assert observed["receipt"] is token


def test_real_replay_receipt_survives_journal_to_cli_publication(
    replay_qualified_input, tmp_path, monkeypatch
):
    """Run real collector, replay child, journal validation and CLI publisher."""
    from test_adjudicator_native_publication import _validated_native

    repo, row, qualification, git = replay_qualified_input
    base = git("rev-parse", "HEAD")
    runner_path = "scripts/private-replay.py"
    text = """import json
print(json.dumps([{"test": "control", "runs": [
    {"phase": "baseline", "exit": 0, "output": "pass"},
    {"phase": "broken", "exit": 1, "output": "AssertionError"},
    {"phase": "restored", "exit": 0, "output": "pass"}]}]))
"""
    (repo / "scripts").mkdir()
    (repo / runner_path).write_text(text)
    (repo / "proof.txt").write_text("complete private proof")
    git("add", ".")
    git("commit", "-qm", "private replay evaluated commit")
    sha = git("rev-parse", "HEAD")
    row = copy.deepcopy(row)
    row.update(
        run_id="original-private-run",
        decision="needs_more_evidence",
        retry_attempts=[{"backend_run_id": "original-paid"}],
    )
    row["packet"]["disputed_finding"]["decision"]["evaluated_sha"] = sha
    row["packet"]["ground_truth_evidence"].update(
        head_sha=sha,
        merge_sha=sha,
        diff_summary=[{"path": runner_path}, {"path": "proof.txt"}],
    )
    row["collection_requirements"]["source_paths"] = [runner_path, "proof.txt"]
    body = "## Acceptance Criteria\n- Entire proof is inspected.\n- Control proof is replayed.\n"
    row["inventory_provenance"] = {
        "source_criterion_tasks": body,
        "pr_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    qualification = copy.deepcopy(qualification)
    qualification.update(
        comparison_base=base, snapshot={"number": 1, "headRefOid": sha, "body": body}
    )
    qualification["criterion_mapping"].append(
        {
            "criterion": "Control proof is replayed.",
            "evidence_locations": ["git-path:proof.txt"],
            "unknowns": [],
        }
    )
    runner = replay.ReviewedRunner(
        runner_path, hashlib.sha256(text.encode()).hexdigest(), ("control",), "one", sha, ()
    )
    monkeypatch.setattr(replay, "REVIEWED_RUNNERS", {runner_path: runner})
    token = replay.collect_replay(repo, "one", sha, runner_path, runner.sha256, runner.tests)
    case = retro.qualify_collected_case(row, repo, qualification, replay_receipt=token)
    original, canonical = tmp_path / "original.json", tmp_path / "canonical.json"
    original.write_text(json.dumps({"rows": [row], "summary": {"graded": 0}, "population": 299}))
    canonical.write_bytes(original.read_bytes())
    journal = retro.NativeAttemptJournal(tmp_path / "journal.sqlite")
    _validated_native(journal, original, case, "private-native-paid")
    # Only external later-truth/accounting reads are private fixture boundaries.
    # No provider, production Brain or live report is touched.
    monkeypatch.setattr(retro, "native_later_truth", lambda *_args: "PASS")
    monkeypatch.setattr(
        retro.feedback,
        "role_cost_report",
        lambda *_args, **_kwargs: {"status": "unmeasured", "cost_usd": None},
    )
    qualification_path = tmp_path / "qualification.json"
    qualification_path.write_text(json.dumps(qualification))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "case_id": "one",
                    "input_report": original.name,
                    "repository": str(repo),
                    "qualification": qualification_path.name,
                    "replay_runner": runner_path,
                }
            ]
        )
    )
    source_bytes = original.read_bytes()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "adjudicator_retro",
            "--native-publish",
            str(canonical),
            "--report",
            str(original),
            "--output",
            str(tmp_path / "unused-projection.json"),
            "--native-journal",
            str(journal.path),
            "--cohort-manifest",
            str(manifest),
            "--expected-source-hash",
            hashlib.sha256(source_bytes).hexdigest(),
            "--expected-canonical-hash",
            hashlib.sha256(canonical.read_bytes()).hexdigest(),
        ],
    )
    assert retro.main() == 0
    published = json.loads(canonical.read_text())
    assert published["summary"]["graded"] == 1
    assert published["rows"][0]["native_projection"]["trusted"] is True
    assert published["native_publication"]["paid_backend_ids"] == [
        "original-paid",
        "private-native-paid",
    ]
    assert original.read_bytes() == source_bytes
