"""Bounded no-native-ACT source upgrade preparation.

This is intentionally *not* a retry or a retirement override.  It recognizes
only a completed, non-accepted source disposition that stopped before source
ownership collection and before a native correction intent could be issued.
The old campaign remains an immutable witness; a different current source may
later create one fresh held campaign after its own qualification.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast

import closer_host_restore_contract as host

SCHEMA = "closer-native-source-upgrade/v1"
# This route is deliberately scoped to the reviewed predecessor whose owner
# criterion stopped before Native ACT. A manifest that merely self-describes a
# matching shape is not enough to establish that control-flow ordering.
REVIEWED_OLD_CANDIDATE = "f858dc3857ad7a90461126abd945edc0d380d9b3"
# Canonical SHA-256 of the sorted 191-entry module map reviewed from f858.
# This is checked before an old module is imported; the old validator is only
# a second, version-aware validation fence.
REVIEWED_OLD_MODULES_SHA256 = "c5dea1b0710044385caa0f0baf0490b7a61aa49b10b49496b03dd2f258371add"
REVIEWED_OLD_DESCRIPTOR_SHA256 = "019d7bc8010dd5e921d194ad6e073d5ab90ab13299996c82463a5b0356a63ea6"
REVIEWED_OLD_INTERPRETER_SHA256 = "d01b09973b5bd3ff617e7849ce9c0bdb8991256f495ce190720ccc976cdaaee7"
# A second reviewed predecessor stopped in the generic exception router before
# selecting a provider. Its signed round is a capacity disposition, not the
# earlier owner-criterion disposition and never a no-B/accepted result.
REVIEWED_CAPACITY_CANDIDATE = "4e8ae9de575bc1108498933fb0cd0ee495a42aa8"
REVIEWED_CAPACITY_MODULES_SHA256 = (
    "2fed5eaa7da160068d4655cb4358cfa8459719d29be8a17d7564c5e7f77db041"
)
REVIEWED_CAPACITY_DESCRIPTOR_SHA256 = (
    "d785039a57ef32c7ea7e2f72f349802b75658ebba1b40917c856c9ab5a3c1f58"
)
# The source17be CSV canary stopped after its bound STRONG adjudication but
# before ownership collection.  It is deliberately a separate literal branch:
# it is neither the generic f858 owner-criterion stop nor the 4e8 capacity
# stop, and it must never turn an arbitrary owned_unknown/OK result into a
# source-upgrade candidate.
REVIEWED_CLOCK_CANDIDATE = "17be674425c41f619f38325c4266c5983366094f"
REVIEWED_CLOCK_MODULES_SHA256 = "a74ae76eb426482d528e695926d12a53311081925e75c939181fdbdb20508791"
REVIEWED_CLOCK_DESCRIPTOR_SHA256 = "8deaf7d76cd0965f44f031521121d03f7f104147b99fc34bdbe0f71cc7fef5b2"
# Deliberately literal: the inspector is executed before any installed-release
# fact can be trusted.  This is the clean reviewed f858 source file, not a
# digest supplied by a release journal or the inspected generation.
REVIEWED_INSTALL_VERIFIED_SNAPSHOT_SHA256 = (
    "98775accf1ce81aead5c99eec4bf7e3e802774b2397440dd356bf9ff7649c966"
)
_SEAL_NAME = "source-upgrade-current-disposition.json"
_INTENT_NAME = "source-upgrade-intent.json"
_KNOWN_NAME = "source-upgrade-known.json"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class SourceUpgradeHeld(RuntimeError):
    pass


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _plan_sha(plan: dict[str, Any]) -> str:
    body = {
        key: value
        for key, value in plan.items()
        if key not in {"plan_sha256", "stage", "status", "at"}
    }
    return _sha(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())


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


def _pinned_object(
    path: str | Path, sha256: object, label: str
) -> tuple[Path, bytes, dict[str, Any]]:
    """Parse an old witness only after binding the exact bytes first."""
    value, raw, doc = _object(path, label)
    _need(isinstance(sha256, str) and _sha(raw) == sha256, label + " changed")
    return value, raw, doc


def _source_upgrade_root(manifest: dict[str, Any]) -> Path:
    import closer_mechanisms as mechanisms

    try:
        # This is the mechanism's owned 0700 root check, not merely a
        # canonical-path check. It must run before any key read or creation.
        root = mechanisms._root(manifest)
    except (KeyError, TypeError, OSError, ValueError, mechanisms.MechanismError) as exc:
        raise SourceUpgradeHeld("current source observation root unavailable") from exc
    return _canonical(root)


def _upgrade_path(manifest: dict[str, Any], name: str) -> Path:
    root = _source_upgrade_root(manifest)
    path = _canonical(root / name)
    _need(path.parent == root and path.name == name, "source upgrade record path differs")
    return path


def _signed_upgrade_record(manifest: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Sign a source-owned disposition with the existing observer key domain."""
    import closer_mechanisms as mechanisms

    root = _source_upgrade_root(manifest)
    try:
        # Deliberately never create a key: installation/qualification owns it.
        key = mechanisms._key(root)
    except (OSError, mechanisms.MechanismError) as exc:
        raise SourceUpgradeHeld("current source signing key unavailable") from exc
    return {"record": record, "signature": mechanisms._signature(record, key)}


def _provision_upgrade_key(manifest: dict[str, Any]) -> None:
    """Provision the current observer key only for a brand-new sealed route.

    This is source-owned installation bookkeeping, not an admission, native
    entry, or business authority.  Recovery never calls this helper: it must
    authenticate the original signed records with the key that already exists.
    """
    import closer_mechanisms as mechanisms

    try:
        mechanisms._key(_source_upgrade_root(manifest), create=True)
    except (OSError, mechanisms.MechanismError) as exc:
        raise SourceUpgradeHeld("current source signing key unavailable") from exc


def _read_signed_upgrade(
    path: str | Path,
    manifest: dict[str, Any],
    *,
    name: str,
    label: str,
    manifest_sha256: str,
) -> tuple[Path, bytes, dict[str, Any]]:
    """Read only a canonical, current-source signed upgrade record."""
    import closer_mechanisms as mechanisms

    expected = _upgrade_path(manifest, name)
    value, raw, envelope = _object(path, label)
    _need(value == expected, label + " path differs")
    try:
        record = mechanisms.verify_signed_record(_source_upgrade_root(manifest), envelope)
    except (OSError, ValueError, KeyError, TypeError, mechanisms.MechanismError) as exc:
        raise SourceUpgradeHeld(label + " signature differs") from exc
    producer = _sha(Path(__file__).read_bytes())
    _need(
        record.get("producer_sha256") == producer
        and manifest.get("modules", {}).get(Path(__file__).name) == producer,
        label + " producer differs",
    )
    new = record.get("new")
    _need(
        isinstance(new, dict) and new.get("manifest_sha256") == manifest_sha256,
        label + " manifest differs",
    )
    return value, raw, record


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
        isinstance(accounting, list)
        and len(accounting) == 1
        and isinstance(accounting[0], dict)
        and accounting[0].get("operation_id") is None
        and accounting[0].get("operation_status") is None,
        "source deferred accounting is not sole/null",
    )


def _clock_no_act(correction: dict[str, Any], queue: dict[str, Any], *, strong_as_of: object) -> None:
    """Accept only the reviewed source17be pre-ACT clock refusal."""
    _need(isinstance(correction, dict), "clock disposition has no correction")
    _need(
        correction == {
            "status": "owned_unknown",
            "reason": "native observation clock is not current",
            "remote_authenticated": False,
            "retry_safe": False,
            "act_authorized": False,
            "source_closure_authorized": False,
        },
        "clock disposition is not the isolated ownership observation stop",
    )
    _need(isinstance(queue, dict) and queue.get("events") == [], "clock ACT queue is not empty")
    _need(
        strong_as_of is True,
        "clock disposition lacks successful bound STRONG historical qualification",
    )
    accounting = queue.get("accounting")
    _need(
        isinstance(accounting, list)
        and len(accounting) == 1
        and isinstance(accounting[0], dict)
        and accounting[0].get("reason") == "observation_failed"
        and accounting[0].get("next_trigger") == "retry_observation_next_round"
        and accounting[0].get("operation_id") is None
        and accounting[0].get("operation_status") is None,
        "clock deferred accounting is not sole/null observation retry",
    )


