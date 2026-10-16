"""Execute a reviewed acceptance-control runner in a private exact-revision tree.

This is deliberately a producer, not a historical-receipt reader.  A receipt
only exists after this module has exported the reviewed runner from the named
Git commit and executed it.  It says nothing about the historical native
provider attempt; callers must retain that distinction in their qualification.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


class ReplayRefused(ValueError):
    """The requested replay does not match a reviewed, immutable runner."""


@dataclass(frozen=True)
class ReviewedRunner:
    path: str
    sha256: str
    tests: tuple[str, ...]
    original_case_id: str
    evaluated_sha: str
    input_paths: tuple[str, ...]


REVIEWED_RUNNERS: Mapping[str, ReviewedRunner] = {
    "scripts/verify_role_prompt_offering.py": ReviewedRunner(
        path="scripts/verify_role_prompt_offering.py",
        sha256="8006c6d40aac7eef1906eba3ed6a622127730fed6835e9c723308d63367e6f4e",
        tests=(
            "test_research_program_surface_is_declared_and_binds_role_prompt",
            "test_wrong_moment_declines_never_demote",
            "test_a_batch_counts_once_against_the_cycle_cap",
        ),
        original_case_id="44e85dfcdd64335e346a7132a927917e61846910b9ca04e81e346a654c5e88a5",
        evaluated_sha="1264f68b8df9277ea0b4aa1cfe554d1848099789",
        input_paths=(
            "src/capability_advisor.py",
            "src/capability_propensity.py",
            "src/roles.py",
            "tests/test_role_prompt_offering.py",
        ),
    )
}
_ISSUER = object()


@dataclass(frozen=True)
class ReplayEvidence:
    """Opaque, process-local authority to inspect a just-issued receipt."""

    _issuer: object
    _payload: Mapping[str, Any]
    _digest: str
    _receipt_identity: str


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _digest(material_evidence: Mapping[str, Any]) -> str:
    """Stable proof identity, intentionally independent of local execution noise."""
    return hashlib.sha256(_canonical(material_evidence)).hexdigest()


def _git(repository: Path, *args: str, input: bytes | None = None) -> bytes:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(repository), *args],
        input=input,
        capture_output=True,
        timeout=30,
        env=environment,
    )
    if result.returncode:
        raise ReplayRefused(result.stderr.decode("utf-8", "replace").strip() or "git read failed")
    return result.stdout


def _registered(
    original_case_id: str,
    evaluated_sha: str,
    path: str,
    expected_sha256: str,
    expected_tests: Sequence[str],
) -> ReviewedRunner:
    runner = REVIEWED_RUNNERS.get(path)
    if runner is None or runner.path != path:
        raise ReplayRefused("runner is not registered for replay")
    if expected_sha256 != runner.sha256 or tuple(expected_tests) != runner.tests:
        raise ReplayRefused("caller expectations do not match the reviewed runner")
    if original_case_id != runner.original_case_id or evaluated_sha != runner.evaluated_sha:
        raise ReplayRefused("caller subject does not match the reviewed replay binding")
    return runner


def _safe_extract_regular(archive: bytes, destination: Path) -> None:
    """Extract only regular tracked files/directories; reject link traversal."""
    import io

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for member in bundle.getmembers():
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts or not member_path.parts:
                raise ReplayRefused("archive contains an unsafe member path")
            if not (member.isdir() or member.isreg()):
                raise ReplayRefused("archive contains non-regular tracked content")
        for member in bundle.getmembers():
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise ReplayRefused("archive regular member could not be read")
            with target.open("xb") as output:
                output.write(source.read())
            os.chmod(target, member.mode & 0o777)


def _validate_output(stdout: str, tests: tuple[str, ...]) -> list[dict[str, Any]]:
    try:
        rows = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ReplayRefused("reviewed runner did not emit JSON proof") from error
    if not isinstance(rows, list) or len(rows) != len(tests):
        raise ReplayRefused("reviewed runner emitted an incomplete proof")
    by_test: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("test"), str):
            raise ReplayRefused("reviewed runner proof has an invalid row")
        if row["test"] in by_test:
            raise ReplayRefused("reviewed runner proof repeats a named test")
        runs = row.get("runs")
        if not isinstance(runs, list) or len(runs) != 3:
            raise ReplayRefused("reviewed runner proof lacks three control phases")
        expected = (("baseline", 0), ("broken", 1), ("restored", 0))
        for run, (phase, exit_code) in zip(runs, expected):
            if (
                not isinstance(run, dict)
                or run.get("phase") != phase
                or run.get("exit") != exit_code
            ):
                raise ReplayRefused("reviewed runner proof has an invalid control result")
            if not isinstance(run.get("output"), str) or not run["output"]:
                raise ReplayRefused("reviewed runner proof lacks raw child output")
        if "AssertionError" not in runs[1]["output"]:
            raise ReplayRefused("deliberate break did not record an assertion failure")
        by_test[row["test"]] = row
    if tuple(by_test) != tests:
        raise ReplayRefused("reviewed runner proof names an unexpected test set")
    return [by_test[name] for name in tests]


def _stable_controls(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Retain only protocol facts; raw child output stays in the full packet."""
    result = []
    for row in rows:
        result.append(
            {
                "test": row["test"],
                "runs": [
                    {
                        "phase": run["phase"],
                        "exit": run["exit"],
                        "assertion_failure": "AssertionError" in run["output"],
                    }
                    for run in row["runs"]
                ],
            }
        )
    return result


