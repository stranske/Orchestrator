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
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol

# Published packages remain immutable, including their Python import artifacts.
if (Path(__file__).resolve().parent / "closer-runtime-package.json").is_file():
    sys.dont_write_bytecode = True

import claims  # noqa: E402

STATE_SCHEMA = 1
MANIFEST_SCHEMA = 1
CONTRACT = "closer-operation/v1"
MAX_WORKERS_CAP = 4
OUTCOMES = {"ok", "unavailable", "denied", "unknown"}
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
        stream = self.lock_path.open("a+b")
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        return stream

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
    ) -> dict[str, Any]:
        """Create the record, or resume the same campaign under the same owner.

        `worker_config` and `release_record` live IN the record so a later scheduled tick
        needs no memory of the process that began it."""
        if not 1 <= max_workers <= MAX_WORKERS_CAP:
            raise ValueError(f"max_workers must be 1..{MAX_WORKERS_CAP}")
        worker_config = dict(worker_config or {})
        mode = worker_config.setdefault("granularity", "round")
        if mode not in GRANULARITIES:
            raise ValueError(f"unsupported work-unit granularity {mode!r}")
        assignment_targets = list(targets)
        if mode == "round":  # one whole-round owner; the dispatcher gives its own parallelism
            targets = [ROUND_UNIT]
            max_workers = 1
        fresh = {
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
            if current is not None:
                if current["campaign_id"] != campaign_id or current["owner"] != owner:
                    raise CampaignError("campaign is owned by another id or runner")
                if current["release_pin"] != release_pin:
                    raise CampaignError(
                        "campaign is pinned to a different release; reconcile first"
                    )
                return current
            fresh["revision"] = 1
            fresh["updated_at"] = _now()
            self._write_unlocked(fresh)
            return fresh
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
                try:
                    result = json.loads(result_path(state_path, worker["worker_id"]).read_text())
                except ValueError:
                    result = {"detail": "result file unreadable"}
                if not isinstance(result, dict):
                    result = {}
                outcome = result.get("outcome")
                if outcome not in OUTCOMES:
                    outcome = "unknown"
                worker.update(state="terminal", outcome=outcome, detail=result.get("detail"))
                doc["targets"][worker["target"]] = {
                    "ok": "done",
                    "unknown": "reconcile",
                    "unavailable": "pending",
                    "denied": "denied",
                }[outcome]
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

    Same rules as the dispatcher's low-effort lane: fresh and complete report (else `hold`,
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
            and p["reasoning_effort"] in {"none", "minimal", "low"}
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
    learned = (
        router.learned_ranks().get("closer_interpret") if hasattr(router, "learned_ranks") else None
    )
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
    from closer_runtime_binding import policy_problem

    initial = campaign.read()
    reason = policy_problem(((initial or {}).get("worker_config") or {}).get("policy") or {})
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
    except Exception as exc:  # noqa: BLE001 - classified, never swallowed silently
        outcome = classify_exception(exc)
    if outcome not in OUTCOMES:
        outcome = "unknown"
    try:
        adapter.settle(handle, outcome)
    except Exception:  # noqa: BLE001 - the effect may have landed: the intent stays a barrier
        return "unknown"
    return outcome


def _atomic_write(path: Path, doc: dict[str, Any]) -> None:
    payload = (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
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
    command = [
        python,
        str(module_dir / "closer_dispatcher.py"),
        "run",
        "--assignment",
        cfg["assignment"],
        "--state",
        cfg["dispatch_state"],
        "--budget-seconds",
        str(int(cfg.get("budget_seconds", 600))),
        "--max-workers",
        str(min(MAX_WORKERS_CAP, max(1, int(cfg.get("dispatch_workers", 1))))),
    ]
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


def write_result(path: str | Path, worker_id: str, target: str, outcome: str, detail: str) -> None:
    _atomic_write(
        Path(path),
        {
            "worker_id": worker_id,
            "target": target,
            "outcome": outcome,
            "detail": detail,
            "finished": _now(),
        },
    )


def delegate_target(
    cfg: dict[str, Any], target: str, agent: str, profile_id: str, *, delegate=None
) -> tuple[str, str]:
    """Per-target repair through the existing `dispatcher.delegate`, which makes the exact
    real-target claim itself. A foreign owner of that claim is a barrier, not a race."""
    from closer_runtime_binding import policy_problem

    reason = policy_problem(cfg.get("policy") or {})
    if reason:
        return "unavailable", reason
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
) -> str:
    """One bounded work unit. Fences BEFORE any effect, then writes exactly one durable result.

    `round`: the unit is the whole closer assignment (target must be ROUND_UNIT).
    `target-delegate`: the unit is one real target, handed to `dispatcher.delegate` on the
    provider the router currently selects. Any other granularity is refused."""
    worker_id = Path(result).stem
    module_dir = module_dir or Path(__file__).resolve().parent

    def finish(outcome, detail):
        write_result(result, worker_id, target, outcome, detail)
        return outcome

    try:
        doc = Campaign(state).read()
    except CampaignError as exc:  # includes IncompatibleState: refuse before writing anything
        return finish("unavailable", f"fenced before effects: {exc}")
    mine = next((w for w in (doc or {}).get("workers", []) if w["worker_id"] == worker_id), None)
    claim_holder = holder(f"{doc['campaign_id']}:{target}") if doc else None
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
    from closer_runtime_binding import policy_problem

    worker_policy = (doc.get("worker_config") or {}).get("policy") or {}
    if runtime_manifest:
        try:
            worker_policy = load_manifest(runtime_manifest).get("policy") or {}
        except (OSError, ValueError, CampaignError):
            return finish("unavailable", "pinned worker policy unreadable")
    policy = policy_problem(worker_policy)
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
    "closer_native_lease.py",
    "closer_launcher.py",
    "closer_release.py",
    "claims.py",
    "closer_dispatcher.py",
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
    return manifest


def qualify(manifest: dict[str, Any], root: str | Path) -> list[str]:
    """Why this pinned version may NOT take effects yet; empty means qualified.

    Needs (1) valid manifest + verified modules, (2) `<root>/promotion.json`
    (`{"runners": [...], "receipts": [...]}`) that `check_promotion` accepts, i.e. real
    desktop-runner receipts for exactly this package and state versions, (3) a runner that
    understands the recorded state schema, and (4) the pinned Core loadable as an adapter.
    Any failure holds; nothing falls back to another action path."""
    problems = validate_manifest(manifest) + verify_modules(manifest)
    try:
        promotion = _load(str(Path(root) / "promotion.json"))
        verdict = check_promotion(
            manifest, list(promotion.get("runners") or []), list(promotion.get("receipts") or [])
        )
        problems += verdict["problems"]
    except (OSError, ValueError, AttributeError) as exc:
        problems.append(f"promotion receipts unreadable: {exc}")
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
) -> list[dict[str, Any]]:
    """Discover every `*.campaign.json` under `root` and advance each by at most
    `max_steps`. Needs only the files: a second process after worker death resumes here.
    Reconcile always runs; launches need a fresh cached capacity report (fail closed)."""
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, ValueError, CampaignError) as exc:
        return [{"kind": "skipped", "reason": f"pin unusable: {exc}"}]
    from closer_runtime_binding import policy_problem

    policy = policy_problem(manifest.get("policy") or {})
    if policy:
        return [{"kind": "held", "reason": policy}]
    unqualified = qualifier(manifest, root)
    if unqualified:  # before ANY effect, reconcile included: an unqualified pin is inert
        return [{"kind": "held", "reason": "version not qualified", "problems": unqualified}]
    py, module = manifest["runtime"]["python"], manifest["module_path"]
    report = []
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
        capacity = capacity_json or cfg.get("capacity_json")
        route = route_fn(capacity)
        capacity_ok = bool(route.get("agent"))
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
                *extra,
            ]

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
                launcher=launcher,
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


def controller_entry(argv: list[str] | None, fallback: Callable[[list[str] | None], int]) -> int:
    """Hook for the desktop controller's `main`. `closer-campaign ...` is ours; any other
    scheduled invocation first runs one bounded, never-raising tick, then the fallback."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["closer-campaign"]:
        return main(args[1:])
    tick_safe()
    return fallback(argv)


if __name__ == "__main__":
    raise SystemExit(main())