def _incomplete_cohort(cohort: dict[str, Any], proof: dict[str, Any]) -> None:
    """Keep retirement's incomplete-cohort refusal intact and separate."""
    _need(
        cohort.get("completed") is False and proof.get("accepted") is False,
        "cohort is completion authority",
    )


def _capacity_no_act(value: object) -> None:
    """Recognize only the reviewed pre-route exception-capacity branch."""
    _need(isinstance(value, dict), "capacity disposition unavailable")
    row = cast(dict[str, Any], value)
    _need(
        row.get("row_status") == "capacity_blocked"
        and row.get("row_error") == "worker_capacity"
        and row.get("row_next_trigger") == "fresh shared-pool capacity snapshot"
        and row.get("result_stages") == ["exception", "interpret", "prepare"]
        and row.get("native_effect_contract") is None,
        "capacity disposition is not the isolated pre-route branch",
    )
    exception = row.get("exception")
    _need(isinstance(exception, dict), "capacity exception unavailable")
    exception = cast(dict[str, Any], exception)
    evidence = exception.get("resource_evidence")
    _need(isinstance(evidence, dict), "capacity evidence unavailable")
    capacity = cast(dict[str, Any], evidence).get("capacity")
    _need(isinstance(capacity, dict), "capacity evidence unavailable")
    capacity = cast(dict[str, Any], capacity)
    sampled = capacity.get("sampled_at")
    _need(
        set(exception) == {"status", "reason", "next_trigger", "resource_evidence"}
        and exception.get("status") == "deferred"
        and exception.get("reason") == "worker_capacity"
        and exception.get("next_trigger") == "fresh shared-pool capacity snapshot"
        and evidence == {"capacity": capacity}
        and set(capacity) == {"lane", "available", "required", "sampled_at"}
        and capacity.get("lane") == "exception"
        and type(capacity.get("available")) is int
        and capacity.get("available") == 0
        and type(capacity.get("required")) is int
        and capacity.get("required") == 1
        and isinstance(sampled, (int, float))
        and not isinstance(sampled, bool)
        and 0 < sampled < float("inf"),
        "capacity exception contains effects or a different stop",
    )
    interpretation = row.get("interpretation")
    _need(isinstance(interpretation, dict), "capacity interpretation unavailable")
    interpretation = cast(dict[str, Any], interpretation)
    actual = interpretation.get("actual")
    _need(
        isinstance(actual, dict)
        and set(actual) == {"kind", "provider_generation_performed", "host_intake_ref"}
        and actual.get("kind") == "source_native_conservative_inventory"
        and actual.get("provider_generation_performed") is False,
        "capacity disposition has prior provider work",
    )
    reference = cast(dict[str, Any], actual).get("host_intake_ref")
    _need(
        isinstance(reference, dict)
        and set(reference) == {"path", "sha256", "bytes"}
        and isinstance(reference.get("path"), str)
        and Path(reference["path"]).is_absolute()
        and isinstance(reference.get("sha256"), str)
        and _DIGEST.fullmatch(reference["sha256"])
        and type(reference.get("bytes")) is int
        and 0 < reference["bytes"] <= 128 * 1024,
        "capacity source intake reference invalid",
    )
    _need(
        interpretation
        == {
            "classification": "source_native_conservative_inventory",
            "authority": False,
            "actual": actual,
            "requested_agent": None,
            "requested_profile": None,
        },
        "capacity disposition has prior provider work",
    )


def _diagnostic_no_act(candidate: str, diagnostic: dict[str, Any]) -> None:
    if candidate == REVIEWED_CAPACITY_CANDIDATE:
        _capacity_no_act(diagnostic.get("capacity_disposition"))
    else:
        _need(
            candidate in {REVIEWED_OLD_CANDIDATE, REVIEWED_CLOCK_CANDIDATE},
            "unreviewed no-ACT predecessor",
        )
        correction, queue = diagnostic.get("correction"), diagnostic.get("queue_receipt")
        _need(
            isinstance(correction, dict) and isinstance(queue, dict), "old diagnostic shape invalid"
        )
        if candidate == REVIEWED_CLOCK_CANDIDATE:
            _clock_no_act(
                cast(dict[str, Any], correction),
                cast(dict[str, Any], queue),
                strong_as_of=diagnostic.get("strong_as_of"),
            )
        else:
            _no_act(cast(dict[str, Any], correction), cast(dict[str, Any], queue))


def _reviewed_old_metadata(doc: dict[str, Any], descriptor_raw: bytes) -> dict[str, str]:
    """Validate literal pre-import f858 identity metadata without filesystem effects."""
    modules = doc.get("modules")
    _need(
        doc.get("candidate_sha")
        in {REVIEWED_OLD_CANDIDATE, REVIEWED_CAPACITY_CANDIDATE, REVIEWED_CLOCK_CANDIDATE},
        "old release is not reviewed no-ACT predecessor",
    )
    candidate = doc.get("candidate_sha")
    expected_modules = {
        REVIEWED_OLD_CANDIDATE: REVIEWED_OLD_MODULES_SHA256,
        REVIEWED_CAPACITY_CANDIDATE: REVIEWED_CAPACITY_MODULES_SHA256,
        REVIEWED_CLOCK_CANDIDATE: REVIEWED_CLOCK_MODULES_SHA256,
    }[candidate]
    expected_descriptor = {
        REVIEWED_OLD_CANDIDATE: REVIEWED_OLD_DESCRIPTOR_SHA256,
        REVIEWED_CAPACITY_CANDIDATE: REVIEWED_CAPACITY_DESCRIPTOR_SHA256,
        REVIEWED_CLOCK_CANDIDATE: REVIEWED_CLOCK_DESCRIPTOR_SHA256,
    }[candidate]
    _need(
        isinstance(modules, dict)
        and bool(modules)
        and all(
            isinstance(name, str)
            and Path(name).name == name
            and isinstance(digest, str)
            and _DIGEST.fullmatch(digest)
            for name, digest in modules.items()
        ),
        "old manifest closure invalid",
    )
    _need(
        _sha(json.dumps(modules, sort_keys=True, separators=(",", ":")).encode())
        == expected_modules,
        "old manifest closure is not the reviewed predecessor",
    )
    _need(
        doc.get("public_runtime_descriptor") == "closer-runtime-package.json"
        and _sha(descriptor_raw)
        == expected_descriptor,
        "old public descriptor is not the reviewed predecessor",
    )
    try:
        descriptor = json.loads(descriptor_raw)
    except ValueError as exc:
        raise SourceUpgradeHeld("old public descriptor invalid") from exc
    _need(isinstance(descriptor, dict), "old public descriptor invalid")
    for field in ("candidate_sha", "modules", "package_digest", "state_versions"):
        _need(descriptor.get(field) == doc.get(field), "old public descriptor binding differs")
    return dict(cast(dict[str, str], modules))


def _old_module_bytes(doc: dict[str, Any]) -> tuple[bytes, dict[str, bytes]]:
    """Capture a reviewed old closure without importing any of its code."""
    try:
        base = _canonical(doc["module_dir"])
        _need(
            _canonical(doc["module_path"]) == base / "closer_campaign.py",
            "old runtime entrypoint differs",
        )
        descriptor_raw = host._read(base / "closer-runtime-package.json")[1]
        modules = _reviewed_old_metadata(doc, descriptor_raw)
        executable = {
            str(path.relative_to(base))
            for path in base.rglob("*")
            if path.is_file() and path.suffix in {".py", ".pyc", ".pyo"}
        }
        _need(executable == set(modules), "old runtime contains undeclared executable code")
        captured = {}
        for name, digest in modules.items():
            raw = host._read(base / name)[1]
            _need(_sha(raw) == digest, "old manifest module differs")
            captured[name] = raw
        return descriptor_raw, captured
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        if isinstance(exc, SourceUpgradeHeld):
            raise
        raise SourceUpgradeHeld("old runtime closure unavailable") from exc


