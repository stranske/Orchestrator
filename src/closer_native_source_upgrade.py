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
from typing import Any, cast

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


def _no_act(correction: dict[str, Any], queue: dict[str, Any]) -> None:
    """Require the precise early owner-criterion branch, not merely a failure."""
    if not isinstance(correction, dict):
        raise SourceUpgradeHeld("source disposition has no correction")
    _need(correction.get("status") == "owned_unknown", "source disposition is not owned_unknown")
    _need(
        correction.get("reason") == "owner source criterion differs from seed",
        "source disposition reason is not the isolated owner criterion",
    )
    for key in ("remote_authenticated", "act_authorized", "source_closure_authorized"):
        _need(correction.get(key) is False, "source disposition permits native ACT")
    if not isinstance(queue, dict):
        raise SourceUpgradeHeld("source ACT queue is not empty")
    _need(queue.get("events") == [], "source ACT queue is not empty")
    accounting = queue.get("accounting")
    _need(
        isinstance(accounting, list) and len(accounting) == 1
        and accounting[0].get("operation_id") is None
        and accounting[0].get("operation_status") is None,
        "source deferred accounting is not sole/null",
    )


def _incomplete_cohort(cohort: dict[str, Any], proof: dict[str, Any]) -> None:
    """Keep retirement's incomplete-cohort refusal intact and separate."""
    _need(
        cohort.get("completed") is False and proof.get("accepted") is False,
        "cohort is completion authority",
    )


def _old_diagnostic(manifest: Path, state: Path, worker_id: str, *, interpreter: Path) -> dict[str, Any]:
    """Run a fixed read-only validator under the old physical interpreter.

    It imports the old pinned modules only after hashing their closure, then
    calls the actual old cohort and completion validators. It never invokes
    native entry or a provider.
    """
    _, raw, doc = _object(manifest, "old manifest")
    modules = doc.get("modules")
    if not isinstance(modules, dict) or not modules:
        raise SourceUpgradeHeld("old manifest module closure missing")
    program = (
        "import hashlib,json,pathlib,sys\n"
        "r=pathlib.Path(sys.argv[1]); s=pathlib.Path(sys.argv[2]); w=sys.argv[3]; m=json.loads(r.read_text()); out={}\n"
        "for n,h in m['modules'].items():\n"
        " p=pathlib.Path(m['module_dir'])/n; b=p.read_bytes();\n"
        " if hashlib.sha256(b).hexdigest()!=h: raise SystemExit(31)\n"
        " out[n]=h\n"
        "sys.path.insert(0,str(pathlib.Path(m['module_dir']))); import closer_campaign as c, closer_native_observer as n, closer_resources as z\n"
        "m=c.load_manifest(r); d=c.Campaign(s).read(); x=next(x for x in d['workers'] if x['worker_id']==w); e,cfg,detail,terminal=c._cohort_worker_evidence(s,d,x,m,[128*1024*1024]); p=n.completed_round_proof(m,cfg,detail['completed_round']); rd=pathlib.Path(p['receipt_path']).read_bytes(); a=json.loads(rd); target=x['target']; row=a['targets'][target]; correction=row['results']['exception']['correction']; queue=row['results']['act']['queue_receipt']; verdict=z.validate_stop(pathlib.Path(str(cfg['dispatch_state'])+'.pipeline.json'),p['round_id'],[target]);\n"
        "if p!=detail['completed_round'] or terminal.get('completed') is not False or p.get('accepted') is not False or verdict.get('valid') is not True or verdict.get('global_stop_valid') is not True: raise SystemExit(32)\n"
        "print(json.dumps({'schema':'old-source-diagnostic/v3','manifest_sha256':hashlib.sha256(r.read_bytes()).hexdigest(),'modules':out,'result':e['result'],'cohort':terminal,'completed_round':p,'round_sha256':hashlib.sha256(rd).hexdigest(),'target':target,'correction':correction,'queue_receipt':queue,'stop':verdict},sort_keys=True,separators=(',',':')))\n"
    )
    try:
        run = subprocess.run(
            [str(interpreter), "-I", "-B", "-c", program, str(manifest), str(state), worker_id],
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
    value, raw, doc = _object(path, "manifest")
    _need(not campaign.validate_manifest(doc) and not campaign.verify_modules(doc), "manifest closure invalid")
    return value, raw, doc


def _completed_reference(manifest_path: Path, state_path: str | Path, worker_id: str) -> dict[str, Any]:
    """Read an actual terminal worker without importing no-start authority."""
    import closer_campaign as campaign

    state = _canonical(state_path)
    _, state_raw, document = _object(state, "old campaign state")
    _need(
        document.get("phase") == "work"
        and document.get("hold") is None
        and document.get("targets") == {campaign.ROUND_UNIT: "done"},
        "old campaign is not terminal exact round state",
    )
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


def _old_custody_quiet(old: dict[str, Any]) -> dict[str, Any]:
    """Physical custody is evidence, not a JSON claim from a caller."""
    import closer_correction_controller as controller

    config = old["policy"]["correction_controller_installation"]["config"]
    try:
        inspection = controller.inspect_provisioned_custody(config)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise SourceUpgradeHeld("old physical custody unavailable") from exc
    _need(not inspection["highwater"]["pending_transactions"], "old custody has pending transaction")
    return inspection


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


def plan(old_manifest: str | Path, old_state: str | Path, worker_id: str,
         new_manifest: str | Path, new_state: str | Path, restore_receipt: str | Path) -> dict[str, Any]:
    """Seal no-ACT old evidence and prepare a new source-owned wrapper CAS."""
    import closer_native_wrapper_install as install

    old_path, old_raw, old = _manifest(old_manifest)
    new_path, new_raw, new = _manifest(new_manifest)
    _need(old.get("candidate_sha") != new.get("candidate_sha"), "source candidate release is not different")
    _same_identity(old, new)
    reference = _completed_reference(old_path, old_state, worker_id)
    _old_native_terminal(old)
    custody = _old_custody_quiet(old)
    interpreter = Path(old["mechanism_contract"]["interpreter"]["path"])
    diagnostic = _old_diagnostic(old_path, Path(reference["state_path"]), worker_id, interpreter=interpreter)
    _need(diagnostic.get("target") == reference["worker"].get("target"), "old diagnostic target differs")
    correction, queue = diagnostic.get("correction"), diagnostic.get("queue_receipt")
    cohort, completed = diagnostic.get("cohort"), diagnostic.get("completed_round")
    if not all(isinstance(value, dict) for value in (correction, queue, cohort, completed)):
        raise SourceUpgradeHeld("old diagnostic shape invalid")
    _no_act(cast(dict[str, Any], correction), cast(dict[str, Any], queue))
    _incomplete_cohort(cast(dict[str, Any], cohort), cast(dict[str, Any], completed))
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
                      "custody": custody, "diagnostic": diagnostic},
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


