"""Durable closer-campaign ownership: phase, next action, bounded detached workers.

One campaign record answers "what happens next" after any worker death, restart or
capacity exhaustion, so a scheduler needs no memory of its own. It reuses `claims`
(per-target ownership) and `closer_release.ReleaseRecord` (sealed verification
receipts); it never verifies, installs or merges itself.

Rules enforced here (each has a test):
- A worker is replaced only after it is TERMINAL. An unknown worker (pid gone, no
  result) moves the campaign to `reconcile`, never to a fresh launch.
- A permission/policy denial holds the campaign with the exact reason. No fallback.
- Release resume reuses the sealed receipt: a release past `verified` is never
  re-verified, and past `installing` never re-installed (#794 stays as journaled).
- Promotion fails closed without a receipt recorded by the actual desktop runner;
  unit-test/fixture receipts never count.
- A runner that does not understand the state schema stops before writing.
- Package rollback never rolls state back; it is allowed only when state versions match.

Portable boundary: Workflows owns the claim/intent/reconcile fences (Core,
`closer-operation/v1`). This module has NO action lock of its own: every action effect goes
through `submit_effect`, which requires a `CoreOperationAdapter` (production: `PinnedPortable
.operation_adapter`, loaded only at a pinned sha256) and holds when it is absent. Campaign-local
phase ownership (generation, worker rows, `claims`) stays here. `OperationPort` is the effect
injection point; `LocalOnlyPort` (basic path) goes through the same gate.
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Protocol, cast

# The physical CLI file and native source-frame checks share one module object.
# Refuse a prior named import rather than execute two independently loaded copies.
if __name__ == "__main__":
    _entry_module = sys.modules.get("__main__")
    if _entry_module is None or _entry_module.__dict__ is not globals():
        raise RuntimeError("campaign entry module identity unavailable")
    _canonical_module = sys.modules.get("closer_campaign")
    if _canonical_module is not None and _canonical_module is not _entry_module:
        raise RuntimeError("conflicting canonical campaign module")
    sys.modules["closer_campaign"] = _entry_module

# Published packages remain immutable, including their Python import artifacts.
if (Path(__file__).resolve().parent / "closer-runtime-package.json").is_file():
    sys.dont_write_bytecode = True

import claims  # noqa: E402

STATE_SCHEMA = 1
MANIFEST_SCHEMA = 1
CONTRACT = "closer-operation/v1"
MAX_WORKERS_CAP = 4
OUTCOMES = {"ok", "unavailable", "denied", "unknown"}
WORKER_OUTCOMES = OUTCOMES | {"scoped_continuation"}
RUNNER_PHASES = ("startup", "handoff", "crash_restart", "result_recording")
_UNPROVEN_KINDS = {"unit_test", "fixture", "simulated"}
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SHA = re.compile(r"^[0-9a-f]{40,64}$")
AGENT = "closer-campaign"
TARGET = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")
ROUND_UNIT = "*round*"  # the existing dispatcher `run` owns the WHOLE assignment: one unit
GRANULARITIES = ("round", "target-delegate")


class CampaignError(RuntimeError):
    pass


class IncompatibleState(CampaignError):
    """The record is newer than this runner understands; refuse to write it."""


class OperationPort(Protocol):
    def submit(self, operation_key: str, action: dict[str, Any]) -> str: ...


class LocalOnlyPort:
    """No remote mutation: every submission is `unavailable` and stays queued locally."""

    def submit(self, operation_key: str, action: dict[str, Any]) -> str:
        return "unavailable"


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------- state store


class Campaign:
    """Atomic, flock-serialised, revision-checked campaign record."""

    def __init__(self, path: str | Path, *, supported_schema: int = STATE_SCHEMA):
        self.path = Path(path).expanduser().absolute()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.supported_schema = supported_schema

    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        stream = os.fdopen(descriptor, "a+b")
        try:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
            ):
                raise CampaignError("campaign lock is not an owned regular file")
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            return stream
        except BaseException:
            stream.close()
            raise

    def _read_unlocked(self) -> dict[str, Any] | None:
        try:
            doc = json.loads(self.path.read_bytes())
        except FileNotFoundError:
            return None
        except ValueError as exc:
            raise CampaignError(f"campaign record is invalid: {exc}") from exc
        version = doc.get("schema_version") if isinstance(doc, dict) else None
        if not isinstance(version, int):
            raise CampaignError("campaign record has no schema version")
        if version > self.supported_schema:
            raise IncompatibleState(
                f"record schema {version} is newer than supported {self.supported_schema}"
            )
        worker_config = doc.get("worker_config") or {}
        frontier = worker_config.get("finite_frontier")
        if frontier is not None:
            try:
                from closer_frontier_policy import validate_frontier

                validate_frontier(frontier, targets=doc.get("assignment_targets"))
            except (TypeError, ValueError) as exc:
                raise CampaignError(f"finite frontier record is invalid: {exc}") from exc
        return doc

    def _write_unlocked(self, doc: dict[str, Any]) -> None:
        payload = (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def read(self) -> dict[str, Any] | None:
        if not self.path.parent.is_dir():
            return None
        stream = self._locked()
        try:
            return self._read_unlocked()
        finally:
            stream.close()

    def update(self, mutate: Callable[[dict[str, Any]], object], *, create=None) -> dict[str, Any]:
        """Apply `mutate` to the record under the lock; bumps the revision."""
        stream = self._locked()
        try:
            doc = self._read_unlocked()
            if doc is None:
                if create is None:
                    raise CampaignError("no campaign record exists")
                doc = create
            mutate(doc)
            doc["revision"] = doc.get("revision", 0) + 1
            doc["updated_at"] = _now()
            self._write_unlocked(doc)
            return doc
        finally:
            stream.close()

    def begin(
        self,
        campaign_id: str,
        targets: list[str],
        *,
        owner: str,
        release_pin: str,
        max_workers: int = 2,
        release_record: str | None = None,
        worker_config: dict[str, Any] | None = None,
        demand_assignment_bytes: bytes | None = None,
        demand_request: Mapping[str, Any] | None = None,
        qualified_package: Mapping[str, Any] | None = None,
        demand_now: float | None = None,
        _demand_outcome: dict[str, Any] | None = None,
        _retirement: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create the record, or resume the same campaign under the same owner.

        `worker_config` and `release_record` live IN the record so a later scheduled tick
        needs no memory of the process that began it."""
        demand_inputs = (demand_assignment_bytes, demand_request, qualified_package)
        if any(value is not None for value in demand_inputs) and not all(
            value is not None for value in demand_inputs
        ):
            raise ValueError("completed demand needs assignment, request and verified package")
        if not 1 <= max_workers <= MAX_WORKERS_CAP:
            raise ValueError(f"max_workers must be 1..{MAX_WORKERS_CAP}")
        worker_config = dict(worker_config or {})
        mode = worker_config.setdefault("granularity", "round")
        if mode not in GRANULARITIES:
            raise ValueError(f"unsupported work-unit granularity {mode!r}")
        assignment_targets = list(targets)
        frontier = worker_config.get("finite_frontier")
        if frontier is not None:
            try:
                from closer_frontier_policy import validate_frontier

                frontier = validate_frontier(frontier, targets=assignment_targets)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"finite frontier does not match campaign targets: {exc}") from exc
            worker_config["finite_frontier"] = frontier
        if mode == "round":  # one whole-round owner; the dispatcher gives its own parallelism
            targets = [ROUND_UNIT]
            max_workers = 1
        fresh: dict[str, Any] = {
            "schema_version": STATE_SCHEMA,
            "campaign_id": campaign_id,
            "revision": 0,
            "owner": owner,
            "release_pin": release_pin,
            "phase": "work",
            "max_workers": max_workers,
            "targets": {t: "pending" for t in targets},
            "workers": [],
            "hold": None,
            "generation": 1,
            "release_record": release_record,
            "worker_config": worker_config,
            "assignment_targets": assignment_targets,
            "release_attempt": None,
            "created_at": _now(),
        }
        stream = self._locked()
        try:
            current = self._read_unlocked()
            original_current = current
            retirement_record = None
            if current is not None:
                if current["campaign_id"] != campaign_id or current["owner"] != owner:
                    raise CampaignError("campaign is owned by another id or runner")
                if current["release_pin"] != release_pin:
                    raise CampaignError(
                        "campaign is pinned to a different release; reconcile first"
                    )
                if _retirement is not None:
                    if demand_request is None:
                        raise CampaignError("retirement requires fresh completed discovery")
                    current, retirement_record = _retirement_view(
                        self.path, current, demand_request, worker_config, _retirement
                    )
                current_frontier = (current.get("worker_config") or {}).get("finite_frontier")
                if current_frontier != frontier:
                    raise CampaignError(
                        "campaign finite frontier is immutable; reconcile or use a new campaign"
                    )
                if frontier is not None and current.get("assignment_targets") != assignment_targets:
                    raise CampaignError(
                        "campaign finite frontier targets changed; use a new campaign"
                    )
                if demand_request is None:
                    return current
            selected: dict[str, Any] = current if current is not None else fresh
            if demand_request is not None:
                if current is None:
                    selected["revision"] = 1  # logical initial record, still not written
                original_revision = selected["revision"]
                assert demand_assignment_bytes is not None and qualified_package is not None
                from closer_demand_admission import admit_in_transaction

                outcome = admit_in_transaction(
                    selected,
                    assignment_bytes=demand_assignment_bytes,
                    request=demand_request,
                    qualified_package=qualified_package,
                    now=_now() if demand_now is None else demand_now,
                )
                if _demand_outcome is not None:
                    _demand_outcome.update(outcome)
                if outcome["status"] == "refused":
                    raise CampaignError("completed demand refused: " + outcome["reason"])
                receipt = selected.get("demand_admission")
                if (
                    not isinstance(receipt, Mapping)
                    or receipt.get("schema") != "closer-demand-admission/v1"
                ):
                    raise CampaignError("completed demand did not bind an admission receipt")
                if outcome["status"] == "replay":
                    return selected  # An exact replay neither writes nor bumps revision.
                selected["demand_assignment"] = _store_demand_assignment(
                    self.path, demand_assignment_bytes, receipt
                )
                selected["worker_config"]["assignment"] = selected["demand_assignment"]["path"]
                selected["demand_continuations"] = []
                if retirement_record is not None:
                    manifest = load_manifest(selected["worker_config"]["runtime_manifest"])
                    selected["cohort_retirement"] = _store_retirement(
                        self.path, manifest, retirement_record
                    )
                    if (
                        self._read_unlocked() != original_current
                        or _retirement_evidence(self.path, original_current, manifest)
                        != _retirement
                    ):
                        raise CampaignError("cohort changed during archive publication")
                    if demand_request["deadline"] <= max(demand_now or _now(), _now()):
                        raise CampaignError(
                            "fresh cohort admission expired during archive publication"
                        )
                if original_current is not None:
                    cohort_manifest = (
                        load_manifest(selected["worker_config"]["runtime_manifest"])
                        if selected.get("cohort_retirement")
                        else None
                    )
                    owned = (
                        _owned_cohort_workers(self.path, selected, cohort_manifest)
                        if cohort_manifest is not None
                        else selected.get("workers")
                    )
                    if owned or selected.get("hold_followups"):
                        raise CampaignError(
                            "existing campaign work remains owned; demand cannot replace it"
                        )
                selected["demand_prepared"] = {
                    "schema": "closer-demand-prepared/v1",
                    "status": "prepared",
                    "request_id": receipt["request_id"],
                    "snapshot": dict(selected["demand_assignment"]),
                }
                selected["revision"] = original_revision
            selected["revision"] = selected["revision"] + 1
            selected["updated_at"] = _now()
            self._write_unlocked(selected)
            return selected
        finally:
            stream.close()

    def takeover(self, owner: str, *, pid_alive=claims._pid_alive) -> dict[str, Any]:
        """New owner, new generation: only when every worker is terminal.

        Bumping the generation fences any older worker BEFORE it can cause an effect
        (`run_worker` compares it). Non-terminal workers must reconcile first."""

        def mutate(doc):
            for w in doc["workers"]:
                if classify_worker(w, self.path, pid_alive=pid_alive) != "terminal" and w[
                    "state"
                ] not in {"terminal", "denied"}:
                    raise CampaignError("workers are not terminal; reconcile before takeover")
            doc["owner"] = owner
            doc["generation"] = doc.get("generation", 1) + 1

        return self.update(mutate)


# -------------------------------------------------------------- worker handling


