#!/usr/bin/env python3
"""Replay the #1216 rollback and identity mutations in a disposable source copy.

Requires pytest in the selected interpreter. Writes complete transcripts and a
receipt only after both intended failures and their restored runs are verified.
The source checkout is never mutated; no worker or provider request is launched.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

TARGET = "src/model_profile_trial.py"
TEST_MODULE = "tests/test_profile_trial_ingest.py"
MUTATIONS = {
    "early-commit": (
        "    if write_feedback:\n        # Session identity is a claim",
        "    if write_feedback:\n"
        "        feedback.record_run(\n"
        "            attempts[0]['run_id'], 'stranske/Orchestrator', 'implementation',\n"
        "            'codex', ts=timestamp,\n"
        "        )\n"
        "        # Session identity is a claim",
        "test_ingest_is_one_transaction_and_a_mid_write_failure_leaves_nothing"
        "[_record_run_in_conn-2]",
        "AssertionError: runs",
    ),
    "identity-gate-removed": (
        "    if not attempts or provider_response_lookup is None:\n        return False\n",
        "    if provider_response_lookup is None:\n        return True\n"
        "    if not attempts:\n        return False\n",
        "test_local_identity_claims_require_an_independent_provider_lookup",
        "DID NOT RAISE",
    ),
}


def mutate(source: str, name: str) -> str:
    anchor, replacement, _, _ = MUTATIONS[name]
    if source.count(anchor) != 1:
        raise ValueError(f"Expected exactly one source anchor for {name}")
    return source.replace(anchor, replacement, 1)


def verify_result(xml: Path, test_id: str, failure: str | None) -> None:
    cases = ET.parse(xml).getroot().findall(".//testcase")
    if len(cases) != 1 or cases[0].get("name") != test_id:
        raise ValueError("Pytest did not execute exactly the requested mutation test")
    case = cases[0]
    if case.find("error") is not None or case.find("skipped") is not None:
        raise ValueError("Setup errors and skips are not mutation evidence")
    failed = case.find("failure")
    if failure is None:
        if failed is not None:
            raise ValueError("Restored test failed")
    elif failed is None or failure not in " ".join(failed.itertext()):
        raise ValueError("Test did not fail at the intended assertion")


def run_pytest(
    checkout: Path,
    output: Path,
    label: str,
    test_id: str,
    python: str,
    failure: str | None = None,
) -> dict:
    xml = output / f"{label}.xml"
    command = [
        python,
        "-m",
        "pytest",
        "-q",
        f"{TEST_MODULE}::{test_id}",
        "-m",
        "not slow",
        "--tb=short",
        f"--junitxml={xml}",
    ]
    # A fresh cache prevents same-size edits in one timestamp tick from reusing
    # the original bytecode. Runtime state is isolated from the operational host.
    with tempfile.TemporaryDirectory(prefix="profile-trial-runtime-") as runtime, (
        output / f"{label}.log"
    ).open("wb") as log:
        result = subprocess.run(
            command,
            cwd=checkout,
            env={
                **os.environ,
                "PYTHONPYCACHEPREFIX": str(Path(runtime) / "bytecode"),
                "ORCH_STATE_DIR": str(Path(runtime) / "state"),
            },
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
    if result.returncode != (1 if failure else 0):
        raise ValueError(f"Unexpected pytest exit status for {label}: {result.returncode}")
    verify_result(xml, test_id, failure)
    return {"command": command, "exit_status": result.returncode, "test_id": test_id}


def capture(repository: Path, output: Path, python: str = sys.executable) -> None:
    repository, output = repository.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Use an empty evidence directory; prior evidence must not be overwritten")
    original = (repository / TARGET).read_bytes()
    source = original.decode("utf-8")
    # Fail before running any tests if current source no longer matches a mutation.
    changed = {name: mutate(source, name).encode("utf-8") for name in MUTATIONS}
    receipt = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": hashlib.sha256(original).hexdigest(),
        "test_sha256": hashlib.sha256((repository / TEST_MODULE).read_bytes()).hexdigest(),
        "mutations": {},
    }
    with tempfile.TemporaryDirectory(prefix="profile-trial-mutations-") as temporary:
        checkout = Path(temporary)
        shutil.copytree(
            repository / "src", checkout / "src", ignore=shutil.ignore_patterns("__pycache__")
        )
        (checkout / "tests").mkdir()
        for relative in (TEST_MODULE, "tests/conftest.py", "pyproject.toml"):
            shutil.copy2(repository / relative, checkout / relative)
        target = checkout / TARGET
        for name, (_, _, test_id, failure) in MUTATIONS.items():
            baseline = run_pytest(checkout, output, f"{name}-baseline", test_id, python)
            patch = "".join(
                difflib.unified_diff(
                    source.splitlines(keepends=True),
                    changed[name].decode("utf-8").splitlines(keepends=True),
                    fromfile=f"a/{TARGET}",
                    tofile=f"b/{TARGET}",
                )
            )
            (output / f"{name}.patch").write_text(patch, encoding="utf-8")
            try:
                target.write_bytes(changed[name])
                failed = run_pytest(checkout, output, f"{name}-failure", test_id, python, failure)
            finally:
                target.write_bytes(original)
            if target.read_bytes() != original:
                raise ValueError("Source restoration failed")
            restored = run_pytest(checkout, output, f"{name}-restored", test_id, python)
            receipt["mutations"][name] = {
                "baseline": baseline,
                "failure": failed,
                "restored": restored,
                "restored_source_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
    if (repository / TARGET).read_bytes() != original:
        raise ValueError("Source checkout changed during capture")
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    capture(args.repository, args.output, args.python)
    print(f"Both mutations and restored tests verified: {args.output / 'receipt.json'}")


if __name__ == "__main__":
    main()