def _old_diagnostic(
    manifest: Path, state: Path, worker_id: str, *, interpreter: Path
) -> dict[str, Any]:
    """Run a fixed read-only validator under the old physical interpreter.

    Imports use privately staged, already verified bytes. Original manifest,
    state and proof paths remain unchanged. Native entry/providers are never called.
    """
    _, raw, doc = _object(manifest, "old manifest")
    state, state_raw, _state_doc = _object(state, "old campaign state")
    try:
        _need(
            _sha(host._read(_canonical(interpreter))[1]) == REVIEWED_OLD_INTERPRETER_SHA256,
            "old interpreter differs from reviewed predecessor",
        )
    except OSError as exc:
        raise SourceUpgradeHeld("old interpreter unavailable") from exc
    descriptor_raw, captured = _old_module_bytes(doc)
    modules = doc["modules"]
    program = (
        "import hashlib,json,pathlib,sys\n"
        "r=pathlib.Path(sys.argv[1]); s=pathlib.Path(sys.argv[2]); w=sys.argv[3]; stage=pathlib.Path(sys.argv[4]); raw=sys.stdin.buffer.read(); m=json.loads(raw); out=m['modules']\n"
        "for name,digest in out.items():\n"
        " if hashlib.sha256((stage/name).read_bytes()).hexdigest()!=digest: raise SystemExit(31)\n"
        "sys.path.insert(0,str(stage)); import closer_campaign as c, closer_native_observer as n, closer_resources as z\n"
        "if c.validate_manifest(m) or c.verify_modules(m): raise SystemExit(35)\n"
        "from closer_mechanisms import mechanism; mechanism(m)\n"
        # apply() already holds the real campaign lock. The old reader may
        # take its own lock only on this private byte-identical snapshot;
        # cohort/proof validation still uses the original campaign path.
        "snapshot=stage/'old-campaign-snapshot.json'; d=c.Campaign(snapshot).read(); x=next(x for x in d['workers'] if x['worker_id']==w); e,cfg,detail,terminal=c._cohort_worker_evidence(s,d,x,m,[128*1024*1024]); p=n.completed_round_proof(m,cfg,detail['completed_round']);\n"
        "if not isinstance(p,dict): raise SystemExit(34)\n"
        "rd=pathlib.Path(p['receipt_path']).read_bytes(); a=json.loads(rd); targets=p['finite_frontier']['targets'];\n"
        "if not isinstance(targets,list) or len(targets)!=1 or set(a['targets'])!=set(targets) or set(p['row_sha256'])!=set(targets): raise SystemExit(33)\n"
        "source_target=targets[0]; row=a['targets'][source_target]; results=row['results']; correction=None; queue=None; capacity=None; strong_as_of=None\n"
        f"if m['candidate_sha']=={REVIEWED_CAPACITY_CANDIDATE!r}:\n"
        " if len(d['workers'])!=1 or d.get('coding_continuations'): raise SystemExit(36)\n"
        " i=results['interpret']; ref=i['actual']['host_intake_ref']; ip=pathlib.Path(ref['path']); ir=ip.read_bytes()\n"
        " if ip.is_symlink() or ip.resolve()!=ip or len(ir)>128*1024 or len(ir)!=ref['bytes'] or hashlib.sha256(ir).hexdigest()!=ref['sha256']: raise SystemExit(37)\n"
        " capacity={'row_status':row.get('status'),'row_error':row.get('error'),'row_next_trigger':row.get('deferred_next_trigger'),'result_stages':sorted(results),'native_effect_contract':results['prepare']['native_effect_contract'],'exception':results['exception'],'interpretation':{'classification':i.get('classification'),'authority':i.get('authority'),'actual':i['actual'],'requested_agent':i['requested_agent'],'requested_profile':i['requested_profile']}}\n"
        "else:\n"
        " correction=results['exception']['correction']; queue=results['act']['queue_receipt']\n"
        f" if m['candidate_sha']=={REVIEWED_CLOCK_CANDIDATE!r}:\n"
        "  import calendar,time, closer_dispatcher as cd, closer_role_qualification as roles, execution_profiles as profiles\n"
        "  stage=results['exception']; actual=stage['actual']; proof=actual['_closer_controlled_proof']; expected=proof['expected']; stamp=proof['observed']['finished']['os_started']; at=calendar.timegm(time.strptime(stamp,'%a %b %d %H:%M:%S %Y')); profile=profiles.get_profile(expected['profile_id']); role=roles.TASK_ROLES['closer_exception']; record=roles.qualified_record(profile,role,now=at)\n"
        "  strong_as_of=bool(actual.get('run_id')==proof.get('run_id') and expected.get('task_type')=='closer_exception' and expected.get('operation_id') and stage.get('requested_agent')=='claude' and stage.get('requested_profile')==expected.get('profile_id') and record is not None and record.get('qualification_id')==expected.get('qualification_id') and cd._contract_digest(record)==expected.get('qualification_record_sha256') and roles.execution_record_matches(record,expected,checked_at=at) and actual.get('exit')==0)\n"
        "verdict=z.validate_stop(pathlib.Path(str(cfg['dispatch_state'])+'.pipeline.json'),p['round_id'],targets)\n"
        "if p!=detail['completed_round'] or terminal.get('completed') is not False or p.get('accepted') is not False or verdict.get('valid') is not True or verdict.get('global_stop_valid') is not True: raise SystemExit(32)\n"
        "print(json.dumps({'schema':'old-source-diagnostic/v3','manifest_sha256':hashlib.sha256(raw).hexdigest(),'state_sha256':hashlib.sha256(snapshot.read_bytes()).hexdigest(),'modules':out,'result':e['result'],'cohort':terminal,'completed_round':p,'round_sha256':hashlib.sha256(rd).hexdigest(),'worker_target':x['target'],'source_target':source_target,'correction':correction,'queue_receipt':queue,'strong_as_of':strong_as_of,'capacity_disposition':capacity,'stop':verdict},sort_keys=True,separators=(',',':')))\n"
    )
    with tempfile.TemporaryDirectory(prefix="old-source-diagnostic-") as stage_name:
        stage = Path(stage_name)
        stage.chmod(0o700)
        try:
            for name, module_raw in captured.items():
                target = stage / name
                target.write_bytes(module_raw)
                target.chmod(0o600)
            (stage / "closer-runtime-package.json").write_bytes(descriptor_raw)
            snapshot = stage / "old-campaign-snapshot.json"
            snapshot.write_bytes(state_raw)
            snapshot.chmod(0o600)
            policy_raw = host._read(Path(doc["module_dir"]) / "closer_role_selection.json")[1]
            (stage / "closer_role_selection.json").write_bytes(policy_raw)
            run = subprocess.run(
                [
                    str(interpreter),
                    "-I",
                    "-B",
                    "-c",
                    program,
                    str(manifest),
                    str(state),
                    worker_id,
                    str(stage),
                ],
                input=raw.decode(),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            _need(
                run.returncode == 0 and len(run.stdout) <= 65536,
                "old source diagnostic failed: " + run.stderr[-500:],
            )
            answer = json.loads(run.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise SourceUpgradeHeld("old source diagnostic unavailable") from exc
    _old_module_bytes(doc)
    _need(
        answer.get("manifest_sha256") == _sha(raw)
        and answer.get("state_sha256") == _sha(state_raw)
        and answer.get("modules") == modules
        and host._read(manifest)[1] == raw,
        "old source diagnostic differs",
    )
    _need(host._read(state)[1] == state_raw, "old campaign changed during diagnostic")
    return answer


def _manifest(path: str | Path, *, old: bool = False) -> tuple[Path, bytes, dict[str, Any]]:
    import closer_campaign as campaign

    value, raw, doc = _object(path, "manifest")
    if old:
        _old_module_bytes(doc)
        contract = doc.get("mechanism_contract")
        interpreter = contract.get("interpreter") if isinstance(contract, dict) else None
        _need(
            isinstance(interpreter, dict)
            and interpreter.get("sha256") == REVIEWED_OLD_INTERPRETER_SHA256,
            "old interpreter is not the reviewed predecessor",
        )
    else:
        _need(
            not campaign.validate_manifest(doc) and not campaign.verify_modules(doc),
            "manifest closure invalid",
        )
    return value, raw, doc


def _installed_release(path: str | Path, candidate: str) -> dict[str, Any]:
    """Bind the replacement to an actually installed, verified generation."""
    import closer_release

    journal = _canonical(path)
    try:
        record = closer_release.ReleaseRecord(journal).read()
    except (OSError, ValueError, RuntimeError) as exc:
        raise SourceUpgradeHeld("new release journal unavailable") from exc
    if not isinstance(record, dict):
        raise SourceUpgradeHeld("new release journal unavailable")
    verification = record.get("verification")
    _need(
        record.get("stage") == "installed"
        and record.get("candidate_sha") == candidate
        and isinstance(record.get("installed_generation"), str)
        and isinstance(verification, dict)
        and verification.get("verified") is True
        and isinstance(verification.get("snapshot_digest"), str),
        "new candidate is not fully installed/verified",
    )
    verified = cast(dict[str, Any], verification)
    _canonical(verified["snapshot_path"])
    mirror = (Path.home() / ".codex/orchestrator-mirror").parent.resolve() / "orchestrator-mirror"
    physical_mirror = mirror.resolve()
    inspector = physical_mirror / "scripts/install_verified_snapshot.py"
    try:
        inspector_raw = host._read(inspector)[1]
    except (OSError, RuntimeError) as exc:
        raise SourceUpgradeHeld("installed generation inspector unavailable") from exc
    _need(
        _sha(inspector_raw) == REVIEWED_INSTALL_VERIFIED_SNAPSHOT_SHA256,
        "installed generation inspector differs",
    )
    # Execute the already-pinned bytes, not the mutable pathname that supplied
    # them.  The physical pathname remains __file__ for the inspector's own
    # structural checks; the logical mirror remains the generation identity.
    runner = (
        "import sys\n"
        "code=sys.stdin.buffer.read()\n"
        "sys.argv=[sys.argv[1],*sys.argv[2:]]\n"
        "exec(compile(code,sys.argv[0],'exec'),{'__name__':'__main__','__file__':sys.argv[0]})\n"
    )
    python = str(Path(sys.executable).resolve())
    try:
        generation = subprocess.run(
            [
                python,
                "-I",
                "-B",
                "-c",
                runner,
                str(inspector),
                str(mirror),
                "--generation-token",
            ],
            text=True,
            input=inspector_raw.decode("utf-8"),
            capture_output=True,
            timeout=10,
            check=False,
        )
        digest = subprocess.run(
            [
                python,
                "-I",
                "-B",
                "-c",
                runner,
                str(inspector),
                str(physical_mirror),
                "--digest",
            ],
            text=True,
            input=inspector_raw.decode("utf-8"),
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceUpgradeHeld("installed generation inspector unavailable") from exc
    _need(generation.returncode == digest.returncode == 0, "installed generation inspector failed")
    try:
        inspector_after = host._read(inspector)[1]
    except (OSError, RuntimeError) as exc:
        raise SourceUpgradeHeld("installed generation inspector unavailable") from exc
    _need(
        _sha(inspector_after) == REVIEWED_INSTALL_VERIFIED_SNAPSHOT_SHA256,
        "installed generation inspector changed",
    )
    _need(
        generation.stdout.strip() == record["installed_generation"]
        and digest.stdout.strip() == verified["snapshot_digest"],
        "installed generation/digest readback differs",
    )
    return {
        "path": str(journal),
        "sha256": _sha(host._read(journal)[1]),
        "candidate_sha": candidate,
        "installed_generation": record["installed_generation"],
        "snapshot_digest": verified["snapshot_digest"],
        "mirror": str(mirror),
        "physical_mirror": str(physical_mirror),
        "python": python,
        "inspector": str(inspector),
        "inspector_sha256": _sha(inspector_after),
    }


def _completed_reference(
    manifest_path: Path,
    state_path: str | Path,
    worker_id: str,
    *,
    manifest_sha256: str | None = None,
    state_sha256: str | None = None,
) -> dict[str, Any]:
    """Read an actual terminal worker without importing no-start authority."""
    import closer_campaign as campaign

    manifest_path = _canonical(manifest_path)
    manifest_value, manifest_raw, manifest = _object(manifest_path, "old manifest")
    if manifest_sha256 is not None:
        _need(_sha(manifest_raw) == manifest_sha256, "old manifest changed")
    state = _canonical(state_path)
    _, state_raw, document = _object(state, "old campaign state")
    if state_sha256 is not None:
        _need(_sha(state_raw) == state_sha256, "old state changed")
    _need(
        document.get("phase") == "work"
        and document.get("hold") is None
        and document.get("targets") == {campaign.ROUND_UNIT: "done"},
        "old campaign is not terminal exact round state",
    )
    workers = document.get("workers")
    if not isinstance(workers, list):
        raise SourceUpgradeHeld("old campaign worker list missing")
    worker = next(
        (row for row in workers if isinstance(row, dict) and row.get("worker_id") == worker_id),
        None,
    )
    if not isinstance(worker, dict):
        raise SourceUpgradeHeld("old worker is not terminal")
    _need(worker.get("state") == "terminal", "old worker is not terminal")
    if worker.get("pid"):
        host._dead(worker["pid"])
    result_path = campaign.result_path(state, worker_id)
    _, result_raw, result = _object(result_path, "old result")
    _need(
        result.get("worker_id") == worker_id and result.get("target") == worker.get("target"),
        "old result does not join terminal worker",
    )
    _need(
        worker.get("outcome") == result.get("outcome") == "ok",
        "old completed worker is not exact OK",
    )
    return {
        "manifest_path": str(manifest_value),
        "manifest": manifest,
        "state_path": str(state),
        "state_sha256": _sha(state_raw),
        "state": document,
        "worker": worker,
        "result_path": str(result_path),
        "result_sha256": _sha(result_raw),
        "result": result,
    }


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
    for key in (
        "repository",
        "repository_id",
        "epoch",
        "writer_id",
        "writer_login",
        "writer_type",
        "bootstrap_anchor_sha256",
    ):
        _need(old_config.get(key) == new_config.get(key), "source upgrade identity changed: " + key)


def _derived_coding_capability(policy: dict[str, Any]) -> dict[str, str] | None:
    """Derive, never caller-select, the static coding capability.

    The old f858 policy predates the continuation field.  It is safe to add the
    field only when the currently materialized controller installation already
    has exactly one complete Source-owned Codex coding/publication consumer.
    This function is data validation only; it does not create an admission,
    alter controller custody, or invoke a provider.
    """
    try:
        import closer_coding_continuation as continuation
        import closer_coding_publication as publication
        import closer_coding_session_runtime as coding_runtime
        import closer_coding_session_v2 as coding_session
        import closer_correction_controller as controller
        import execution_profiles

        config = policy["correction_controller_installation"]["config"]
        consumers = config["consumers"]
        if not isinstance(consumers, dict):
            return None
        draft = coding_session.prepare_admission_policy(consumers)
        if draft.snapshot_utf8 is None:
            return None
        snapshot = coding_session.validate_admission_snapshot(json.loads(draft.snapshot_utf8))
        consumer_id = snapshot["consumer_id"]
        consumer = consumers.get(consumer_id)
        if not isinstance(consumer, dict):
            return None
        # A continuation cannot lawfully create a coding worker without the
        # existing publication path and its independent current-budget ceiling.
        if publication.validate_consumer_mode(consumer_id, consumer) is None:
            return None
        coding_session.validate_budget(consumer[coding_runtime.CURRENT_FIELD])
        profile = execution_profiles.get_profile(coding_session.PROFILE)
        if not (
            consumer.get("runner") == "existing-local-controller"
            and consumer.get("agent") == "codex"
            and consumer.get("profile_id") == coding_session.PROFILE
            and consumer.get("profile_sha256")
            == snapshot["profile_sha256"]
            == controller.source_digest(profile)
            and isinstance(consumer.get("owner_login"), str)
            and bool(consumer["owner_login"])
        ):
            return None
        return continuation.validate_capability(
            {"schema": continuation.CAPABILITY_SCHEMA, "consumer_id": consumer_id}
        )
    except (ImportError, KeyError, TypeError, ValueError, RuntimeError):
        return None


def _rebased_policy(old: dict[str, Any], new: dict[str, Any], new_state: Path) -> dict[str, Any]:
    """The sole lawful policy transformation for this reviewed source upgrade."""
    expected = json.loads(json.dumps(old["policy"], sort_keys=True))
    try:
        config = expected["correction_controller_installation"]["config"]
        config["source_sha"] = new["candidate_sha"]
        config["state_path"] = str(new_state)
        config["native_transport_source_sha256"] = new["modules"][
            "closer_correction_native_effects.py"
        ]
        consumers = config.get("consumers", {})
        for consumer in consumers.values():
            bootstrap = consumer["native_bootstrap"]
            bootstrap["private_root"] = str(new_state.parent.parent / "controller-private")
            confinement = bootstrap["confinement"]
            helper = _canonical(new["module_dir"]) / "closer_codex_confinement_probe.py"
            confinement["helper_path"] = str(helper)
            confinement["helper_sha256"] = new["modules"][helper.name]
    except (KeyError, TypeError) as exc:
        raise SourceUpgradeHeld("policy lacks reviewed rebase structure") from exc
    try:
        import closer_coding_continuation as continuation

        derived = _derived_coding_capability(expected)
        existing = expected.get(continuation.POLICY_FIELD)
        if existing is None:
            if derived is not None:
                expected[continuation.POLICY_FIELD] = derived
        else:
            _need(
                derived is not None and continuation.validate_capability(existing) == derived,
                "existing coding capability no longer matches qualified source consumer",
            )
    except ImportError as exc:
        raise SourceUpgradeHeld("coding capability validator unavailable") from exc
    _need(new.get("policy") == expected, "new policy changes fields outside lawful rebase")
    return expected


def _physical_recipe(
    old_cfg: dict[str, Any],
    old_doc: dict[str, Any],
    old_policy: dict[str, Any],
    old_state: Path,
    new: dict[str, Any],
    new_path: Path,
    new_state: Path,
    frontier: dict[str, Any],
) -> dict[str, Any]:
    """Known-f858 rebind: retain data knobs, derive every executable path."""
    import closer_pipeline

    allowed = {
        "runtime_manifest",
        "finite_frontier",
        "assignment",
        "dispatch_state",
        "policy",
        "granularity",
        "mechanism",
        "execution_policy",
        "dispatch_workers",
        "coordinator_mode",
        "coordinator_contract_pin",
        "request_timeout_seconds",
        "handoff_agent",
        "handoff_dir",
        "native_hooks",
        "receipt_reserve_seconds",
        "apply",
    }
    _need(set(old_cfg) <= allowed, "old worker config has unknown executable field")
    _need(
        set(old_cfg)
        >= {"assignment", "dispatch_state", "native_hooks", "handoff_dir", "handoff_agent"},
        "old worker config lacks physical recipe fields",
    )
    old_root = old_state.parent
    dispatch = _canonical(old_cfg["dispatch_state"])
    _need(dispatch.parent == old_root, "old dispatch path is outside old campaign root")
    assignment = _canonical(old_cfg["assignment"])
    demand = old_doc.get("demand_assignment")
    admission = old_doc.get("demand_admission")
    _need(
        isinstance(demand, dict)
        and isinstance(admission, dict)
        and assignment.parent == old_root / (old_state.name + ".demand")
        and assignment.name == str(demand.get("sha256", "")) + ".json"
        and demand.get("path") == str(assignment)
        and _sha(host._read(assignment)[1]) == demand.get("sha256")
        and admission.get("assignment_sha256") == demand.get("sha256")
        and admission.get("campaign_id") == old_doc.get("campaign_id")
        and admission.get("owner") == old_doc.get("owner")
        and admission.get("campaign_generation") == old_doc.get("generation"),
        "old source demand assignment binding differs",
    )
    cfg = json.loads(json.dumps(old_cfg, sort_keys=True))
    expected_handoff = (Path.home() / ".codex/handoff").resolve()
    _need(
        _canonical(cfg["handoff_dir"]) == expected_handoff and cfg.get("handoff_agent") == "codex",
        "old handoff identity/path differs",
    )
    cfg["runtime_manifest"] = str(new_path)
    cfg["finite_frontier"] = frontier
    cfg["assignment"] = str(new_state.parent / assignment.name)
    cfg["dispatch_state"] = str(new_state.parent / Path(old_cfg["dispatch_state"]).name)
    cfg["policy"] = _rebased_policy({"policy": old_policy}, new, new_state)
    cfg["coordinator_contract_pin"] = closer_pipeline.scoped_capability()
    _need(
        cfg.get("granularity", "round") == "round"
        and cfg.get("mechanism") == "native-handoff/v1"
        and cfg.get("execution_policy") == "finite-frontier/v2"
        and cfg.get("dispatch_workers") == 1
        and cfg.get("coordinator_mode") == "resource_scoped"
        and cfg.get("apply") is True
        and cfg.get("request_timeout_seconds") == 120,
        "physical recipe mode differs",
    )
    old_hooks = old_cfg["native_hooks"]
    _need(isinstance(old_hooks, dict), "old native hooks invalid")
    hook_root = new_state.parent.parent / "native-hooks"
    hooks: dict[str, Any] = {"bounded_no_new_relay": True}
    for phase in ("prerun", "postrun"):
        old_hook = old_hooks.get(phase)
        path = hook_root / ("handoff-" + phase + ".sh")
        _need(
            isinstance(old_hook, dict) and _sha(host._read(path)[1]) == old_hook.get("sha256"),
            "new hook differs from old reviewed hook",
        )
        hooks[phase] = {"path": str(path), "sha256": old_hook["sha256"]}
    module_dir = _canonical(new["module_dir"])
    lease = module_dir / "closer_native_lease.py"
    _need(
        _sha(host._read(lease)[1]) == new["modules"].get(lease.name), "new lease module pin differs"
    )
    hooks["lease_module"] = {"path": str(lease), "sha256": new["modules"][lease.name]}
    interpreter = new["mechanism_contract"]["interpreter"]
    _need(isinstance(interpreter, dict), "new interpreter physical pin missing")
    mirror = (Path.home() / ".codex/orchestrator-mirror/closer_dispatcher.py").resolve()
    _need(
        _sha(host._read(mirror)[1]) == new["modules"].get(mirror.name),
        "current dispatcher pin differs",
    )
    hooks["postrun_dependencies"] = {
        "interpreter": {"path": interpreter["path"], "sha256": interpreter["sha256"]},
        "dispatcher": {"path": str(mirror), "sha256": new["modules"][mirror.name]},
    }
    cfg["native_hooks"] = hooks
    return cfg


def _old_custody_quiet(old: dict[str, Any]) -> dict[str, Any]:
    """Physical custody is evidence, not a JSON claim from a caller."""
    import closer_correction_controller as controller

    controller_file = Path(controller.__file__).resolve()
    _need(
        old.get("modules", {}).get(controller_file.name) == _sha(host._read(controller_file)[1]),
        "current controller differs from old pinned controller",
    )
    config = old["policy"]["correction_controller_installation"]["config"]
    try:
        inspection = controller.inspect_provisioned_custody(config)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise SourceUpgradeHeld("old physical custody unavailable") from exc
    _need(
        not inspection["highwater"]["pending_transactions"], "old custody has pending transaction"
    )
    custody_path = _canonical(inspection["identity"]["custody_path"])
    _need(
        set(os.listdir(custody_path))
        == {"namespace.json", "highwater-0000000000000000.json", "writer.lock"},
        "old custody inventory is not pristine no-ACT state",
    )
    _need(
        inspection["highwater"]["anchor"].get("revision") == 0, "old custody revision is not zero"
    )
    _need(
        controller.inspect_provisioned_custody(config) == inspection,
        "old custody changed during recheck",
    )
    return inspection


def fresh_read_native_index(config: dict[str, Any], inspection: dict[str, Any]) -> dict[str, Any]:
    """Bounded GET-only proof that remote native index still is the old anchor."""
    import closer_correction_controller as controller

    anchor = inspection["highwater"]["anchor"]
    repository = config.get("repository")
    if not isinstance(repository, str) or not repository:
        raise SourceUpgradeHeld("controller repository unavailable")

    def get(endpoint: str) -> dict[str, Any]:
        try:
            call = subprocess.run(
                ["gh", "api", endpoint], capture_output=True, text=True, timeout=10, check=False
            )
            _need(
                call.returncode == 0 and len(call.stdout) <= 1024 * 1024,
                "native index GET unavailable",
            )
            value = json.loads(call.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise SourceUpgradeHeld("native index GET unavailable") from exc
        if not isinstance(value, dict):
            raise SourceUpgradeHeld("native index GET response invalid")
        return value

    ref = get("repos/" + repository + "/git/ref/heads/" + controller.BRANCH)
    _need(ref.get("object", {}).get("sha") == anchor["commit_sha"], "native index head changed")
    contents = get(
        "repos/"
        + repository
        + "/contents/"
        + controller.INDEX_PATH
        + "?ref="
        + anchor["commit_sha"]
    )
    _need(
        contents.get("sha") == anchor["blob_sha"] and contents.get("encoding") == "base64",
        "native index blob changed",
    )
    principal, repo = get("user"), get("repos/" + repository)
    _need(
        {key: principal.get(key) for key in ("id", "login", "type")}
        == {
            "id": config.get("writer_id"),
            "login": config.get("writer_login"),
            "type": config.get("writer_type"),
        },
        "native index principal changed",
    )
    _need(
        repo.get("id") == config.get("repository_id")
        and controller.native_repository_name(repo.get("full_name"), repository.lower()),
        "native index repository changed",
    )
    try:
        raw = base64.b64decode(str(contents.get("content", "")).replace("\n", ""), validate=True)
        data = json.loads(raw)
        _need(
            controller.validate_index(data, controller.ControllerConfig(**config)) == data,
            "native index source validation failed",
        )
    except (ValueError, TypeError, RuntimeError) as exc:
        raise SourceUpgradeHeld("native index payload invalid") from exc
    _need(controller.digest(data) == anchor["state_sha256"], "native index state changed")
    _need(data.get("revision") == 0 and data.get("entries") == [], "native index not pristine")
    return {
        "commit_sha": anchor["commit_sha"],
        "blob_sha": anchor["blob_sha"],
        "state_sha256": anchor["state_sha256"],
        "principal": {key: principal[key] for key in ("id", "login", "type")},
        "repository_id": repo["id"],
        "repository": repo["full_name"],
    }


def _inert_new_custody(
    manifest: dict[str, Any], state: Path, anchor: dict[str, Any]
) -> dict[str, Any]:
    """Inspect only pre-provisioned metadata while the new campaign is absent.

    The controller's full inspector intentionally requires an existing campaign.
    This check grants no controller authority; normal admission uses that full
    inspector after the producer has created and sealed the held campaign.
    """
    import closer_correction_controller as controller

    config = controller.ControllerConfig(
        **manifest["policy"]["correction_controller_installation"]["config"]
    )
    root = host._path(Path(str(state) + ".evidence") / config.namespace)
    before = root.stat()
    _need(
        stat.S_ISDIR(before.st_mode)
        and before.st_uid == os.getuid()
        and stat.S_IMODE(before.st_mode) == 0o700,
        "new inert custody is not private/owned",
    )
    expected = {
        "namespace.json": controller.canonical(
            {
                "schema": "local-correction-controller-namespace/v1",
                "config_sha256": controller.digest(config.source_binding()),
            }
        ),
        "highwater-0000000000000000.json": controller.canonical(anchor),
        "writer.lock": b"",
    }
    _need(set(os.listdir(root)) == set(expected), "new inert custody inventory differs")
    files = {}
    for name, raw in expected.items():
        path, observed = host._read(root / name)
        info = path.stat()
        _need(observed == raw and not info.st_mode & 0o077, "new inert custody bytes/mode differ")
        files[name] = {"sha256": _sha(raw), "device": info.st_dev, "inode": info.st_ino}
    after = root.stat()
    _need(
        (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
        and set(os.listdir(root)) == set(expected),
        "new inert custody changed during inspection",
    )
    return {
        "identity": {"custody_path": str(root), "device": before.st_dev, "inode": before.st_ino},
        "files": files,
    }


def _wrapper_plan(
    old_manifest: Path, old: dict[str, Any], new_manifest: Path, new: dict[str, Any], receipt: Path
) -> dict[str, Any]:
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
    old_manifest: str | Path,
    old_state: str | Path,
    worker_id: str,
    new_manifest: str | Path,
    new_state: str | Path,
    restore_receipt: str | Path,
    new_worker_config: dict[str, Any],
    frontier: dict[str, Any],
    new_release_journal: str | Path,
) -> dict[str, Any]:
    """Seal no-ACT old evidence and prepare a new source-owned wrapper CAS."""
    import closer_native_wrapper_install as install

    old_path, old_raw, old = _manifest(old_manifest, old=True)
    new_path, new_raw, new = _manifest(new_manifest)
    _need(
        old.get("candidate_sha")
        in {REVIEWED_OLD_CANDIDATE, REVIEWED_CAPACITY_CANDIDATE, REVIEWED_CLOCK_CANDIDATE},
        "old release is not reviewed no-ACT predecessor",
    )
    _need(
        old.get("candidate_sha") != new.get("candidate_sha"),
        "source candidate release is not different",
    )
    release = _installed_release(new_release_journal, new["candidate_sha"])
    _need(
        new.get("modules", {}).get(Path(__file__).name) == _sha(Path(__file__).read_bytes()),
        "new manifest does not pin executing source-upgrade boundary",
    )
    _same_identity(old, new)
    reference = _completed_reference(old_path, old_state, worker_id)
    _old_native_terminal(old)
    host._lane_absent()
    custody = _old_custody_quiet(old)
    index = fresh_read_native_index(
        old["policy"]["correction_controller_installation"]["config"], custody
    )
    interpreter = Path(old["mechanism_contract"]["interpreter"]["path"])
    diagnostic = _old_diagnostic(
        old_path, Path(reference["state_path"]), worker_id, interpreter=interpreter
    )
    _need(
        diagnostic.get("state_sha256") == reference["state_sha256"],
        "old diagnostic campaign differs from captured reference",
    )
    _need(
        diagnostic.get("worker_target") == reference["worker"].get("target")
        and diagnostic.get("source_target")
        in reference["state"]["worker_config"]["finite_frontier"]["targets"],
        "old diagnostic scopes differ",
    )
    cohort, completed = diagnostic.get("cohort"), diagnostic.get("completed_round")
    if not all(isinstance(value, dict) for value in (cohort, completed)):
        raise SourceUpgradeHeld("old diagnostic shape invalid")
    _diagnostic_no_act(old["candidate_sha"], diagnostic)
    _incomplete_cohort(cast(dict[str, Any], cohort), cast(dict[str, Any], completed))
    state = _canonical(new_state)
    _need(
        not state.exists()
        and state.parent == _canonical(new["mechanism_contract"]["campaign_root"]),
        "new state is not fresh current campaign",
    )
    # Freeze the only proposed inputs before CAS. These remain non-authoritative
    # until the later current-source admission path observes demand again.
    import closer_frontier_policy as policy
    import closer_native_readmission as readmission

    try:
        fresh = policy.validate_frontier(frontier, targets=frontier.get("targets"))
        old_frontier = policy.validate_frontier(
            reference["state"]["worker_config"]["finite_frontier"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceUpgradeHeld("pre-CAS frontier invalid") from exc
    cfg = _physical_recipe(
        reference["state"]["worker_config"],
        reference["state"],
        old["policy"],
        Path(reference["state_path"]),
        new,
        new_path,
        state,
        fresh,
    )
    supplied = json.loads(json.dumps(new_worker_config, sort_keys=True))
    _need(supplied == cfg, "pre-CAS worker config is not strict old-to-new rebind")
    new_custody = _inert_new_custody(new, state, custody["highwater"]["anchor"])
    _need(
        cfg.get("granularity", "round") == "round"
        and cfg.get("mechanism") == "native-handoff/v1"
        and cfg.get("execution_policy") == "finite-frontier/v2"
        and cfg.get("dispatch_workers") == 1
        and cfg.get("coordinator_mode") == "resource_scoped"
        and isinstance(cfg.get("coordinator_contract_pin"), dict),
        "strict rebind worker modes/coordinator differ",
    )
    hooks = cfg.get("native_hooks")
    _need(
        isinstance(hooks, dict)
        and hooks.get("bounded_no_new_relay") is True
        and isinstance(hooks.get("prerun"), dict)
        and isinstance(hooks.get("postrun"), dict)
        and isinstance(hooks.get("lease_module"), dict),
        "strict rebind native hooks invalid",
    )
    readmission._guard_fresh(new, cfg, state, fresh, old_frontier)
    receipt = _canonical(restore_receipt)
    wrapper = _wrapper_plan(old_path, old, new_path, new, receipt)
    _restored, predecessor = install._restored_predecessor(receipt)
    rows = install._prospective_rows(predecessor, reference, new, state)
    # The restore predecessor may be historical attempt13 rather than this
    # stopped worker. Its custody fence cannot stand in for either current
    # controller namespace.
    for inspection in (custody, new_custody):
        writer = str(Path(inspection["identity"]["custody_path"]) / "writer.lock")
        if not any(row["path"] == writer for row in rows):
            rows.append({"path": writer, "must_exist": True})
    rows.sort(key=lambda row: row["path"])
    result = {
        "schema": SCHEMA,
        "authority": False,
        "eligible": True,
        "producer_sha256": _sha(Path(__file__).read_bytes()),
        "old": {
            "manifest": str(old_path),
            "manifest_sha256": _sha(old_raw),
            "state": reference["state_path"],
            "state_sha256": reference["state_sha256"],
            "result": reference["result_path"],
            "result_sha256": reference["result_sha256"],
            "custody": custody,
            "native_index": index,
            "diagnostic": diagnostic,
        },
        "new": {
            "manifest": str(new_path),
            "manifest_sha256": _sha(new_raw),
            "state": str(state),
            "custody": new_custody,
        },
        "restore_receipt": {"path": str(receipt), "sha256": _sha(host._read(receipt)[1])},
        "new_release": release,
        "held_recipe": {
            "worker_config": cfg,
            "worker_config_sha256": _sha(
                json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()
            ),
            "frontier": fresh,
            "frontier_sha256": _sha(
                json.dumps(fresh, sort_keys=True, separators=(",", ":")).encode()
            ),
        },
        "wrapper": wrapper,
        "locks": rows,
        "seal": str(_upgrade_path(new, _SEAL_NAME)),
    }
    result["plan_sha256"] = _plan_sha(result)
    return result


def apply(
    old_manifest,
    old_state,
    worker_id,
    new_manifest,
    new_state,
    restore_receipt,
    new_worker_config,
    frontier,
    new_release_journal,
    *,
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """One-way CAS after a sealed intent; it creates no campaign or native work."""
    import closer_native_wrapper_install as install

    inputs = (
        old_manifest,
        old_state,
        worker_id,
        new_manifest,
        new_state,
        restore_receipt,
        new_worker_config,
        frontier,
        new_release_journal,
    )
    first = plan(*inputs)
    _need(first["plan_sha256"] == expected_plan_sha256, "plan changed")
    with host._hold(first["locks"]):
        second = plan(*inputs)
        _need(second["plan_sha256"] == first["plan_sha256"], "plan changed under locks")
        new_path = Path(first["new"]["manifest"])
        _, new_raw, new = _manifest(new_path)
        _need(_sha(new_raw) == first["new"]["manifest_sha256"], "new manifest changed before CAS")
        seal = _upgrade_path(new, _SEAL_NAME)
        _need(str(seal) == first["seal"], "source upgrade seal path differs")
        intent = _upgrade_path(new, _INTENT_NAME)
        known_path = _upgrade_path(new, _KNOWN_NAME)
        _need(
            not known_path.exists(),
            "source upgrade already known",
        )
        _need(not intent.exists() or seal.exists(), "intent has no pre-CAS seal")
        # A fully fresh observation root has no supervisor key yet.  Once any
        # upgrade witness exists, recovery must verify it with the original
        # key rather than minting or replacing one.
        if not seal.exists() and not intent.exists():
            _provision_upgrade_key(new)
        if seal.exists():
            _seal_path, _seal_raw, prior_seal = _read_signed_upgrade(
                seal,
                new,
                name=_SEAL_NAME,
                label="pre-CAS source upgrade seal",
                manifest_sha256=_sha(new_raw),
            )
            _need(
                prior_seal.get("stage") == "sealed_pre_cas"
                and _plan_sha(prior_seal) == first["plan_sha256"]
                and prior_seal.get("plan_sha256") == first["plan_sha256"],
                "pre-CAS source upgrade records differ",
            )
        if intent.exists():
            _intent_path, _intent_raw, prior_intent = _read_signed_upgrade(
                intent,
                new,
                name=_INTENT_NAME,
                label="pre-CAS source upgrade intent",
                manifest_sha256=_sha(new_raw),
            )
            _need(
                prior_intent.get("stage") == "intent"
                and _plan_sha(prior_intent) == first["plan_sha256"]
                and prior_intent.get("plan_sha256") == first["plan_sha256"],
                "pre-CAS source upgrade records differ",
            )
        # Both records are immutable, and the seal intentionally precedes CAS:
        # after the wrapper changes, old native.contract would be circular.
        sealed = {**first, "stage": "sealed_pre_cas", "at": time.time()}
        _need(_plan_sha(sealed) == first["plan_sha256"], "plan body differs before seal")
        wrapper = Path(first["wrapper"]["path"])
        _need(
            _sha(host._read(wrapper)[1]) == first["wrapper"]["before_sha256"],
            "wrapper CAS preimage changed",
        )
        # Re-read the archived base through the installer helper; no old
        # contract is invoked after this point.
        receipt_path, receipt_raw = host._ref(first["restore_receipt"], "sealed_restore_receipt")
        _need(
            _sha(receipt_raw) == first["restore_receipt"]["sha256"],
            "sealed restore receipt changed",
        )
        restored_doc, _ = install._restored_predecessor(receipt_path)
        data = install._render(
            install._guard_source(install._archived_base(restored_doc)), new, new_path
        )
        _need(_sha(data) == first["wrapper"]["after_sha256"], "wrapper CAS render changed")
        fd, tmp = tempfile.mkstemp(dir=wrapper.parent, prefix=".source-upgrade-wrapper-")
        replaced = False
        phase = "wrapper_readback"
        try:
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), 0o755)
                os.fsync(stream.fileno())
            # A valid seal without its later intent is recoverable only after
            # plan() reauthenticates the exact old wrapper and all facts above.
            # Partial or different existing records are never overwritten.
            if not seal.exists():
                host._write_once(seal, _signed_upgrade_record(new, sealed))
            if not intent.exists():
                host._write_once(
                    intent,
                    _signed_upgrade_record(new, {**first, "stage": "intent", "at": time.time()}),
                )
            os.replace(tmp, wrapper)
            replaced = True
            directory = os.open(wrapper.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            _need(
                _sha(host._read(wrapper)[1]) == first["wrapper"]["after_sha256"],
                "wrapper CAS readback failed",
            )
            # Current contract only: old contract is invalid by design now.
            import closer_native_observer as native

            phase = "native_contract"
            native.contract(new)
            phase = "wrapper_entry"
            native._check_wrapper_entry(new, new_path)
            # The only permitted post-CAS difference is the canonical wrapper.
            # Re-read every new module/source pin after replacement.
            phase = "module_closure"
            _, verified_raw, verified = _manifest(new_path)
            _need(
                _sha(verified_raw) == first["new"]["manifest_sha256"] and verified == new,
                "new source closure changed after wrapper CAS",
            )
            phase = "known_receipt_write"
            host._write_once(
                known_path,
                _signed_upgrade_record(
                    new,
                    {**first, "stage": "cas_known", "status": "installed", "at": time.time()},
                ),
            )
            return {**first, "status": "installed"}
        except Exception as exc:
            if replaced:
                raise SourceUpgradeHeld(
                    "post-CAS outcome uncertain: " + phase + ":" + type(exc).__name__
                ) from None
            raise
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                Path(tmp).unlink()
            except FileNotFoundError:
                pass


def prepare_held(seal_path: str | Path, *, confirm_plan_sha256: str) -> dict[str, Any]:
    """After CAS, consume a sealed upgrade once into a fresh held campaign.

    This is deliberately a preparation record only.  It cannot issue typed
    business demand, clear the source initialization hold, or invoke native
    entry; those remain the current source's later admission boundary.
    """
    import closer_campaign as campaign
    import closer_frontier_policy as policy
    import closer_native_observer as native
    import closer_native_readmission as readmission

    # The envelope itself is untrusted until its referenced current manifest
    # validates.  It is used only to locate that manifest, then immediately
    # re-read through the existing source closure validator and HMAC domain.
    candidate_seal, _candidate_raw, candidate_envelope = _object(seal_path, "source upgrade seal")
    candidate_record = candidate_envelope.get("record")
    if not isinstance(candidate_record, dict):
        raise SourceUpgradeHeld("upgrade seal invalid")
    candidate_new = candidate_record.get("new")
    if not isinstance(candidate_new, dict):
        raise SourceUpgradeHeld("upgrade seal invalid")
    candidate_manifest = candidate_new.get("manifest")
    if not isinstance(candidate_manifest, str) or not candidate_manifest:
        raise SourceUpgradeHeld("upgrade seal invalid")
    new_path = _canonical(candidate_manifest)
    _, raw, manifest = _manifest(new_path)
    manifest_sha = _sha(raw)
    seal, seal_raw, record = _read_signed_upgrade(
        candidate_seal,
        manifest,
        name=_SEAL_NAME,
        label="source upgrade seal",
        manifest_sha256=manifest_sha,
    )
    _need(
        record.get("schema") == SCHEMA and record.get("stage") == "sealed_pre_cas",
        "upgrade seal invalid",
    )
    _need(
        record.get("plan_sha256") == confirm_plan_sha256,
        "held confirmation differs from sealed upgrade",
    )
    _need(_plan_sha(record) == confirm_plan_sha256, "sealed plan body differs from digest")
    _need(new_path == _canonical(record["new"]["manifest"]), "new manifest differs from seal")
    known = _upgrade_path(manifest, _KNOWN_NAME)
    _need(known.exists(), "wrapper CAS is not known")
    _known_path, _known_raw, known_record = _read_signed_upgrade(
        known,
        manifest,
        name=_KNOWN_NAME,
        label="source upgrade known receipt",
        manifest_sha256=manifest_sha,
    )
    _need(
        known_record.get("plan_sha256") == confirm_plan_sha256
        and _plan_sha(known_record) == confirm_plan_sha256
        and known_record.get("status") == "installed",
        "known receipt differs from sealed plan",
    )
    destination = _canonical(record["new"]["state"])
    _need(not destination.exists(), "fresh held campaign already exists")
    _need(manifest_sha == record["new"]["manifest_sha256"], "new manifest differs from seal")
    release = _installed_release(record["new_release"]["path"], manifest["candidate_sha"])
    _need(release == record["new_release"], "new installed release changed")
    native.contract(manifest)  # current/new contract only, after CAS
    _need(
        _sha(host._read(record["wrapper"]["path"])[1]) == record["wrapper"]["after_sha256"],
        "current wrapper differs from sealed new pin",
    )
    problems = campaign.qualify(manifest, new_path.parent)
    _need(not problems, "new source qualification failed")
    old_manifest_path = _canonical(record["old"]["manifest"])
    _old_manifest_path, _old_manifest_raw, old_manifest = _pinned_object(
        old_manifest_path, record["old"]["manifest_sha256"], "old manifest"
    )
    _old_module_bytes(old_manifest)
    old_state = _canonical(record["old"]["state"])
    old_result = _canonical(record["old"]["result"])
    _need(_sha(host._read(old_state)[1]) == record["old"]["state_sha256"], "old state changed")
    _need(_sha(host._read(old_result)[1]) == record["old"]["result_sha256"], "old result changed")
    _old_native_terminal(old_manifest)
    host._lane_absent()
    _completed_reference(
        old_manifest_path,
        old_state,
        record["old"]["diagnostic"]["cohort"]["worker_id"],
        manifest_sha256=record["old"]["manifest_sha256"],
        state_sha256=record["old"]["state_sha256"],
    )
    inspection = _old_custody_quiet(old_manifest)
    _need(
        fresh_read_native_index(
            old_manifest["policy"]["correction_controller_installation"]["config"], inspection
        )
        == record["old"]["native_index"],
        "native index changed after source upgrade seal",
    )
    recipe = record.get("held_recipe")
    if (
        not isinstance(recipe, dict)
        or not isinstance(recipe.get("worker_config"), dict)
        or not isinstance(recipe.get("frontier"), dict)
    ):
        raise SourceUpgradeHeld("sealed held recipe unavailable")
    cfg_path, _m, _raw, cfg = readmission._new_runtime(new_path, recipe["worker_config"])
    _need(cfg_path == new_path, "new worker config runtime changed")
    try:
        fresh = policy.validate_frontier(
            recipe["frontier"], targets=recipe["frontier"].get("targets")
        )
        _old_state_path, _old_state_raw, old_doc = _pinned_object(
            old_state, record["old"]["state_sha256"], "old campaign state"
        )
        old_frontier = policy.validate_frontier(old_doc["worker_config"]["finite_frontier"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceUpgradeHeld("fresh held frontier invalid") from exc
    _need(
        _sha(json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode())
        == recipe.get("worker_config_sha256"),
        "sealed worker configuration changed",
    )
    _need(
        _sha(json.dumps(fresh, sort_keys=True, separators=(",", ":")).encode())
        == recipe.get("frontier_sha256"),
        "sealed frontier changed",
    )
    readmission._guard_fresh(manifest, cfg, destination, fresh, old_frontier)
    # Existing source initialization produces a held record; no demand is
    # admitted here. A one-use marker prevents a second preparation.
    marker = seal.with_name("source-upgrade-held-consumed.json")
    _need(not marker.exists(), "sealed source upgrade already consumed")
    sealed_locks = record.get("locks")
    if not isinstance(sealed_locks, list) or not all(isinstance(row, dict) for row in sealed_locks):
        raise SourceUpgradeHeld("sealed lock closure unavailable")
    # Campaign.begin takes its own state lock; retain every other old/new/global
    # fence from the pre-CAS plan across the final evidence reread.
    rows = [row for row in sealed_locks if row.get("path") != str(destination) + ".lock"]
    with host._hold(rows):
        _locked_manifest_path, _locked_manifest_raw, locked_old_manifest = _pinned_object(
            old_manifest_path, record["old"]["manifest_sha256"], "old manifest"
        )
        _need(locked_old_manifest == old_manifest, "old manifest changed under locks")
        _old_module_bytes(locked_old_manifest)
        _locked_state_path, _locked_state_raw, old_doc = _pinned_object(
            old_state, record["old"]["state_sha256"], "old campaign state"
        )
        _need(
            _sha(host._read(old_state)[1]) == record["old"]["state_sha256"],
            "old state changed under locks",
        )
        _need(
            _sha(host._read(old_result)[1]) == record["old"]["result_sha256"],
            "old result changed under locks",
        )
        _old_native_terminal(locked_old_manifest)
        host._lane_absent()
        _need(
            _completed_reference(
                old_manifest_path,
                old_state,
                record["old"]["diagnostic"]["cohort"]["worker_id"],
                manifest_sha256=record["old"]["manifest_sha256"],
                state_sha256=record["old"]["state_sha256"],
            )["result_sha256"]
            == record["old"]["result_sha256"],
            "old result changed under locks",
        )
        _need(
            _old_custody_quiet(locked_old_manifest) == inspection,
            "old custody changed under locks",
        )
        _need(
            _inert_new_custody(manifest, destination, inspection["highwater"]["anchor"])
            == record["new"]["custody"],
            "new custody changed under locks",
        )
        _need(
            fresh_read_native_index(
                locked_old_manifest["policy"]["correction_controller_installation"]["config"],
                inspection,
            )
            == record["old"]["native_index"],
            "native index changed under locks",
        )
        _need(not destination.exists() and not marker.exists(), "fresh held state changed")
        reference = {
            "state_path": str(old_state),
            "state_sha256": record["old"]["state_sha256"],
            "worker": record["old"]["diagnostic"].get("cohort", {}),
            "result_path": str(old_result),
            "result_sha256": record["old"]["result_sha256"],
            # This is a sealed no-ACT disposition reference, never a fabricated
            # no-start proof; generic preparation only hashes it for provenance.
            "proof": {
                "schema": SCHEMA,
                "plan_sha256": confirm_plan_sha256,
                "diagnostic": record["old"]["diagnostic"],
            },
        }
        plan_doc, readmission_sha = readmission._plan(
            reference, new_path, raw, cfg, destination, fresh
        )
        token = readmission._intent_token(plan_doc, readmission_sha)
        intent = seal.with_name("source-upgrade-readmission-intent.json")
        known = readmission._recover_or_create(
            campaign,
            native,
            destination,
            manifest,
            cfg,
            fresh,
            old_doc,
            readmission_sha,
            intent,
            token,
        )
        created = campaign.Campaign(destination).read()
        if created is None:
            raise SourceUpgradeHeld("known preparation did not create held campaign")
        host._write_once(
            marker,
            {
                "schema": SCHEMA,
                "seal_sha256": _sha(seal_raw),
                "state": str(destination),
                "revision": created["revision"],
                "known": str(known),
                "authority": False,
            },
        )
    return {
        "status": "fresh_held_prepared",
        "authority": False,
        "state": str(destination),
        "revision": created["revision"],
        "marker": str(marker),
    }
