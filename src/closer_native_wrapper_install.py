"""One-way, source-owned CAS installation of a newly pinned native wrapper.

This deliberately does not create a campaign or launch native entry.  It only
installs bytes already pinned by a new host manifest after a retained-no-B
reconciliation has been authenticated.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import shlex
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

import closer_host_restore_contract as host

SCHEMA = "closer-native-wrapper-install/v1"
PROSPECTIVE_SCHEMA = "closer-native-wrapper-prospective-install/v1"
RESTORED_SHA = "6ba0afd39a280aac9ae8b088efdfe0c17390f7d8f103e6a309c3bf1cb9ede71b"
RESTORED_MODE = 0o755


class InstallHeld(RuntimeError):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _need(ok: object, why: str) -> None:
    if not ok:
        raise InstallHeld(why)


def _receipt(path: str | Path) -> dict[str, Any]:
    _, _, value = host._obj(path)
    _need(
        value.get("status") == "host_wrapper_restored"
        and value.get("before_sha256") == RESTORED_SHA
        and value.get("mode") == RESTORED_MODE,
        "host restoration receipt is not the reviewed canonical preimage",
    )
    return value


def _manifest(path: str | Path) -> tuple[Path, bytes, dict[str, Any]]:
    import closer_campaign as campaign

    value, raw, manifest = host._obj(path)
    _need(
        not campaign.validate_manifest(manifest) and not campaign.verify_modules(manifest),
        "new manifest closure invalid",
    )
    contract = manifest.get("mechanism_contract", {})
    _need(
        contract.get("mechanism") == "native-handoff/v1"
        and isinstance(contract.get("wrapper"), dict),
        "new native contract invalid",
    )
    wrapper = host._path(Path.home() / ".codex/bin/cursor-lane.sh")
    _need(
        contract["wrapper"].get("path") == str(wrapper),
        "new manifest does not pin canonical wrapper",
    )
    _need(
        isinstance(contract["wrapper"].get("sha256"), str)
        and len(contract["wrapper"]["sha256"]) == 64,
        "new wrapper hash invalid",
    )
    for key in ("campaign_root", "observation_root", "interpreter"):
        _need(key in contract, "new native contract incomplete")
    _need(isinstance(manifest.get("launcher", {}).get("path"), str), "new launcher missing")
    import closer_native_observer as native
    import closer_runtime_binding as binding

    for module in (native, binding):
        filename = module.__file__
        _need(isinstance(filename, str), "current native source path unavailable")
        assert isinstance(filename, str)
        source = Path(filename).resolve()
        _need(
            manifest["modules"].get(source.name) == _sha(host._read(source)[1]),
            "new native engine differs from current repaired source",
        )
    for key in ("campaign_root", "observation_root"):
        root = host._path(contract[key])
        info = root.stat()
        _need(
            stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022,
            "new native root is not owned/non-writable",
        )
    interpreter = contract["interpreter"]
    _need(
        isinstance(interpreter, dict)
        and Path(interpreter["path"]).is_file()
        and os.access(interpreter["path"], os.X_OK)
        and _sha(Path(interpreter["path"]).read_bytes()) == interpreter["sha256"],
        "new interpreter pin changed",
    )
    launcher = manifest["launcher"]
    _need(_sha(host._read(launcher["path"])[1]) == launcher["sha256"], "new launcher pin changed")
    return value, raw, manifest


def _guard_source(source: bytes) -> bytes:
    """Only accept the exact reviewed restored predecessor, never a loose regex base."""
    _need(_sha(source) == RESTORED_SHA, "canonical preimage changed")
    text = source.decode("utf-8")
    marker, end = "# BEGIN CLOSER PINNED NATIVE ENTRY v1", "# END CLOSER PINNED NATIVE ENTRY v1"
    _need(text.count(marker) == text.count(end) == 1, "reviewed predecessor entry shape changed")
    start, finish = text.index(marker), text.index(end) + len(end)
    guard = text.rfind('if [[ "$LANE" == "closer" ]]; then\n', 0, start)
    _need(
        guard >= 0 and "original window expired" in text[guard:start].lower(),
        "reviewed expired guard absent",
    )
    return (text[:guard] + text[finish:]).encode()


def _render(base: bytes, manifest: dict[str, Any], manifest_path: Path) -> bytes:
    import closer_native_observer as native

    contract = manifest["mechanism_contract"]
    rendered = native.render_cursor_wrapper(
        base.decode(),
        manifest_path=manifest_path,
        campaign_root=Path(contract["campaign_root"]),
        interpreter=Path(
            contract["interpreter"]["path"]
            if isinstance(contract["interpreter"], dict)
            else contract["interpreter"]
        ),
        launcher=Path(manifest["launcher"]["path"]),
    )
    # Normal lane execution remains unavailable before readmission; qualification
    # remains model-free and can enter only with its explicit request flag.
    state = manifest["policy"]["correction_controller_installation"]["config"]["state_path"]
    gate = (
        'if [[ "$LANE" == "closer" ]]; then\n'
        '  [[ -f "$HOME/.codex/handoff/closer-native-handoff.enabled" ]] || exit 78\n'
        '  if [[ -z "${CLOSER_NATIVE_QUALIFICATION_REQUEST:-}" && ! -f '
        + shlex.quote(state)
        + " ]]; then\n"
        '    echo "closer native readmission required; no fallback" >&2\n'
        "    exit 78\n  fi\nfi\n"
    )
    _need(rendered.count(native.MARKER) == 1, "new native entry marker is not unique")
    return rendered.replace(native.MARKER, gate + native.MARKER, 1).encode()


def _rows(
    old_manifest: Path, old_state: Path, new: dict[str, Any], state: Path
) -> list[dict[str, Any]]:
    import closer_correction_controller as controller
    import closer_retained_reconciliation as retained

    wrapper = Path.home() / ".codex/bin/cursor-lane.sh"
    rows = retained.lock_rows(old_manifest, old_state)
    rows.append(
        {"path": str(wrapper.parent / ".closer-public989-install.lock"), "must_exist": True}
    )
    rows.append({"path": str(state) + ".lock", "must_exist": False})
    root = Path(new["mechanism_contract"]["observation_root"])
    rows.extend(
        {"path": str(root / name), "must_exist": True}
        for name in ("native-entry.lock", "native-supervisor.lock")
    )
    config = controller.ControllerConfig(
        **new["policy"]["correction_controller_installation"]["config"]
    )
    custody_lock = Path(str(state) + ".evidence") / config.namespace / "writer.lock"
    if custody_lock.exists() or custody_lock.is_symlink():
        rows.append({"path": str(custody_lock), "must_exist": True})
    merged: dict[str, bool] = {}
    for row in rows:
        path = str(host._path(row["path"]))
        merged[path] = merged.get(path, False) or row["must_exist"]
    return [{"path": path, "must_exist": merged[path]} for path in sorted(merged)]


def _restored_predecessor(
    receipt_path: str | Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Join the restored canonical bytes to the exact old native campaign.

    The prospective path deliberately has no retained-reconciliation record.
    It still needs the immutable host-restore receipt and its original swap
    witness; otherwise a caller could pair a real no-start proof with an
    unrelated archived wrapper base.  The old no-start campaign is *not* the
    historical attempt-13 campaign, so no result/state cross-join belongs here.
    """
    restored = _receipt(receipt_path)
    witnesses = restored.get("witnesses")
    if not isinstance(witnesses, dict):
        raise InstallHeld("host restoration witnesses missing")
    _, swap_intent = host._json_ref(witnesses.get("swap_intent"), "restored_swap_intent")
    predecessor = swap_intent.get("plan")
    if not isinstance(predecessor, dict):
        raise InstallHeld("restoration predecessor plan missing")
    _need(predecessor.get("before_sha256") == RESTORED_SHA, "restoration predecessor differs")
    return restored, predecessor