def prepare_held(seal_path: str | Path, new_worker_config: dict[str, Any], frontier: dict[str, Any], *,
                 confirm_plan_sha256: str) -> dict[str, Any]:
    """After CAS, consume a sealed upgrade once into a fresh held campaign.

    This is deliberately a preparation record only.  It cannot issue typed
    business demand, clear the source initialization hold, or invoke native
    entry; those remain the current source's later admission boundary.
    """
    import closer_campaign as campaign
    import closer_frontier_policy as policy
    import closer_native_observer as native
    import closer_native_readmission as readmission

    seal, seal_raw, record = _object(seal_path, "source upgrade seal")
    _need(record.get("schema") == SCHEMA and record.get("stage") == "sealed_pre_cas", "upgrade seal invalid")
    _need(record.get("plan_sha256") == confirm_plan_sha256, "held confirmation differs from sealed upgrade")
    known = seal.with_name("source-upgrade-known.json")
    _need(known.exists(), "wrapper CAS is not known")
    new_path = _canonical(record["new"]["manifest"])
    destination = _canonical(record["new"]["state"])
    _need(not destination.exists(), "fresh held campaign already exists")
    _, raw, manifest = _manifest(new_path)
    _need(_sha(raw) == record["new"]["manifest_sha256"], "new manifest differs from seal")
    native.contract(manifest)  # current/new contract only, after CAS
    _need(_sha(host._read(record["wrapper"]["path"])[1]) == record["wrapper"]["after_sha256"],
          "current wrapper differs from sealed new pin")
    problems = campaign.qualify(manifest, new_path.parent)
    _need(not problems, "new source qualification failed")
    old_state = _canonical(record["old"]["state"])
    old_result = _canonical(record["old"]["result"])
    _need(_sha(host._read(old_state)[1]) == record["old"]["state_sha256"], "old state changed")
    _need(_sha(host._read(old_result)[1]) == record["old"]["result_sha256"], "old result changed")
    cfg_path, _m, _raw, cfg = readmission._new_runtime(new_path, new_worker_config)
    _need(cfg_path == new_path, "new worker config runtime changed")
    try:
        fresh = policy.validate_frontier(frontier, targets=frontier.get("targets"))
        old_doc = _object(old_state, "old campaign state")[2]
        old_frontier = policy.validate_frontier(old_doc["worker_config"]["finite_frontier"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceUpgradeHeld("fresh held frontier invalid") from exc
    readmission._guard_fresh(manifest, cfg, destination, fresh, old_frontier)
    # Existing source initialization produces a held record; no demand is
    # admitted here. A one-use marker prevents a second preparation.
    marker = seal.with_name("source-upgrade-held-consumed.json")
    _need(not marker.exists(), "sealed source upgrade already consumed")
    with host._hold(readmission._new_lock_rows(manifest)):
        _need(not destination.exists() and not marker.exists(), "fresh held state changed")
        created = readmission._begin_fresh(campaign, destination, manifest, cfg, fresh, old_doc,
                                           confirm_plan_sha256)
        host._write_once(marker, {"schema": SCHEMA, "seal_sha256": _sha(seal_raw),
                                  "state": str(destination), "revision": created["revision"],
                                  "authority": False})
    return {"status": "fresh_held_prepared", "authority": False, "state": str(destination),
            "revision": created["revision"], "marker": str(marker)}
