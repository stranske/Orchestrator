"""Current, same-host reconciliation of the exact 112e pre-B exception.

This is a new observation under the trusted-host model in closer_mechanisms.
It neither manufactures an old producer proof nor changes the old UNKNOWN.
Its only consumer permission is one explicitly requested fresh preparation.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import closer_host_restore_contract as host
import closer_mechanisms as mechanisms

SCHEMA = "native-retained-no-b-reconciliation/v1"
CANDIDATE = "112e9263fd7545bd5a3ab50913a867126fbe862a"
WORKER = "08854e801de3"
NONCE = "8677d9216de24ab3154beae26cd203d8"
HEAD = "05a7ce20246df00b09daf091d16e5f25c13e96a7"
MANIFEST_SHA256 = "a03d3b2a015db29702639576497b8c9b6dcaa35354014d364f8b2a23bcb1ae70"
MODULE_SET_SHA256 = "e069a709ada9403035a8c910257a55e792c79b6524e46ddf72abd47a681b2756"
MODULES = {
    "closer_runtime_binding.py": "d5ca902d6fc9d2e1a9608f099a55fe4a8fd26b31d06ffb2ee2d6ce3eb7bfcb66",
    "closer_native_observer.py": "967c9386ebdc24cc83f77251504071f222ca97557f44530e0eda2d38b879cf9f",
    "closer_campaign.py": "39f8108abffdecb43b50e8e62649aea2467485674ed12cfa257ea6e1177d4496",
    "closer_mechanisms.py": "08ce50a6ad7ddf1b7980751ceb97f01e67696dedb62c131fd0e3dce9440740a8",
    "closer_resources.py": "ce46f4a711b2636b420ad8948077548624e8b19904336855eea87e80dedce534",
}


class ReconciliationError(ValueError):
    pass


def _need(ok: object, reason: str) -> None:
    if not ok:
        raise ReconciliationError(reason)


def _ref(path: Path) -> dict[str, str]:
    physical, raw = host._read(path)
    return {"path": str(physical), "sha256": host._sha(raw)}


def _producer() -> dict[str, str]:
    return {
        name: host._sha(host._read(Path(__file__).resolve().parent / name)[1])
        for name in (
            Path(__file__).name,
            "closer_host_restore_contract.py",
            "closer_mechanisms.py",
            "closer_correction_controller.py",
        )
    }


def _source_closure(manifest: dict[str, Any]) -> None:
    _need(manifest.get("candidate_sha") == CANDIDATE, "unreviewed historical source")
    # The live package checker requires the *new* module set. Historical
    # observation instead checks this exact immutable 185-module package.
    _need(
        host._sha(
            json.dumps(manifest.get("modules"), sort_keys=True, separators=(",", ":")).encode()
        )
        == MODULE_SET_SHA256,
        "historical module set changed",
    )
    root = host._path(manifest["module_dir"])
    for name, expected in manifest["modules"].items():
        _need(
            isinstance(name, str)
            and Path(name).name == name
            and host._sha(host._read(root / name)[1]) == expected,
            "historical package closure changed",
        )
    for name, expected in MODULES.items():
        _need(
            manifest["modules"].get(name) == expected
            and host._sha(host._read(root / name)[1]) == expected,
            "reviewed pre-B source changed",
        )


def _gh(endpoint: str) -> dict[str, Any]:
    result = subprocess.run(
        ["gh", "api", "repos/stranske/Orchestrator/" + endpoint],
        capture_output=True,
        timeout=25,
        check=False,
    )
    _need(
        result.returncode == 0 and len(result.stdout) <= host.MAX_BYTES,
        "live GitHub observation unavailable",
    )
    value = json.loads(result.stdout)
    _need(isinstance(value, dict), "invalid GitHub observation")
    return value


def _remote(anchor: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    import closer_correction_controller as controller

    pr = _gh("pulls/989")
    issue = _gh("issues/988")
    source = _gh("pulls/984")
    _need(
        pr.get("number") == 989
        and pr.get("state") == "open"
        and pr.get("draft") is False
        and pr.get("head", {}).get("sha") == HEAD
        and "#988" in pr.get("body", ""),
        "corrective PR identity/head changed",
    )
    _need(
        issue.get("number") == 988
        and issue.get("state") == "open"
        and "#984" in issue.get("body", ""),
        "corrective source association changed",
    )
    _need(
        source.get("number") == 984
        and source.get("merged") is True
        and source.get("merge_commit_sha") == "d1947b5b4775c2e6a07bb46b3f85eb2e28c1b001",
        "original source association changed",
    )
    branch = _gh("git/ref/heads/" + controller.BRANCH)
    _need(
        branch.get("ref") == "refs/heads/" + controller.BRANCH
        and branch.get("object", {}).get("sha") == anchor["commit_sha"],
        "remote native index differs from local high-water",
    )
    blob = _gh("contents/" + controller.INDEX_PATH + "?ref=" + anchor["commit_sha"])
    _need(
        blob.get("sha") == anchor["blob_sha"]
        and blob.get("encoding") == "base64"
        and type(blob.get("size")) is int
        and 0 < blob["size"] <= config["max_bytes"],
        "remote native index blob differs",
    )
    data = json.loads(base64.b64decode(blob["content"].replace("\n", ""), validate=True))
    _need(
        controller.validate_index(data, controller.ControllerConfig(**config)) == data
        and controller.digest(data) == anchor["state_sha256"],
        "remote native index data differs",
    )
    return {
        "corrective_pr": {"number": 989, "head": HEAD, "state": "open", "draft": False},
        "corrective_issue": 988,
        "source_pr": 984,
        "association_body_sha256": {
            "989": host._sha(pr["body"].encode()),
            "988": host._sha(issue["body"].encode()),
        },
        "index_commit": anchor["commit_sha"],
        "index_blob": anchor["blob_sha"],
    }


def _inventory(directory: Path, dispatch: Path, custody: Path) -> dict[str, Any]:
    import closer_resources as resources

    expected = {
        "admission.json",
        "assignment.json",
        "scoped-capacity.json",
        "scoped-capacity.json.lock",
        "startup.json",
        "terminal.json",
        "worker-" + WORKER + ".json",
    }
    output_name = "retained-no-b-current-" + WORKER + ".json"
    names = {item.name for item in directory.iterdir()}
    _need(
        names - {output_name} == expected, "native invocation inventory has an unreviewed boundary"
    )
    witnesses = [_ref(directory / name) for name in sorted(expected)]
    root = directory.parent
    _, _, active = host._obj(root / "native-active.json")
    _need(
        active.get("status") == "terminal"
        and re.fullmatch(r"[0-9a-f]{32}", str(active.get("nonce", ""))),
        "current native entry is not terminal",
    )
    terminal_path = root / active["nonce"] / "terminal.json"
    terminal = host._signed(terminal_path, root, "current_native_terminal")
    _need(
        terminal.get("schema") == "native-wrapper-lifecycle/v1"
        and terminal.get("source") == "owned-native-wrapper-process"
        and terminal.get("nonce") == active["nonce"]
        and terminal.get("binding") == active.get("binding"),
        "current native terminal does not join active registry",
    )
    for field in ("wrapper", "entry"):
        host._dead(terminal.get(field, {}).get("pid"))
    for event in terminal.get("tick_result", []):
        if isinstance(event, dict) and event.get("kind") == "launched":
            host._dead(event.get("pid"))
    absent = [
        dispatch,
        Path(str(dispatch) + ".pipeline.json"),
        resources.scoped_root(dispatch),
        resources.scoped_root(Path(str(dispatch) + ".pipeline.json")),
        dispatch.parent / (dispatch.stem + ".actions"),
        Path(str(dispatch) + ".evidence"),
    ]
    for path in absent:
        physical = host._path(path)
        _need(
            not physical.exists() and not physical.is_symlink(),
            "dispatcher/effect sink is occupied",
        )
    custody_names = {p.name for p in custody.iterdir()}
    _need(
        custody_names == {"namespace.json", "writer.lock", "highwater-0000000000000000.json"},
        "controller custody has transaction/effect history",
    )
    return {
        "native_witnesses": witnesses,
        "current_native_witnesses": [_ref(root / "native-active.json"), _ref(terminal_path)],
        "absent_sinks": [str(p) for p in absent],
        "custody_witnesses": [_ref(custody / name) for name in sorted(custody_names)],
    }


def _snapshot(manifest_path: Path, state: Path, worker_id: str) -> dict[str, Any]:
    import claims
    import closer_correction_controller as controller

    _, manifest_raw, manifest = host._obj(manifest_path)
    _need(host._sha(manifest_raw) == MANIFEST_SHA256, "unreviewed historical manifest")
    _source_closure(manifest)
    _, state_raw, document = host._obj(state)
    _need(
        worker_id == WORKER and document.get("targets") == {"*round*": "reconcile"},
        "unsupported retained worker/scope",
    )
    workers = document.get("workers")
    _need(
        isinstance(workers, list)
        and len(workers) == 1
        and isinstance(workers[0], dict)
        and workers[0].get("worker_id") == worker_id
        and workers[0].get("state") == "terminal"
        and workers[0].get("outcome") == "unknown",
        "original UNKNOWN ownership changed",
    )
    assert isinstance(workers, list)
    cfg = document["worker_config"]
    _need(cfg.get("runtime_manifest") == str(manifest_path), "campaign manifest changed")
    frontier = cfg["finite_frontier"]
    _need(frontier.get("targets") == ["stranske/Orchestrator#984"], "historical frontier differs")
    root = host._path(manifest["mechanism_contract"]["observation_root"])
    directory = root / NONCE
    result_path = Path(str(state) + ".results") / (worker_id + ".json")
    _, result_raw, result = host._obj(result_path)
    request = {
        "admission": _ref(directory / "admission.json"),
        "worker_handoff": _ref(directory / ("worker-" + worker_id + ".json")),
        "terminal": _ref(directory / "terminal.json"),
        "result": _ref(result_path),
    }
    host._check_lifecycle(
        {
            "new_observation_root": str(root),
            "new_runtime_root": str(manifest_path.parent),
            "source_sha": CANDIDATE,
            "canary_state": str(state),
        },
        request,
        result,
        manifest,
    )
    context = result["native_admission"]
    _need(
        context.get("campaign")
        == {
            "state": str(state),
            "campaign_id": document["campaign_id"],
            "owner": document["owner"],
            "generation": document["generation"],
        },
        "original signed campaign owner/generation changed",
    )
    handoff = host._signed(directory / ("worker-" + worker_id + ".json"), root, "worker_handoff")
    terminal = host._signed(directory / "terminal.json", root, "terminal")
    _need(
        handoff["fields"].get("owner") == document["owner"]
        and any(
            row.get("kind") == "launched"
            and row.get("worker_id") == worker_id
            and row.get("pid") == workers[0].get("pid")
            for row in terminal["tick_result"]
            if isinstance(row, dict)
        ),
        "original signed worker owner/PID changed",
    )
    detail = json.loads(result["detail"])
    b = detail["native_execution"]["phases"]["B"]
    _need(
        detail.get("actual_detail")
        == "dispatcher outcome unknown: MechanismError; ACTION C65; preserve no_matching_pipeline_run and ledger"
        and b.get("started") is False
        and b.get("started_at") is None
        and b.get("finished_at") is None
        and result.get("target") == "*round*"
        and result.get("mechanism") == mechanisms.NATIVE
        and document["workers"][0].get("detail") == result["detail"],
        "not the reviewed pre-dispatch exception",
    )
    config = manifest["policy"]["correction_controller_installation"]["config"]
    _need(
        document.get("correction_controller", {}).get("config") == config
        and config.get("state_path") == str(state)
        and config.get("source_sha") == CANDIDATE,
        "controller source binding changed",
    )
    inspection = controller.inspect_provisioned_custody(config)
    highwater = inspection["highwater"]
    _need(
        highwater["pending_transactions"] == [] and highwater["anchor"]["revision"] == 0,
        "native index has effects or unresolved transactions",
    )
    host._lane_absent()
    _need(
        not any(
            claims.holder(target)
            for target in (
                "stranske/Orchestrator#984",
                "stranske/Orchestrator#988",
                "stranske/Orchestrator#989",
                document["campaign_id"] + ":*round*",
            )
        ),
        "current exact-target claim is held",
    )
    inventory = _inventory(
        directory,
        host._path(cfg["dispatch_state"]),
        host._path(inspection["identity"]["custody_path"]),
    )
    return {
        "producer": _producer(),
        "manifest": {"path": str(manifest_path), "sha256": host._sha(manifest_raw)},
        "original": {
            "state": str(state),
            "state_sha256": host._sha(state_raw),
            "campaign_id": document["campaign_id"],
            "revision": document["revision"],
            "generation": document["generation"],
            "worker_id": worker_id,
            "admission_nonce": NONCE,
            "result": {"path": str(result_path), "sha256": host._sha(result_raw)},
            "finite_frontier": frontier,
        },
        "current": {
            "inventory": inventory,
            "native_index": inspection,
            "github_association": _remote(highwater["anchor"], config),
        },
        "output": str(directory / ("retained-no-b-current-" + worker_id + ".json")),
    }


def lock_rows(manifest_path: str | Path, campaign_state: str | Path) -> list[dict[str, Any]]:
    _, _, manifest = host._obj(manifest_path)
    config = manifest["policy"]["correction_controller_installation"]["config"]
    import closer_correction_controller as controller

    inspection = controller.inspect_provisioned_custody(config)
    root = host._path(manifest["mechanism_contract"]["observation_root"])
    paths = [
        root / "native-entry.lock",
        root / "native-supervisor.lock",
        Path(inspection["identity"]["custody_path"]) / "writer.lock",
        Path(str(campaign_state) + ".lock"),
        Path.home() / ".codex/handoff/.lane-closer.lock.coord.lock",
    ]
    return [{"path": str(host._path(path)), "must_exist": True} for path in sorted(set(paths))]


def collect(
    manifest_path: str | Path, campaign_state: str | Path, worker_id: str, *, seal: bool = True
) -> dict[str, Any]:
    manifest_path, state = host._path(manifest_path), host._path(campaign_state)
    with host._hold(lock_rows(manifest_path, state)):
        before = _snapshot(manifest_path, state, worker_id)
        _need(
            _snapshot(manifest_path, state, worker_id) == before,
            "current observation changed under locks",
        )
        record = {
            "schema": SCHEMA,
            "authority": False,
            "purpose": "business_no_B",
            "trust_model": "reviewed_same_user_host",
            "decision": {
                "status": "confirmed_no_b_current_observation",
                "authorize_fresh_prepare": True,
                "max_fresh_generations": 1,
                "old_outcome": "unknown_preserved",
                "acceptance_authority": False,
            },
            "observed_at": time.time(),
            **before,
        }
        _, _, manifest = host._obj(manifest_path)
        root = host._path(manifest["mechanism_contract"]["observation_root"])
        envelope = {
            "record": record,
            "signature": mechanisms._signature(record, mechanisms._key(root)),
        }
        path = Path(before["output"])
        if seal:
            host._write_once(path, envelope)
            return {**_ref(path), "record": record, "sealed": True}
        return {"path": str(path), "record": record, "sealed": False}


def plan(manifest_path: str | Path, campaign_state: str | Path, worker_id: str) -> dict[str, Any]:
    return collect(manifest_path, campaign_state, worker_id, seal=False)


def validate(
    reference: dict[str, str], manifest_path: str | Path, campaign_state: str | Path, worker_id: str
) -> dict[str, Any]:
    path, _, envelope = host._obj(reference["path"])
    _need(
        _ref(path) == {"path": reference["path"], "sha256": reference["sha256"]},
        "reconciliation bytes changed",
    )
    _, _, manifest = host._obj(manifest_path)
    root = host._path(manifest["mechanism_contract"]["observation_root"])
    record = host._signed(path, root, "retained_reconciliation")
    _need(
        record == envelope["record"]
        and record.get("schema") == SCHEMA
        and record.get("authority") is False
        and record.get("purpose") == "business_no_B"
        and path == root / NONCE / ("retained-no-b-current-" + worker_id + ".json"),
        "unrecognized reconciliation",
    )
    current = _snapshot(host._path(manifest_path), host._path(campaign_state), worker_id)
    _need(
        all(record.get(key) == value for key, value in current.items()),
        "reconciliation current evidence changed",
    )
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "collect"), nargs="?")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--campaign", type=Path)
    parser.add_argument("--worker", default=WORKER)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        try:
            _need(False, "selftest refusal")
        except ReconciliationError:
            pass
        else:
            raise AssertionError("refusal did not fail closed")
        _need(len(_producer()) == 4, "producer closure unavailable")
        print(json.dumps({"selftest": "ok", "authority": False}))
        return 0
    if args.mode is None or args.manifest is None or args.campaign is None:
        parser.error("mode, --manifest and --campaign are required")
    try:
        value = collect(args.manifest, args.campaign, args.worker, seal=args.mode == "collect")
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        print(json.dumps({"status": "held", "authority": False, "reason": str(exc)}))
        return 2
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