def _archived_base(restored: dict[str, Any]) -> bytes:
    """Read the only permitted renderer base from the restore receipt witness."""
    witnesses = restored.get("witnesses")
    if not isinstance(witnesses, dict):
        raise InstallHeld("host restoration witnesses missing")
    _, backup = host._json_ref(witnesses.get("backup"), "restored_backup")
    try:
        raw = base64.b64decode(backup.get("base64", ""), validate=True)
    except (TypeError, ValueError) as exc:
        raise InstallHeld("restored backup bytes invalid") from exc
    _need(
        _sha(raw) == RESTORED_SHA
        and backup.get("sha256") == RESTORED_SHA
        and backup.get("mode") == RESTORED_MODE,
        "restored backup is not the reviewed canonical preimage",
    )
    return raw


def _prospective_rows(
    predecessor: dict[str, Any],
    reference: dict[str, Any],
    manifest: dict[str, Any],
    state: Path,
) -> list[dict[str, Any]]:
    """Derive, rather than accept, the old/new writer closure.

    The host-restore predecessor is only a renderer witness; it is not trusted
    to name the current source custody.  Current manifests instead derive both
    controller namespaces from their pinned controller configurations.  The
    locks coordinate cooperative source writers; they are not an assertion of
    atomic replacement against an arbitrary same-UID writer.
    """
    wrapper = Path.home() / ".codex/bin/cursor-lane.sh"
    rows = host._locks(predecessor, wrapper)
    old_root = Path(reference["manifest"]["mechanism_contract"]["observation_root"])
    new_root = Path(manifest["mechanism_contract"]["observation_root"])
    rows.extend(
        {"path": str(root / name), "must_exist": True}
        for root in (old_root, new_root)
        for name in ("native-entry.lock", "native-supervisor.lock")
    )
    rows.extend(
        [
            {"path": str(reference["state_path"]) + ".lock", "must_exist": True},
            {"path": str(state) + ".lock", "must_exist": False},
            {"path": str(wrapper.parent / ".closer-public989-install.lock"), "must_exist": True},
            {
                "path": str(Path.home() / ".codex/handoff/.lane-closer.lock.coord.lock"),
                "must_exist": True,
            },
        ]
    )
    for current_manifest, current_state, label in (
        (reference["manifest"], Path(reference["state_path"]), "old"),
        (manifest, state, "new"),
    ):
        rows.append(
            {
                "path": str(_current_custody_writer(current_manifest, current_state, label)),
                "must_exist": True,
            }
        )
    merged: dict[str, bool] = {}
    for row in rows:
        path = str(host._path(row["path"]))
        merged[path] = merged.get(path, False) or bool(row["must_exist"])
    return [{"path": path, "must_exist": merged[path]} for path in sorted(merged)]