def default_launcher(command: list[str], log_path: Path) -> int:
    """Start a detached process in its own session; return its pid."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True
        )
    return proc.pid


def result_path(state_path: Path, worker_id: str) -> Path:
    return state_path.with_name(state_path.name + ".results") / f"{worker_id}.json"


def classify_worker(
    worker: dict[str, Any],
    state_path: Path,
    *,
    pid_alive=claims._pid_alive,
    now: float | None = None,
) -> str:
    """One of terminal | active | unknown. Only `terminal` permits replacement."""
    if worker["state"] in {"terminal", "denied"}:
        return worker["state"]
    result = result_path(state_path, worker["worker_id"])
    if result.is_file():
        return "terminal"
    if worker.get("pid") and pid_alive(worker["pid"]):
        return "active"
    return "unknown"  # pid gone with no result: outcome of any write is not known


def reconcile(
    campaign: Campaign, *, pid_alive=claims._pid_alive, release_claim=claims.release
) -> dict[str, Any]:
    """Fold worker results into the record. Never launches. Safe while the cap is full."""
    state_path = campaign.path

    def mutate(doc):
        for worker in doc["workers"]:
            kind = classify_worker(worker, state_path, pid_alive=pid_alive)
            if kind == "terminal" and worker["state"] != "terminal":
                native_detail = None
                try:
                    result = json.loads(result_path(state_path, worker["worker_id"]).read_text())
                except ValueError:
                    result = {"detail": "result file unreadable"}
                if not isinstance(result, dict):
                    result = {}
                outcome = result.get("outcome")
                if outcome not in WORKER_OUTCOMES:
                    outcome = "unknown"
                # Only newly produced signed no-start evidence can settle this
                # native C65 case. Older UNKNOWN/plaintext records remain held.
                try:
                    native_detail = json.loads(result.get("detail", ""))
                    no_start = native_detail.get("native_no_start")
                    execution = native_detail.get("native_execution") or {}
                    phases = execution.get("phases") or {}
                    requires_no_start = execution.get("schema") == "native-round-execution/v1" and (
                        phases.get("C", {}).get("exit_code") == 65
                        or (
                            phases.get("A", {}).get("admitted") is True
                            and phases.get("B", {}).get("started") is False
                            and phases.get("C", {}).get("attempted") is True
                        )
                    )
                except (ValueError, TypeError, AttributeError):
                    no_start = None
                    requires_no_start = (
                        isinstance(native_detail, dict) and "native_execution" in native_detail
                    )
                if no_start is not None or (outcome == "unavailable" and requires_no_start):
                    from closer_native_observer import (
                        pre_dispatch_no_start_proof,
                        result_admission_config,
                    )

                    try:
                        cfg = result_admission_config(
                            doc.get("worker_config") or {},
                            result["native_admission"],
                            state_path,
                            doc,
                            worker,
                        )
                        if no_start is not None:
                            valid = pre_dispatch_no_start_proof(
                                load_manifest(cfg["runtime_manifest"]), cfg, no_start
                            )
                            if (
                                valid is not None
                                and native_detail.get("native_execution") != valid["execution"]
                            ):
                                valid = None
                        else:
                            # A terminal B can separately prove a no-start scoped
                            # domain. Keep that existing proof path intact.
                            from closer_runtime_binding import scoped_continuation_proof

                            candidate = native_detail.get("proof")
                            valid = scoped_continuation_proof(cfg, candidate)
                            if (
                                not isinstance(valid, dict)
                                or valid.get("domain_outcome") != "not_started"
                            ):
                                valid = None
                    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                        valid = None
                    if (
                        outcome != "unavailable"
                        or result.get("mechanism") != "native-handoff/v1"
                        or valid is None
                        or valid
                        != (no_start if no_start is not None else native_detail.get("proof"))
                    ):
                        outcome = "unknown"
                if outcome == "scoped_continuation":
                    from closer_runtime_binding import scoped_continuation_proof

                    try:
                        candidate = json.loads(result["detail"])["proof"]
                    except (KeyError, TypeError, ValueError):
                        candidate = None
                    cfg = doc.get("worker_config") or {}
                    if result.get("native_admission") is not None:
                        from closer_native_observer import result_admission_config

                        try:
                            cfg = result_admission_config(
                                cfg, result["native_admission"], state_path, doc, worker
                            )
                        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                            candidate = None
                    if result.get("mechanism") == "native-handoff/v1" and isinstance(
                        candidate, dict
                    ):
                        handoff = Path(
                            cfg.get("handoff_dir")
                            or os.environ.get("HANDOFF_DIR")
                            or Path.home() / ".codex/handoff"
                        )
                        lane = handoff / ".lane-closer.lock"
                        if lane.exists() or lane.is_symlink():
                            continue  # Live ownership defers observation; it is not an outcome.
                    if (
                        result.get("mechanism") != "native-handoff/v1"
                        or not isinstance(candidate, dict)
                        or scoped_continuation_proof(cfg, candidate) != candidate
                    ):
                        outcome = "unknown"
                worker.update(state="terminal", outcome=outcome, detail=result.get("detail"))
                if (doc.get("worker_config") or {}).get("finite_frontier") is not None:
                    # A finite descriptor authorizes one attempt per target. Even
                    # pre-effect unavailability requires explicit new authority;
                    # it never becomes an automatic campaign retry.
                    target_state = {
                        "ok": "done",
                        "unknown": "reconcile",
                        "unavailable": "reconcile",
                        "scoped_continuation": "reconcile",
                        "denied": "denied",
                    }[outcome]
                else:
                    target_state = {
                        "ok": "done",
                        "unknown": "reconcile",
                        "unavailable": "pending",
                        "scoped_continuation": "pending",
                        "denied": "denied",
                    }[outcome]
                doc["targets"][worker["target"]] = target_state
                if outcome == "denied":
                    worker["state"] = "denied"
                    doc["hold"] = {
                        "kind": "denied",
                        "target": worker["target"],
                        "reason": result.get("detail") or "denied",
                    }
                release_claim(worker["claim_target"], AGENT)
            elif kind == "unknown" and worker["state"] != "unknown":
                worker["state"] = "unknown"
                doc["targets"][worker["target"]] = "reconcile"
            elif kind == "active":
                worker["state"] = "active"

    return campaign.update(mutate)


def granularity(doc: dict[str, Any]) -> str:
    return (doc.get("worker_config") or {}).get("granularity", "round")


def native_barrier(doc: dict[str, Any], *, active=None) -> str | None:
    """Read native markers without reaping. Target barriers stay item scoped.

    Pipeline runlock is an advisory flock: an existing but unlocked file is NOT an
    owner. A lane directory, including unreadable metadata, remains an owner until
    native terminal cleanup. Deadlines never confer takeover authority.
    """
    cfg = doc.get("worker_config") or {}
    lane = Path(cfg.get("handoff_dir") or claims._handoff_dir()) / ".lane-closer.lock"
    if lane.exists():
        try:
            token = (lane / "meta").read_text()
        except OSError:
            return "native closer lane metadata unknown; preserve owner"
        own = cfg.get("native_lane_token")
        if not own or token != own:
            return "native closer lane owned; exact lease token required"
    state = cfg.get("dispatch_state")
    if state:
        runlock = Path(str(state) + ".pipeline.json.runlock")
        if runlock.exists():
            try:
                with runlock.open("r") as stream:
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return "native pipeline coordinator owns runlock"
                    finally:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                return "native pipeline coordinator ownership unknown"
        if Path(state).exists():
            try:
                ledger = json.loads(Path(state).read_text())
            except (OSError, ValueError):
                return "legacy dispatch state unreadable; preserving uncertain outcomes"
            # A whole round lets the original queue skip held targets itself.
            if granularity(doc) != "round":
                for target in doc.get("targets", {}):
                    row = (ledger.get("items") or {}).get(target) or {}
                    op = row.get("operation") or {}
                    if op.get("status") in {"submitted", "running", "uncertain", "unknown"}:
                        return f"native target outcome unresolved: {target}"
    if granularity(doc) != "round":
        for target in doc.get("targets", {}):
            meta = active.get(target) if active is not None else claims.holder(target)
            if meta:
                return f"native owner holds exact target {target}"
    return None


def drainable_workers(doc: dict[str, Any]) -> int:
    """Workers that reconcile can settle without launching anything."""
    return sum(1 for w in doc["workers"] if w["state"] in {"active", "unknown", "launched"})


def next_action(
    doc: dict[str, Any], *, release: dict[str, Any] | None = None, capacity_ok: bool = True
) -> dict[str, Any]:
    """The durable next action, derived from the record alone (plus cached capacity)."""
    if doc.get("hold"):
        return {"kind": "held", "reason": doc["hold"]}
    mode = granularity(doc)
    if mode not in GRANULARITIES:
        return {"kind": "held", "reason": f"unsupported work-unit granularity {mode!r}"}
    live = [w for w in doc["workers"] if w["state"] in {"launched", "active"}]
    unknown = [w for w in doc["workers"] if w["state"] == "unknown"]
    if mode == "round" and (unknown or any(s == "reconcile" for s in doc["targets"].values())):
        return {"kind": "reconcile", "workers": [w["worker_id"] for w in unknown]}
    if release is not None and release["stage"] in {
        "prepared",
        "verifying",
        "verified",
        "installing",
        "rollback_pending",
    }:
        return {"kind": "release_resume", "stage": release["stage"]}
    pending = [t for t, s in doc["targets"].items() if s == "pending"]
    if pending:
        if len(live) >= doc["max_workers"]:
            return {
                "kind": "wait",
                "reason": "worker_cap",
                "active": len(live),
                "cap": doc["max_workers"],
            }
        if not capacity_ok:
            return {"kind": "wait", "reason": "capacity_exhausted", "pending": len(pending)}
        return {"kind": "launch", "target": pending[0]}
    if unknown or any(s == "reconcile" for s in doc["targets"].values()):
        return {"kind": "reconcile", "workers": [w["worker_id"] for w in unknown]}
    if live:
        return {"kind": "wait", "reason": "workers_running", "active": len(live)}
    if release is not None and release["stage"] == "acceptance_pending":
        return {"kind": "await_acceptance"}
    return {"kind": "complete"}


def select_route(
    path: str | Path | None,
    *,
    max_age: float = 1800,
    now: float | None = None,
    router=None,
    profiles=None,
) -> dict[str, Any]:
    """The provider/profile the existing router picks from an EXISTING capacity JSON.

    Same role qualification as the dispatcher's classification lane: fresh and complete report (else `hold`,
    never a global `capacity.py` run), offload profiles with ok/warn profile and pool state,
    Aider excluded, every other approved agent (Gemini included) eligible. The caller
    dispatches the SAME agent/profile it records here. Seam: route through the shared Vibe
    routing API once it is stable; until then this is read-only on router/execution_profiles."""
    try:
        report = json.loads(Path(path).read_text())  # type: ignore[arg-type]
        age = (now or _now()) - float(report["generated_at"])
        shape = ("agents", "profiles", "pools")
        if age < -30 or age > max_age or any(not isinstance(report.get(k), dict) for k in shape):
            return {"hold": "capacity report stale or partial"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"hold": "capacity report missing or unreadable"}
    if router is None or profiles is None:
        import execution_profiles as profiles  # noqa: PLW0127
        import router  # noqa: PLW0127
    import adapters
    import closer_role_qualification as roles

    usable = {"ok", "warn"}
    options: dict[str, list[dict[str, Any]]] = {}
    for agent_name, row in report["agents"].items():
        if str(agent_name).casefold() == "aider" or not isinstance(row, dict):
            continue
        if row.get("state") not in usable:
            continue
        found = [
            p
            for p in profiles.profiles_for_agent(agent_name, transport="offload")
            if str(p.get("agent", "")).casefold() != "aider"
            and adapters.automatic_profile_within_cap(p)
            and roles.is_runtime_qualified(p, roles.BOUNDED_CLASSIFICATION)
            and report["profiles"].get(p["profile_id"], {}).get("state") in usable
            and p.get("capacity_pool_ids")
            and all(
                report["pools"].get(x, {}).get("state") in usable for x in p["capacity_pool_ids"]
            )
        ]
        if found:
            options[agent_name] = found
    if not options:
        return {"hold": "no approved provider/profile has capacity"}
    allowed = {p["profile_id"] for rows in options.values() for p in rows}
    known = set(getattr(profiles, "PROFILE_REGISTRY", {})) | set(report["profiles"]) | allowed
    qualified = {
        **report,
        "profiles": {
            key: report["profiles"].get(key, {}) if key in allowed else {"state": "unknown"}
            for key in known
        },
    }
    learned_ranks = router.learned_ranks() if hasattr(router, "learned_ranks") else None
    learned = learned_ranks.get("closer_interpret") if learned_ranks is not None else None
    pick = router.select_agent(
        "closer_interpret",
        qualified,
        only=set(options),
        allow_warn=True,
        profile_transport="offload",
        load={},
        per_agent_cap=1,
        learned=learned,
    )
    if not pick or pick["agent"] not in options:
        return {"hold": "router selected no provider"}
    selected = pick.get("selected_profile_id")
    if selected not in {p["profile_id"] for p in options[pick["agent"]]}:
        return {"hold": "router profile missing or outside qualified capacity"}
    return {"agent": pick["agent"], "profile_id": selected}


def step(
    campaign: Campaign,
    *,
    worker_command: Callable[[str], list[str]],
    launcher: Callable[[list[str], Path], int] = default_launcher,
    claim=claims.claim,
    capacity_ok: bool = True,
    release: dict | None = None,
    pid_alive=claims._pid_alive,
    owner_generation: int | None = None,
    native_probe: Callable[[dict[str, Any]], str | None] = native_barrier,
) -> dict[str, Any]:
    """Reconcile, then perform at most one next action. Returns the action taken.

    A caller that holds an older generation is fenced before any effect."""
    from closer_runtime_binding import policy_for_mechanism as policy_problem

    initial = campaign.read()
    configured = (initial or {}).get("worker_config") or {}
    reason = policy_problem(
        configured.get("policy") or {},
        mechanism=configured.get("mechanism", "desktop-scheduler/v1"),
    )
    if reason:
        return {"kind": "held", "reason": reason}
    doc = reconcile(campaign, pid_alive=pid_alive)
    if owner_generation is not None and doc.get("generation", 1) != owner_generation:
        return {"kind": "fenced", "reason": "stale owner generation"}
    action = next_action(doc, release=release, capacity_ok=capacity_ok)
    if action["kind"] != "launch":
        return action
    target = action["target"]
    scoped = doc
    if granularity(doc) == "target-delegate":
        # Evaluate each pending item independently. Native lane/coordinator barriers
        # still hold every item; target claims and ledger uncertainty do not.
        candidates = [t for t, status in doc["targets"].items() if status == "pending"]
        for candidate in candidates:
            scoped = {**doc, "targets": {candidate: "pending"}}
            barrier = native_probe(scoped)
            if not barrier:
                target = candidate
                break
        else:
            return {"kind": "wait", "reason": "native_owner", "detail": barrier}
    barrier = native_probe(scoped)
    if barrier:
        return {"kind": "wait", "reason": "native_owner", "detail": barrier, "target": target}
    claim_target = f"{doc['campaign_id']}:{target}"
    if not claim(claim_target, AGENT):
        return {"kind": "wait", "reason": "target_claimed_elsewhere", "target": target}
    worker_id = uuid.uuid4().hex[:12]
    # Intent is durable BEFORE the process exists, so a crash here reads as `unknown`.
    campaign.update(
        lambda d: (
            d["workers"].append(
                {
                    "worker_id": worker_id,
                    "target": target,
                    "pid": None,
                    "claim_target": claim_target,
                    "state": "launched",
                    "started": _now(),
                }
            ),
            d["targets"].__setitem__(target, "running"),
        )
    )
    try:
        pid = launcher(
            worker_command(target) + ["--result", str(result_path(campaign.path, worker_id))],
            campaign.path.with_name(campaign.path.name + f".{worker_id}.log"),
        )
    except PermissionError as exc:  # denial: hold with the exact reason, no fallback
        reason = str(exc)

        def deny(d):
            for w in d["workers"]:
                if w["worker_id"] == worker_id:
                    w["state"] = "denied"
            d["targets"][target] = "denied"
            d["hold"] = {"kind": "denied", "target": target, "reason": reason}

        campaign.update(deny)
        claims.release(claim_target, AGENT)
        return {"kind": "held", "reason": reason}
    campaign.update(
        lambda d: [w.update(pid=pid) for w in d["workers"] if w["worker_id"] == worker_id]
    )
    claims.update_metadata(claim_target, AGENT, pid=pid)
    return {"kind": "launched", "target": target, "worker_id": worker_id, "pid": pid}


def status_line(doc: dict[str, Any]) -> str:
    """Blocking quantity AND drainable quantity together; prints `complete` when drained."""
    live = sum(1 for w in doc["workers"] if w["state"] in {"launched", "active"})
    return (
        f"phase={doc['phase']} workers={live}/{doc['max_workers']} "
        f"drainable={drainable_workers(doc)} hold={'yes' if doc.get('hold') else 'no'}"
    )


COHORT_RECORD_LIMIT = 4 * 1024 * 1024
COHORT_HISTORY_LIMIT = 64
COHORT_HISTORY_BYTES = 32 * 1024 * 1024


def _complete_rows_from_leaves(manifest, cfg, detail, leaves):
    """Authenticate full completion; STOP alone also admits nonactionable waits."""
    import closer_native_observer as native
    import closer_resources as resources
    from closer_dispatcher import targets_from_assignment

    targets = targets_from_assignment(Path(cfg["assignment"]))
    if len(targets) > 64:
        raise CampaignError("cohort completion target count exceeds bound")
    proof = detail.get("completed_round")
    if not isinstance(proof, dict) or native.completed_round_proof(manifest, cfg, proof) != proof:
        raise CampaignError("cohort lacks authenticated completed round")
    state = Path(str(cfg["dispatch_state"]) + ".pipeline.json")
    verdict = resources.validate_stop(state, proof["round_id"], targets)
    if verdict.get("global_stop_valid") is not True or verdict.get("valid") is not True:
        raise CampaignError("cohort no longer has global STOP")
    receipt = json.loads(native._bounded_bytes(Path(proof["receipt_path"]), 32 * 1024 * 1024))
    covered = {}
    budget = [128 * 1024 * 1024]
    loaded = {}
    for leaf in leaves:
        if leaf["active"]:
            target = leaf["target"]
            if target in covered:
                raise CampaignError("cohort completion has duplicate active leaf")
            ref = leaf["receipt_ref"]
            key = (ref["receipt_path"], ref["receipt_sha256"])
            if key not in loaded:
                loaded[key] = _cohort_read(Path(ref["receipt_path"]), 32 * 1024 * 1024, budget)
            raw = loaded[key]
            import hashlib

            from closer_native_descendants import _digest

            if hashlib.sha256(raw).hexdigest() != ref["receipt_sha256"]:
                raise CampaignError("cohort active leaf receipt changed")
            row = json.loads(raw)["targets"][target]
            if _digest(row) != leaf["source_row_sha256"]:
                raise CampaignError("cohort active leaf row changed")
            covered[target] = row
    for target in targets:
        if target not in covered:
            covered[target] = receipt["targets"][target]
    if set(covered) != set(targets) or any(
        row.get("status") != "completed"
        or row.get("stage_status") != "completed"
        or row.get("provider_outcome_uncertain") not in (None, False)
        or row.get("recovery_barrier") is not None
        for row in covered.values()
    ):
        raise CampaignError("cohort retains incomplete, held or unknown active leaves")
    return {target: sha256_json(covered[target]) for target in targets}


def _completed_cohort_rows(manifest, cfg, detail, doc):
    leaves = []
    if detail.get("descendant_bundle") is not None:
        from closer_native_descendants import _HistoryBudget, _physical, observed_leaves

        budget = _HistoryBudget()
        state = _physical(cfg["native_worker"]["state"])
        budget.reserve_path(state)
        budget.campaign_path = state
        budget.campaign_doc = doc
        leaves = observed_leaves(
            cfg, detail["descendant_bundle"], require_finalized=True, _history_budget=budget
        )
        budget.verify_sources()
    return _complete_rows_from_leaves(manifest, cfg, detail, leaves)


def _producer_completed_cohort_rows(manifest, cfg, detail):
    """Actual result is not published yet; use pinned hook's finalized C bundle."""
    leaves = []
    if detail.get("descendant_bundle") is not None:
        from closer_native_descendants import FINAL_SCHEMA, _digest, _read_signed, bundle_record

        ref = detail["descendant_bundle"]
        finalized = _read_signed(cfg, ref)
        sealed = bundle_record(cfg, ref)
        if (
            finalized.get("schema") != FINAL_SCHEMA
            or finalized.get("sealed_sha256") != _digest(sealed)
            or finalized.get("worker") != cfg["native_worker"]
            or finalized.get("admission") != cfg["native_admission"]
            or finalized.get("cleanup") != detail["native_execution"]["phases"]["C"]["cleanup"]
            or sealed.get("aggregate") != "completed"
        ):
            raise CampaignError("actual completed cohort lacks finalized exact cleanup")
        leaves = sealed["leaves"]
    return _complete_rows_from_leaves(manifest, cfg, detail, leaves)


def _completion_cleanup(cfg, detail, outcome="ok"):
    """Called by the actual worker result producer, never on caller assertions."""
    import math

    execution = detail.get("native_execution") or {}
    phases = execution.get("phases") or {}
    a, b, c = (phases.get(key) or {} for key in ("A", "B", "C"))
    cleanup = c.get("cleanup") or {}
    times = [
        a.get("started_at"),
        a.get("finished_at"),
        b.get("started_at"),
        b.get("finished_at"),
        c.get("started_at"),
        cleanup.get("released_at"),
        c.get("finished_at"),
    ]
    handoff = Path(cfg.get("handoff_dir") or claims._handoff_dir())
    if (
        execution.get("schema") != "native-round-execution/v1"
        or execution.get("admission_nonce") != cfg["native_admission"]["nonce"]
        or execution.get("worker") != cfg["native_worker"]
        or a.get("admitted") is not True
        or a.get("exit_code") != 0
        or b.get("started") is not True
        or type(b.get("exit_code")) is not int
        or b["exit_code"] not in ({0} if outcome == "ok" else {0, 78})
        or c.get("attempted") is not True
        or type(c.get("exit_code")) is not int
        or c["exit_code"] not in ({0} if outcome == "ok" else {0, 65})
        or c.get("dependencies_valid") is not True
        or cleanup.get("schema") != "closer-native-lease-cleanup/v1"
        or cleanup.get("lane") != "closer"
        or cleanup.get("lane_dir") != str(handoff / ".lane-closer.lock")
        or cleanup.get("released") is not True
        or cleanup.get("exact_token_matched") is not True
        or not _DIGEST.fullmatch(str(a.get("token_sha256", "")))
        or cleanup.get("expected_token_sha256") != a["token_sha256"]
        or cleanup.get("meta_sha256") != a["token_sha256"]
        or type(a.get("lease_inode")) is not int
        or a["lease_inode"] <= 0
        or type(cleanup.get("released_lease_inode")) is not int
        or cleanup.get("released_lease_inode") != a["lease_inode"]
        or any(type(stamp) not in (int, float) or not math.isfinite(stamp) for stamp in times)
        or times != sorted(times)
    ):
        raise CampaignError("cohort lacks exact actual A/B0/C0 cleanup")
    return execution


