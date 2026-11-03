"""Read the one sealed dd8/fa4 failed-before-pipeline witness.

This is evidence-only: it neither repairs the old campaign nor authorizes a
retry.  The deliberately literal pins prevent it becoming a generic recovery
route for unknown native workers.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import closer_mechanisms as mechanisms

ROOT = Path(
    "/Users/teacher/.codex/orchestrator/runtime-campaigns/csv-ownership-source1031-20261030"
)
CANDIDATE = "dd8d71119b92e15ff85836cc44ad40e20207ac6f"
WORKER = "fa4c00f5e8c6"
NONCE = "1fdc84bcba46ce26fcbbb76bcb990cf3"
MANIFEST_SHA256 = "4f2c671709ac857b61bc5cf5ad313b69b00573401ae2c815c655f1404e1368c6"
STATE_SHA256 = "26fd5230e4485b2f25480aad6482f5075b4bc666ba610632128ef9a2633fa047"
RESULT_SHA256 = "36867fddd0bf1a94afa056976725bc2fedde192a6a4e569d274e605ff898be83"
TERMINAL_SHA256 = "20b107f8a96b755aaa87a71bc817ee75f18580c7a61d8ac99f4aa5fedbeea852"
HANDOFF_SHA256 = "858a3dcdf0ed412fbe329d59b018d3524b228994f2fe4072244aa0b4d2a9a004"
DESCRIPTOR_SHA256 = "cad5fb172e63e1c7ad4b622fe4a33c782402c3fde24070bca974d404ca805ea3"
MODULE_MAP_SHA256 = "4007ea60345427a10b5a00d3e279f4ac6ed764ad0faa8734b620bc76dcafaa94"
DISPATCH_STATE = ROOT / "campaigns/source984-dispatch-state.json"
PYC = {
    "backlog.cpython-312.pyc": "0a1f55a9adc84133f9357976de0208355fb2491eb778f9b114b59145087b647b",
    "claims.cpython-312.pyc": "e1b53bf4fa0f4605fa16f890cdb19dd20d67ee5c15fca653273f96ffdc801107",
    "closer_campaign.cpython-312.pyc": "1ff60ffef28906e2bdf759a8630d95e912463cfb0631ce553d6725948b06706a",
    "closer_dispatcher.cpython-312.pyc": "20d25b585c8556866c05f7ededee940db6ca0dc81a9f3208a1505d46bc5cefca",
    "closer_evidence.cpython-312.pyc": "a647a754a71d4976f600e3f00a538929d8889bb459e3392dbf088dea07fecd1c",
    "closer_frontier_policy.cpython-312.pyc": "c666b084536e962641f5fc87164e9ccbbf3249f0a879edf3dd91655439f41a13",
    "closer_model_contracts.cpython-312.pyc": "dceb88a7dfe52e694df967df90557b4d1c914dc786e91aee5452bf9c0414a1d2",
    "closer_pipeline.cpython-312.pyc": "3cc451c6c63cc9f28081d8f7b8c0768fc227b833dc0483c144fb829ed71b9f3a",
    "closer_queue.cpython-312.pyc": "db0ba49ec163cde8ec7cef785c393a2551beea94845c100a137630e116c25f18",
    "closer_resources.cpython-312.pyc": "fd6571eb916ac5c9e79d4f01be946a04453f79eea3c7489c06c9aabdcb1afe54",
    "closer_runtime_binding.cpython-312.pyc": "a3e590481af9895c50d24c15170bf4306b2bed67a86966d733eea79203b741be",
    "gh_capacity.cpython-312.pyc": "8a61534cf5445282fa3689521372b4e82bd74f30216653c2d143fcb31007872e",
    "verification_events.cpython-312.pyc": "bfe8abe123279c37c213996454e7d1886a1e0adc20efb955d7e43aeb1122f61e",
    "verifier_evidence.cpython-312.pyc": "a86e868de015abee45424f771df3c493275c2583193c66b0d5aa8ffc02c7e14b",
}


def _raw(path: Path, expected: str) -> bytes:
    if path.is_symlink() or path.resolve() != path or not path.is_file():
        raise ValueError("failed predecessor path is unsafe")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("failed predecessor bytes differ")
    return raw


def _dead(pid: int) -> None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise ValueError("failed predecessor process is not dead")


def read_dd8_fa4_failed_before_pipeline(manifest_path, state_path, worker_id) -> dict:
    """Return bounded authenticated facts for only the dd8/fa4 UNKNOWN witness."""
    manifest_path, state_path = Path(manifest_path), Path(state_path)
    if (
        worker_id != WORKER
        or manifest_path != ROOT / "campaigns/pin.manifest.json"
        or state_path != ROOT / "campaigns/public989-readmission.campaign.json"
    ):
        raise ValueError("foreign failed predecessor")
    manifest = json.loads(_raw(manifest_path, MANIFEST_SHA256))
    state = json.loads(_raw(state_path, STATE_SHA256))
    if manifest.get("candidate_sha") != CANDIDATE:
        raise ValueError("failed predecessor candidate differs")
    base = ROOT / "runtime"
    modules = manifest.get("modules")
    if (
        hashlib.sha256(
            json.dumps(modules, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        != MODULE_MAP_SHA256
    ):
        raise ValueError("failed predecessor module map differs")
    if not isinstance(modules, dict) or any(
        hashlib.sha256(_raw(base / n, d)).hexdigest() != d for n, d in modules.items()
    ):
        raise ValueError("failed predecessor Python closure differs")
    cache = base / "__pycache__"
    if {p.name for p in cache.glob("*.pyc")} != set(PYC) or any(
        hashlib.sha256(_raw(cache / n, d)).hexdigest() != d for n, d in PYC.items()
    ):
        raise ValueError("failed predecessor bytecode contamination differs")
    _raw(base / "closer-runtime-package.json", DESCRIPTOR_SHA256)
    executable = {
        str(path.relative_to(base))
        for path in base.rglob("*")
        if path.is_file() and path.suffix in {".py", ".pyc", ".pyo"}
    }
    expected_executable = set(modules) | {"__pycache__/" + name for name in PYC}
    if executable != expected_executable or any(path.is_symlink() for path in base.rglob("*")):
        raise ValueError("failed predecessor executable inventory differs")
    observation = ROOT / "native-observation" / NONCE
    result_path = state_path.with_name(state_path.name + ".results") / (WORKER + ".json")
    result = json.loads(_raw(result_path, RESULT_SHA256))
    terminal = mechanisms.verify_signed_record(
        observation.parent, json.loads(_raw(observation / "terminal.json", TERMINAL_SHA256))
    )
    handoff = mechanisms.verify_signed_record(
        observation.parent,
        json.loads(_raw(observation / ("worker-" + WORKER + ".json"), HANDOFF_SHA256)),
    )
    workers = state.get("workers") or []
    if not (
        state.get("phase") == "work"
        and state.get("targets") == {"*round*": "running"}
        and len(workers) == 1
        and workers[0].get("worker_id") == WORKER
        and workers[0].get("state") == "active"
    ):
        raise ValueError("failed predecessor state was rewritten")
    detail = json.loads(result.get("detail", ""))
    phases = detail.get("native_execution", {}).get("phases", {})
    cleanup = phases.get("C", {}).get("cleanup", {})
    if not (
        result.get("outcome") == "unknown"
        and phases.get("A", {}).get("exit_code") == 0
        and phases.get("B", {}).get("exit_code") == 1
        and phases.get("C", {}).get("exit_code") == 65
        and cleanup.get("released") is True
        and cleanup.get("released_lease_inode") == 567877901
    ):
        raise ValueError("failed predecessor outcome differs")
    if (
        terminal.get("nonce") != NONCE
        or handoff.get("nonce") != NONCE
        or not terminal.get("passed")
    ):
        raise ValueError("failed predecessor signed evidence differs")
    fields = handoff.get("fields", {})
    if (
        terminal.get("binding") != handoff.get("binding")
        or fields.get("result") != str(result_path)
        or fields.get("state") != str(state_path)
        or fields.get("target") != "*round*"
        or fields.get("generation") != "1"
    ):
        raise ValueError("failed predecessor witness binding differs")
    execution = detail.get("native_execution", {})
    if (
        execution.get("admission_nonce") != NONCE
        or execution.get("worker") != fields
        or result.get("worker_id") != WORKER
        or result.get("target") != "*round*"
        or result.get("mechanism") != "native-handoff/v1"
    ):
        raise ValueError("failed predecessor identity differs")
    if not (
        cleanup.get("exact_token_matched") is True
        and cleanup.get("expected_token_sha256") == cleanup.get("meta_sha256")
    ):
        raise ValueError("failed predecessor cleanup differs")
    if state.get("worker_config", {}).get("dispatch_state") != str(DISPATCH_STATE):
        raise ValueError("failed predecessor dispatch state differs")
    pipeline = Path(str(DISPATCH_STATE) + ".pipeline.json")
    scoped = Path(str(pipeline) + ".scoped")
    if any(
        path.exists() or path.is_symlink()
        for path in (pipeline, scoped, observation / ("completed-cohort-" + WORKER + ".json"))
    ):
        raise ValueError("failed predecessor has forbidden pipeline or named receipt")
    pid = workers[0].get("pid")
    if not isinstance(pid, int):
        raise ValueError("failed predecessor worker is invalid")
    _dead(pid)
    for row in (terminal.get("entry"), terminal.get("wrapper")):
        if not isinstance(row, dict) or not isinstance(row.get("pid"), int):
            raise ValueError("failed predecessor native process evidence differs")
        _dead(row["pid"])
    return {
        "schema": "closer-dd8-fa4-failed-predecessor/v1",
        "authority": False,
        "accepted": False,
        "retry_authority": False,
        "manifest": manifest,
        "manifest_path": str(manifest_path),
        "manifest_sha256": MANIFEST_SHA256,
        "state": state,
        "state_path": str(state_path),
        "state_sha256": STATE_SHA256,
        "worker": workers[0],
        "result": result,
        "result_path": str(result_path),
        "result_sha256": RESULT_SHA256,
        "terminal": terminal,
        "terminal_sha256": TERMINAL_SHA256,
        "handoff": handoff,
        "handoff_sha256": HANDOFF_SHA256,
        "outcome": "unknown",
    }