def _current_custody_writer(manifest: dict[str, Any], state: Path, label: str) -> Path:
    """Derive an actual controller writer fence from the current manifest.

    A source-controlled manifest cannot omit its full controller identity and
    fall back to unrelated historical restore locks.  This is deliberately a
    lock-shape check, not controller admission or a native operation.
    """
    import closer_correction_controller as controller

    try:
        config = manifest["policy"]["correction_controller_installation"]["config"]
        current = controller.ControllerConfig(**config)
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallHeld(f"{label} controller config is unavailable") from exc
    _need(current.state_path == str(state), f"{label} controller state path differs")
    custody = host._path(Path(str(state) + ".evidence") / current.namespace)
    try:
        info = custody.stat()
    except OSError as exc:
        raise InstallHeld(f"{label} controller custody is unavailable") from exc
    _need(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700,
        f"{label} controller custody is not private/owned",
    )
    writer = custody / "writer.lock"
    try:
        writer_info = os.lstat(writer)
    except OSError as exc:
        raise InstallHeld(f"{label} controller writer lock is unavailable") from exc
    _need(
        stat.S_ISREG(writer_info.st_mode)
        and writer_info.st_uid == os.getuid()
        and not writer.is_symlink(),
        f"{label} controller writer lock is unsafe",
    )
    if not state.exists():
        expected = {
            "namespace.json",
            "highwater-0000000000000000.json",
            "writer.lock",
        }
        _need(set(os.listdir(custody)) == expected, f"{label} controller custody is not pristine")
        try:
            namespace = controller.decode(host._read(custody / "namespace.json")[1])
            anchor = controller.decode(host._read(custody / "highwater-0000000000000000.json")[1])
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            raise InstallHeld(f"{label} controller custody metadata is unavailable") from exc
        _need(
            namespace
            == {
                "schema": "local-correction-controller-namespace/v1",
                "config_sha256": controller.digest(current.source_binding()),
            }
            and anchor.get("revision") == 0,
            f"{label} controller custody metadata differs",
        )
    return writer