def _cohort_signed_bytes(record, root):
    import closer_mechanisms as mechanisms

    raw = json.dumps(
        {"record": record, "signature": mechanisms._signature(record, mechanisms._key(root))},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    if len(raw) > COHORT_RECORD_LIMIT:
        raise CampaignError("cohort record exceeds bound")
    return raw


def _cohort_read(path, limit, budget):
    import closer_native_observer as native

    size = Path(path).stat(follow_symlinks=False).st_size
    if size > budget[0]:
        raise CampaignError("cohort evidence byte budget exhausted")
    raw = native._bounded_bytes(path, limit)
    if len(raw) > budget[0]:
        raise CampaignError("cohort evidence changed beyond budget")
    budget[0] -= len(raw)
    return raw


def _signed_cohort_record(ref, expected_path, root, budget):
    import hashlib
    import hmac

    import closer_mechanisms as mechanisms

    if (
        not isinstance(ref, dict)
        or set(ref) != {"path", "sha256"}
        or ref["path"] != str(expected_path)
    ):
        raise CampaignError("cohort reference path differs")
    raw = _cohort_read(expected_path, COHORT_RECORD_LIMIT, budget)
    signed = json.loads(raw)
    record = signed["record"]
    if hashlib.sha256(raw).hexdigest() != ref["sha256"] or not hmac.compare_digest(
        str(signed.get("signature", "")), mechanisms._signature(record, mechanisms._key(root))
    ):
        raise CampaignError("cohort record hash/signature differs")
    return record, raw


def _cohort_worker_evidence(state_path, doc, worker, manifest, budget):
    """Historical generation comes from authenticated original admission, not <=."""
    import hashlib

    import closer_native_observer as native

    result_file = result_path(Path(state_path), worker["worker_id"])
    raw = _cohort_read(result_file, COHORT_RECORD_LIMIT, budget)
    result = json.loads(raw)
    context = result["native_admission"]
    native.validate_admission(manifest, context, for_effects=False)
    if (
        result.get("worker_id") != worker["worker_id"]
        or result.get("target") != ROUND_UNIT
        or result.get("mechanism") != "native-handoff/v1"
        or result.get("outcome") != worker.get("outcome")
        or worker.get("state") != "terminal"
        or worker.get("outcome") not in {"ok", "scoped_continuation", "unavailable"}
        or context.get("demand_assignment") != doc.get("demand_assignment")
        or context["campaign"]["owner"] != doc["owner"]
        or context["campaign"]["campaign_id"] != doc["campaign_id"]
        or context["campaign"]["state"] != str(Path(state_path))
    ):
        raise CampaignError("cohort worker identity or original scope differs")
    original = {**doc, "generation": context["campaign"]["generation"]}
    cfg = native.result_admission_config(
        doc["worker_config"], context, Path(state_path), original, worker
    )
    detail = json.loads(result["detail"])
    evidence = {
        "worker_id": worker["worker_id"],
        "result": {"path": str(result_file), "sha256": hashlib.sha256(raw).hexdigest()},
        "result_bytes": raw.decode("utf-8"),
    }
    if worker["outcome"] == "unavailable":
        proof = detail.get("native_no_start")
        if (
            not isinstance(proof, dict)
            or native.pre_dispatch_no_start_proof(manifest, cfg, proof) != proof
        ):
            raise CampaignError("historical worker lacks signed no-start proof")
        return evidence, cfg, detail, None
    root = native.contract(manifest)[3]
    path = root / context["nonce"] / ("completed-cohort-" + worker["worker_id"] + ".json")
    record, terminal_bytes = _signed_cohort_record(
        detail.get("cohort_terminal"), path, root, budget
    )
    if (
        record.get("schema") != "closer-terminal-cohort-worker/v1"
        or record.get("binding") != native.binding(manifest)
        or record.get("admission") != context
        or record.get("campaign") != context["campaign"]
        or record.get("worker") != cfg["native_worker"]
        or record.get("worker_id") != worker["worker_id"]
        or record.get("outcome") != worker["outcome"]
        or type(record.get("completed")) is not bool
        or record.get("native_execution") != detail.get("native_execution")
        or record.get("completed_round") != detail.get("completed_round")
        or record.get("proof") != detail.get("proof")
        or record.get("descendant_bundle") != detail.get("descendant_bundle")
    ):
        raise CampaignError("cohort producer attestation differs from exact result")
    _completion_cleanup(cfg, record, worker["outcome"])
    evidence["terminal"] = detail["cohort_terminal"]
    evidence["terminal_bytes"] = terminal_bytes.decode("utf-8")
    return evidence, cfg, detail, record


def _retired_prefix(state_path, doc, manifest):
    """Authenticate the exact retained historical prefix; never infer from age."""
    import closer_native_observer as native

    ref = doc.get("cohort_retirement")
    if ref is None:
        return {"workers": 0, "depth": 0, "bytes": 0}
    root = native.contract(manifest)[3]
    expected_binding = native.binding(manifest)
    current = doc
    total = 0
    budget = [COHORT_HISTORY_BYTES]
    count = None
    depth = None
    for _index in range(COHORT_HISTORY_LIMIT):
        digest = ref.get("sha256") if isinstance(ref, dict) else None
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise CampaignError("retirement archive identity differs")
        path = Path(state_path).with_name(Path(state_path).name + ".cohorts") / (digest + ".json")
        record, raw = _signed_cohort_record(ref, path, root, budget)
        total += len(raw)
        if total > COHORT_HISTORY_BYTES:
            raise CampaignError("retirement archive history exceeds byte bound")
        old = record.get("previous")
        prefix = old.get("workers") if isinstance(old, dict) else None
        if (
            record.get("schema") != "closer-cohort-retirement/v1"
            or record.get("binding") != expected_binding
            or record.get("state") != str(Path(state_path))
            or not isinstance(prefix, list)
            or not prefix
            or prefix != doc["workers"][: len(prefix)]
            or old.get("owner") != doc["owner"]
            or old.get("campaign_id") != doc["campaign_id"]
            or old.get("release_pin") != doc["release_pin"]
            or record.get("next_generation") != old.get("generation", 0) + 1
            or record.get("next_generation") != current["demand_assignment"]["campaign_generation"]
            or record.get("next_request_id") != current["demand_admission"]["request_id"]
            or record.get("authority") is not False
        ):
            raise CampaignError("retirement archive scope/prefix/generation differs")
        if (
            type(record.get("depth")) is not int
            or not 1 <= record["depth"] <= COHORT_HISTORY_LIMIT
            or (depth is not None and record["depth"] != depth - _index)
        ):
            raise CampaignError("retirement archive depth differs")
        if count is None:
            count = len(prefix)
            depth = record["depth"]
        ref = old.get("cohort_retirement")
        if ref is None:
            if record["depth"] != 1:
                raise CampaignError("retirement archive chain is incomplete")
            return {"workers": count, "depth": depth, "bytes": total}
        current = old
    raise CampaignError("retirement archive history exceeds count bound")


def _owned_cohort_workers(state_path, doc, manifest):
    workers = doc.get("workers")
    if not isinstance(workers, list) or len(workers) > 1024:
        raise CampaignError("cohort worker registry exceeds bound")
    return workers[_retired_prefix(state_path, doc, manifest)["workers"] :]


def current_cohort_workers(state_path, doc, manifest):
    """Authenticated retired prefix only; retained JSON status is not an origin."""
    import closer_native_observer as native

    current = _owned_cohort_workers(state_path, doc, manifest)
    if len(current) > COHORT_HISTORY_LIMIT:
        raise CampaignError("current cohort worker validation exceeds bound")
    budget = [128 * 1024 * 1024]
    if doc.get("demand_admission") is not None:
        for worker in current:
            # Publication already refuses live/unknown rows. Known results must
            # identify the exact CURRENT origin before defeating first-start expiry.
            if worker.get("state") != "terminal":
                raise CampaignError("current cohort still has live/unknown workers")
            result = json.loads(
                _cohort_read(
                    result_path(Path(state_path), worker["worker_id"]), COHORT_RECORD_LIMIT, budget
                )
            )
            context = result["native_admission"]
            native.validate_admission(manifest, context, for_effects=False)
            if context.get("demand_assignment") != doc.get("demand_assignment"):
                raise CampaignError("worker history belongs to another unretired demand origin")
            old = {**doc, "generation": context["campaign"]["generation"]}
            native.result_admission_config(
                doc["worker_config"], context, Path(state_path), old, worker
            )
    return current


def _retirement_evidence(state_path, doc, manifest):
    """Read-only completion proof before and again inside the existing CAS."""
    import closer_native_observer as native

    workers = current_cohort_workers(state_path, doc, manifest)
    if (
        doc.get("phase") != "work"
        or doc.get("hold")
        or doc.get("release_record") is not None
        or doc.get("release_attempt") is not None
        or doc.get("targets") != {ROUND_UNIT: "done"}
        or not workers
        or len(workers) > COHORT_HISTORY_LIMIT
        or any(
            w.get("state") != "terminal"
            or w.get("outcome") not in {"ok", "scoped_continuation", "unavailable"}
            for w in workers
        )
        or any(w.get("pid") and claims._pid_alive(w["pid"]) for w in workers)
        or any(
            row.get("status") not in {"consumed", "resolved"}
            for row in (doc.get("hold_followups") or {}).values()
        )
        or any(
            row.get("status") not in {"acknowledged", "ack_overdue"}
            for row in (doc.get("hold_escalations") or {}).values()
        )
    ):
        raise CampaignError("cohort retains ownership, unknown outcomes or held budgets")
    if native_barrier(doc) is not None or claims.holder(f"{doc['campaign_id']}:{ROUND_UNIT}"):
        raise CampaignError("cohort native/claim ownership is still live")
    if qualify(manifest, Path(state_path).parent):
        raise CampaignError("cohort package is no longer qualified")
    evidence = []
    budget = [128 * 1024 * 1024]
    for worker in workers:
        item, cfg, detail, terminal = _cohort_worker_evidence(
            state_path, doc, worker, manifest, budget
        )
        evidence.append(item)
    if (
        workers[-1]["outcome"] != "ok"
        or terminal is None
        or terminal.get("completed") is not True
        or terminal.get("campaign") != native._campaign_identity(state_path, doc)
        or detail.get("cohort_completion") != detail.get("cohort_terminal")
        or terminal["row_sha256"] != _completed_cohort_rows(manifest, cfg, detail, doc)
    ):
        raise CampaignError("cohort has no final authenticated full completion")
    if cfg["native_admission"].get("demand_continuations", []) != doc.get(
        "demand_continuations", []
    ):
        raise CampaignError("cohort continuation journal differs from final signed origin")
    released_holds = set()
    continuation_bytes = []
    for ref in cfg["native_admission"].get("demand_continuations", []):
        raw = _cohort_read(Path(ref["path"]), COHORT_RECORD_LIMIT, budget)
        # validate_admission above already authenticated this exact chain. Keep
        # its immutable bytes and demand that every discharged budget appears
        # in an actual signed release, rather than accepting a status label.
        if sha256_file(ref["path"]) != ref["sha256"]:
            raise CampaignError("cohort continuation changed before archive")
        continuation_bytes.append(raw.decode("utf-8"))
        released_holds.update(row["hold_id"] for row in json.loads(raw)["record"]["releases"])
    if any(
        row.get("status") != "consumed"
        or row.get("hold_id") != hold_id
        or hold_id not in released_holds
        for hold_id, row in (doc.get("hold_followups") or {}).items()
    ):
        raise CampaignError("cohort held budget lacks authenticated consumed release")
    original_assignment = _cohort_read(Path(cfg["assignment"]), 2 * 1024 * 1024, budget)
    original_receipt = _cohort_read(
        Path(detail["completed_round"]["receipt_path"]), 32 * 1024 * 1024, budget
    )
    if (
        sha256_file(detail["completed_round"]["receipt_path"])
        != detail["completed_round"]["receipt_sha256"]
    ):
        raise CampaignError("completed cohort receipt changed before archive")
    # Preserve the exact existing per-target journals and immutable receipt
    # proofs before another cohort can touch the same scoped infrastructure.
    # Paths come only from the authenticated original cfg/bundle, never a scan.
    import hashlib

    import closer_pipeline as pipeline
    import closer_resources as resources

    pipeline_path = Path(str(cfg["dispatch_state"]) + ".pipeline.json")
    scoped_root = resources.scoped_root(pipeline_path)
    paths = {pipeline_path, Path(detail["completed_round"]["receipt_path"])}
    for target in terminal["row_sha256"]:
        shard = resources.shard_dir(scoped_root, target) / "state.json"
        paths.add(shard)
        paths.add(shard.parent / "state.actions" / (pipeline._digest(target) + ".json"))
    bundle_ref = detail.get("descendant_bundle")
    if bundle_ref is not None:
        import closer_native_descendants as descendants

        paths.add(Path(bundle_ref["path"]))
        sealed = descendants.bundle_record(cfg, bundle_ref)
        for leaf in sealed["leaves"]:
            paths.add(Path(leaf["receipt_ref"]["receipt_path"]))
    if len(paths) > 256:
        raise CampaignError("retirement journal path count exceeds bound")
    journals = []
    for path in sorted(paths):
        # Missing optional legacy/actions files are absence, not a replacement
        # for the authenticated receipt/current row proof already required.
        if path.exists():
            raw = _cohort_read(path, COHORT_RECORD_LIMIT, budget)
            journals.append(
                {
                    "path": str(path),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": raw.decode("utf-8"),
                }
            )
    return {
        "journals": journals,
        "continuation_bytes": continuation_bytes,
        "workers": evidence,
        "completed": detail["cohort_completion"],
        "row_sha256": terminal["row_sha256"],
        "assignment_bytes": original_assignment.decode("utf-8"),
        "receipt_bytes": original_receipt.decode("utf-8"),
    }


def _next_cohort_config(doc, targets, request):
    """Freeze fresh existing read-only observations, never reuse a retired frontier."""
    import copy

    import closer_dispatcher as dispatcher
    import closer_frontier_policy as policy

    cfg = copy.deepcopy(doc["worker_config"])
    deadline = _now() + 30
    runtime_binding = None
    if cfg.get("runtime_manifest"):
        from closer_runtime_binding import binding_from_manifest

        try:
            runtime_binding = binding_from_manifest(cfg["runtime_manifest"])
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            # This optional prospective guard is not launch/effect admission.
            # The existing campaign independently qualifies its runtime later.
            runtime_binding = None
    backend = dispatcher.Closer(
        dispatcher.GitHub(deadline=deadline),
        runtime_binding=runtime_binding,
        state_path=cfg["dispatch_state"],
        apply=False,
        workers=False,
    )
    observations = {target: backend.observe(target) for target in targets}
    bindings = {}
    for target, row in observations.items():
        if (
            row.get("target") != target
            or row.get("revision") in (None, "unknown")
            or row.get("_local_cached") is True
            or not isinstance(row.get("evidence"), dict)
            or row.get("action") == "complete"
            or not (
                row.get("eligible") is True
                or row.get("action") == "collect_evidence"
                or (
                    row.get("action") == "wait"
                    and row.get("reason") == "current-head checks pending or unknown"
                    and row.get("pending_check_run_ids")
                    and row.get("pending_check_head") == row.get("head")
                )
            )
        ):
            raise CampaignError("fresh demand lacks exact new actionable/CI-wait evidence")
        bindings[target] = {
            "revision": row["revision"],
            "action_id": policy.action_identity(
                target, row["revision"], row["action"], row["operation_binding"]
            ),
        }
    if cfg.get("finite_frontier") is not None:
        cfg["finite_frontier"] = policy.make_frontier(
            targets,
            owner=doc["owner"],
            frontier_id="demand-" + sha256_json([request, bindings])[:40],
            bindings=bindings,
        )
        if cfg["finite_frontier"] == doc["worker_config"]["finite_frontier"]:
            raise CampaignError("retired frontier cannot authorize another cohort")
    return cfg


@contextmanager
def _retirement_locks(manifest):
    import closer_native_observer as native

    root = native.contract(manifest)[3]
    with (
        (root / "native-supervisor.lock").open("a+") as supervisor,
        (root / "native-entry.lock").open("a+") as entry,
    ):
        try:
            fcntl.flock(supervisor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(entry.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CampaignError("existing native owner blocks cohort retirement") from exc
        yield


def _retirement_view(state_path, doc, request, cfg, expected):
    import copy

    import closer_native_observer as native

    manifest = load_manifest(doc["worker_config"]["runtime_manifest"])
    actual = _retirement_evidence(state_path, doc, manifest)
    if {key: value for key, value in cfg.items() if key != "finite_frontier"} != {
        key: value for key, value in doc["worker_config"].items() if key != "finite_frontier"
    }:
        raise CampaignError("fresh cohort changed qualified worker configuration")
    if doc["worker_config"].get("finite_frontier") is not None:
        from closer_frontier_policy import validate_frontier

        frontier = validate_frontier(cfg.get("finite_frontier"), targets=request["targets"])
        if frontier["owner"] != doc["owner"] or frontier == doc["worker_config"]["finite_frontier"]:
            raise CampaignError("fresh cohort must not reuse original finite authority")
    elif cfg.get("finite_frontier") is not None:
        raise CampaignError("fresh cohort changed native publication mechanism")
    if (
        actual != expected
        or request["campaign_generation"] != doc["generation"] + 1
        or request["expected_revision"] != doc["revision"]
    ):
        raise CampaignError("cohort changed before retirement CAS")
    # Archive precedes canonical generation/admission. A crash leaves only an
    # unreferenced, non-authorizing orphan; accepted requests retain their receipt.
    history = _retired_prefix(state_path, doc, manifest)
    if history["depth"] >= COHORT_HISTORY_LIMIT:
        raise CampaignError("retirement archive history exceeds count bound")
    from closer_demand_admission import _hash as demand_hash

    record = {
        "schema": "closer-cohort-retirement/v1",
        "depth": history["depth"] + 1,
        "state": str(Path(state_path)),
        "binding": native.binding(manifest),
        "previous": copy.deepcopy(doc),
        "evidence": actual,
        "next_generation": request["campaign_generation"],
        "next_request_id": demand_hash(request),
        "authority": False,
    }
    selected = copy.deepcopy(doc)
    selected.update(
        generation=request["campaign_generation"],
        targets={ROUND_UNIT: "pending"},
        assignment_targets=list(request["targets"]),
        worker_config=copy.deepcopy(cfg),
        hold_followups={},
        hold_escalations={},
    )
    selected.pop("demand_wake", None)
    selected.pop("demand_prepared", None)
    return selected, record


def _store_retirement(state_path, manifest, record):
    import hashlib

    import closer_native_observer as native

    history = _retired_prefix(state_path, record["previous"], manifest)
    root = native.contract(manifest)[3]
    raw = _cohort_signed_bytes(record, root)
    if history["bytes"] + len(raw) > COHORT_HISTORY_BYTES:
        raise CampaignError("retirement archive history exceeds byte bound")
    digest = hashlib.sha256(raw).hexdigest()
    directory = Path(state_path).with_name(Path(state_path).name + ".cohorts")
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / (digest + ".json")
    if path.resolve() != path:
        raise CampaignError("retirement archive path is aliased")
    if not path.exists():
        count = 0
        used = 0
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    raise CampaignError("retirement archive contains unknown storage")
                count += 1
                used += entry.stat(follow_symlinks=False).st_size
                if count >= COHORT_HISTORY_LIMIT or used + len(raw) > COHORT_HISTORY_BYTES:
                    raise CampaignError("retirement archive storage capacity exhausted")
    try:
        native._immutable(path, raw)
    except FileExistsError:
        if native._bounded_bytes(path, COHORT_RECORD_LIMIT) != raw:
            raise CampaignError("retirement archive bytes conflict")
    return {"path": str(path), "sha256": digest}


def _seal_completed_cohort(state_path, cfg, detail, worker_id, outcome):
    """Only run_worker.finish may seal its actual successful runtime result."""
    import hashlib

    import closer_native_observer as native

    manifest = load_manifest(cfg["runtime_manifest"])
    doc = Campaign(state_path).read()
    matches = [worker for worker in doc["workers"] if worker["worker_id"] == worker_id]
    if len(matches) != 1:
        raise CampaignError("cohort completion worker identity differs")
    original_cfg = native.result_admission_config(
        doc["worker_config"], cfg["native_admission"], Path(state_path), doc, matches[0]
    )
    original_cfg["native_worker"] = cfg["native_worker"]
    complete = False
    rows = None
    if outcome == "ok":
        proof = detail.get("completed_round")
        if native.completed_round_proof(manifest, original_cfg, proof) != proof or not isinstance(
            proof, dict
        ):
            raise CampaignError("known terminal worker lacks signed C0 boundary")
        try:
            rows = _producer_completed_cohort_rows(manifest, original_cfg, detail)
            complete = True
        except CampaignError:
            pass  # Known terminality is weaker than completed-cohort retirement.
    else:
        from closer_runtime_binding import scoped_continuation_proof

        proof = detail.get("proof")
        if not isinstance(proof, dict) or scoped_continuation_proof(original_cfg, proof) != proof:
            raise CampaignError("known terminal worker lacks scoped continuation proof")
    execution = _completion_cleanup(original_cfg, detail, outcome)
    record = {
        "schema": "closer-terminal-cohort-worker/v1",
        "completed": complete,
        "outcome": outcome,
        "binding": native.binding(manifest),
        "campaign": cfg["native_admission"]["campaign"],
        "admission": cfg["native_admission"],
        "worker": cfg["native_worker"],
        "worker_id": worker_id,
        "completed_round": detail.get("completed_round"),
        "proof": detail.get("proof"),
        "descendant_bundle": detail.get("descendant_bundle"),
        "row_sha256": rows,
        "native_execution": execution,
        "authority": False,
    }
    root = native.contract(manifest)[3]
    path = root / cfg["native_admission"]["nonce"] / ("completed-cohort-" + worker_id + ".json")
    raw = _cohort_signed_bytes(record, root)
    try:
        native._immutable(path, raw)
    except FileExistsError:
        if native._bounded_bytes(path, COHORT_RECORD_LIMIT) != raw:
            raise CampaignError("cohort completion bytes already differ")
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}, complete


def _store_demand_assignment(state_path, raw, receipt):
    """Prepare exact immutable bytes; an orphan after failed CAS grants no launch."""
    import hashlib

    digest = hashlib.sha256(raw).hexdigest()
    if digest != receipt["assignment_sha256"]:
        raise CampaignError("demand snapshot differs from admitted bytes")
    directory = Path(state_path).with_name(Path(state_path).name + ".demand")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.resolve() != directory or directory.is_symlink():
        raise CampaignError("demand snapshot directory is aliased")
    path = directory / (digest + ".json")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        from closer_native_observer import _bounded_bytes

        if _bounded_bytes(path, 2 * 1024 * 1024) != raw:
            raise CampaignError("demand snapshot bytes changed")
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return {
        "schema": "closer-demand-assignment/v1",
        "path": str(path),
        "sha256": digest,
        **{
            key: receipt[key]
            for key in (
                "campaign_id",
                "owner",
                "campaign_generation",
                "assignment_generation",
                "request_id",
            )
        },
    }


# ------------------------------------------------------------- release status


def release_plan(record: dict[str, Any] | None) -> dict[str, Any]:
    """What a resume may do, from the sealed journal alone. Executes nothing.

    verify/install appear only for stages that have not yet produced the receipt."""
    if record is None:
        return {"action": "none", "reason": "no release record"}
    stage = record["stage"]
    receipt = record.get("verification") or {}
    sealed = bool(receipt.get("verified")) and bool(
        _DIGEST.fullmatch(str(receipt.get("snapshot_digest", "")))
    )
    plan = {
        "stage": stage,
        "revision": record["revision"],
        "sealed_receipt": sealed,
        "reverify": False,
        "reinstall": False,
    }
    if stage in {"installed", "acceptance_pending"}:
        plan.update(
            action="await_acceptance",
            reason="installed generation journaled; no reverify/reinstall",
        )
    elif stage in {"accepted", "rolled_back", "failed"}:
        plan.update(action="terminal")
    elif stage in {"verified", "installing"}:
        if not sealed:
            plan.update(action="hold", reason="stage requires a sealed receipt that is missing")
        else:
            plan.update(
                action="resume_install", reason="reuse sealed receipt; verification skipped"
            )
    elif stage == "rollback_pending":
        plan.update(action="resume_rollback")
    else:  # prepared / verifying
        plan.update(action="resume_verify", reverify=True, reason="no sealed receipt exists yet")
    return plan


# ------------------------------------------------------- manifest & promotion


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    problems = []
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        problems.append("unsupported manifest schema")
    if manifest.get("contract") != CONTRACT:
        problems.append("contract version mismatch")
    if not _SHA.fullmatch(str(manifest.get("candidate_sha", ""))):
        problems.append("invalid candidate_sha")
    if not _DIGEST.fullmatch(str(manifest.get("package_digest", ""))):
        problems.append("invalid package_digest")
    versions = manifest.get("state_versions")
    if not isinstance(versions, dict) or not versions:
        problems.append("state_versions missing")
    dist = manifest.get("distribution")
    if dist is not None:
        if (dist.get("core") or {}).get("owner") != "Workflows":
            problems.append("distribution: Core must be owned by Workflows")
        if (dist.get("orchestrator") or {}).get("runtime_version") != RUNTIME_VERSION:
            problems.append("distribution: runtime version not supported by this runner")
        launcher = manifest.get("launcher")
        if launcher is not None and launcher.get("version") != LAUNCHER_VERSION:
            problems.append("launcher version not supported by this runner")
        if launcher is not None and launcher.get("compatible_contract") != CONTRACT:
            problems.append("launcher pins a different core contract")
    return problems


def check_promotion(
    manifest: dict[str, Any],
    enabled_runners: list[str],
    receipts: list[dict[str, Any]],
    *,
    observer=None,
) -> dict[str, Any]:
    """Fail closed: promotable only if EVERY enabled runner has a real receipt for
    this exact candidate digest and state versions, covering all four phases."""
    problems = [f"manifest: {p}" for p in validate_manifest(manifest)]
    if not enabled_runners:
        problems.append("no enabled runners listed")
    by_runner: dict[str, dict[str, Any]] = {}
    for candidate_receipt in receipts:
        runner_id = candidate_receipt.get("runner_id")
        if not isinstance(runner_id, str) or not runner_id:
            problems.append("runner receipt has no valid runner_id")
            continue
        if runner_id in by_runner:
            problems.append(f"{runner_id}: duplicate runner receipts")
            continue
        by_runner[runner_id] = candidate_receipt
    for runner in enabled_runners:
        receipt = by_runner.get(runner)
        if receipt is None:
            problems.append(f"{runner}: no runner receipt")
            continue
        if receipt.get("kind") in {"harness-subprocess", "launcher-entrypoint"}:
            problems.append(
                f"{runner}: {receipt['kind']} receipt proves the lifecycle only; "
                "desktop-agent runtime validation is still pending"
            )
        elif receipt.get("kind") in _UNPROVEN_KINDS or receipt.get("kind") != "desktop-runner":
            problems.append(f"{runner}: receipt is not from the actual runner")
        from closer_runtime_binding import desktop_problems

        problems.extend(
            f"{runner}: {p}"
            for p in desktop_problems(manifest, receipt, observer, enabled_runners=enabled_runners)
        )
        if receipt.get("package_digest") != manifest.get("package_digest"):
            problems.append(f"{runner}: receipt is for a different package digest")
        if receipt.get("state_versions") != manifest.get("state_versions"):
            problems.append(f"{runner}: receipt state versions differ from manifest")
        phases = receipt.get("phases") or {}
        for phase in RUNNER_PHASES:
            if (phases.get(phase) or {}).get("passed") is not True or not (
                phases.get(phase) or {}
            ).get("evidence"):
                problems.append(f"{runner}: phase {phase} lacks a passing, evidenced result")
    return {"promotable": not problems, "problems": problems, "runners": sorted(enabled_runners)}


def promotion_owner(campaign_doc: dict[str, Any] | None, runner_pin: str) -> dict[str, Any]:
    """One owner during partial promotion: a running campaign stays on its pin."""
    if campaign_doc is None or campaign_doc["phase"] == "complete":
        return {"may_write": True}
    if campaign_doc["release_pin"] == runner_pin:
        return {"may_write": True}
    return {
        "may_write": False,
        "reason": "campaign is pinned to another release",
        "owner_pin": campaign_doc["release_pin"],
    }


def rollback_decision(
    previous: dict[str, Any],
    current_state_versions: dict[str, int],
    campaign_doc: dict[str, Any] | None,
) -> dict[str, Any]:
    """Package rollback only; state is never rolled back."""
    problems = validate_manifest(previous)
    if previous.get("state_versions") != current_state_versions:
        problems.append("state versions differ; package rollback would orphan newer state")
    if (
        campaign_doc
        and campaign_doc["release_pin"] != previous.get("package_digest")
        and any(w["state"] in {"launched", "active", "unknown"} for w in campaign_doc["workers"])
    ):
        problems.append("workers are pinned to the newer release; reconcile first")
    return {"allowed": not problems, "problems": problems}


# ------------------------------------------------- shared fence & uncertainty


def classify_exception(exc: BaseException) -> str:
    """Offline vs denied vs uncertain. Only a refused connection is `unavailable`:
    a timeout or any other failure AFTER a request may have been sent is `unknown`."""
    if isinstance(exc, PermissionError):
        return "denied"
    if isinstance(exc, ConnectionRefusedError):
        return "unavailable"
    return "unknown"


class StaleOwner(CampaignError):
    """The caller's campaign generation is no longer current; nothing was submitted."""


class CoreAdapterMissing(CampaignError):
    """No pinned Core operation-store adapter: an action effect must hold, never fall back."""


@dataclasses.dataclass(frozen=True)
class ActionSpec:
    """What an action effect targets. The Core adapter derives the canonical target/action
    fence key from repository + PR + action ONLY, so it survives head/criteria changes."""

    repository: str
    pr_number: int
    action: str
    head_sha: str
    criteria_fingerprint: str = ""
    review_revision: str = ""
    base_sha: str | None = None
    # Core's schema requires `authority` >= 2 and 40-hex head / 64-hex criteria / a review
    # revision; the adapter forwards them unchanged and Core's validation is the authority.
    guard_versions: tuple[tuple[str, Any], ...] = (("authority", 2),)
    blocking_labels: tuple[str, ...] = ()
    native_binding: dict[str, Any] | None = None


class CoreOperationAdapter(Protocol):
    """Injection point for Core-owned claim/intent/reconcile fences (Workflows owns them).

    `claim` returns `{"state": "go"|"denied"|"unknown", "handle": ..., "detail": ...}` and
    must report `denied` for any recorded denial and `unknown` for any unresolved or foreign
    operation on the same target/action key, whatever its head. `record_intent` is durable
    before the effect; `settle` maps `ok|unavailable|denied|unknown` onto Core status."""

    contract: str

    def claim(self, spec: ActionSpec, holder: str) -> dict[str, Any]: ...

    def record_intent(self, handle: Any, intent: dict[str, Any]) -> None: ...

    def settle(self, handle: Any, outcome: str) -> None: ...

    def reject_not_accepted(self, handle: Any, evidence: dict[str, Any]) -> None: ...


class VerificationRequestNotAccepted(Exception):
    """Synchronous authoritative rejection of this exact verification POST."""

    def __init__(self, evidence):
        self.evidence = evidence
        super().__init__("verification request explicitly not accepted")


def submit_effect(
    adapter: CoreOperationAdapter | None,
    port: OperationPort,
    spec: ActionSpec,
    action: dict[str, Any],
    *,
    holder: str,
) -> str:
    """The ONE gate for every action effect, preferred port or basic fallback alike.

    Absent or wrong-contract adapter: `held`, nothing submitted. A denial or an unresolved
    prior attempt on the target/action is a barrier that returns without calling the port."""
    if adapter is None or getattr(adapter, "contract", None) != CONTRACT:
        return "held"
    try:
        verdict = adapter.claim(spec, holder)
    except Exception as exc:  # noqa: BLE001 - an unreadable store is a barrier, not a pass
        return "held" if not isinstance(exc, PermissionError) else "denied"
    state = verdict.get("state")
    if state == "denied":
        return "denied"
    if state != "go":
        return "unknown"
    handle = verdict["handle"]
    try:
        adapter.record_intent(
            handle,
            {
                "action": spec.action,
                "head_sha": spec.head_sha,
                **({"native_binding": spec.native_binding} if spec.native_binding else {}),
            },
        )
    except PermissionError:
        return "denied"
    except Exception:
        return "unknown"  # intent CAS may have landed; never submit without readback
    try:
        outcome = port.submit(spec.action, action)
    except VerificationRequestNotAccepted as exc:
        try:
            adapter.reject_not_accepted(handle, exc.evidence)
        except Exception:
            return "unknown"  # rejection receipt CAS may have landed; inspect it first
        return "not_accepted"
    except Exception as exc:  # noqa: BLE001 - classified, never swallowed silently
        outcome = classify_exception(exc)
    if outcome not in OUTCOMES:
        outcome = "unknown"
    try:
        adapter.settle(handle, outcome)
    except Exception:  # noqa: BLE001 - the effect may have landed: the intent stays a barrier
        return "unknown"
    return outcome


def _atomic_write(
    path: Path, doc: dict[str, Any], *, create_only: bool = False, parent_fd: int | None = None
) -> None:
    if parent_fd is not None:
        # Opt-in only: an owned directory handle pins custody across path races.
        # Defaults below retain the existing campaign writer's exact behavior.
        if type(parent_fd) is not int or create_only is not True:
            raise CampaignError("owned directory write requires exclusive-create mode")
        if any(
            function not in os.supports_dir_fd
            for function in (os.open, os.stat, os.link, os.unlink)
        ):
            raise CampaignError("descriptor-relative custody writes are unsupported")
        path = Path(path)
        held = os.fstat(parent_fd)
        if (
            not path.is_absolute()
            or path.parent.resolve() != path.parent
            or path.name in {"", ".", ".."}
            or not stat.S_ISDIR(held.st_mode)
            or held.st_uid != os.getuid()
            or held.st_mode & 0o022
        ):
            raise CampaignError("owned custody directory differs")

        def unchanged_parent():
            named = os.stat(path.parent, follow_symlinks=False)
            if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (
                held.st_dev,
                held.st_ino,
            ):
                raise CampaignError("owned custody parent moved")

        unchanged_parent()
        payload = (
            json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode()
        if not 0 < len(payload) <= 512 * 1024:
            raise CampaignError("owned custody document outside bounds")
        temporary = "." + path.name + "." + uuid.uuid4().hex
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            unchanged_parent()
            os.link(
                temporary,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.fsync(parent_fd)
            unchanged_parent()
        finally:
            os.unlink(temporary, dir_fd=parent_fd)
        return
    payload = (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if create_only:
            # Link only a complete, fsynced file; an existing result is never replaced.
            os.link(name, path)
            parent_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        else:
            os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def sha256_file(path: str | Path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_json(value: Any) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class PinnedCoreAdapter:
    """Production-compatible adapter over the pinned Core module (closer-operation/v1).

    Maps Core's claim/intent/terminalize/submitted records onto `CoreOperationAdapter`;
    all ownership rules (target/action key, CAS, lease, denial) stay Core's."""

    contract = CONTRACT
    REQUIRED = (
        "OperationSpec",
        "ExecutorIdentity",
        "LocalOperationStore",
        "canonical_target_fence_key",
        "record_mutation_intent",
        "mark_submitted",
        "terminalize",
        "reconcile_unknown",
        "ReconciliationEvidence",
        "OwnershipDenied",
        "CONTRACT",
    )
    # Core refuses `succeeded` without typed reconciliation evidence, so a port's own `ok` is
    # not terminal: it stays `submitted` (a barrier) until `reconcile` records the read-back.
    TERMINAL_FOR = {"unavailable": "uncertain", "denied": "denied"}

    def __init__(self, module: Any, root: str | Path, *, lease_seconds: int = 900, store=None):
        missing = [n for n in self.REQUIRED if not hasattr(module, n)]
        if missing or getattr(module, "CONTRACT", None) != CONTRACT:
            raise CampaignError(f"pinned core is not {CONTRACT}-compatible: missing {missing}")
        self.m = module
        self.store = store if store is not None else module.LocalOperationStore(Path(root))
        self.shared = store is not None and bool(getattr(store, "_allow_shared", False))
        self.lease_seconds = lease_seconds

    def _spec(self, s: ActionSpec):
        return self.m.OperationSpec(
            repository=s.repository,
            pr_number=s.pr_number,
            action=s.action,
            head_sha=s.head_sha,
            base_sha=s.base_sha,
            criteria_fingerprint=s.criteria_fingerprint,
            review_revision=s.review_revision,
            guard_versions={
                **dict(s.guard_versions),
                **({"native_binding": sha256_json(s.native_binding)} if s.native_binding else {}),
            },
            blocking_labels=s.blocking_labels,
        )

    def target_fence_key(self, spec: ActionSpec) -> str:
        return self.m.canonical_target_fence_key(self._spec(spec))

    def claim(self, spec: ActionSpec, holder: str) -> dict[str, Any]:
        import datetime as dt

        ospec = self._spec(spec)
        key = self.m.canonical_target_fence_key(ospec)
        try:
            existing = self.store.read(key)
        except self.m.PolicyDenied as exc:
            return {"state": "denied", "detail": str(exc)}
        if existing is not None:
            if existing.get("denial") or existing.get("status") == "denied":
                return {"state": "denied", "detail": "core denial fence"}
            if existing.get("status") in {"intent_recorded", "submitted", "uncertain"}:
                return {"state": "unknown", "detail": f"unresolved {existing['status']}"}
        executor = self.m.ExecutorIdentity("orchestrator_local", holder)
        deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=self.lease_seconds)
        try:
            record, _ = self.store.claim(
                ospec,
                executor,
                lease_deadline=deadline,
                acknowledge_replacement=True,
            )
        except self.m.PolicyDenied as exc:
            return {"state": "denied", "detail": str(exc)}
        except self.m.OwnershipDenied as exc:
            return {"state": "unknown", "detail": f"held by another owner: {exc}"}
        return {
            "state": "go",
            "handle": {"key": key, "spec": ospec, "executor": executor, "record": record},
        }

    def record_intent(self, handle: Any, intent: dict[str, Any]) -> None:
        rec = handle["record"]
        try:
            handle["record"] = self.m.record_mutation_intent(
                self.store,
                handle["key"],
                handle["executor"],
                intent,
                expected_revision=rec["lease"]["revision"],
                expected_epoch=rec["lease"]["epoch"],
                spec=handle["spec"],
            )
        except self.m.PolicyDenied as exc:
            raise PermissionError(str(exc)) from exc

    def reject_not_accepted(self, handle: Any, evidence: dict[str, Any]) -> None:
        rec = handle["record"]
        proof = self.m.VerificationRequestRejected(**evidence)
        handle["record"] = self.m.reject_not_accepted(
            self.store,
            handle["key"],
            handle["executor"],
            proof,
            expected_revision=rec["lease"]["revision"],
            expected_epoch=rec["lease"]["epoch"],
            spec=handle["spec"],
        )

    def settle(self, handle: Any, outcome: str) -> None:
        rec = handle["record"]
        common = dict(
            expected_revision=rec["lease"]["revision"],
            expected_epoch=rec["lease"]["epoch"],
            spec=handle["spec"],
        )
        if outcome in self.TERMINAL_FOR:
            extra = {"denial_reason": "denied by action port"} if outcome == "denied" else {}
            self.m.terminalize(
                self.store,
                handle["key"],
                self.TERMINAL_FOR[outcome],
                executor=handle["executor"],
                **common,
                **extra,
            )
        else:  # ok/unknown: submitted is a barrier until Core reconciliation reads it back
            self.m.mark_submitted(self.store, handle["key"], handle["executor"], **common)

    def reconcile(
        self, spec: ActionSpec, holder: str, *, result: str, source: str, observation_digest: str
    ) -> str:
        """Record the read-back for a submitted/uncertain operation; returns Core's status."""
        ospec = self._spec(spec)
        key = self.m.canonical_target_fence_key(ospec)
        rec = self.store.read(key)
        if rec is None:
            raise CampaignError("no operation to reconcile")
        evidence = self.m.ReconciliationEvidence(
            operation_key=rec["operation_key"],
            target_fence_key=key,
            head_sha=spec.head_sha,
            action=spec.action,
            result=result,
            source=source,
            observation_digest=observation_digest,
            **(
                {"context": "/".join(ospec.verification_context())}
                if spec.action == "verify"
                else {}
            ),
        )
        done = self.m.reconcile_unknown(
            self.store,
            key,
            expected_revision=rec["lease"]["revision"],
            executor=self.m.ExecutorIdentity("orchestrator_local", holder),
            spec=ospec,
            expected_epoch=rec["lease"]["epoch"],
            evidence=evidence,
        )
        return done["status"]


class PinnedPortable:
    """The vendored Workflows core, loaded only when its file matches the pinned digest.

    Workflows owns the claim/intent/reconcile fences; this loads that code (never a mutable
    draft) and exposes it through `operation_adapter`. A digest mismatch or an incompatible
    module raises, and the caller holds: there is no private substitute."""

    def __init__(self, path: str | Path, digest: str):
        if not _DIGEST.fullmatch(digest) or sha256_file(path) != digest:
            raise CampaignError("portable core does not match its pinned digest")
        import importlib.util

        spec = importlib.util.spec_from_file_location("_closer_core_offline_" + digest, str(path))
        if spec is None or spec.loader is None:
            raise CampaignError("pinned Core has no supported Python loader")
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.module
        spec.loader.exec_module(self.module)

    def fingerprint(self, head_sha: str, criteria: str, review: str) -> str:
        return self.module.verification_fingerprint(head_sha, criteria, review)

    def operation_adapter(self, root: str | Path, **kw: Any) -> PinnedCoreAdapter:
        return PinnedCoreAdapter(self.module, root, **kw)


# ------------------------------------------------------ dispatcher adapter


def dispatch_command(cfg: dict[str, Any], python: str, module_dir: Path) -> list[str]:
    """The existing `closer_dispatcher.py run` command for the WHOLE assignment.

    That command is a whole-round owner (its mandatory ACTION A/C run once, under the state
    file's original lease); parallelism comes from its own `--max-workers`, not from us."""
    purpose = (cfg.get("policy") or {}).get("correction_coding_worker")
    if purpose is not None:
        import closer_coding_continuation

        closer_coding_continuation.validate_capability(purpose)
        if cfg.get("mechanism") != "native-handoff/v1" or cfg.get("finite_frontier") is None:
            raise CampaignError("coding WORKER requires ordinary finite native admission")
    continuation = cfg.get("coding_continuation")
    if continuation is not None:
        from closer_native_observer import coding_dispatch_selected

        coding_dispatch_selected(cfg)
    command = [
        python,
        str(module_dir / "closer_dispatcher.py"),
        "run",
        "--assignment",
        cfg["assignment"],
        "--state",
        cfg["dispatch_state"],
        "--max-workers",
        str(min(MAX_WORKERS_CAP, max(1, int(cfg.get("dispatch_workers", 1))))),
    ]
    if continuation is not None:
        command.append("--coding-worker")
    finite_frontier = cfg.get("finite_frontier")
    if finite_frontier is not None:
        from closer_frontier_policy import request_window, validate_frontier

        try:
            finite_frontier = validate_frontier(finite_frontier)
            request_timeout = cfg.get("request_timeout_seconds")
            request_window(time.time(), request_timeout)
        except (TypeError, ValueError) as exc:
            raise CampaignError(f"invalid finite frontier dispatcher binding: {exc}") from exc
        if cfg.get("execution_policy") != "finite-frontier/v2":
            raise CampaignError("finite frontier dispatcher requires its explicit execution policy")
        command.extend(
            [
                "--finite-frontier-json",
                json.dumps(finite_frontier, sort_keys=True, separators=(",", ":")),
                "--request-timeout-seconds",
                str(request_timeout),
            ]
        )
    else:
        command.extend(
            [
                "--budget-seconds",
                str(
                    int(
                        cfg.get("dispatch_budget_seconds", cfg.get("budget_seconds", 600))
                        if cfg.get("mechanism") == "native-handoff/v1"
                        and cfg.get("native_admission") is not None
                        else cfg.get("budget_seconds", 600)
                    )
                ),
            ]
        )
    workspace_pin = cfg.get("workspaces")
    if workspace_pin is not None:
        if not isinstance(workspace_pin, dict) or set(workspace_pin) != {"path", "sha256"}:
            raise ValueError("workspace config requires an exact path/SHA256 pin")
        import hashlib

        from closer_native_observer import _bounded_bytes

        path = Path(workspace_pin["path"]).expanduser().absolute()
        if (
            path.is_symlink()
            or path.resolve() != path
            or not path.is_file()
            or not _DIGEST.fullmatch(str(workspace_pin["sha256"]))
            or hashlib.sha256(_bounded_bytes(path, 1024 * 1024)).hexdigest()
            != workspace_pin["sha256"]
        ):
            raise ValueError("workspace config differs from admitted path/SHA256 pin")
        command.extend(["--workspaces", str(path), "--workspaces-sha256", workspace_pin["sha256"]])
    mode = cfg.get("coordinator_mode", "legacy")
    if mode not in {"legacy", "resource_scoped"}:
        raise ValueError("unsupported pinned coordinator mode")
    if mode == "resource_scoped":
        import closer_pipeline

        pin = cfg.get("coordinator_contract_pin")
        if json.dumps(pin, sort_keys=True, allow_nan=False) != json.dumps(
            closer_pipeline.scoped_capability(), sort_keys=True, allow_nan=False
        ):
            raise ValueError("scoped native dispatch requires the exact current coordinator pin")
        command.extend(
            [
                "--coordinator-mode",
                mode,
                "--coordinator-contract-pin",
                json.dumps(pin, sort_keys=True, allow_nan=False),
            ]
        )
    if cfg.get("apply"):
        command.extend(["--apply", "--workers", "--pipeline"])
        # New launch paths always opt in; absent binding holds instead of legacy fallback.
        command.extend(["--runtime-manifest", str(cfg.get("runtime_manifest") or "")])
    return command


def run_dispatcher(
    command: list[str], *, timeout: float, apply: bool, runner=subprocess.run
) -> tuple[str, str]:
    """Bounded call. A timeout or signal death of a possibly-writing run is `unknown`:
    it is never reported as a safe retry."""
    try:
        done = runner(command, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return "unknown", f"dispatcher timed out after {timeout}s; written effects unknown"
    except PermissionError as exc:
        return "denied", str(exc)
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        return "unavailable", f"dispatcher not started: {exc}"
    code = done.returncode
    if code == 0:
        return "ok", "dispatcher exit 0"
    if code < 0:
        return "unknown", f"dispatcher killed by signal {-code}"
    if code == 77:
        return "denied", "dispatcher refused: permission/policy"
    return ("unknown" if apply else "unavailable"), f"dispatcher exit {code}"


def write_result(
    path: str | Path,
    worker_id: str,
    target: str,
    outcome: str,
    detail: str,
    *,
    mechanism: str | None = None,
    native_admission: dict[str, Any] | None = None,
    create_only: bool = False,
) -> None:
    record = {
        "worker_id": worker_id,
        "target": target,
        "outcome": outcome,
        "detail": detail,
        "finished": _now(),
    }
    if mechanism is not None:
        record["mechanism"] = mechanism
    if native_admission is not None:
        record["native_admission"] = native_admission
    _atomic_write(Path(path), record, create_only=create_only)


def _existing_worker_result(path: Path, worker_id: str, target: str) -> str | None:
    """Return an existing result without following links or trusting unstable bytes.

    None means absent. Every unsafe or contradictory existing result is an UNKNOWN
    barrier, including a dangling symlink; callers must never repair it in place.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return "unknown" if path.is_symlink() else None
    except OSError:
        return "unknown"
    try:
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024:
                return "unknown"
            raw = stream.read(1024 * 1024 + 1)
            after = os.fstat(stream.fileno())
            current = path.stat(follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (
            len(raw) != before.st_size
            or len(raw) > 1024 * 1024
            or not stat.S_ISREG(current.st_mode)
            or any(getattr(before, k) != getattr(after, k) for k in fields)
            or any(getattr(after, k) != getattr(current, k) for k in fields)
        ):
            return "unknown"

        def unique_object(pairs):
            record = {}
            for key, value in pairs:
                if key in record:
                    raise ValueError("duplicate worker-result field")
                record[key] = value
            return record

        record = json.loads(raw, object_pairs_hook=unique_object)
        if (
            not isinstance(record, dict)
            or record.get("worker_id") != worker_id
            or record.get("target") != target
            or record.get("outcome") not in WORKER_OUTCOMES
        ):
            return "unknown"
        return record["outcome"]
    except (OSError, ValueError, TypeError, RecursionError):
        return "unknown"


def delegate_target(
    cfg: dict[str, Any], target: str, agent: str, profile_id: str, *, delegate=None
) -> tuple[str, str]:
    """Per-target repair through the existing `dispatcher.delegate`, which makes the exact
    real-target claim itself. A foreign owner of that claim is a barrier, not a race."""
    from closer_runtime_binding import policy_for_mechanism as policy_problem

    reason = policy_problem(
        cfg.get("policy") or {}, mechanism=cfg.get("mechanism", "desktop-scheduler/v1")
    )
    if reason:
        return "unavailable", reason
    if cfg.get("mechanism") == "cli-offload/v1":
        try:
            pin = load_manifest(cfg["runtime_manifest"])
            contract = pin["mechanism_contract"]
            if cfg.get("policy") != pin.get("policy") or policy_problem(
                pin.get("policy") or {}, mechanism="cli-offload/v1"
            ):
                return (
                    "unavailable",
                    "CLI delegate configured policy differs from fresh pinned policy",
                )
            if (
                contract.get("mechanism") != "cli-offload/v1"
                or (agent, profile_id) != (contract["agent"], contract["profile_id"])
                or qualify(pin, Path(cfg["runtime_manifest"]).parent)
            ):
                return (
                    "unavailable",
                    "CLI delegate profile/mechanism is not independently qualified",
                )
        except (KeyError, OSError, ValueError, RuntimeError):
            return "unavailable", "CLI delegate manifest is unavailable"
    if cfg.get("mechanism") == "native-handoff/v1":
        try:
            pin = load_manifest(cfg["runtime_manifest"])
            if cfg.get("policy") != pin.get("policy") or qualify(
                pin, Path(cfg["runtime_manifest"]).parent
            ):
                return "unavailable", "native delegate pinned qualification/policy differs"
        except (KeyError, OSError, ValueError, RuntimeError):
            return "unavailable", "native delegate manifest is unavailable"
    spec = cfg.get("delegate") or {}
    if not TARGET.fullmatch(target) or not spec.get("lane") or not spec.get("prompt"):
        return "denied", "per-target granularity needs a real target and a delegate lane+prompt"
    owner = claims.holder(target)
    if owner:
        return "unavailable", f"real target claim held by {owner.get('agent')}"
    if delegate is None:
        import dispatcher

        delegate = dispatcher.delegate
    try:
        out = delegate(
            agent,
            target,
            spec["lane"],
            spec["prompt"],
            profile_id=profile_id,
            task_type=spec.get("task_type", "implement"),
        )
    except Exception as exc:  # noqa: BLE001
        return classify_exception(exc), f"delegate raised {type(exc).__name__}: {exc}"
    if not isinstance(out, dict) or out.get("error") or not out.get("pid"):
        return "unavailable", f"delegate refused: {(out or {}).get('error')}"
    return "ok", f"handoff only: pid={out['pid']} log={out.get('log')}; outcome is the dispatcher's"


def run_worker(
    state: str,
    result: str,
    target: str,
    owner: str,
    generation: int,
    *,
    runner=subprocess.run,
    python: str = sys.executable,
    module_dir: Path | None = None,
    holder=claims.holder,
    agent: str | None = None,
    profile_id: str | None = None,
    route_fn=None,
    delegate=None,
    native_probe: Callable[[dict[str, Any]], str | None] = native_barrier,
    runtime_manifest: str | None = None,
    mechanism: str | None = None,
) -> str:
    """One bounded work unit. Fences BEFORE any effect, then writes exactly one durable result.

    `round`: the unit is the whole closer assignment (target must be ROUND_UNIT).
    `target-delegate`: the unit is one real target, handed to `dispatcher.delegate` on the
    provider the router currently selects. Any other granularity is refused."""
    worker_id = Path(result).stem
    result_mechanism = None
    worker_admission = None
    module_dir = module_dir or Path(__file__).resolve().parent

    def finish(outcome, detail):
        if (
            outcome in {"ok", "scoped_continuation"}
            and result_mechanism == "native-handoff/v1"
            and worker_admission is not None
            and worker_admission.get("coding_continuation") is None
        ):
            payload = None
            try:
                payload = json.loads(detail)
                reference, complete = _seal_completed_cohort(
                    Path(state), cfg, payload, worker_id, outcome
                )
                payload["cohort_terminal"] = reference
                if complete:
                    payload["cohort_completion"] = reference
                detail = json.dumps(payload, sort_keys=True)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
                if isinstance(payload, dict):
                    payload["cohort_completion_unavailable"] = f"{type(exc).__name__}: {exc}"
                    detail = json.dumps(payload, sort_keys=True)
                # Original outcome is preserved; absence never grants retirement.
        try:
            write_result(
                result,
                worker_id,
                target,
                outcome,
                detail,
                mechanism=result_mechanism,
                native_admission=worker_admission,
                create_only=True,
            )
        except FileExistsError:
            return _existing_worker_result(Path(result), worker_id, target) or "unknown"
        return outcome

    existing = _existing_worker_result(Path(result), worker_id, target)
    if existing is not None:
        return existing

    try:
        doc = Campaign(state).read()
    except CampaignError as exc:  # includes IncompatibleState: refuse before writing anything
        return finish("unavailable", f"fenced before effects: {exc}")
    mine = next((w for w in (doc or {}).get("workers", []) if w["worker_id"] == worker_id), None)
    private_coding = None
    if (
        mine is None
        and doc is not None
        and runtime_manifest
        and any(
            (row.get("worker") or {}).get("worker_id") == worker_id
            for row in (doc.get("coding_continuations") or {}).values()
        )
    ):
        from closer_native_observer import coding_worker_registration

        try:
            private_coding = coding_worker_registration(
                doc,
                state=state,
                result=result,
                target=target,
                owner=owner,
                generation=generation,
                manifest_path=runtime_manifest,
            )
            mine = private_coding
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            return finish("unavailable", "private coding worker source registration held")
    claim_target = (
        private_coding["claim_target"]
        if private_coding
        else (f"{doc['campaign_id']}:{target}" if doc else None)
    )
    claim_holder = holder(claim_target) if claim_target else None
    if (
        doc is None
        or mine is None
        or mine["state"] in {"terminal", "denied"}
        or doc["owner"] != owner
        or doc.get("generation", 1) != generation
        or doc.get("hold")
        or not claim_holder
        or claim_holder.get("agent") != AGENT
    ):
        return finish("unavailable", "fenced before effects: stale owner, worker or claim")
    mode = granularity(doc)
    if mode not in GRANULARITIES:
        return finish("denied", f"unsupported work-unit granularity {mode!r}")
    if (mode == "round") != (target == ROUND_UNIT) or target not in doc["targets"]:
        return finish("denied", f"target {target!r} is not a {mode} work unit")
    from closer_runtime_binding import policy_for_mechanism as policy_problem

    configured = doc.get("worker_config") or {}
    worker_policy = configured.get("policy") or {}
    actual_mechanism = configured.get("mechanism", "desktop-scheduler/v1")
    if mechanism is not None and not runtime_manifest:
        return finish("unavailable", "explicit worker mechanism requires pinned manifest")
    if runtime_manifest:
        try:
            pinned_worker = load_manifest(runtime_manifest)
            from closer_mechanisms import mechanism as selected_mechanism

            actual_mechanism = selected_mechanism(pinned_worker, mechanism)
            result_mechanism = actual_mechanism
            if (doc.get("worker_config") or {}).get(
                "mechanism", "desktop-scheduler/v1"
            ) != actual_mechanism:
                return finish("unavailable", "worker mechanism differs from campaign")
            if actual_mechanism in {"cli-offload/v1", "native-handoff/v1"} and configured.get(
                "policy"
            ) != pinned_worker.get("policy"):
                return finish(
                    "unavailable", "worker configured policy differs from pinned host policy"
                )
            if actual_mechanism == "native-handoff/v1":
                from closer_native_observer import accept_worker

                worker_admission = accept_worker(
                    pinned_worker,
                    state=state,
                    target=target,
                    owner=owner,
                    generation=generation,
                    result=result,
                    manifest_path=runtime_manifest,
                    campaign_id=doc["campaign_id"],
                )
            if actual_mechanism in {"cli-offload/v1", "native-handoff/v1"} and qualify(
                pinned_worker, Path(runtime_manifest).parent
            ):
                return finish("unavailable", "worker execution mechanism is not qualified")
            worker_policy = pinned_worker.get("policy") or {}
        except (OSError, ValueError, RuntimeError):
            return finish("unavailable", "pinned worker policy unreadable")
    if actual_mechanism != "desktop-scheduler/v1" and not runtime_manifest:
        return finish("unavailable", "explicit worker mechanism requires pinned manifest")
    policy = policy_problem(worker_policy, mechanism=actual_mechanism)
    if policy:
        return finish("unavailable", policy)
    barrier = native_probe(
        {**doc, "targets": {target: doc["targets"][target]}} if mode == "target-delegate" else doc
    )
    if barrier:
        return finish("unavailable", f"fenced before effects: {barrier}")
    cfg = dict(doc.get("worker_config") or {})
    if runtime_manifest:
        cfg["runtime_manifest"] = runtime_manifest
    if worker_admission is not None:
        from closer_native_observer import admission_config

        try:
            cfg = admission_config(pinned_worker, cfg, worker_admission)
            cfg["native_worker"] = {
                "state": str(Path(state).resolve()),
                "target": target,
                "owner": owner,
                "generation": str(generation),
                "result": str(Path(result).resolve()),
                "runtime-manifest": str(Path(cast(str, runtime_manifest)).resolve()),
                "mechanism": actual_mechanism,
            }
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            return finish("unavailable", f"native original admission held: {exc}")
    if mode == "target-delegate":
        route = (route_fn or (lambda: select_route(cfg.get("capacity_json"))))()
        if agent is None or profile_id is None:
            return finish("unavailable", "selected provider/profile missing before effects")
        if not route.get("agent") or (agent, profile_id) != (route["agent"], route["profile_id"]):
            return finish("unavailable", f"fenced before effects: route is {route!r}")
        outcome, detail = delegate_target(cfg, target, agent, profile_id, delegate=delegate)
        return finish(outcome, detail)
    timeout = float(cfg.get("worker_timeout", 120))
    command = dispatch_command(cfg, python, module_dir)
    if cfg.get("apply"):
        from closer_runtime_binding import run_native_round

        outcome, detail = run_native_round(command, cfg, runner=runner)
    else:
        outcome, detail = run_dispatcher(command, timeout=timeout, apply=False, runner=runner)
    return finish(outcome, detail)


# --------------------------------------------------------- release resume


def release_fingerprint(record: dict[str, Any], verifier_digest: str) -> str:
    receipt = record.get("verification") or {}
    return sha256_json(
        [
            record["candidate_sha"],
            record["incumbent_token"],
            receipt.get("source_identity"),
            verifier_digest,
        ]
    )


def resume_release(
    record_path: str | Path,
    campaign: Campaign,
    *,
    expected: dict[str, Any],
    verifier_digest: str,
    launcher: Callable[[list[str], Path], int] = default_launcher,
    release_command: Callable[[str], list[str]],
    pid_alive=claims._pid_alive,
) -> dict[str, Any]:
    """Execute (by detached worker) exactly what `release_plan` allows, after an exact CAS.

    `expected` = {candidate_sha, revision, snapshot_digest?, generation?}: any mismatch
    refuses. Never reverifies past a sealed receipt, never reinstalls past `installing`,
    never reruns an unchanged verifier, never starts a second run of an active attempt."""
    import closer_release

    record = closer_release.ReleaseRecord(record_path).read()
    if record is None:
        return {"action": "none", "reason": "no release record"}
    receipt = record.get("verification") or {}
    installed = record.get("installed_generation") or None
    for key, actual in (
        ("candidate_sha", record["candidate_sha"]),
        ("revision", record["revision"]),
        ("snapshot_digest", receipt.get("snapshot_digest")),
        ("generation", installed),
    ):
        if key in expected and expected[key] != actual:
            return {"action": "refused", "reason": f"{key} changed since planning"}
    plan = release_plan(record)
    if plan["action"] not in {"resume_verify", "resume_install", "resume_rollback"}:
        return plan  # await_acceptance / terminal / hold: nothing to execute
    fingerprint = release_fingerprint(record, verifier_digest)
    attempt = (campaign.read() or {}).get("release_attempt")
    if attempt and attempt.get("revision") == record["revision"]:
        result = result_path(campaign.path, attempt["attempt_id"])
        if not result.is_file():
            if attempt.get("pid") and pid_alive(attempt["pid"]):
                return {"action": "wait", "reason": "same attempt running", **_pick(attempt)}
            return {
                "action": "reconcile",
                "reason": "attempt died without result",
                **_pick(attempt),
            }
        if attempt.get("fingerprint") == fingerprint:
            return {"action": "skip_unchanged", "reason": "same verifier input already ran"}
    attempt_id = uuid.uuid4().hex[:12]
    new = {
        "attempt_id": attempt_id,
        "revision": record["revision"],
        "fingerprint": fingerprint,
        "plan": plan["action"],
        "pid": None,
        "started": _now(),
    }
    campaign.update(lambda d: d.__setitem__("release_attempt", new))  # intent before process
    log = campaign.path.with_name(campaign.path.name + f".{attempt_id}.log")
    pid = launcher(
        release_command(plan["action"]) + ["--result", str(result_path(campaign.path, attempt_id))],
        log,
    )
    campaign.update(lambda d: d["release_attempt"].__setitem__("pid", pid))
    return {"action": "launched_release", "plan": plan["action"], "attempt_id": attempt_id}


def _pick(attempt: dict[str, Any]) -> dict[str, Any]:
    return {"attempt_id": attempt.get("attempt_id"), "pid": attempt.get("pid")}


def run_release_worker(config: dict[str, Any], result: str, *, release_factory=None) -> str:
    """Detached worker: one `verify_and_install` call; an exception is `unknown`, because
    the installer may have swapped the mirror before dying."""
    import closer_release

    factory = release_factory or closer_release.ReleaseRecord
    attempt_id = Path(result).stem
    try:
        factory(config["record"]).verify_and_install(**config["args"])
    except closer_release.ReleaseConflict as exc:
        write_result(result, attempt_id, "release", "unavailable", f"CAS refused: {exc}")
        return "unavailable"
    except Exception as exc:  # noqa: BLE001
        write_result(result, attempt_id, "release", "unknown", f"{type(exc).__name__}: {exc}")
        return "unknown"
    write_result(result, attempt_id, "release", "ok", "verify_and_install returned")
    return "ok"


# ------------------------------------------------------ manifest & pin


RUNTIME_VERSION = "1"
LAUNCHER_VERSION = "1"
PINNED_MODULES = (
    "closer_campaign.py",
    "closer_runtime_binding.py",
    "closer_mechanisms.py",
    "closer_operational_capacity.py",
    "closer_native_lease.py",
    "closer_launcher.py",
    "closer_release.py",
    "claims.py",
    "closer_dispatcher.py",
    # Subprocess entrypoints are not Python imports. Root the existing terminal
    # guard and runtime-AC UI helper so their imports are sealed by module_closure.
    "merge_guard.py",
    "frontend_verify.py",
)


def module_closure(base: Path, roots: tuple[str, ...]) -> tuple[str, ...]:
    """Closed set of repository-owned Python dependencies, including lazy imports.

    Dynamic provider imports remain a capability boundary; routing's source owner
    must add any nonliteral module to the roots. No host files are bundled.
    """
    import ast

    pending, found = list(roots), set()
    while pending:
        name = pending.pop()
        if name in found:
            continue
        if Path(name).name != name or not name.endswith(".py"):
            raise CampaignError("runtime module path must be a local Python basename")
        path = base / name
        tree = ast.parse(path.read_bytes(), filename=str(path))
        found.add(name)
        for node in ast.walk(tree):
            imports = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports = [node.module]
            for dependency in imports:
                if "." not in dependency and (base / (dependency + ".py")).is_file():
                    pending.append(dependency + ".py")
    return tuple(sorted(found))


def public_descriptor(manifest: dict[str, Any]) -> dict[str, Any]:
    """Only own source pins and compatible public Core pin; host binding stays private."""
    core = manifest.get("portable") or {}
    descriptor = {
        "schema_version": 1,
        "owner": "Orchestrator",
        "package_kind": "closer-campaign",
        "runtime_version": RUNTIME_VERSION,
        "launcher_version": LAUNCHER_VERSION,
        "contract": CONTRACT,
        "candidate_sha": manifest["candidate_sha"],
        "modules": dict(manifest["modules"]),
        "state_versions": manifest["state_versions"],
        "core": {
            "owner": "Workflows",
            "contract": CONTRACT,
            "package_digest": core.get("package_digest"),
        },
    }
    descriptor["package_digest"] = sha256_json(descriptor)
    return descriptor


def make_manifest(
    module_dir: str | Path,
    *,
    candidate_sha: str,
    state_versions: dict[str, int],
    python: str = sys.executable,
    portable: dict[str, str] | None = None,
    modules: tuple[str, ...] = PINNED_MODULES,
    policy: dict[str, Any] | None = None,
    campaign_root: str | Path | None = None,
    manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    """Bind installed module digests, runtime, command and state versions to a candidate."""
    module_dir = Path(module_dir).expanduser().resolve()
    modules = module_closure(module_dir, modules)
    digests = {name: sha256_file(module_dir / name) for name in modules}
    runtime = {
        "python": str(python),
        "version": subprocess.run(
            [python, "-c", "import sys;print(sys.version)"], capture_output=True, text=True
        ).stdout.strip(),
    }
    state_root = Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator"))
    root = (
        Path(
            campaign_root
            or os.environ.get("ORCH_CLOSER_CAMPAIGN_DIR")
            or state_root / "closer-campaigns"
        )
        .expanduser()
        .resolve()
    )
    pinned_manifest = Path(manifest_path or root / "pin.manifest.json").expanduser().resolve()
    command = [
        str(python),
        str(module_dir / "closer_campaign.py"),
        "tick",
        "--root",
        str(root),
        "--manifest",
        str(pinned_manifest),
    ]
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "contract": CONTRACT,
        "candidate_sha": candidate_sha,
        "module_dir": str(module_dir),
        "modules": digests,
        "module_path": str(module_dir / "closer_campaign.py"),
        "runtime": runtime,
        "runtime_digest": sha256_json(runtime),
        "command": command,
        "command_digest": sha256_json(command),
        "state_versions": dict(state_versions),
        "state_digest": sha256_json(state_versions),
        "portable": portable,
        "policy": dict(policy or {}),
        "created_at": _now(),
    }
    launcher = (
        {
            "version": LAUNCHER_VERSION,
            "path": str(module_dir / "closer_launcher.py"),
            "sha256": digests["closer_launcher.py"],
            "compatible_runtime": RUNTIME_VERSION,
            "compatible_contract": CONTRACT,
        }
        if "closer_launcher.py" in digests
        else None
    )
    manifest["launcher"] = launcher
    # Versioned distribution: the Orchestrator owns its runtime and launcher code; the Core
    # is owned and versioned by Workflows and only pinned here.
    manifest["distribution"] = {
        "schema": 1,
        "orchestrator": {
            "owner": "Orchestrator",
            "runtime_version": RUNTIME_VERSION,
            "modules": dict(digests),
        },
        "launcher": launcher,
        "core": {"owner": "Workflows", "contract": CONTRACT, "pinned": portable},
    }
    manifest["package_digest"] = sha256_json(
        [
            candidate_sha,
            sorted(digests.items()),
            manifest["runtime_digest"],
            manifest["state_digest"],
            portable,
        ]
    )
    return manifest


def write_manifest(path: str | Path, manifest: dict[str, Any]) -> None:
    problems = validate_manifest(manifest)
    if problems:
        raise CampaignError("; ".join(problems))
    output = Path(path).expanduser().resolve()
    command = manifest.get("command", [])
    if not isinstance(command, list) or command.count("--manifest") != 1:
        raise CampaignError("manifest command must bind its output path")
    index = command.index("--manifest")
    if (
        index + 1 >= len(command)
        or not isinstance(command[index + 1], str)
        or Path(command[index + 1]).expanduser().resolve() != output
    ):
        raise CampaignError("manifest output differs from its pinned command path")
    _atomic_write(output, manifest)


def verify_modules(manifest: dict[str, Any]) -> list[str]:
    """Recompute every pinned module digest from disk."""
    problems = []
    for field in ("runtime", "command", "state_versions"):
        digest_field = "state_digest" if field == "state_versions" else field + "_digest"
        if manifest.get(digest_field) != sha256_json(manifest.get(field)):
            problems.append(f"{field} differs from its pinned digest")
    if not manifest.get("public_runtime_descriptor"):
        expected = sha256_json(
            [
                manifest.get("candidate_sha"),
                sorted((manifest.get("modules") or {}).items()),
                manifest.get("runtime_digest"),
                manifest.get("state_digest"),
                manifest.get("portable"),
            ]
        )
        if manifest.get("package_digest") != expected:
            problems.append("package digest differs from source/Core/runtime/state pins")
    base = Path(manifest.get("module_dir", ""))
    for name, digest in (manifest.get("modules") or {}).items():
        try:
            if sha256_file(base / name) != digest:
                problems.append(f"{name}: digest differs from manifest")
        except OSError:
            problems.append(f"{name}: unreadable")
    if not manifest.get("modules"):
        problems.append("manifest pins no modules")
    public_name = manifest.get("public_runtime_descriptor")
    if public_name:
        try:
            from closer_distribution_package import (
                check_core_compatibility,
                validate_public_runtime_descriptor,
            )

            if Path(public_name).name != public_name:
                raise ValueError("public descriptor path escapes package")
            public = json.loads((base / public_name).read_text())
            problems.extend(validate_public_runtime_descriptor(public))
            actual_code = {
                str(path.relative_to(base))
                for path in base.rglob("*")
                if path.is_file() and path.suffix in {".py", ".pyc", ".pyo"}
            }
            if actual_code != set(manifest.get("modules") or {}):
                problems.append("Runtime tree contains undeclared executable code")
            if public.get("modules") != manifest.get("modules") or public.get(
                "package_digest"
            ) != manifest.get("package_digest"):
                problems.append("host Runtime binding differs from public package")
            if public.get("state_versions") != manifest.get("state_versions"):
                problems.append("host state differs from public Runtime state")
            pin = manifest.get("portable") or {}
            descriptor = json.loads(
                (Path(pin["package_root"]) / "closer-core-package.json").read_text()
            )
            problems.extend(check_core_compatibility(public, descriptor))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            problems.append(f"public package binding unreadable: {exc}")
    if Path(manifest.get("module_path", "")).resolve() != (base / "closer_campaign.py").resolve():
        problems.append("runtime entrypoint is not pinned closer_campaign.py")
    for name in manifest.get("modules") or {}:
        if Path(name).name != name:
            problems.append("runtime module path escapes closed package")
    return problems


def select_pin(
    manifests: list[dict[str, Any]],
    campaign_doc: dict[str, Any] | None,
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    """A running campaign stays on its pin; otherwise the newest manifest whose modules
    verify AND which holds a bound harness receipt. Nothing qualifying => no pin."""
    by_digest = {m.get("package_digest"): m for m in manifests}
    if campaign_doc and campaign_doc["phase"] != "complete":
        pinned = by_digest.get(campaign_doc["release_pin"])
        if pinned is None:
            return {"pin": None, "reason": "running campaign's pinned manifest is missing"}
        return {"pin": pinned["package_digest"], "reason": "running campaign keeps its pin"}
    ranked = sorted(manifests, key=lambda m: m.get("created_at", 0), reverse=True)
    for manifest in ranked:
        if validate_manifest(manifest) or verify_modules(manifest):
            continue
        for receipt in receipts:
            if receipt.get("package_digest") == manifest["package_digest"] and not (
                check_harness_receipt(manifest, receipt)
            ):
                return {"pin": manifest["package_digest"], "reason": "newest compatible"}
    return {"pin": None, "reason": "no manifest has verified modules and a bound receipt"}


# ----------------------------------------- runner compatibility producer


def _process_identity(proc: subprocess.Popen) -> dict[str, Any]:
    """Identity as the PARENT saw it: its own Popen handle (pid, argv digest) plus `ps`
    start time when the sandbox allows it. The child's self-report is never the source."""
    identity = {
        "pid": proc.pid,
        "parent_pid": os.getpid(),
        "argv_digest": sha256_json(proc.args),
        "observed": f"popen pid={proc.pid} ppid={os.getpid()}",
        "source": "popen",
    }
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=,command=", "-p", str(proc.pid)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    if out:
        identity.update(observed=out, source="ps")
    return identity


def _wait_for(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        time.sleep(0.02)
    return False


def harness_child(directory: str, role: str, manifest_path: str) -> int:
    """The test process. `first` starts, awaits handoff, journals it, then waits to be
    killed; `restart` resumes from the durable journal and records the result."""
    d = Path(directory)
    manifest = json.loads(Path(manifest_path).read_text())
    if role == "first":
        _atomic_write(
            d / "ready.json",
            {"pid": os.getpid(), "package_digest": manifest["package_digest"]},
        )
        if not _wait_for(d / "handoff.json", 20):
            return 3
        handoff = json.loads((d / "handoff.json").read_text())
        _atomic_write(d / "journal.json", {"token": handoff["token"], "acked_by": os.getpid()})
        time.sleep(60)  # the parent kills this process; reaching here is a failure
        return 4
    journal = json.loads((d / "journal.json").read_text())
    _atomic_write(
        d / "result.json",
        {
            "token": journal["token"],
            "resumed_from_pid": journal["acked_by"],
            "pid": os.getpid(),
            "package_digest": manifest["package_digest"],
            "state_digest": manifest["state_digest"],
        },
    )
    return 0


def produce_harness_receipt(
    manifest: dict[str, Any],
    manifest_path: str | Path,
    workdir: str | Path,
    *,
    runner_id: str = "harness",
    timeout: float = 20,
    spawn_argv: Callable[[str], list[str]] | None = None,
    env: dict[str, str] | None = None,
    kind: str = "harness-subprocess",
) -> dict[str, Any]:
    """Run startup, handoff, forced crash of OWN child, restart and durable result as real
    subprocesses of the pinned module, and observe them. This proves the lifecycle on this
    machine only; it is NOT the desktop agent, so it never satisfies `check_promotion`."""
    problems = validate_manifest(manifest) + verify_modules(manifest)
    if problems:
        raise CampaignError("; ".join(problems))
    d = Path(workdir)
    d.mkdir(parents=True, exist_ok=True)
    for stale in ("ready.json", "handoff.json", "journal.json", "result.json"):
        (d / stale).unlink(missing_ok=True)
    py, module = manifest["runtime"]["python"], manifest["module_path"]

    def default_argv(role):
        return [
            py,
            module,
            "harness-child",
            "--dir",
            str(d),
            "--role",
            role,
            "--manifest",
            str(manifest_path),
        ]

    def spawn(role):
        return subprocess.Popen(
            (spawn_argv or default_argv)(role),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    phases: dict[str, dict[str, Any]] = {}
    processes: list[dict[str, Any]] = []
    first = spawn("first")
    try:
        ok = _wait_for(d / "ready.json", timeout)
        ready = json.loads((d / "ready.json").read_text()) if ok else {}
        ok = ok and ready.get("pid") == first.pid
        processes.append({"role": "first", **_process_identity(first)})
        phases["startup"] = _phase(ok, d / "ready.json")
        token = uuid.uuid4().hex
        _atomic_write(d / "handoff.json", {"token": token, "parent_pid": os.getpid()})
        ok = ok and _wait_for(d / "journal.json", timeout)
        journal = json.loads((d / "journal.json").read_text()) if ok else {}
        ok = ok and journal.get("token") == token and journal.get("acked_by") == first.pid
        phases["handoff"] = _phase(ok, d / "journal.json")
    finally:
        first.kill()  # forced crash of our own test process
        crash_code = first.wait(timeout=timeout)
    processes[0]["exit_code"] = crash_code
    crashed = crash_code == -9 and phases["handoff"]["passed"]
    second = spawn("restart")
    try:
        restart_code = second.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        second.kill()
        restart_code = second.wait()
    processes.append({"role": "restart", **_process_identity(second), "exit_code": restart_code})
    resumed = False
    if (d / "result.json").is_file():
        result = json.loads((d / "result.json").read_text())
        resumed = (
            restart_code == 0
            and result.get("token") == token
            and result.get("resumed_from_pid") == first.pid
            and result.get("pid") == second.pid != first.pid
            and result.get("package_digest") == manifest["package_digest"]
            and result.get("state_digest") == manifest["state_digest"]
        )
    phases["crash_restart"] = _phase(crashed and resumed, d / "journal.json")
    phases["result_recording"] = _phase(resumed, d / "result.json")
    return {
        "kind": kind,
        "runner_id": runner_id,
        "package_digest": manifest["package_digest"],
        "candidate_sha": manifest["candidate_sha"],
        "command_digest": manifest["command_digest"],
        "runtime_digest": manifest["runtime_digest"],
        "state_digest": manifest["state_digest"],
        "state_versions": manifest["state_versions"],
        "phases": phases,
        "processes": processes,
        "desktop_runtime_validated": False,
        "produced_at": _now(),
    }


DESKTOP_ADAPTER_LIMITATION = (
    "No desktop-app control interface is available to this runtime: the producer starts the "
    "configured launcher ENTRYPOINT (argv) and observes its OS process; it cannot attest that "
    "the desktop application itself scheduled, ran or restarted it. Promotion stays held."
)


def produce_launcher_receipt(
    manifest: dict[str, Any],
    manifest_path: str | Path,
    launcher_config: dict[str, Any],
    workdir: str | Path,
    *,
    timeout: float = 20,
) -> list[dict[str, Any]]:
    """Invoke each ENABLED configured launcher entrypoint itself (never an agent's echo) in
    bounded compatibility mode: scratch dir only, no repository path, no credentials in the
    environment. The parent independently observes startup, handoff, SIGKILL of its own
    child, restart and the durable result. Result kind is `launcher-entrypoint`, never
    `desktop-runner`: only an adapter that can drive the real app may mint that."""
    pinned = manifest.get("launcher") or {}
    receipts = []
    for entry in launcher_config.get("launchers", []):
        if not entry.get("enabled"):
            continue
        argv = list(entry["argv"])
        if not pinned or str(manifest.get("launcher", {}).get("path")) not in argv:
            raise CampaignError(f"{entry['runner_id']}: argv does not run the pinned launcher")
        d = Path(workdir) / entry["runner_id"]
        d.mkdir(parents=True, exist_ok=True)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(d), "ORCH_CLOSER_COMPAT": "1"}

        def launcher_argv(role: str) -> list[str]:
            return argv + [
                "compat-child",
                "--dir",
                str(d),
                "--role",
                role,
                "--manifest",
                str(manifest_path),
            ]

        receipt = produce_harness_receipt(
            manifest,
            manifest_path,
            d,
            runner_id=entry["runner_id"],
            timeout=timeout,
            spawn_argv=launcher_argv,
            env=env,
            kind="launcher-entrypoint",
        )
        receipt.update(
            launcher={"argv_digest": sha256_json(argv), "interface": entry.get("interface")},
            launcher_version=pinned.get("version"),
            adapter_limitation=DESKTOP_ADAPTER_LIMITATION,
            compat_mode={"repository_mutation": False, "credentials": "none", "scratch": str(d)},
        )
        receipts.append(receipt)
    return receipts


def _phase(passed: bool, evidence: Path) -> dict[str, Any]:
    ref = {"path": str(evidence), "sha256": sha256_file(evidence)} if evidence.is_file() else None
    return {"passed": bool(passed and ref), "evidence": ref}


def check_harness_receipt(manifest: dict[str, Any], receipt: dict[str, Any]) -> list[str]:
    """Independent recheck of a harness receipt: bindings, observed processes, evidence files.
    A receipt that merely claims `passed` fails: evidence must hash-match on disk."""
    problems = []
    if receipt.get("kind") not in {"harness-subprocess", "launcher-entrypoint"}:
        return ["not a lifecycle (harness-subprocess or launcher-entrypoint) receipt"]
    for key in (
        "package_digest",
        "candidate_sha",
        "command_digest",
        "runtime_digest",
        "state_digest",
        "state_versions",
    ):
        if receipt.get(key) != manifest.get(key):
            problems.append(f"{key} differs from manifest")
    procs = receipt.get("processes") or []
    if len(procs) != 2 or len({p.get("pid") for p in procs}) != 2:
        problems.append("expected two distinct observed processes")
    else:
        if procs[0].get("exit_code") != -9 or not procs[0].get("observed"):
            problems.append("first process was not observed killed (-9)")
        if procs[1].get("exit_code") != 0:
            problems.append("restart process did not exit 0")
    for phase in RUNNER_PHASES:
        entry = (receipt.get("phases") or {}).get(phase) or {}
        ref = entry.get("evidence")
        try:
            hashed = isinstance(ref, dict) and sha256_file(ref["path"]) == ref["sha256"]
        except (OSError, KeyError, TypeError):
            hashed = False
        if entry.get("passed") is not True or not hashed:
            problems.append(f"phase {phase} lacks hash-matching evidence")
    return problems


# --------------------------------------------------------- scheduled tick


def load_manifest(path: str | Path) -> dict[str, Any]:
    manifest = json.loads(Path(path).read_text())
    problems = validate_manifest(manifest) + verify_modules(manifest)
    if problems:
        raise CampaignError("; ".join(problems))
    from closer_mechanisms import mechanism

    try:
        mechanism(manifest)
    except RuntimeError as exc:
        raise CampaignError(str(exc)) from exc
    return manifest


def _current_qualification_problems(
    manifest: dict[str, Any], root: str | Path, *, _scope=None
) -> list[str]:
    """Fresh state predicates only; empty is NOT complete package qualification."""
    problems: list[str] = []
    from closer_mechanisms import CLI, DESKTOP, MechanismError, mechanism, qualification_problems

    try:
        selected = mechanism(manifest) if _scope is None else mechanism(manifest, _scope=_scope)
    except MechanismError as exc:
        return problems + [f"execution mechanism remains held: {exc}"]
    if selected == CLI:
        problems += qualification_problems(manifest)
    elif selected == "native-handoff/v1":
        from closer_native_observer import qualification_problems as native_qualification_problems

        problems += (
            native_qualification_problems(manifest)
            if _scope is None
            else native_qualification_problems(manifest, _scope=_scope)
        )
    elif selected != DESKTOP:
        problems.append("unknown execution mechanism remains held")
    else:
        try:
            promotion = _load(str(Path(root) / "promotion.json"))
            verdict = check_promotion(
                manifest,
                list(promotion.get("runners") or []),
                list(promotion.get("receipts") or []),
            )
            problems += verdict["problems"]
        except (OSError, ValueError, AttributeError) as exc:
            problems.append(f"promotion receipts unreadable: {exc}")
    recorded = (manifest.get("state_versions") or {}).get("campaign")
    if not isinstance(recorded, int) or recorded != STATE_SCHEMA:
        problems.append(f"state version {recorded!r} is not the runner's schema {STATE_SCHEMA}")
    try:
        from closer_runtime_binding import dependency_problems

        problems.extend(dependency_problems())
    except (KeyError, TypeError, OSError, CampaignError, AttributeError) as exc:
        problems.append(f"pinned core unusable: {exc}")
    return problems


def qualify(manifest: dict[str, Any], root: str | Path) -> list[str]:
    """Why this pinned version may NOT take effects yet; empty means qualified.

    Needs (1) valid manifest + verified modules, (2) `<root>/promotion.json`
    (`{"runners": [...], "receipts": [...]}`) that `check_promotion` accepts, i.e. real
    desktop-runner receipts for exactly this package and state versions, (3) a runner that
    understands the recorded state schema, and (4) the pinned Core loadable as an adapter.
    Any failure holds; nothing falls back to another action path."""
    problems = validate_manifest(manifest) + verify_modules(manifest)
    problems.extend(_current_qualification_problems(manifest, root))
    pin = manifest.get("portable") or {}
    try:
        from closer_runtime_binding import CoreBundle

        CoreBundle(pin)
    except (KeyError, TypeError, OSError, CampaignError, AttributeError) as exc:
        problems.append(f"pinned core unusable: {exc}")
    return problems


def qualify_communication(manifest: dict[str, Any], root: str | Path) -> list[str]:
    """Static native package/receipt checks for escalation communication ONLY.

    This does not qualify a native entry, worker, effect, or hold rearm. Those
    callers retain qualify(), including its unknown/current-entry barrier.
    """
    problems = validate_manifest(manifest) + verify_modules(manifest)
    from closer_mechanisms import NATIVE, MechanismError, mechanism
    from closer_native_observer import qualification_receipt_problems

    try:
        if mechanism(manifest) != NATIVE:
            return problems + ["communication qualification requires native mechanism"]
    except MechanismError as exc:
        return problems + [f"communication mechanism remains held: {exc}"]
    problems += qualification_receipt_problems(manifest)
    recorded = (manifest.get("state_versions") or {}).get("campaign")
    if not isinstance(recorded, int) or recorded != STATE_SCHEMA:
        problems.append(f"state version {recorded!r} is not the runner's schema {STATE_SCHEMA}")
    pin = manifest.get("portable") or {}
    try:
        from closer_runtime_binding import CoreBundle, dependency_problems

        problems.extend(dependency_problems())
        CoreBundle(pin)
    except (KeyError, TypeError, OSError, CampaignError, AttributeError) as exc:
        problems.append(f"pinned core unusable: {exc}")
    return problems


def tick(
    root: str | Path,
    manifest_path: str | Path,
    *,
    capacity_json: str | None = None,
    route_fn: Callable[[str | None], dict[str, Any]] = select_route,
    max_steps: int = 2,
    launcher: Callable[[list[str], Path], int] = default_launcher,
    pid_alive=claims._pid_alive,
    qualifier: Callable[[dict[str, Any], str | Path], list[str]] = qualify,
    native_probe: Callable[[dict[str, Any]], str | None] = native_barrier,
    mechanism: str | None = None,
) -> list[dict[str, Any]]:
    """Discover every `*.campaign.json` under `root` and advance each by at most
    `max_steps`. Needs only the files: a second process after worker death resumes here.
    Reconcile always runs; launches need a fresh cached capacity report (fail closed)."""
    try:
        manifest = load_manifest(manifest_path)
        from closer_mechanisms import mechanism as selected_mechanism

        execution_mechanism = selected_mechanism(manifest, mechanism)
    except (OSError, ValueError, CampaignError, RuntimeError) as exc:
        return [{"kind": "skipped", "reason": f"pin unusable: {exc}"}]
    from closer_runtime_binding import policy_for_mechanism as policy_problem

    if execution_mechanism == "native-handoff/v1":
        from closer_native_observer import entry_active

        if not entry_active(manifest):
            return [{"kind": "held", "reason": "native tick requires observed wrapper entry"}]
    policy = policy_problem(manifest.get("policy") or {}, mechanism=execution_mechanism)
    if policy:
        return [{"kind": "held", "reason": policy}]
    unqualified = qualifier(manifest, root)
    if unqualified:  # before ANY effect, reconcile included: an unqualified pin is inert
        return [{"kind": "held", "reason": "version not qualified", "problems": unqualified}]
    py, module = manifest["runtime"]["python"], manifest["module_path"]
    if execution_mechanism == "native-handoff/v1":
        from closer_native_observer import contract as native_contract

        py = str(native_contract(manifest)[2])
    report: list[dict[str, Any]] = []
    for path in sorted(Path(root).glob("*.campaign.json")):
        campaign = Campaign(path)
        try:
            doc = campaign.read()
        except IncompatibleState as exc:
            report.append({"campaign": path.name, "kind": "refused", "reason": str(exc)})
            continue
        except CampaignError as exc:
            report.append({"campaign": path.name, "kind": "skipped", "reason": str(exc)})
            continue
        owner = promotion_owner(doc, manifest["package_digest"])
        if doc is None or doc["phase"] == "complete" or not owner["may_write"]:
            report.append({"campaign": path.name, "kind": "skipped", "reason": "not ours to write"})
            continue
        cfg = doc.get("worker_config") or {}
        if cfg.get("mechanism", "desktop-scheduler/v1") != execution_mechanism:
            report.append(
                {
                    "campaign": path.name,
                    "kind": "held",
                    "reason": "campaign mechanism differs from pin",
                }
            )
            continue
        if execution_mechanism in {"cli-offload/v1", "native-handoff/v1"} and cfg.get(
            "policy"
        ) != manifest.get("policy"):
            report.append(
                {
                    "campaign": path.name,
                    "kind": "held",
                    "reason": "campaign policy differs from pinned host policy",
                }
            )
            continue
        admission = None
        if execution_mechanism == "native-handoff/v1":
            import closer_hold_followup
            from closer_native_observer import rearm_on_hold_followup

            try:
                doc = reconcile(campaign, pid_alive=pid_alive)
                receipts = closer_hold_followup.validated_receipts(campaign, doc, manifest)
                doc, followups = closer_hold_followup.consume(
                    campaign, manifest, str(manifest_path)
                )
                report.extend({"campaign": path.name, **entry} for entry in followups)
                doc = rearm_on_hold_followup(campaign, doc, manifest)
                if receipts or doc.get("hold_followups"):
                    try:
                        from closer_stage_feedback import read_stage_events, summarize_efficiency

                        stamp = _now()
                        held = list((doc.get("hold_followups") or {}).values())
                        preliminary = summarize_efficiency(receipts, holds=held, now=stamp)
                        observed = read_stage_events(preliminary["model_runs"]["bindings"])
                        summary = summarize_efficiency(
                            receipts, stage_events=observed["events"], holds=held, now=stamp
                        )
                        summary["stage_event_collection"] = {
                            key: value for key, value in observed.items() if key != "events"
                        }
                        report.append(
                            {
                                "campaign": path.name,
                                "kind": "hold_followup_summary",
                                "efficiency": summary,
                            }
                        )
                    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                        report.append(
                            {
                                "campaign": path.name,
                                "kind": "efficiency_unavailable",
                                "reason": type(exc).__name__,
                                "authority": False,
                            }
                        )
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                report.append(
                    {
                        "campaign": path.name,
                        "kind": "held",
                        "reason": f"native hold follow-up: {exc}",
                    }
                )
                continue
        if execution_mechanism == "native-handoff/v1":
            from closer_native_observer import (
                capacity_contract,
                publish_admission,
                rearm_on_assignment_change,
            )

            if capacity_contract(manifest, control_plane_only=True) is not None:
                # Fold only actual terminal results first. Unknown or live workers
                # never receive a fresh report, assignment or execution window.
                doc = reconcile(campaign, pid_alive=pid_alive)
                try:
                    if doc.get("demand_admission") is None:
                        doc = rearm_on_assignment_change(campaign, doc, manifest)
                except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                    report.append(
                        {
                            "campaign": path.name,
                            "kind": "held",
                            "reason": f"native standing admission held: {exc}",
                        }
                    )
                    continue
                pending = next_action(doc, capacity_ok=True)
                if pending.get("kind") != "launch":
                    from closer_native_observer import run_coding_if_selected

                    coding = run_coding_if_selected(campaign, doc, manifest, manifest_path, pending)
                    if coding is not None:
                        report.append({"campaign": path.name, **coding})
                        continue
                    maintenance = None
                    from closer_native_observer import correction_cleanup_enabled

                    if correction_cleanup_enabled(doc.get("worker_config") or {}):
                        from closer_correction_maintenance import run_if_selected

                        maintenance = run_if_selected(
                            campaign, doc, manifest, manifest_path, pending
                        )
                    report.append(
                        {"campaign": path.name, **(pending if maintenance is None else maintenance)}
                    )
                    continue
                try:
                    if capacity_json is not None:
                        raise CampaignError(
                            "native publication cannot use a caller report override"
                        )
                    admission = publish_admission(manifest, manifest_path, path, doc)
                except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                    report.append(
                        {
                            "campaign": path.name,
                            "kind": "held",
                            "reason": f"native admission held: {exc}",
                        }
                    )
                    continue
        capacity = (
            admission["report"]["path"]
            if admission is not None
            else capacity_json or cfg.get("capacity_json")
        )
        route = route_fn(capacity)
        capacity_ok = bool(route.get("agent"))
        if admission is not None and granularity(doc) == "round":
            from closer_native_observer import validate_admission

            try:
                validate_admission(manifest, admission, for_effects=True)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                report.append(
                    {
                        "campaign": path.name,
                        "kind": "held",
                        "reason": f"native control-plane admission held: {exc}",
                    }
                )
                continue
            # A round launches the deterministic coordinator, not this model.
            # Private reasoning still needs its own qualified role and capacity.
            capacity_ok = True
        if admission is not None and route.get("agent"):
            import execution_profiles

            selected_id = route.get("profile_id")
            selected_profile = (
                execution_profiles.get_profile(selected_id)
                if isinstance(selected_id, str)
                else None
            )
            if (
                route.get("profile_id") not in capacity_contract(manifest)["profile_ids"]
                or not selected_profile
                or selected_profile.get("agent") != route.get("agent")
            ):
                report.append(
                    {
                        "campaign": path.name,
                        "kind": "held",
                        "reason": "native capacity route outside pinned scope",
                    }
                )
                continue
        if (
            execution_mechanism == "cli-offload/v1"
            and capacity_ok
            and (route.get("agent"), route.get("profile_id"))
            != (
                manifest["mechanism_contract"]["agent"],
                manifest["mechanism_contract"]["profile_id"],
            )
        ):
            report.append(
                {
                    "campaign": path.name,
                    "kind": "held",
                    "reason": "capacity route differs from qualified CLI profile",
                }
            )
            continue
        generation = doc.get("generation", 1)

        def command(target, _doc=doc, _gen=generation, _state=str(path), _route=route):
            extra = (
                ["--agent", _route["agent"], "--profile-id", _route["profile_id"]]
                if granularity(_doc) == "target-delegate" and capacity_ok
                else []
            )
            return [
                py,
                module,
                "worker",
                "--state",
                _state,
                "--target",
                target,
                "--owner",
                _doc["owner"],
                "--generation",
                str(_gen),
                "--runtime-manifest",
                str(manifest_path),
                "--mechanism",
                execution_mechanism,
                *extra,
            ]

        def observed_launcher(argv, log_path):
            if execution_mechanism == "native-handoff/v1":
                from closer_native_observer import authorize_worker

                authorize_worker(manifest, argv)
            return launcher(argv, log_path)

        for _ in range(max_steps):
            if doc is None:
                report.append(
                    {"campaign": path.name, "kind": "held", "reason": "campaign disappeared"}
                )
                break
            release = None
            if doc.get("release_record"):
                import closer_release

                release = closer_release.ReleaseRecord(doc["release_record"]).read()
            action = step(
                campaign,
                worker_command=command,
                launcher=observed_launcher,
                capacity_ok=capacity_ok,
                release=release,
                pid_alive=pid_alive,
                owner_generation=generation,
                native_probe=native_probe,
            )
            if action["kind"] == "release_resume" and release is not None:
                action = _tick_release(campaign, doc, release, py, module, launcher, pid_alive)
            report.append({"campaign": path.name, **action})
            if action["kind"] not in {"launched"}:
                break
            doc = campaign.read()
    return report


def _tick_release(campaign, doc, release, py, module, launcher, pid_alive) -> dict[str, Any]:
    cfg = (doc.get("worker_config") or {}).get("release")
    if not cfg:
        return {"kind": "release_resume", "reason": "no release config recorded", "executed": False}
    config_path = Path(cfg["config_file"])
    outcome = resume_release(
        doc["release_record"],
        campaign,
        expected={"candidate_sha": release["candidate_sha"], "revision": release["revision"]},
        verifier_digest=cfg["verifier_digest"],
        launcher=launcher,
        release_command=lambda plan: [py, module, "release-worker", "--config", str(config_path)],
        pid_alive=pid_alive,
    )
    return {
        "kind": "release_resume",
        "executed": outcome["action"] == "launched_release",
        **outcome,
    }


def _demand_read(path):
    from closer_demand_admission import MAX_ASSIGNMENT_BYTES
    from closer_native_observer import _bounded_bytes

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CampaignError("duplicate demand JSON key")
            result[key] = value
        return result

    raw = _bounded_bytes(path, MAX_ASSIGNMENT_BYTES)
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise CampaignError("completed discovery document must be an object")
    return value, raw


def _controller_demand_context(argv_root=None):
    if os.environ.get("ORCH_CLOSER_CAMPAIGN_TICK", "1") == "0":
        return None
    state = Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator"))
    root = Path(
        argv_root or os.environ.get("ORCH_CLOSER_CAMPAIGN_DIR") or state / "closer-campaigns"
    )
    path = root / "pin.manifest.json"
    if not path.is_file():
        return None
    manifest = load_manifest(path)
    contract = (manifest.get("mechanism_contract") or {}).get("controller_demand")
    if contract is None:
        return None
    required = {
        "schema",
        "controller_root",
        "campaign_state",
        "campaign_id",
        "owner",
        "publisher_sha256",
    }
    if (
        not isinstance(contract, dict)
        or not required <= contract.keys()
        or set(contract) - required - {"worker_config"}
        or contract.get("schema") != "closer-controller-demand/v1"
    ):
        raise CampaignError("unsupported pinned controller demand contract")
    from closer_mechanisms import NATIVE, mechanism
    from closer_native_observer import contract as native_contract

    if mechanism(manifest) != NATIVE:
        raise CampaignError("controller demand requires the existing native wrapper")
    native_root = native_contract(manifest)[0]["campaign_root"]
    campaign_path = Path(contract["campaign_state"])
    controller_root = Path(contract["controller_root"])
    if (
        root.resolve() != root
        or str(root) != native_root
        or path.resolve() != path
        or campaign_path.resolve() != campaign_path
        or campaign_path.parent != root
        or not campaign_path.name.endswith(".campaign.json")
        or controller_root.resolve() != controller_root
        or not controller_root.is_dir()
        or not all(
            isinstance(contract.get(key), str) and contract[key] for key in ("campaign_id", "owner")
        )
    ):
        raise CampaignError("pinned demand publisher/campaign path differs from canonical state")
    publisher = controller_root / "demand_router.py"
    if (
        not isinstance(contract.get("publisher_sha256"), str)
        or not _DIGEST.fullmatch(contract["publisher_sha256"])
        or publisher.resolve() != publisher
        or sha256_file(publisher) != contract["publisher_sha256"]
    ):
        raise CampaignError("pinned demand publisher source differs")
    return {
        "root": root,
        "publisher": publisher,
        "manifest_path": path,
        "manifest": manifest,
        "manifest_sha256": sha256_file(path),
        "contract": contract,
    }


def controller_demand_before_discovery(argv_root=None):
    """Capture publisher identity before main; never claim its exit code proves discovery."""
    context = _controller_demand_context(argv_root)
    if context is None:
        return None
    path = Path(context["contract"]["controller_root"]) / "demand-plan.json"
    lock = path.parent / ".demand.lock"
    with lock.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        context["previous_plan_sha256"] = (
            sha256_json(_demand_read(path)[0]) if path.is_file() else None
        )
    return context


def controller_demand_after_discovery(context, *, launcher=default_launcher, now=None):
    """Bridge completed discovery to the canonical campaign, then the normal wrapper.

    The publisher lock covers generation and immutable snapshot preparation plus
    Campaign's existing CAS. It is released before invoking any process. Replay
    never submits a second wrapper; uncertain submission is retained for its owner.
    """
    import copy

    import closer_native_observer as native
    from closer_demand_admission import admit_in_transaction

    current = _now() if now is None else now
    pinned = _controller_demand_context(str(context["root"]))
    if pinned is None or pinned["manifest_sha256"] != context["manifest_sha256"]:
        raise CampaignError("demand manifest changed after discovery began")
    manifest, contract = pinned["manifest"], pinned["contract"]
    controller_root = Path(contract["controller_root"])
    manifest_path = pinned["manifest_path"]
    native_value, wrapper, _python, observer_root = native.contract(manifest)
    qualification_path = observer_root / "native-qualification.json"
    qualification_sha = sha256_file(qualification_path)
    problems = qualify(manifest, pinned["root"])
    if problems:
        return {"status": "held", "reason": "version not qualified", "problems": problems}
    if (
        sha256_file(qualification_path) != qualification_sha
        or sha256_file(manifest_path) != pinned["manifest_sha256"]
    ):
        raise CampaignError("demand qualification changed during verification")
    package = {
        "package_digest": manifest["package_digest"],
        "manifest_sha256": pinned["manifest_sha256"],
        "qualification_digest": qualification_sha,
    }
    state_path = controller_root / "demand-state.json"
    campaign = Campaign(contract["campaign_state"])
    outcome = {}
    with (controller_root / ".demand.lock").open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        plan, plan_raw = _demand_read(controller_root / "demand-plan.json")
        state, _state_raw = _demand_read(state_path)
        assignment, _assignment_raw = _demand_read(controller_root / "closer-assignment.json")
        import hashlib

        plan_sha = sha256_json(plan)
        if plan_sha == context["previous_plan_sha256"]:
            return {"status": "inert", "reason": "no completed discovery in this invocation"}
        generated = plan.get("generated_at")
        if (
            plan.get("errors") != []
            or assignment.get("errors") != []
            or generated is None
            or generated != state.get("last_complete_at")
            or generated != assignment.get("generated_at")
            or plan.get("ownership_complete") is not True
            or (plan.get("routes") or {}).get("closer") != assignment.get("targets")
        ):
            return {"status": "refused", "reason": "discovery/assignment/state completion differs"}
        doc = campaign.read()
        if doc is not None and (
            doc["campaign_id"] != contract["campaign_id"] or doc["owner"] != contract["owner"]
        ):
            return {"status": "refused", "reason": "canonical campaign belongs to another owner"}
        cfg = copy.deepcopy((doc or {}).get("worker_config") or contract.get("worker_config") or {})
        if (
            cfg.get("mechanism") != "native-handoff/v1"
            or cfg.get("policy") != manifest["policy"]
            or cfg.get("granularity", "round") != "round"
            or cfg.get("apply") is not True
            or cfg.get("coordinator_mode") != "resource_scoped"
        ):
            return {"status": "refused", "reason": "canonical native worker configuration required"}
        if cfg.get("runtime_manifest") not in (None, str(manifest_path)):
            return {
                "status": "refused",
                "reason": "worker manifest differs from pinned demand package",
            }
        cfg["runtime_manifest"] = str(manifest_path)
        try:
            dispatch_command(cfg, manifest["runtime"]["python"], Path(manifest["module_dir"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {
                "status": "refused",
                "reason": "pinned demand worker config invalid: " + str(exc),
            }
        owned_workers = (
            _owned_cohort_workers(campaign.path, doc, manifest) if doc is not None else []
        )
        previous = state.get("closer_campaign_demand") or {}
        source = {key: value for key, value in assignment.items() if key != "generation"}
        source_sha = sha256_json(source)
        same_source = (
            previous.get("generated_at") == generated
            and previous.get("source_sha256") == source_sha
        )
        if doc is not None and doc.get("demand_admission") and not doc.get("demand_wake"):
            # The same-commit marker proves the original bridge has not crossed
            # its durable submit boundary, even if discovery replaced its cache.
            prepared = doc.get("demand_prepared")
            original = doc["demand_admission"]
            if (
                not isinstance(prepared, dict)
                or prepared
                != {
                    "schema": "closer-demand-prepared/v1",
                    "status": "prepared",
                    "request_id": original.get("request_id"),
                    "snapshot": doc.get("demand_assignment"),
                }
                or (previous.get("wake") or {}).get("status")
                in {"submitted", "unknown", "started", "denied"}
                or owned_workers
                or doc.get("hold_followups")
                or not original["not_before"] <= current < original["deadline"]
            ):
                return {"status": "held", "reason": "original unsubmitted demand remains owned"}
            retained = native._bounded_bytes(Path(prepared["snapshot"]["path"]), 2 * 1024 * 1024)
            frozen = json.loads(retained)
            if hashlib.sha256(retained).hexdigest() != original["assignment_sha256"]:
                raise CampaignError("prepared demand snapshot changed")
            current_assignment = {**assignment, "generation": original["assignment_generation"]}
            current_raw = json.dumps(
                current_assignment, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
            trial_request = {
                **original,
                "schema": "closer-demand-request/v1",
                "expected_revision": doc["revision"],
                "assignment_sha256": hashlib.sha256(current_raw).hexdigest(),
            }
            for field in ("request_id", "accepted_at", "admission_count", "record_digest"):
                trial_request.pop(field, None)
            trial = copy.deepcopy(doc)
            trial.pop("demand_admission")
            tested = admit_in_transaction(
                trial,
                assignment_bytes=current_raw,
                request=trial_request,
                qualified_package=package,
                now=current,
            )
            if tested["status"] != "admitted":
                return {
                    "status": "held",
                    "reason": "fresh discovery cannot recover prepared demand: " + tested["reason"],
                }

            def material(value):
                projected = {
                    key: copy.deepcopy(item)
                    for key, item in value.items()
                    if key not in {"generation", "generated_at"}
                }
                for row in projected.get("targets", []):
                    if isinstance(row, dict):
                        row.pop("due", None)
                        row.pop("not_before", None)
                return projected

            if material(assignment) != material(frozen):
                return {
                    "status": "held",
                    "reason": "fresh demand differs from original prepared scope",
                }
            request = {
                key: value
                for key, value in original.items()
                if key not in {"request_id", "accepted_at", "admission_count", "record_digest"}
            }
            request["schema"] = "closer-demand-request/v1"
            previous = {
                "generation": original["assignment_generation"],
                "generated_at": frozen["generated_at"],
                "source_sha256": sha256_json(
                    {key: value for key, value in frozen.items() if key != "generation"}
                ),
                "request": request,
                "snapshot": dict(prepared["snapshot"]),
                "wake": {"status": "prepared", "request_id": original["request_id"]},
            }
            same_source = True  # Exact original request, independently validated fresh scope.
        owned_wake = (doc or {}).get("demand_wake") or previous.get("wake") or {}
        if not same_source and owned_wake.get("status") in {
            "submitted",
            "unknown",
            "started",
            "denied",
        }:
            # Campaign worker results are the existing effect owner. In their
            # absence an uncertain wrapper wake cannot be replaced by discovery.
            if owned_wake.get("status") == "denied":
                return {"status": "held", "reason": "existing wrapper submission was denied"}
            previous_workers = set(owned_wake.get("prior_worker_ids") or [])
            known_workers = [
                worker
                for worker in (doc or {}).get("workers") or []
                if worker.get("worker_id") not in previous_workers
            ]
            if not known_workers or any(
                w.get("state") != "terminal"
                or w.get("outcome") not in {"ok", "unavailable", "scoped_continuation"}
                for w in known_workers
            ):
                return {
                    "status": "held",
                    "reason": "existing wrapper/campaign outcome remains owned",
                }
        if same_source:
            request = previous["request"]
            raw = native._bounded_bytes(Path(previous["snapshot"]["path"]), 2 * 1024 * 1024)
        else:
            generations = [
                previous.get("generation", 0),
                (doc or {}).get("demand_admission", {}).get("assignment_generation", 0),
            ]
            if any(type(value) is not int or value < 0 for value in generations):
                raise CampaignError("demand generation accounting malformed")
            generation = max(generations) + 1
            assignment["generation"] = generation
            raw = (
                json.dumps(assignment, sort_keys=True, separators=(",", ":"), allow_nan=False)
                + "\n"
            ).encode()
            targets = [
                row.get("target") if isinstance(row, dict) else row
                for row in assignment.get("targets", [])
            ]
            if not targets or any(
                not isinstance(target, str) or not TARGET.fullmatch(target) for target in targets
            ):
                return {"status": "refused", "reason": "no exact current demand targets"}
            request = {
                "schema": "closer-demand-request/v1",
                "campaign_id": contract["campaign_id"],
                "owner": contract["owner"],
                "campaign_generation": (doc or {}).get("generation", 1),
                "expected_revision": (doc or {}).get("revision", 1),
                "assignment_generation": generation,
                "assignment_sha256": hashlib.sha256(raw).hexdigest(),
                **package,
                "not_before": current,
                "deadline": current + 120,
                "targets": targets,
            }
        retirement = None
        from closer_demand_admission import _hash as demand_hash

        if (
            doc is not None
            and (owned_workers or doc.get("hold_followups"))
            and demand_hash(request) != (doc.get("demand_admission") or {}).get("request_id")
        ):
            try:
                retirement = _retirement_evidence(campaign.path, doc, manifest)
                if same_source:
                    if request["campaign_generation"] != doc["generation"] + 1:
                        raise CampaignError("prepared retirement request generation differs")
                else:
                    request["campaign_generation"] = doc["generation"] + 1
                cfg = _next_cohort_config(doc, request["targets"], request)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                return {"status": "held", "reason": str(exc)}
        prospective = (
            copy.deepcopy(doc)
            if doc is not None
            else {
                "campaign_id": contract["campaign_id"],
                "owner": contract["owner"],
                "generation": 1,
                "revision": 1,
                "release_pin": manifest["package_digest"],
                "phase": "work",
                "hold": None,
                "workers": [],
            }
        )
        if retirement is not None:
            prospective.update(
                generation=request["campaign_generation"],
                targets={ROUND_UNIT: "pending"},
                assignment_targets=list(request["targets"]),
                worker_config=copy.deepcopy(cfg),
                hold_followups={},
                hold_escalations={},
            )
        checked = admit_in_transaction(
            prospective,
            assignment_bytes=raw,
            request=request,
            qualified_package=package,
            now=current,
        )
        if checked["status"] == "refused":
            return checked
        if (
            checked["status"] == "admitted"
            and doc is not None
            and retirement is None
            and (owned_workers or doc.get("hold_followups"))
        ):
            return {"status": "held", "reason": "existing campaign work remains owned"}
        snapshot = _store_demand_assignment(campaign.path, raw, checked["record"])
        if checked["status"] == "admitted":
            _atomic_write(controller_root / "closer-assignment.json", json.loads(raw))
            state["closer_campaign_demand"] = {
                "generation": request["assignment_generation"],
                "generated_at": generated,
                "source_sha256": source_sha,
                "request": request,
                "snapshot": snapshot,
                "wake": {"status": "prepared", "request_id": checked["record"]["request_id"]},
            }
            _atomic_write(state_path, state)  # prepared before CAS; no process submitted yet
        from contextlib import nullcontext

        with _retirement_locks(manifest) if retirement is not None else nullcontext():
            doc = campaign.begin(
                contract["campaign_id"],
                request["targets"],
                owner=contract["owner"],
                release_pin=manifest["package_digest"],
                worker_config=cfg,
                demand_assignment_bytes=raw,
                demand_request=request,
                qualified_package=package,
                demand_now=current,
                _demand_outcome=outcome,
                _retirement=retirement,
            )
        recovering = (
            outcome["status"] == "replay"
            and same_source
            and previous.get("wake")
            == {"status": "prepared", "request_id": outcome["record"]["request_id"]}
            and not doc.get("demand_wake")
            and not _owned_cohort_workers(campaign.path, doc, manifest)
            and not doc.get("hold_followups")
            and doc.get("demand_assignment") == snapshot
            and outcome["record"]["not_before"] <= current < outcome["record"]["deadline"]
        )
        if outcome["status"] != "admitted" and not recovering:
            return outcome
        wake = {
            "schema": "closer-demand-wake/v1",
            "status": "submitted",
            "request_id": outcome["record"]["request_id"],
            "prior_worker_ids": [worker["worker_id"] for worker in doc.get("workers", [])],
        }

        def record_wake(current_doc):
            if (
                current_doc.get("demand_admission") != doc.get("demand_admission")
                or current_doc.get("generation") != doc.get("generation")
                or current_doc.get("demand_assignment") != snapshot
                or current_doc.get("demand_wake")
                or current_doc.get("demand_prepared") != doc.get("demand_prepared")
                or (current_doc.get("demand_prepared") or {}).get("status") != "prepared"
                or current_doc.get("phase") != "work"
                or current_doc.get("hold")
                or current_doc.get("release_attempt") is not None
                or current_doc.get("campaign_id") != contract["campaign_id"]
                or current_doc.get("owner") != contract["owner"]
                or current_doc.get("release_pin") != manifest["package_digest"]
                or current_doc.get("assignment_targets") != request["targets"]
                or current_doc.get("worker_config") != doc.get("worker_config")
                or _owned_cohort_workers(campaign.path, current_doc, manifest)
                or current_doc.get("hold_followups")
                or not outcome["record"]["not_before"]
                <= max(current, _now())
                < outcome["record"]["deadline"]
            ):
                raise CampaignError("prepared demand wake changed before submission intent")
            current_doc["demand_prepared"]["status"] = "submitted"
            current_doc["demand_wake"] = wake

        campaign.update(record_wake)
        state["closer_campaign_demand"] = (
            {**previous, "wake": dict(wake)}
            if recovering
            else {**state["closer_campaign_demand"], "wake": dict(wake)}
        )
        _atomic_write(state_path, state)  # durable intent before the process boundary
    # The normal pinned wrapper establishes its own observed entry, supervisor
    # lock and canonical ACTION A/C lease. No in-process capability is invented.
    try:
        pid = launcher(
            ["/bin/bash", str(wrapper), "closer"],
            campaign.path.with_name(campaign.path.name + ".demand-wrapper.log"),
        )
    except BaseException as exc:
        with (controller_root / ".demand.lock").open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            state, _raw = _demand_read(state_path)
            wake = state["closer_campaign_demand"]["wake"]
            wake.update(
                status="denied" if isinstance(exc, PermissionError) else "unknown",
                reason=type(exc).__name__,
            )
            campaign.update(lambda current_doc: current_doc.__setitem__("demand_wake", dict(wake)))
            _atomic_write(state_path, state)
        raise
    with (controller_root / ".demand.lock").open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        state, _raw = _demand_read(state_path)
        state["closer_campaign_demand"]["wake"].update(status="started", pid=pid)
        campaign.update(
            lambda current_doc: current_doc.__setitem__(
                "demand_wake", dict(state["closer_campaign_demand"]["wake"])
            )
        )
        _atomic_write(state_path, state)
    return {
        "status": "admitted",
        "request_id": outcome["record"]["request_id"],
        "pid": pid,
        "campaign": str(campaign.path),
        "assignment": snapshot,
    }


_LAST_CONTROLLER_DEMAND_REPORT = None


def _controller_escalations(root, manifest_path, manifest):
    """Communication only: an exact pinned route, with no native capability."""
    from closer_hold_escalation import service
    from closer_runtime_binding import policy_for_mechanism

    name = manifest.get("hold_escalation_campaign_path")
    if name is None:
        return []
    problem = policy_for_mechanism(manifest.get("policy") or {}, mechanism="native-handoff/v1")
    if problem:
        return [{"kind": "held", "reason": problem}]
    unqualified = qualify_communication(manifest, root)
    if unqualified:
        return [{"kind": "held", "reason": "version not qualified", "problems": unqualified}]
    if not isinstance(name, str):
        raise CampaignError("explicit escalation campaign path is malformed")
    path = Path(name)
    if (
        not path.is_absolute()
        or path.resolve() != path
        or path.parent != root
        or not path.name.endswith(".campaign.json")
    ):
        raise CampaignError("explicit escalation campaign escapes pinned root")
    campaign = Campaign(path)
    doc = campaign.read()
    if doc is None:
        return [{"kind": "held", "reason": "explicit escalation campaign is absent"}]
    cfg = doc.get("worker_config") or {}
    if (
        doc.get("release_pin") != manifest["package_digest"]
        or cfg.get("mechanism") != "native-handoff/v1"
        or cfg.get("policy") != manifest.get("policy")
    ):
        return [{"kind": "held", "reason": "explicit escalation campaign pin/policy differs"}]
    # service obtains a fresh wallclock at each write boundary. This path never
    # calls consume, rearm, tick, admission publication, or a worker launcher.
    _doc, reports = service(campaign, manifest, str(manifest_path))
    return [{"campaign": path.name, **row} for row in reports]


def tick_safe(argv_root: str | None = None) -> list[dict[str, Any]]:
    """Controller-side entry. Never raises and never blocks the controller's own work."""
    if os.environ.get("ORCH_CLOSER_CAMPAIGN_TICK", "1") == "0":
        return []
    state = Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator"))
    configured_root = argv_root or os.environ.get("ORCH_CLOSER_CAMPAIGN_DIR")
    root = Path(configured_root) if configured_root else state / "closer-campaigns"
    manifest = root / "pin.manifest.json"
    if not manifest.is_file():
        return []  # nothing pinned: inert
    try:
        pinned = load_manifest(manifest)
        from closer_mechanisms import NATIVE, mechanism
        from closer_native_observer import contract as native_contract
        from closer_native_observer import entry_active

        if mechanism(pinned) == NATIVE:
            observer_root = native_contract(pinned)[3]
            active = observer_root / "native-active.json"
            observation_reports = []
            try:
                observed_entry = (active.exists() or active.is_symlink()) and entry_active(pinned)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                observed_entry = False
                observation_reports.append(
                    {
                        "kind": "error",
                        "reason": f"native entry observation unavailable: {type(exc).__name__}: {exc}",
                    }
                )
            if not observed_entry:
                try:
                    observation_reports.extend(_controller_escalations(root, manifest, pinned))
                except Exception as exc:
                    observation_reports.append(
                        {
                            "kind": "error",
                            "reason": f"communication service held: {type(exc).__name__}: {exc}",
                        }
                    )
                return observation_reports + [
                    {"kind": "held", "reason": "native tick requires observed wrapper entry"}
                ]
        return tick(root, manifest)
    except Exception as exc:  # noqa: BLE001 - scheduled tick must not break the controller
        return [{"kind": "error", "reason": f"{type(exc).__name__}: {exc}"}]


# ----------------------------------------------------------------------- CLI


def _load(path: str) -> Any:
    return json.loads(Path(path).read_text())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    st = sub.add_parser("status", help="read-only: campaign phase, next action, release plan")
    st.add_argument("--state", required=True)
    st.add_argument("--release-record")
    st.add_argument("--capacity-json")
    pr = sub.add_parser("check-promotion", help="read-only: gate a manifest on runner receipts")
    pr.add_argument("--manifest", required=True)
    pr.add_argument("--receipts", required=True)
    pr.add_argument("--runner", action="append", required=True)
    tk = sub.add_parser("tick", help="discover and advance every campaign under --root")
    tk.add_argument("--root", required=True)
    tk.add_argument("--manifest")
    tk.add_argument("--capacity-json")
    tk.add_argument("--max-steps", type=int, default=2)
    wk = sub.add_parser("worker", help="bounded dispatcher worker (started by step/tick)")
    wk.add_argument("--state", required=True)
    wk.add_argument("--target", required=True)
    wk.add_argument("--owner", required=True)
    wk.add_argument("--generation", type=int, required=True)
    wk.add_argument("--result", required=True)
    wk.add_argument("--runtime-manifest")
    wk.add_argument("--mechanism")
    wk.add_argument("--agent")
    wk.add_argument("--profile-id")
    rw = sub.add_parser("release-worker", help="detached verify_and_install worker")
    rw.add_argument("--config", required=True)
    rw.add_argument("--result", required=True)
    mm = sub.add_parser("make-manifest", help="bind module/runtime/candidate/state digests")
    mm.add_argument("--module-dir", required=True)
    mm.add_argument("--candidate-sha", required=True)
    mm.add_argument("--state-version", action="append", required=True, help="name=int")
    mm.add_argument("--out", required=True)
    mm.add_argument("--root", help="campaign directory (defaults to configured campaign root)")
    hr = sub.add_parser("harness-receipt", help="produce a subprocess lifecycle receipt")
    hr.add_argument("--manifest", required=True)
    hr.add_argument("--workdir", required=True)
    hr.add_argument("--out", required=True)
    lr = sub.add_parser("launcher-receipt", help="observe configured launcher entrypoints")
    lr.add_argument("--manifest", required=True)
    lr.add_argument("--config", required=True)
    lr.add_argument("--workdir", required=True)
    lr.add_argument("--out", required=True)
    hc = sub.add_parser("harness-child")  # internal test process
    hc.add_argument("--dir", required=True)
    hc.add_argument("--role", required=True)
    hc.add_argument("--manifest", required=True)
    args = p.parse_args(argv)
    if args.command == "check-promotion":
        verdict = check_promotion(_load(args.manifest), args.runner, _load(args.receipts))
        print(json.dumps(verdict, sort_keys=True))
        return 0 if verdict["promotable"] else 78
    if args.command == "tick":
        manifest = args.manifest or str(Path(args.root) / "pin.manifest.json")
        report = tick(
            args.root, manifest, capacity_json=args.capacity_json, max_steps=args.max_steps
        )
        print(json.dumps(report, sort_keys=True))
        return 0
    if args.command == "worker":
        run_worker(
            args.state,
            args.result,
            args.target,
            args.owner,
            args.generation,
            agent=args.agent,
            profile_id=args.profile_id,
            runtime_manifest=args.runtime_manifest,
            mechanism=args.mechanism,
        )
        return 0
    if args.command == "release-worker":
        run_release_worker(_load(args.config), args.result)
        return 0
    if args.command == "make-manifest":
        versions = {k: int(v) for k, v in (x.split("=", 1) for x in args.state_version)}
        write_manifest(
            args.out,
            make_manifest(
                args.module_dir,
                candidate_sha=args.candidate_sha,
                state_versions=versions,
                campaign_root=args.root,
                manifest_path=args.out,
            ),
        )
        return 0
    if args.command == "harness-receipt":
        manifest = load_manifest(args.manifest)
        receipt = produce_harness_receipt(manifest, args.manifest, args.workdir)
        _atomic_write(Path(args.out), receipt)
        problems = check_harness_receipt(manifest, receipt)
        print(json.dumps({"problems": problems, "desktop_runtime_validated": False}))
        return 0 if not problems else 78
    if args.command == "launcher-receipt":
        manifest = load_manifest(args.manifest)
        receipts = produce_launcher_receipt(
            manifest, args.manifest, _load(args.config), args.workdir
        )
        _atomic_write(Path(args.out), {"receipts": receipts})
        problems = [x for r in receipts for x in check_harness_receipt(manifest, r)]
        print(json.dumps({"problems": problems, "desktop_runtime_validated": False}))
        return 0 if receipts and not problems else 78
    if args.command == "harness-child":
        return harness_child(args.dir, args.role, args.manifest)
    doc = Campaign(args.state).read()
    if doc is None:
        print(json.dumps({"error": "no campaign record"}))
        return 2
    release = None
    if args.release_record:
        import closer_release

        release = closer_release.ReleaseRecord(args.release_record).read()
    capacity_ok = (
        bool(select_route(args.capacity_json).get("agent")) if args.capacity_json else True
    )
    print(
        json.dumps(
            {
                "status": status_line(doc),
                "next_action": next_action(doc, release=release, capacity_ok=capacity_ok),
                "release": release_plan(release) if args.release_record else None,
            },
            sort_keys=True,
        )
    )
    return 0


def _pinned_demand_main(fallback, context):
    """Match loaded executable code to the exact verified publisher source."""
    import hashlib
    import types

    from closer_native_observer import _bounded_bytes

    code = getattr(fallback, "__code__", None)
    source = _bounded_bytes(context["publisher"], 2 * 1024 * 1024)
    if hashlib.sha256(source).hexdigest() != context["contract"]["publisher_sha256"]:
        return False
    compiled = compile(source, str(context["publisher"]), "exec", dont_inherit=True)
    mains = [
        value
        for value in compiled.co_consts
        if isinstance(value, types.CodeType) and value.co_name == "main"
    ]
    return (
        code is not None
        and Path(code.co_filename).resolve() == context["publisher"]
        and getattr(fallback, "__name__", None) == "main"
        and len(mains) == 1
        and code == mains[0]
    )


def controller_entry(argv: list[str] | None, fallback: Callable[[list[str] | None], int]) -> int:
    """Hook for the desktop controller's `main`. `closer-campaign ...` is ours; any other
    scheduled invocation first runs one bounded, never-raising tick, then the fallback."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["closer-campaign"]:
        return main(args[1:])
    global _LAST_CONTROLLER_DEMAND_REPORT
    try:
        demand_context = controller_demand_before_discovery()
        if demand_context is not None:
            if not _pinned_demand_main(fallback, demand_context):
                raise CampaignError("controller fallback is not the pinned demand publisher main")
    except Exception as exc:  # the controller's own discovery must still run
        demand_context = None
        _LAST_CONTROLLER_DEMAND_REPORT = {"status": "held", "reason": str(exc)}
    tick_safe()  # Existing-owned reconciliation remains separate from new demand.
    result = fallback(argv)
    if result == 0 and demand_context is not None:
        try:
            if not _pinned_demand_main(fallback, demand_context):
                raise CampaignError("controller publisher executable code changed during discovery")
            _LAST_CONTROLLER_DEMAND_REPORT = controller_demand_after_discovery(demand_context)
        except Exception as exc:
            _LAST_CONTROLLER_DEMAND_REPORT = {"status": "held", "reason": str(exc)}
    return result


if __name__ == "__main__":
    raise SystemExit(main())
