"""Fail-closed restoration of only the canonical host closer wrapper.

The contract is deliberately a new, bounded operation.  Historical campaign
state remains a byte-for-byte witness: this module has no retry, acceptance,
or UNKNOWN-reclassification authority.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

import closer_mechanisms as mechanisms

SCHEMA = "closer-host-restore-contract/v1"
MAX_BYTES = 8 * 1024 * 1024


class Refused(RuntimeError):
    pass


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _path(value: str | Path) -> Path:
    path = Path(value).absolute()
    resolved = path.resolve(strict=False)
    # macOS presents /tmp through the /private/tmp compatibility alias.  It is
    # a platform namespace alias, not an alternate witness location; normalize
    # it before applying the ordinary no-alias rule.
    tmp_alias = str(path).startswith("/tmp/") and resolved == Path("/private" + str(path))
    if path.is_symlink() or (resolved != path and not tmp_alias):
        raise Refused("aliased_path")
    return resolved


def _read(value: str | Path, limit: int = MAX_BYTES) -> tuple[Path, bytes]:
    """Read a stable, owned, non-aliased regular file."""
    path = _path(value)
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1:
        raise Refused("unsafe_regular_file")
    if before.st_size > limit:
        raise Refused("oversized_witness")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise Refused("witness_changed_during_open")
        raw = b""
        while block := os.read(fd, 65536):
            raw += block
            if len(raw) > limit:
                raise Refused("oversized_witness")
    finally:
        os.close(fd)
    after = os.lstat(path)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise Refused("witness_changed_during_read")
    return path, raw


def _obj(value: str | Path) -> tuple[Path, bytes, dict[str, Any]]:
    path, raw = _read(value)
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refused("invalid_json_witness") from exc
    if not isinstance(parsed, dict):
        raise Refused("object_witness_required")
    return path, raw, parsed


def _ref(value: object, label: str) -> tuple[Path, bytes]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise Refused(f"{label}_exact_reference_required")
    digest = value.get("sha256")
    if not isinstance(value.get("path"), str) or not isinstance(digest, str) or len(digest) != 64:
        raise Refused(f"{label}_exact_reference_required")
    path, raw = _read(value["path"])
    if _sha(raw) != digest:
        raise Refused(f"{label}_bytes_changed")
    return path, raw


def _json_ref(value: object, label: str) -> tuple[Path, dict[str, Any]]:
    path, raw = _ref(value, label)
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refused(f"{label}_not_json") from exc
    if not isinstance(parsed, dict):
        raise Refused(f"{label}_not_object")
    return path, parsed


def _same(actual: object, expected: object, reason: str) -> None:
    if actual != expected:
        raise Refused(reason)


def _signed(path: Path, root: Path, label: str) -> dict[str, Any]:
    if not root.is_dir() or root.is_symlink() or stat.S_IMODE(os.lstat(root).st_mode) != 0o700:
        raise Refused(f"{label}_root_invalid")
    _, _, envelope = _obj(path)
    try:
        record = mechanisms.verify_signed_record(root, envelope)
    except (OSError, ValueError, KeyError, TypeError, mechanisms.MechanismError) as exc:
        raise Refused(f"{label}_signature_invalid") from exc
    if not isinstance(record, dict):
        raise Refused(f"{label}_record_invalid")
    return record


def _dead(pid: object) -> None:
    if type(pid) is not int or pid <= 0:
        raise Refused("signed_pid_invalid")
    for probe in (lambda: os.kill(pid, 0), lambda: os.killpg(pid, 0)):
        try:
            probe()
        except ProcessLookupError:
            continue
        except OSError as exc:
            raise Refused("signed_pid_liveness_unknown") from exc
        raise Refused("signed_process_or_group_is_live")


def _check_manifest(plan: dict[str, Any]) -> dict[str, Any]:
    root = _path(plan["new_runtime_root"])
    _, raw, manifest = _obj(root / "host-manifest.json")
    _same(_sha(raw), plan.get("manifest_sha256"), "manifest_digest_changed")
    _same(manifest.get("candidate_sha"), plan.get("source_sha"), "manifest_source_changed")
    modules = manifest.get("modules")
    _same(modules, plan.get("modules"), "manifest_module_closure_changed")
    if not isinstance(modules, dict) or not modules:
        raise Refused("manifest_module_closure_missing")
    for name, digest in modules.items():
        if not isinstance(name, str) or "/" in name or ".." in name or not isinstance(digest, str):
            raise Refused("unsafe_manifest_module")
        _, module = _read(root / name)
        _same(_sha(module), digest, "manifest_module_bytes_changed")
    return manifest


def _check_lifecycle(
    plan: dict[str, Any], request: dict[str, Any], result: dict[str, Any], manifest: dict[str, Any]
) -> None:
    root = _path(plan["new_observation_root"])
    admission_path = _ref(request["admission"], "admission")[0]
    admission = _signed(admission_path, root, "admission")
    binding = admission.get("binding")
    if not isinstance(binding, dict) or binding.get("candidate_sha") != plan.get("source_sha"):
        raise Refused("admission_binding_changed")
    expected_binding = {
        "mechanism": mechanisms.NATIVE,
        "mechanism_contract_digest": mechanisms.digest(manifest["mechanism_contract"]),
        "policy_digest": mechanisms.digest(manifest["policy"]),
        **{key: manifest.get(key) for key in mechanisms.PIN_KEYS},
    }
    _same(binding, expected_binding, "admission_manifest_binding_changed")
    # Admission records retain the complete context; worker handoff repeats it
    # under ``admission`` and its nonce is the cross-record correlation key.
    context = admission.get("context")
    if not isinstance(context, dict):
        raise Refused("admission_context_invalid")
    if context.get("schema") != "native-invocation-admission/v2":
        raise Refused("finite_admission_required")
    _same(
        context.get("runtime_manifest"),
        str(_path(plan["new_runtime_root"]) / "host-manifest.json"),
        "admission_manifest_path_changed",
    )
    nonce = context.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        raise Refused("admission_nonce_invalid")
    nonce_root = _path(root / nonce)
    _same(admission_path, nonce_root / "admission.json", "admission_path_changed")
    worker_path = _ref(request["worker_handoff"], "worker_handoff")[0]
    terminal_path = _ref(request["terminal"], "terminal")[0]
    worker = _signed(worker_path, root, "worker_handoff")
    terminal = _signed(terminal_path, root, "terminal")
    if worker.get("schema") != "native-worker-handoff/v1":
        raise Refused("worker_handoff_schema_invalid")
    _same(worker.get("binding"), binding, "worker_binding_changed")
    _same(terminal.get("binding"), binding, "terminal_binding_changed")
    _same(worker.get("admission"), context, "worker_admission_changed")
    _same(worker.get("nonce"), nonce, "worker_nonce_changed")
    _same(terminal.get("nonce"), nonce, "terminal_nonce_changed")
    fields = worker.get("fields")
    if not isinstance(fields, dict):
        raise Refused("worker_fields_invalid")
    _same(fields.get("state"), plan.get("canary_state"), "worker_state_changed")
    worker_id = Path(str(fields.get("result", ""))).stem
    if not worker_id or result.get("worker_id") != worker_id or result.get("outcome") != "unknown":
        raise Refused("raw_unknown_result_binding_changed")
    _same(worker_path, nonce_root / f"worker-{worker_id}.json", "worker_handoff_path_changed")
    _same(terminal_path, nonce_root / "terminal.json", "terminal_path_changed")
    result_path = _ref(request["result"], "result")[0]
    _same(fields.get("result"), str(result_path), "worker_result_path_changed")
    if (
        terminal.get("schema") != "native-wrapper-lifecycle/v1"
        or terminal.get("source") != "owned-native-wrapper-process"
        or terminal.get("passed") is not True
    ):
        raise Refused("terminal_lifecycle_invalid")
    tick = terminal.get("tick_result")
    launched = (
        [
            x
            for x in tick
            if isinstance(x, dict)
            and x.get("kind") == "launched"
            and x.get("worker_id") == worker_id
        ]
        if isinstance(tick, list)
        else []
    )
    if len(launched) != 1:
        raise Refused("signed_worker_launch_missing")
    if "pid" in fields:
        _same(launched[0].get("pid"), fields["pid"], "worker_launch_pid_changed")
    _dead(launched[0].get("pid"))
    # Retained native-wrapper-lifecycle/v1 records hold these identities under
    # wrapper/entry, not as caller-selected process identifiers.
    wrapper_identity = terminal.get("wrapper")
    entry_identity = terminal.get("entry")
    if not isinstance(wrapper_identity, dict) or not isinstance(entry_identity, dict):
        raise Refused("signed_wrapper_or_entry_identity_invalid")
    _dead(wrapper_identity.get("pid"))
    _dead(entry_identity.get("pid"))
    try:
        detail = (
            json.loads(result["detail"])
            if isinstance(result.get("detail"), str)
            else result["detail"]
        )
        execution = detail["native_execution"]
        phases = execution["phases"]
        phase_a, phase_b, phase_c = phases["A"], phases["B"], phases["C"]
        cleanup = phase_c["cleanup"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise Refused("cleanup_witness_invalid") from exc
    if not isinstance(execution, dict) or execution.get("schema") != "native-round-execution/v1":
        raise Refused("native_execution_schema_invalid")
    _same(execution.get("admission_nonce"), nonce, "execution_nonce_changed")
    _same(execution.get("worker"), fields, "execution_worker_changed")
    _same(result.get("native_admission"), context, "result_admission_changed")
    if (
        not isinstance(phase_a, dict)
        or phase_a.get("admitted") is not True
        or phase_a.get("exit_code") != 0
    ):
        raise Refused("phase_a_invalid")
    if not isinstance(phase_b, dict) or phase_b.get("started") is not False:
        raise Refused("phase_b_invalid")
    if (
        not isinstance(phase_c, dict)
        or phase_c.get("attempted") is not True
        or phase_c.get("exit_code") != 65
    ):
        raise Refused("phase_c_invalid")
    if not isinstance(cleanup, dict) or cleanup.get("schema") != "closer-native-lease-cleanup/v1":
        raise Refused("cleanup_schema_invalid")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", str(phase_a.get("token_sha256", "")))
        or type(phase_a.get("lease_inode")) is not int
        or phase_a["lease_inode"] <= 0
        or cleanup.get("schema") != "closer-native-lease-cleanup/v1"
        or cleanup.get("lane") != "closer"
        or cleanup.get("lane_dir") != str(Path.home() / ".codex/handoff/.lane-closer.lock")
        or phase_a.get("token_sha256") != cleanup.get("expected_token_sha256")
        or phase_a.get("token_sha256") != cleanup.get("meta_sha256")
        or phase_a.get("lease_inode") != cleanup.get("released_lease_inode")
        or cleanup.get("exact_token_matched") is not True
        or cleanup.get("released") is not True
    ):
        raise Refused("cleanup_token_or_inode_invalid")
    started, finished, released = (
        phase_c.get("started_at"),
        phase_c.get("finished_at"),
        cleanup.get("released_at"),
    )
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for value in (started, finished, released)
    ):
        raise Refused("cleanup_release_time_invalid")
    assert isinstance(started, (int, float))
    assert isinstance(finished, (int, float))
    assert isinstance(released, (int, float))
    if not started <= released <= finished:
        raise Refused("cleanup_release_time_invalid")


def _dynamic_locks(plan: dict[str, Any]) -> list[Path]:
    protected = {
        str(plan["old_manifest_path"]),
        str(Path(plan["new_runtime_root"]) / "host-manifest.json"),
    }
    found: list[Path] = []
    for root_value in (plan["old_campaign_root"], plan["new_campaign_root"]):
        root = _path(root_value)
        campaigns = sorted(root.glob("*.campaign.json"))
        if not root.is_dir() or len(campaigns) > 512:
            raise Refused("campaign_scan_invalid")
        for campaign in campaigns:
            _, _, item = _obj(campaign)
            # Campaign records store worker_config at the top level. A nested
            # state view silently omits the real pipeline's existing runlock.
            config = item.get("worker_config", {})
            if not isinstance(config, dict):
                raise Refused("campaign_worker_config_invalid")
            dispatch = config.get("dispatch_state")
            if config.get("runtime_manifest") in protected and isinstance(dispatch, str):
                candidate = _path(dispatch + ".pipeline.json.runlock")
                if candidate.exists():
                    found.append(candidate)
            if item.get("release_attempt"):
                release = config.get("release")
                if not isinstance(release, dict) or not isinstance(release.get("config_file"), str):
                    raise Refused("campaign_release_config_invalid")
                _, _, release_config = _obj(release["config_file"])
                args = release_config.get("args")
                if not isinstance(args, dict):
                    raise Refused("campaign_release_arguments_invalid")
                resources = [
                    _path(release_config["record"]),
                    _path(args["mirror"]),
                    _path(args["installer"]),
                ]
                protected_resources = [
                    _path(plan[key])
                    for key in (
                        "wrapper",
                        "old_runtime_root",
                        "new_runtime_root",
                        "old_observation_root",
                        "new_observation_root",
                        "custody_path",
                        "canary_state",
                    )
                ]
                if any(
                    a == b or a in b.parents or b in a.parents
                    for a in resources
                    for b in protected_resources
                ):
                    record = item.get("release_record")
                    if not isinstance(record, str):
                        raise Refused("campaign_release_record_invalid")
                    for suffix in (".lock", ".publisher.lock"):
                        candidate = _path(record + suffix)
                        if candidate.exists():
                            found.append(candidate)
    return found


def _locks(plan: dict[str, Any], wrapper: Path) -> list[dict[str, Any]]:
    static = [
        *[
            Path(root) / leaf
            for root in (plan["old_observation_root"], plan["new_observation_root"])
            for leaf in ("native-supervisor.lock", "native-entry.lock")
        ],
        Path(plan["custody_path"]) / "writer.lock",
        wrapper.parent / ".closer-public989-install.lock",
        Path.home() / ".codex/handoff/.lane-closer.lock.coord.lock",
    ]
    rows = {str(_path(item)): False for item in static}
    for item in _dynamic_locks(plan):
        rows[str(_path(item))] = True
    return [{"path": path, "must_exist": rows[path]} for path in sorted(rows)]


def _lane_absent() -> None:
    """A successor lease is current authority, even when attempt-13 is dead."""
    lane = Path.home() / ".codex/handoff/.lane-closer.lock"
    if lane.exists() or lane.is_symlink():
        raise Refused("current_closer_lane_mutex_present")


def plan(request_path: str | Path) -> dict[str, Any]:
    try:
        _, request_raw, request = _obj(request_path)
        if request.get("schema") != SCHEMA or set(request) != {
            "schema",
            "swap_intent",
            "swap_known",
            "backup",
            "admission",
            "worker_handoff",
            "terminal",
            "result",
        }:
            raise Refused("request_schema_or_fields_invalid")
        _, intent = _json_ref(request["swap_intent"], "swap_intent")
        _, known = _json_ref(request["swap_known"], "swap_known")
        _, backup = _json_ref(request["backup"], "backup")
        _, result = _json_ref(request["result"], "result")
        retained = intent.get("plan")
        if (
            not isinstance(retained, dict)
            or retained.get("schema") != "closer-temporary-canonical-wrapper-window/v1"
        ):
            raise Refused("retained_swap_plan_invalid")
        wrapper = _path(Path.home() / ".codex/bin/cursor-lane.sh")
        _same(retained.get("wrapper"), str(wrapper), "canonical_wrapper_mismatch")
        for field in ("before_sha256", "after_sha256"):
            _same(intent.get(field), retained.get(field), "swap_intent_crossjoin_changed")
            _same(known.get(field), retained.get(field), "swap_known_crossjoin_changed")
        _same(
            known.get("original_deadline"),
            retained.get("original_deadline"),
            "deadline_crossjoin_changed",
        )
        original = base64.b64decode(backup.get("base64", ""), validate=True)
        _same(_sha(original), retained.get("before_sha256"), "backup_bytes_changed")
        _same(backup.get("sha256"), retained.get("before_sha256"), "backup_digest_changed")
        _same(backup.get("mode"), retained.get("before_mode"), "backup_mode_changed")
        _, current = _read(wrapper)
        _same(_sha(current), retained.get("after_sha256"), "current_wrapper_not_swapped_bytes")
        manifest = _check_manifest(retained)
        _check_lifecycle(retained, request, result, manifest)
        _lane_absent()
        rows = _locks(retained, wrapper)
        for row in rows:
            if row["must_exist"]:
                _read(row["path"], 1024 * 1024)
        witnesses = {
            name: {"path": str(_ref(request[name], name)[0]), "sha256": request[name]["sha256"]}
            for name in (
                "swap_intent",
                "swap_known",
                "backup",
                "admission",
                "worker_handoff",
                "terminal",
                "result",
            )
        }
        output = {
            "schema": SCHEMA,
            "eligible": True,
            "request_sha256": _sha(request_raw),
            "wrapper": str(wrapper),
            "before_sha256": retained["before_sha256"],
            "after_sha256": retained["after_sha256"],
            "mode": retained["before_mode"],
            "receipt_dir": str(_path(retained["receipts_root"]) / "host-restore-contract"),
            "locks": rows,
            "witnesses": witnesses,
            "host_restore_only": True,
            "effect_retry_authority": False,
            "acceptance_authority": False,
            "unknown_reclassification_authority": False,
        }
        output["plan_sha256"] = _sha(
            json.dumps(output, sort_keys=True, separators=(",", ":")).encode()
        )
        return output
    except (KeyError, OSError, TypeError, ValueError, mechanisms.MechanismError, Refused) as exc:
        return {"schema": SCHEMA, "eligible": False, "reason": str(exc), "host_restore_only": True}


@contextlib.contextmanager
def _hold(rows: list[dict[str, Any]]) -> Iterator[None]:
    held: list[int] = []
    try:
        for row in rows:
            path = _path(row["path"])
            if row["must_exist"]:
                try:
                    _read(path, 1024 * 1024)
                except FileNotFoundError as exc:
                    raise Refused("mandatory_lock_missing") from exc
            flags = (os.O_RDONLY if row["must_exist"] else os.O_RDWR) | getattr(os, "O_NOFOLLOW", 0)
            if not row["must_exist"]:
                flags |= os.O_CREAT
            try:
                fd = os.open(path, flags, 0o600)
            except FileNotFoundError as exc:
                raise Refused("mandatory_lock_missing") from exc
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                os.close(fd)
                raise Refused("unsafe_lock")
            held.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Refused("lock_contended") from exc
            try:
                current = os.lstat(path)
            except FileNotFoundError as exc:
                raise Refused("lock_missing_after_acquisition") from exc
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise Refused("lock_identity_changed")
        yield
    finally:
        for fd in reversed(held):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _write_once(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(fd, "wb") as stream:
        stream.write((json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def apply(request_path: str | Path, expected_plan_sha256: str) -> dict[str, Any]:
    first = plan(request_path)
    if not first.get("eligible") or first.get("plan_sha256") != expected_plan_sha256:
        raise Refused("plan_changed")
    with _hold(first["locks"]):
        _lane_absent()
        if plan(request_path).get("plan_sha256") != first["plan_sha256"]:
            raise Refused("plan_changed_under_locks")
        receipt_dir = Path(first["receipt_dir"])
        intent_path = receipt_dir / "restore-intent.json"
        known_path = receipt_dir / "restore-known.json"
        if intent_path.exists() or known_path.exists():
            raise Refused("prior_restore_outcome_requires_reconciliation")
        _write_once(
            intent_path,
            {**first, "status": "intent_recorded_before_replace", "recorded_at": time.time()},
        )
        _, _, request = _obj(request_path)
        _, backup = _json_ref(request["backup"], "backup")
        replacement = base64.b64decode(backup["base64"], validate=True)
        if _sha(replacement) != first["before_sha256"]:
            raise Refused("replacement_bytes_changed_before_replace")
        wrapper = Path(first["wrapper"])
        fd, temporary = tempfile.mkstemp(prefix=".host-restore-", dir=wrapper.parent)
        replaced = False
        try:
            stream = os.fdopen(fd, "wb")
            fd = -1
            with stream:
                stream.write(replacement)
                stream.flush()
                os.fchmod(stream.fileno(), first["mode"])
                os.fsync(stream.fileno())
            os.replace(temporary, wrapper)
            replaced = True
            directory = os.open(wrapper.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            _, readback = _read(wrapper)
            if (
                _sha(readback) != first["before_sha256"]
                or stat.S_IMODE(os.lstat(wrapper).st_mode) != first["mode"]
            ):
                raise Refused("postreplace_readback_failed")
            known = {**first, "status": "host_wrapper_restored", "restored_at": time.time()}
            _write_once(known_path, known)
            return known
        except Exception:
            if replaced:
                raise Refused("postreplace_outcome_uncertain")
            raise
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                Path(temporary).unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        if args.request or args.apply or args.expected_plan_sha256:
            parser.error("--selftest cannot be combined with restore options")
        with tempfile.TemporaryDirectory(prefix="closer-host-restore-selftest-") as temporary:
            root = Path(temporary).resolve()
            row = {"path": str(root / "writer.lock"), "must_exist": False}
            with _hold([row]):
                record = root / "intent.json"
                _write_once(record, {"stage": "intent"})
                assert _obj(record)[2] == {"stage": "intent"}
                try:
                    _write_once(record, {"stage": "changed"})
                except FileExistsError:
                    pass
                else:
                    raise AssertionError("immutable receipt was overwritten")
        print(
            json.dumps(
                {"schema": SCHEMA, "selftest": "ok", "host_restore_only": True}, sort_keys=True
            )
        )
        return 0
    if not args.request:
        parser.error("--request is required unless --selftest is used")
    if args.apply and not args.expected_plan_sha256:
        parser.error("--apply requires --expected-plan-sha256")
    try:
        result = (
            apply(args.request, args.expected_plan_sha256) if args.apply else plan(args.request)
        )
    except (Refused, OSError, ValueError) as exc:
        result = {
            "schema": SCHEMA,
            "eligible": False,
            "reason": str(exc),
            "host_restore_only": True,
        }
    print(json.dumps(result, sort_keys=True))
    return 2 if result.get("eligible") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