def _pristine_prospective_namespace(manifest: dict[str, Any], state: Path) -> None:
    """Accept only the two pre-provisioned native lock files in a new root."""
    campaign_root = host._path(manifest["mechanism_contract"]["campaign_root"])
    observation_root = host._path(manifest["mechanism_contract"]["observation_root"])
    _need(
        state.parent == campaign_root
        and state.name.endswith(".campaign.json")
        and not state.exists()
        and not state.is_symlink()
        and not list(campaign_root.glob("*.campaign.json")),
        "new campaign namespace is not empty/exact",
    )
    _need(
        not (observation_root / "native-active.json").exists()
        and not (observation_root / "native-active.json").is_symlink(),
        "new native namespace already has an invocation",
    )
    names = {item.name for item in observation_root.iterdir()}
    _need(
        names == {"native-entry.lock", "native-supervisor.lock"},
        "new native namespace is not pristine",
    )


def _old_parent_terminal(reference: dict[str, Any]) -> None:
    """Require the old invocation to be terminal before changing its wrapper pin."""
    root = host._path(reference["manifest"]["mechanism_contract"]["observation_root"])
    _, _raw, active = host._obj(root / "native-active.json")
    nonce = reference["proof"].get("admission_nonce")
    _need(
        active.get("status") == "terminal"
        and isinstance(nonce, str)
        and nonce
        and active.get("nonce") == nonce,
        "old native parent is not terminal for the signed no-start proof",
    )


def _existing_locks_present(rows: list[dict[str, Any]]) -> None:
    """Do not call a vanished inherited lock closure eligible."""
    for row in rows:
        if row["must_exist"]:
            try:
                host._read(row["path"], 1024 * 1024)
            except FileNotFoundError as exc:
                raise InstallHeld("mandatory lock missing") from exc