def collect_replay(
    repository: str | Path,
    original_case_id: str,
    evaluated_sha: str,
    runner_path: str,
    expected_runner_sha256: str,
    expected_test_names: Sequence[str],
    *,
    timeout: float = 240,
) -> ReplayEvidence:
    """Run a registered control runner from an isolated archive of *evaluated_sha*.

    The API deliberately rejects arbitrary programs, pre-existing receipts, and
    unsupported runner metadata.  It performs no network operation and never
    writes into the source checkout.
    """
    repo = Path(repository).resolve()
    runner = _registered(
        original_case_id, evaluated_sha, runner_path, expected_runner_sha256, expected_test_names
    )
    if not repo.is_dir() or not (repo / ".git").exists():
        raise ReplayRefused("repository must be a local Git checkout")
    commit = _git(repo, "rev-parse", "--verify", f"{evaluated_sha}^{{commit}}").decode().strip()
    if commit != evaluated_sha:
        raise ReplayRefused("evaluated revision must be a full commit object id")
    runner_bytes = _git(repo, "show", f"{commit}:{runner.path}")
    runner_sha256 = hashlib.sha256(runner_bytes).hexdigest()
    if runner_sha256 != runner.sha256:
        raise ReplayRefused("evaluated revision does not contain the reviewed runner bytes")
    input_hashes = {
        path: hashlib.sha256(_git(repo, "show", f"{commit}:{path}")).hexdigest()
        for path in runner.input_paths
    }
    archive = _git(repo, "archive", "--format=tar", commit)
    with tempfile.TemporaryDirectory(prefix="adjudicator-replay-") as scratch:
        checkout = Path(scratch) / "checkout"
        checkout.mkdir()
        _safe_extract_regular(archive, checkout)
        copied_runner = checkout / runner.path
        if (
            not copied_runner.is_file()
            or hashlib.sha256(copied_runner.read_bytes()).hexdigest() != runner.sha256
        ):
            raise ReplayRefused("isolated checkout did not preserve reviewed runner bytes")
        for path, digest in input_hashes.items():
            copied_input = checkout / path
            if (
                not copied_input.is_file()
                or hashlib.sha256(copied_input.read_bytes()).hexdigest() != digest
            ):
                raise ReplayRefused("isolated checkout did not preserve reviewed runner inputs")
        command = [sys.executable, "-B", str(copied_runner)]
        private_home = checkout / ".private-home"
        private_home.mkdir()
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(private_home),
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        try:
            result = subprocess.run(
                command,
                cwd=checkout,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise ReplayRefused("reviewed runner timed out") from error
        if result.returncode:
            raise ReplayRefused("reviewed runner failed")
        controls = _validate_output(result.stdout, runner.tests)
        material_evidence = {
            "schema": "adjudicator-replay-material/v1",
            "original_case_id": runner.original_case_id,
            "evaluated_sha": commit,
            "runner": {"path": runner.path, "sha256": runner.sha256},
            "input_hashes": input_hashes,
            "tests": list(runner.tests),
            "controls": _stable_controls(controls),
            "historical_execution": "UNKNOWN; fresh replay does not establish native-attempt history",
        }
        payload: dict[str, Any] = {
            "schema": "adjudicator-replay/v1",
            "original_case_id": runner.original_case_id,
            "evaluated_sha": commit,
            "runner": {"path": runner.path, "sha256": runner.sha256},
            "tests": list(runner.tests),
            "controls": controls,
            "command": [sys.executable, "-B", runner.path],
            "stdout": result.stdout,
            "stderr": result.stderr,
            "executed_at": time.time(),
            "checkout": str(checkout),
            "historical_execution": "UNKNOWN; this is fresh replay evidence, not native-attempt history",
            "material_evidence": material_evidence,
        }
        return ReplayEvidence(
            _ISSUER,
            payload,
            _digest(material_evidence),
            hashlib.sha256(_canonical(payload)).hexdigest(),
        )


def snapshot_receipt(token: ReplayEvidence) -> dict[str, Any]:
    """Return a detached snapshot only for an untampered local issued token."""
    if not isinstance(token, ReplayEvidence) or token._issuer is not _ISSUER:
        raise ReplayRefused("receipt was not issued by this replay producer")
    material = token._payload.get("material_evidence")
    if not isinstance(material, Mapping) or _digest(material) != token._digest:
        raise ReplayRefused("issued replay receipt has been mutated")
    if hashlib.sha256(_canonical(token._payload)).hexdigest() != token._receipt_identity:
        raise ReplayRefused("issued replay receipt full payload has been mutated")
    snapshot = json.loads(_canonical(token._payload).decode("utf-8"))
    snapshot["material_digest"] = token._digest
    snapshot["receipt_identity"] = token._receipt_identity
    return snapshot


def _selftest() -> int:
    """Exercise receipt rails without Git, a provider, or an external helper."""
    checks = 0
    tests = ("one", "two", "three")
    rows = [
        {
            "test": name,
            "runs": [
                {"phase": "baseline", "exit": 0, "output": "pass"},
                {"phase": "broken", "exit": 1, "output": "AssertionError: deliberate"},
                {"phase": "restored", "exit": 0, "output": "pass"},
            ],
        }
        for name in tests
    ]
    validated = _validate_output(json.dumps(rows), tests)
    if _stable_controls(validated)[0]["runs"][1]["assertion_failure"] is not True:
        raise RuntimeError("stable control conversion did not retain the deliberate break")
    checks += 1
    malformed = json.loads(json.dumps(rows))
    malformed[0]["runs"].pop()
    try:
        _validate_output(json.dumps(malformed), tests)
    except ReplayRefused:
        checks += 1
    else:
        raise RuntimeError("incomplete proof was accepted")
    no_assertion = json.loads(json.dumps(rows))
    no_assertion[0]["runs"][1]["output"] = "wrong failure"
    try:
        _validate_output(json.dumps(no_assertion), tests)
    except ReplayRefused:
        checks += 1
    else:
        raise RuntimeError("non-assertion deliberate break was accepted")
    material = {"schema": "selftest", "controls": _stable_controls(validated)}
    payload = {"material_evidence": material, "stdout": "raw", "executed_at": 1.0}
    token = ReplayEvidence(
        _ISSUER,
        payload,
        _digest(material),
        hashlib.sha256(_canonical(payload)).hexdigest(),
    )
    if snapshot_receipt(token)["material_digest"] != _digest(material):
        raise RuntimeError("stable material receipt was not preserved")
    checks += 1
    payload["stdout"] = "tampered"
    try:
        snapshot_receipt(token)
    except ReplayRefused:
        checks += 1
    else:
        raise RuntimeError("tampered full packet was accepted")
    print(f"adjudicator_replay selftest: {checks} checks passed")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--selftest"]:
        return _selftest()
    print("usage: adjudicator_replay.py --selftest", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
