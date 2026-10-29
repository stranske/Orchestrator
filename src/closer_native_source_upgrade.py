"""Bounded no-native-ACT source upgrade preparation.

This is intentionally *not* a retry or a retirement override.  It recognizes
only a completed, non-accepted source disposition that stopped before source
ownership collection and before a native correction intent could be issued.
The old campaign remains an immutable witness; a different current source may
later create one fresh held campaign after its own qualification.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import closer_host_restore_contract as host

SCHEMA = "closer-native-source-upgrade/v1"


class SourceUpgradeHeld(RuntimeError):
    pass


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _need(value: object, reason: str) -> None:
    if not value:
        raise SourceUpgradeHeld(reason)


def _canonical(path: str | Path) -> Path:
    value = Path(path).absolute()
    _need(not value.is_symlink() and value.resolve(strict=False) == value, "aliased path")
    return value


def _object(path: str | Path, label: str) -> tuple[Path, bytes, dict[str, Any]]:
    try:
        value, raw, doc = host._obj(path)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise SourceUpgradeHeld(label + " unavailable") from exc
    return value, raw, doc


def _exact_ref(value: object, label: str) -> tuple[Path, bytes, dict[str, Any]]:
    try:
        path, doc = host._json_ref(value, label)
        raw = host._read(path)[1]
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise SourceUpgradeHeld(label + " reference invalid") from exc
    return path, raw, doc


def _no_act(disposition: dict[str, Any]) -> None:
    """Require the precise early owner-criterion branch, not merely a failure."""
    correction = disposition.get("correction")
    if not isinstance(correction, dict):
        raise SourceUpgradeHeld("source disposition has no correction")
    _need(correction.get("status") == "owned_unknown", "source disposition is not owned_unknown")
    _need(
        correction.get("reason") == "owner source criterion differs from seed",
        "source disposition reason is not the isolated owner criterion",
    )
    for key in ("remote_authenticated", "act_authorized", "source_closure_authorized"):
        _need(correction.get(key) is False, "source disposition permits native ACT")
    queue = disposition.get("act_queue")
    if not isinstance(queue, dict):
        raise SourceUpgradeHeld("source ACT queue is not empty")
    _need(queue.get("events") == [], "source ACT queue is not empty")
    deferred = queue.get("deferred")
    _need(
        isinstance(deferred, dict)
        and deferred.get("operation_id") is None
        and deferred.get("status") is None,
        "source deferred accounting is not sole/null",
    )


def _incomplete_cohort(cohort: dict[str, Any]) -> None:
    """Keep retirement's incomplete-cohort refusal intact and separate."""
    _need(
        cohort.get("completed") is False and cohort.get("accepted") is False,
        "cohort is completion authority",
    )