def plan_prospective(
    old_manifest: str | Path,
    old_state: str | Path,
    worker: str,
    new_manifest: str | Path,
    new_state: str | Path,
    restore_receipt: str | Path,
) -> dict[str, Any]:
    """Prepare only the wrapper half of a signed finite-v2 transition.

    It consumes no proof and creates neither a campaign nor a native entry.
    A later, separately authenticated readmission path may inspect this
    wrapper receipt; this installer grants it no campaign or demand authority.
    """
    import closer_native_readmission as readmission

    reference = readmission.prospective_reference(old_manifest, old_state, worker)
    _old_parent_terminal(reference)
    restored, predecessor = _restored_predecessor(restore_receipt)
    old_path, old_raw, old = _manifest(old_manifest)
    path, raw, manifest = _manifest(new_manifest)
    state = host._path(new_state)
    config = manifest["policy"]["correction_controller_installation"]["config"]
    _need(config.get("state_path") == str(state), "new policy state path differs")
    _pristine_prospective_namespace(manifest, state)
    host._lane_absent()
    wrapper = Path.home() / ".codex/bin/cursor-lane.sh"
    source = host._read(wrapper)[1]
    _need(
        stat.S_IMODE(wrapper.stat().st_mode) == RESTORED_MODE,
        "canonical wrapper permissions changed",
    )
    base = _guard_source(_archived_base(restored))
    old_preview = _render(base, old, old_path)
    _need(
        _sha(source) == old["mechanism_contract"]["wrapper"]["sha256"] and source == old_preview,
        "current wrapper is not the exact old-manifest render",
    )
    preview = _render(base, manifest, path)
    _need(
        _sha(preview) == manifest["mechanism_contract"]["wrapper"]["sha256"],
        "rendered wrapper differs from manifest pin",
    )
    rows = _prospective_rows(predecessor, reference, manifest, state)
    _existing_locks_present(rows)
    result = {
        "schema": PROSPECTIVE_SCHEMA,
        "producer_sha256": _sha(Path(__file__).read_bytes()),
        "eligible": True,
        "authority": False,
        "original": {
            "manifest": reference["manifest_path"],
            "manifest_sha256": _sha(old_raw),
            "state": reference["state_path"],
            "state_sha256": reference["state_sha256"],
            "worker_id": reference["worker"]["worker_id"],
            "result": reference["result_path"],
            "result_sha256": reference["result_sha256"],
            "proof_sha256": _sha(
                json.dumps(reference["proof"], sort_keys=True, separators=(",", ":")).encode()
            ),
        },
        "restore_receipt_sha256": _sha(host._read(restore_receipt)[1]),
        "new_manifest": {"path": str(path), "sha256": _sha(raw)},
        "new_state": str(state),
        "wrapper": str(wrapper),
        "renderer_base_sha256": RESTORED_SHA,
        "before_sha256": _sha(source),
        "after_sha256": _sha(preview),
        "mode": RESTORED_MODE,
        "locks": rows,
        "receipt": str(
            Path(manifest["mechanism_contract"]["observation_root"])
            / "wrapper-install-prospective-known.json"
        ),
    }
    result["plan_sha256"] = _sha(json.dumps(result, sort_keys=True, separators=(",", ":")).encode())
    return result


def apply_prospective(*args, expected_plan_sha256: str) -> dict[str, Any]:
    """Install a future wrapper while preserving the signed finite-v2 evidence."""
    first = plan_prospective(*args)
    _need(first["plan_sha256"] == expected_plan_sha256, "plan changed")
    with host._hold(first["locks"]):
        second = plan_prospective(*args)
        _need(second["plan_sha256"] == first["plan_sha256"], "plan changed under locks")
        receipt = Path(first["receipt"])
        intent = receipt.with_name("wrapper-install-prospective-intent.json")
        if intent.exists() or receipt.exists():
            raise InstallHeld("prior prospective install requires reconciliation")
        host._write_once(intent, {**first, "status": "intent_recorded", "at": time.time()})
        wrapper = Path(first["wrapper"])
        manifest_path = Path(first["new_manifest"]["path"])
        _, manifest_raw, manifest = host._obj(manifest_path)
        _need(_sha(manifest_raw) == first["new_manifest"]["sha256"], "manifest changed")
        restored, _predecessor = _restored_predecessor(args[-1])
        data = _render(_guard_source(_archived_base(restored)), manifest, manifest_path)
        _need(_sha(data) == first["after_sha256"], "preview changed before replacement")
        fd, tmp = tempfile.mkstemp(dir=wrapper.parent, prefix=".native-wrapper-")
        replaced = False
        try:
            stream = os.fdopen(fd, "wb")
            fd = -1
            with stream:
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), RESTORED_MODE)
                os.fsync(stream.fileno())
            # Intent creation and temporary-file I/O are outside the wrapper's
            # own inode. Re-read immediately before replacement so a
            # cooperating source writer that acquired this same closure is
            # never silently overwritten.
            _need(
                _sha(host._read(wrapper)[1]) == first["before_sha256"]
                and stat.S_IMODE(wrapper.stat().st_mode) == RESTORED_MODE,
                "wrapper changed before replacement",
            )
            os.replace(tmp, wrapper)
            replaced = True
            directory = os.open(wrapper.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            _need(
                _sha(host._read(wrapper)[1]) == first["after_sha256"]
                and stat.S_IMODE(wrapper.stat().st_mode) == RESTORED_MODE,
                "postreplace readback failed",
            )
            import closer_native_observer as native

            _need(
                _sha(host._read(manifest_path)[1]) == first["new_manifest"]["sha256"],
                "manifest changed after replacement",
            )
            native._check_wrapper_entry(manifest, manifest_path)
            host._write_once(receipt, {**first, "status": "installed", "at": time.time()})
            return {**first, "status": "installed"}
        except Exception as exc:
            if replaced:
                raise InstallHeld(
                    f"postreplace outcome uncertain: {type(exc).__name__}: {exc}"
                ) from None
            raise
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                Path(tmp).unlink()


def plan(
    reconciliation: dict[str, str],
    old_manifest: str | Path,
    old_state: str | Path,
    worker: str,
    new_manifest: str | Path,
    new_state: str | Path,
    restore_receipt: str | Path,
) -> dict[str, Any]:
    import closer_retained_reconciliation as retained

    original = retained.validate(reconciliation, old_manifest, old_state, worker)
    restored = _receipt(restore_receipt)
    _need(
        restored.get("witnesses", {}).get("result") == original["original"]["result"],
        "host restoration and reconciliation differ",
    )
    _, swap_intent = host._json_ref(restored["witnesses"]["swap_intent"], "restored_swap_intent")
    predecessor = swap_intent["plan"]
    _need(
        predecessor.get("before_sha256") == RESTORED_SHA
        and predecessor.get("canary_state") == str(host._path(old_state))
        and Path(predecessor["new_runtime_root"]) / "host-manifest.json"
        == host._path(old_manifest),
        "restoration predecessor differs from retained source",
    )
    path, raw, manifest = _manifest(new_manifest)
    state = host._path(new_state)
    _need(not state.exists(), "new campaign state already exists")
    cfg = manifest["policy"]["correction_controller_installation"]["config"]
    _need(cfg.get("state_path") == str(state), "new policy state path differs")
    campaign_root = host._path(manifest["mechanism_contract"]["campaign_root"])
    _need(
        state.parent == campaign_root
        and state.name.endswith(".campaign.json")
        and not list(campaign_root.glob("*.campaign.json")),
        "new campaign namespace is not empty/exact",
    )
    host._lane_absent()
    active = Path(manifest["mechanism_contract"]["observation_root"]) / "native-active.json"
    _need(
        not active.exists() and not active.is_symlink(),
        "new native namespace already has an invocation",
    )
    wrapper = Path.home() / ".codex/bin/cursor-lane.sh"
    source = host._read(wrapper)[1]
    _need(
        stat.S_IMODE(wrapper.stat().st_mode) == RESTORED_MODE,
        "canonical wrapper permissions changed",
    )
    preview = _render(_guard_source(source), manifest, path)
    _need(
        _sha(preview) == manifest["mechanism_contract"]["wrapper"]["sha256"],
        "rendered wrapper differs from manifest pin",
    )
    out: dict[str, Any] = {
        "schema": SCHEMA,
        "producer_sha256": _sha(Path(__file__).read_bytes()),
        "eligible": True,
        "authority": False,
        "reconciliation": reconciliation,
        "restore_receipt_sha256": _sha(host._read(restore_receipt)[1]),
        "new_manifest": {"path": str(path), "sha256": _sha(raw)},
        "new_state": str(state),
        "wrapper": str(Path.home() / ".codex/bin/cursor-lane.sh"),
        "before_sha256": RESTORED_SHA,
        "after_sha256": _sha(preview),
        "mode": RESTORED_MODE,
        "locks": _rows(Path(old_manifest), Path(old_state), manifest, state),
        "receipt": str(
            Path(manifest["mechanism_contract"]["observation_root"]) / "wrapper-install-known.json"
        ),
    }
    rows = {row["path"]: row["must_exist"] for row in out["locks"]}
    for row in host._locks(predecessor, wrapper):
        rows[row["path"]] = rows.get(row["path"], False) or row["must_exist"]
    out["locks"] = [{"path": name, "must_exist": rows[name]} for name in sorted(rows)]
    out["plan_sha256"] = _sha(json.dumps(out, sort_keys=True, separators=(",", ":")).encode())
    return out


def apply(*args, expected_plan_sha256: str) -> dict[str, Any]:
    first = plan(*args)
    _need(first["plan_sha256"] == expected_plan_sha256, "plan changed")
    with host._hold(first["locks"]):
        second = plan(*args)
        _need(second["plan_sha256"] == first["plan_sha256"], "plan changed under locks")
        receipt = Path(first["receipt"])
        intent = receipt.with_name("wrapper-install-intent.json")
        if intent.exists() or receipt.exists():
            raise InstallHeld("prior install requires reconciliation")
        host._write_once(intent, {**first, "status": "intent_recorded", "at": time.time()})
        wrapper = Path(first["wrapper"])
        manifest_path = Path(first["new_manifest"]["path"])
        _, manifest_raw, manifest = host._obj(manifest_path)
        _need(
            _sha(manifest_raw) == first["new_manifest"]["sha256"],
            "manifest changed before replacement",
        )
        data = _render(_guard_source(host._read(wrapper)[1]), manifest, manifest_path)
        _need(_sha(data) == first["after_sha256"], "preview changed before replacement")
        fd, tmp = tempfile.mkstemp(dir=wrapper.parent, prefix=".native-wrapper-")
        replaced = False
        try:
            stream = os.fdopen(fd, "wb")
            fd = -1
            with stream:
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), RESTORED_MODE)
                os.fsync(stream.fileno())
            os.replace(tmp, wrapper)
            replaced = True
            directory = os.open(wrapper.parent, os.O_RDONLY)
            os.fsync(directory)
            os.close(directory)
            _need(
                _sha(host._read(wrapper)[1]) == first["after_sha256"]
                and stat.S_IMODE(wrapper.stat().st_mode) == RESTORED_MODE,
                "postreplace readback failed",
            )
            import closer_native_observer as native

            _need(
                _sha(host._read(manifest_path)[1]) == first["new_manifest"]["sha256"],
                "manifest changed after replacement",
            )
            native._check_wrapper_entry(manifest, manifest_path)
            host._write_once(receipt, {**first, "status": "installed", "at": time.time()})
            return {**first, "status": "installed"}
        except Exception:
            if replaced:
                raise InstallHeld("postreplace outcome uncertain")
            raise
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                Path(tmp).unlink()


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=("plan", "apply"), nargs="?")
    p.add_argument("--kind", choices=("historical", "prospective"), default="historical")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--expected-plan-sha256")
    for name in (
        "reconciliation",
        "old-manifest",
        "old-state",
        "new-manifest",
        "new-state",
        "restore-receipt",
    ):
        p.add_argument("--" + name, type=Path)
    p.add_argument("--worker", default="08854e801de3")
    a = p.parse_args(argv)
    if a.selftest:
        try:
            _guard_source(b"not the reviewed canonical preimage")
        except InstallHeld:
            pass
        else:
            raise AssertionError("unreviewed preimage accepted")
        print(json.dumps({"selftest": "ok", "authority": False}))
        return 0
    required = ["old_manifest", "old_state", "new_manifest", "new_state", "restore_receipt"]
    if a.kind == "historical":
        required.insert(0, "reconciliation")
    if not a.mode or any(getattr(a, name) is None for name in required):
        p.error("mode and all path inputs are required")
    if a.mode == "apply" and not a.expected_plan_sha256:
        p.error("apply requires --expected-plan-sha256")
    try:
        inputs = [
            a.old_manifest,
            a.old_state,
            a.worker,
            a.new_manifest,
            a.new_state,
            a.restore_receipt,
        ]
        if a.kind == "historical":
            inputs.insert(0, host._obj(a.reconciliation)[2])
        result = (
            (apply if a.kind == "historical" else apply_prospective)(
                *inputs, expected_plan_sha256=a.expected_plan_sha256
            )
            if a.mode == "apply"
            else (plan if a.kind == "historical" else plan_prospective)(*inputs)
        )
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "held", "authority": False, "reason": str(exc)}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