def _old_diagnostic(manifest: Path, *, interpreter: Path) -> dict[str, Any]:
    """Run a fixed read-only validator under the old physical interpreter.

    The child only hashes the old source closure and emits a bounded canonical
    diagnostic.  It never imports the current runtime or invokes native entry.
    """
    _, raw, doc = _object(manifest, "old manifest")
    modules = doc.get("modules")
    if not isinstance(modules, dict) or not modules:
        raise SourceUpgradeHeld("old manifest module closure missing")
    program = (
        "import hashlib,json,pathlib,sys\n"
        "r=pathlib.Path(sys.argv[1]); m=json.loads(r.read_text()); out={}\n"
        "for n,h in m['modules'].items():\n"
        " p=r.parent/n; b=p.read_bytes();\n"
        " if hashlib.sha256(b).hexdigest()!=h: raise SystemExit(31)\n"
        " out[n]=h\n"
        "print(json.dumps({'schema':'old-source-diagnostic/v1','manifest_sha256':hashlib.sha256(r.read_bytes()).hexdigest(),'modules':out},sort_keys=True,separators=(',',':')))\n"
    )
    try:
        run = subprocess.run(
            [str(interpreter), "-I", "-B", "-c", program, str(manifest)],
            check=False, capture_output=True, text=True, timeout=10,
        )
        _need(run.returncode == 0 and len(run.stdout) <= 65536, "old source diagnostic failed")
        answer = json.loads(run.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise SourceUpgradeHeld("old source diagnostic unavailable") from exc
    _need(answer.get("manifest_sha256") == _sha(raw) and answer.get("modules") == modules,
          "old source diagnostic differs")
    return answer


def _manifest(path: str | Path) -> tuple[Path, bytes, dict[str, Any]]:
    import closer_campaign as campaign
    import closer_native_observer as native

    value, raw, doc = _object(path, "manifest")
    _need(not campaign.validate_manifest(doc) and not campaign.verify_modules(doc), "manifest closure invalid")
    native.contract(doc)
    return value, raw, doc


def _completed_reference(manifest_path: Path, state_path: str | Path, worker_id: str) -> dict[str, Any]:
    """Read an actual terminal worker without importing no-start authority."""
    import closer_campaign as campaign

    state = _canonical(state_path)
    _, state_raw, document = _object(state, "old campaign state")
    workers = document.get("workers")
    if not isinstance(workers, list):
        raise SourceUpgradeHeld("old campaign worker list missing")
    worker = next((row for row in workers if isinstance(row, dict) and row.get("worker_id") == worker_id), None)
    if not isinstance(worker, dict):
        raise SourceUpgradeHeld("old worker is not terminal")
    _need(worker.get("state") == "terminal", "old worker is not terminal")
    if worker.get("pid"):
        host._dead(worker["pid"])
    result_path = campaign.result_path(state, worker_id)
    _, result_raw, result = _object(result_path, "old result")
    _need(result.get("worker_id") == worker_id and result.get("target") == worker.get("target"),
          "old result does not join terminal worker")
    _need(worker.get("outcome") == result.get("outcome") == "ok", "old completed worker is not exact OK")
    return {"manifest_path": str(manifest_path), "state_path": str(state), "state_sha256": _sha(state_raw),
            "state": document, "worker": worker, "result_path": str(result_path),
            "result_sha256": _sha(result_raw), "result": result}


def _old_native_terminal(manifest: dict[str, Any]) -> None:
    root = _canonical(manifest["mechanism_contract"]["observation_root"])
    _path, _raw, active = _object(root / "native-active.json", "old native active record")
    _need(active.get("status") == "terminal", "old native parent is not terminal")
    for key in ("pid", "process_group"):
        value = active.get(key)
        if value is not None:
            host._dead(value)


def _same_identity(old: dict[str, Any], new: dict[str, Any]) -> None:
    """Pin target/owner/principal/epoch semantics across the source upgrade."""
    old_config = old["policy"]["correction_controller_installation"]["config"]
    new_config = new["policy"]["correction_controller_installation"]["config"]
    for key in ("repository", "repository_id", "epoch", "writer_id", "writer_login", "writer_type",
                "bootstrap_anchor_sha256"):
        _need(old_config.get(key) == new_config.get(key), "source upgrade identity changed: " + key)


def _wrapper_plan(old_manifest: Path, old: dict[str, Any], new_manifest: Path, new: dict[str, Any], receipt: Path) -> dict[str, Any]:
    """Delegate rendering/CAS shape to the reviewed installer, before the CAS."""
    import closer_native_wrapper_install as install

    restored, _ = install._restored_predecessor(receipt)
    base = install._guard_source(install._archived_base(restored))
    wrapper = Path.home() / ".codex/bin/cursor-lane.sh"
    before = host._read(wrapper)[1]
    _need(_sha(before) == old["mechanism_contract"]["wrapper"]["sha256"], "old wrapper pin differs")
    _need(before == install._render(base, old, old_manifest), "old wrapper is not canonical render")
    after = install._render(base, new, new_manifest)
    _need(_sha(after) == new["mechanism_contract"]["wrapper"]["sha256"], "new wrapper pin differs")
    return {"path": str(wrapper), "before_sha256": _sha(before), "after_sha256": _sha(after)}


def plan(
    old_manifest: str | Path, old_state: str | Path, worker_id: str, disposition: str | Path,
    completed_cohort: str | Path, old_round: str | Path, new_manifest: str | Path,
    new_state: str | Path, restore_receipt: str | Path,
) -> dict[str, Any]:
    """Seal no-ACT old evidence and prepare a new source-owned wrapper CAS."""
    import closer_native_wrapper_install as install

    old_path, old_raw, old = _manifest(old_manifest)
    new_path, new_raw, new = _manifest(new_manifest)
    _need(_sha(old_raw) != _sha(new_raw), "source release is not different")
    _same_identity(old, new)
    reference = _completed_reference(old_path, old_state, worker_id)
    _old_native_terminal(old)
    # This route is for completed=False source data, never a no-start proof.
    disp_path, disp_raw, disp = _object(disposition, "source disposition")
    _no_act(disp)
    cohort_path, cohort_raw, cohort = _object(completed_cohort, "completed cohort")
    round_path, round_raw, round_doc = _object(old_round, "old source round")
    _need(cohort.get("worker_id") == worker_id and round_doc.get("target") == reference["worker"].get("target"),
          "old cohort/round do not join worker target")
    _incomplete_cohort(cohort)
    interpreter = Path(old["mechanism_contract"]["interpreter"]["path"])
    diagnostic = _old_diagnostic(old_path, interpreter=interpreter)
    state = _canonical(new_state)
    _need(not state.exists() and state.parent == _canonical(new["mechanism_contract"]["campaign_root"]),
          "new state is not fresh current campaign")
    host._lane_absent()
    receipt = _canonical(restore_receipt)
    wrapper = _wrapper_plan(old_path, old, new_path, new, receipt)
    _restored, predecessor = install._restored_predecessor(receipt)
    rows = install._prospective_rows(predecessor, reference, new, state)
    result = {"schema": SCHEMA, "authority": False, "eligible": True,
              "producer_sha256": _sha(Path(__file__).read_bytes()),
              "old": {"manifest": str(old_path), "manifest_sha256": _sha(old_raw), "state": reference["state_path"],
                      "state_sha256": reference["state_sha256"], "result": reference["result_path"], "result_sha256": reference["result_sha256"],
                      "disposition": str(disp_path), "disposition_sha256": _sha(disp_raw), "cohort": str(cohort_path), "cohort_sha256": _sha(cohort_raw),
                      "round": str(round_path), "round_sha256": _sha(round_raw), "diagnostic": diagnostic},
              "new": {"manifest": str(new_path), "manifest_sha256": _sha(new_raw), "state": str(state)},
              "wrapper": wrapper, "locks": rows,
              "seal": str(Path(new["mechanism_contract"]["observation_root"]) / "source-upgrade-current-disposition.json")}
    result["plan_sha256"] = _sha(json.dumps(result, sort_keys=True, separators=(",", ":")).encode())
    return result


def apply(*args: Any, expected_plan_sha256: str) -> dict[str, Any]:
    """One-way CAS after a sealed intent; it creates no campaign or native work."""
    import closer_native_wrapper_install as install
    first = plan(*args)
    _need(first["plan_sha256"] == expected_plan_sha256, "plan changed")
    with host._hold(first["locks"]):
        second = plan(*args)
        _need(second["plan_sha256"] == first["plan_sha256"], "plan changed under locks")
        seal = Path(first["seal"])
        intent = seal.with_name("source-upgrade-intent.json")
        _need(not intent.exists() and not seal.exists(), "source upgrade already consumed")
        # Both records are immutable, and the seal intentionally precedes CAS:
        # after the wrapper changes, old native.contract would be circular.
        host._write_once(seal, {**first, "stage": "sealed_pre_cas", "at": time.time()})
        host._write_once(intent, {**first, "stage": "intent", "at": time.time()})
        wrapper = Path(first["wrapper"]["path"])
        new_path = Path(first["new"]["manifest"])
        _, new_raw, new = _manifest(new_path)
        _need(_sha(new_raw) == first["new"]["manifest_sha256"], "new manifest changed before CAS")
        _need(_sha(host._read(wrapper)[1]) == first["wrapper"]["before_sha256"], "wrapper CAS preimage changed")
        # Re-read the archived base through the installer helper; no old
        # contract is invoked after this point.
        restored_doc, _ = install._restored_predecessor(Path(args[-1]))
        data = install._render(install._guard_source(install._archived_base(restored_doc)), new, new_path)
        _need(_sha(data) == first["wrapper"]["after_sha256"], "wrapper CAS render changed")
        fd, tmp = tempfile.mkstemp(dir=wrapper.parent, prefix=".source-upgrade-wrapper-")
        replaced = False
        try:
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), 0o755)
                os.fsync(stream.fileno())
            os.replace(tmp, wrapper)
            replaced = True
            directory = os.open(wrapper.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            _need(_sha(host._read(wrapper)[1]) == first["wrapper"]["after_sha256"], "wrapper CAS readback failed")
            # Current contract only: old contract is invalid by design now.
            import closer_native_observer as native
            native.contract(new)
            native._check_wrapper_entry(new, new_path)
            host._write_once(seal.with_name("source-upgrade-known.json"),
                             {**first, "stage": "cas_known", "at": time.time()})
            return {**first, "status": "installed"}
        except Exception as exc:
            if replaced:
                raise SourceUpgradeHeld("post-CAS outcome uncertain: " + type(exc).__name__) from None
            raise
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                Path(tmp).unlink()
            except FileNotFoundError:
                pass
