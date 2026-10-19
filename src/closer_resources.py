"""Resource-scoped state for the opt-in closer coordinator mode.

Legacy ``closer_pipeline.run_pipeline`` serialises a whole round behind one
``.runlock`` and rewrites one shared JSON document. The scoped mode shards that
state per target instead: one directory per target holding its own pipeline
state file and a lease, plus a versioned ``contract.json`` for the whole root.
Nothing here reads or writes the legacy ledger.

Lease rules (the only exclusion between co-coordinators of one target):

* The holder keeps a non-blocking ``flock`` for the life of its process, so a
  crashed owner releases implicitly and a live owner can never be evicted.
* ``lease.json`` carries a monotonic epoch. Every state commit of the shard
  re-checks, immediately before the atomic replace, that the lease is still
  held, still carries the holder's epoch, and has not passed its commit expiry.
  A stale or expired holder therefore fails closed instead of committing.
* The flock is held only for one target's work, never across targets, so a
  contested or unknown target blocks only itself.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

CONTRACT_SCHEMA = "closer-scoped-state/v1"
LAYOUT = 1
COORDINATOR_MODES = ("legacy", "resource_scoped")
MODE_ENV = "ORCH_CLOSER_COORDINATOR_MODE"
# A holder may finish its final receipt commit this long after the round deadline.
LEASE_COMMIT_GRACE_SECONDS = 120.0
# Longest a short exclusive resource claim or a pool slot is awaited (also capped by the deadline).
RESOURCE_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.02
_TARGET_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([0-9]+)$")
# Kinds whose in-flight effect leaves a durable intent (an unknown survives a crash).
DURABLE_KINDS = frozenset({"pr", "issue", "branch", "worktree"})
RESOURCE_KINDS = DURABLE_KINDS | {"repo"}


class IncompatibleStateContract(RuntimeError):
    """The scoped root was written by a contract this code does not speak."""


class StaleLease(RuntimeError):
    """The holder lost, released or outlived its lease and may not commit."""


class CorruptShard(RuntimeError):
    """A lease or shard record exists but cannot be trusted; never reset to a fresh epoch."""


class ResourceBlocked(RuntimeError):
    """A mutation reservation or pool slot was not obtained; the effect was NOT started."""


def resolve_mode(mode: str | None, environ: Mapping[str, str] | None = None) -> str:
    chosen = mode if mode is not None else (environ or os.environ).get(MODE_ENV, "legacy")
    chosen = chosen or "legacy"
    if chosen not in COORDINATOR_MODES:
        raise ValueError(f"coordinator mode must be one of {COORDINATOR_MODES}, got {chosen!r}")
    return chosen


def capability(pipeline_schema: str) -> dict[str, Any]:
    """What this code can speak, for a release installer deciding a version pin."""
    return {
        "contract_schema": CONTRACT_SCHEMA,
        "layout": LAYOUT,
        "pipeline_schema": pipeline_schema,
        "modes": list(COORDINATOR_MODES),
        "version_pin": True,
        "resource_keys_callback": True,
        "pool_lease_callback": True,
        "scoped_validate_stop": True,
        "proven_runtime_promotion": False,
        "coordination_gates": 1,
        "requires_terminal_uncooperative_writers": True,
    }


def scoped_root(state_path: str | Path) -> Path:
    path = Path(state_path)
    return path.with_name(path.name + ".scoped")


def shard_dir(root: Path, target: str) -> Path:
    return root / "targets" / hashlib.sha256(canonical_target(target).encode()).hexdigest()[:24]


def _atomic_write(path: Path, doc: Mapping[str, Any]) -> None:
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(doc, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def canonical_target(target: str) -> str:
    """Strict identity: ``Owner/Repo#N`` folds to lower-case owner/repo; anything else is exact.

    Surrounding whitespace or control characters are refused, never trimmed, so two spellings
    cannot name two shards for one resource.
    """
    if not isinstance(target, str) or not target or target != target.strip():
        raise ValueError(f"non-canonical target identity {target!r}")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in target):
        raise ValueError(f"control character in target identity {target!r}")
    match = _TARGET_RE.match(target)
    if match:
        return f"{match.group(1).lower()}/{match.group(2).lower()}#{int(match.group(3))}"
    return target


def normalize_key(key: str) -> str:
    """``kind:value`` resource key; GitHub owner/repo is case-folded, branch names are not."""
    if not isinstance(key, str) or ":" not in key or key != key.strip():
        raise ValueError(f"resource key must be 'kind:value', got {key!r}")
    kind, value = key.split(":", 1)
    if kind not in RESOURCE_KINDS or not value:
        raise ValueError(f"unknown resource kind in {key!r}; known: {sorted(RESOURCE_KINDS)}")
    if kind in {"pr", "issue"}:
        match = _TARGET_RE.match(value)
        if not match:
            raise ValueError(f"{kind} key must be owner/repo#N, got {key!r}")
        return f"{kind}:{match.group(1).lower()}/{match.group(2).lower()}#{int(match.group(3))}"
    if kind == "repo":
        return f"repo:{value.lower()}"
    if kind == "branch":
        repo, sep, branch = value.partition("@")
        if not sep or "/" not in repo or not branch:
            raise ValueError(f"branch key must be branch:owner/repo@name, got {key!r}")
        return f"branch:{repo.lower()}@{branch}"
    return f"worktree:{value}"


def default_resource_keys(target: str) -> list[str]:
    """Conservative act-stage keys for a target the callback did not describe."""
    match = _TARGET_RE.match(canonical_target(target))
    if not match:
        return []
    owner, repo = match.group(1).lower(), match.group(2).lower()
    return [f"pr:{owner}/{repo}#{int(match.group(3))}", f"repo:{owner}/{repo}"]


def open_root(
    state_path: str | Path, *, pipeline_schema: str, pin: Mapping[str, Any] | None = None
) -> Path:
    """Create the scoped root on first use, or refuse an incompatible one.

    ``pin`` is the contract a version-pinned worker was built for
    (``{"layout": 1, "pipeline_schema": ...}``). It is checked against what this code
    speaks BEFORE any directory is created, and against an existing root before it is
    used; a root that differs is refused, never migrated in place.
    """
    root = scoped_root(state_path)
    want = {
        "schema": CONTRACT_SCHEMA,
        "layout": LAYOUT,
        "pipeline_schema": pipeline_schema,
        "coordination_gates": 1,
    }
    if pin is not None and any(
        type(want.get(k)) is not type(v) or want.get(k) != v for k, v in pin.items()
    ):
        raise IncompatibleStateContract(f"worker pin {dict(pin)!r} does not match {want!r}")
    contract = root / "contract.json"
    if not contract.exists():
        (root / "targets").mkdir(parents=True, exist_ok=True)
        temp = root / f".contract.{os.getpid()}.{threading.get_ident()}.tmp"
        with temp.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(want, sort_keys=True))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp, contract)  # atomic create-if-absent: concurrent first users agree
        except FileExistsError:
            pass
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()
        _fsync_dir(root)
    try:
        found = json.loads(contract.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IncompatibleStateContract(f"unreadable scoped contract: {exc}") from exc
    comparable = {k: found.get(k) for k in want} if isinstance(found, dict) else None
    if json.dumps(comparable, sort_keys=True) != json.dumps(want, sort_keys=True):
        raise IncompatibleStateContract(f"scoped root contract {found!r} != supported {want!r}")
    (root / "targets").mkdir(parents=True, exist_ok=True)
    return root


class Lease:
    def __init__(self, directory: Path, target: str, owner: str, epoch: int, handle, expires_at):
        self.directory = directory
        self.target = target
        self.owner = owner
        self.epoch = epoch
        self.expires_at = float(expires_at)
        self._handle = handle
        self.state_path = directory / "state.json"

    def check(self, now: Callable[[], float] = time.time) -> None:
        if self._handle is None or self._handle.closed:
            raise StaleLease(f"{self.target}: lease released")
        if now() > self.expires_at:
            raise StaleLease(f"{self.target}: lease expired at {self.expires_at}")
        try:
            doc = json.loads((self.directory / "lease.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StaleLease(f"{self.target}: lease record unreadable: {exc}") from exc
        if doc.get("epoch") != self.epoch or doc.get("owner") != self.owner:
            raise StaleLease(f"{self.target}: lease superseded by {doc.get('owner')!r}")

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None and not handle.closed:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def acquire(
    root: Path,
    target: str,
    *,
    owner: str,
    expires_at: float,
    now: Callable[[], float] = time.time,
) -> Lease | None:
    """Take the target's lease without waiting; ``None`` means a live owner holds it."""
    directory = shard_dir(root, target)
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "lease.lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    canonical = canonical_target(target)
    record = directory / "lease.json"
    try:
        if record.exists():
            previous = json.loads(record.read_text(encoding="utf-8"))
            prior = previous.get("epoch") if isinstance(previous, dict) else None
            if (
                isinstance(prior, bool)
                or not isinstance(prior, int)
                or prior < 1
                or previous.get("target") != canonical
            ):
                raise CorruptShard(f"{target}: malformed prior lease {previous!r}")
            epoch = prior + 1
        elif (directory / "state.json").exists():
            raise CorruptShard(f"{target}: shard state exists but its lease record is missing")
        else:
            epoch = 1  # genuinely first lease: no record and no state
    except (OSError, ValueError) as exc:
        handle.close()
        raise CorruptShard(f"{target}: unreadable prior lease: {exc}") from exc
    except CorruptShard:
        handle.close()
        raise
    try:
        _atomic_write(
            directory / "lease.json",
            {
                "target": canonical,
                "owner": owner,
                "epoch": epoch,
                "acquired_at": now(),
                "expires_at": expires_at,
            },
        )
    except BaseException:
        handle.close()
        raise
    return Lease(directory, target, owner, epoch, handle, expires_at)


# state-file path -> commit fence; consulted by closer_pipeline._save before replace. Keys are
# per-shard paths and a shard is only ever fenced by its flock holder, so entries never collide;
# registration under a mutex refuses a duplicate instead of overwriting another thread's fence.
SAVE_FENCES: dict[str, Callable[[], None]] = {}
_FENCE_GUARD = threading.Lock()


def fence_for(path: str | Path) -> Callable[[], None] | None:
    with _FENCE_GUARD:
        return SAVE_FENCES.get(str(path))


@contextlib.contextmanager
def fenced(lease: Lease, now: Callable[[], float] = time.time):
    key = str(lease.state_path)
    with _FENCE_GUARD:
        if key in SAVE_FENCES:
            raise StaleLease(f"{lease.target}: shard already fenced by another holder")
        SAVE_FENCES[key] = lambda: lease.check(now)
    try:
        yield
    finally:
        with _FENCE_GUARD:
            SAVE_FENCES.pop(key, None)


_UNRESOLVED = {"running", "uncertain"}


def _unresolved_effect(path: Path, target: str, row: Mapping[str, Any]) -> str | None:
    """Mode switches cannot turn an explicit unknown effect into fresh authority."""
    if row.get("status") in _UNRESOLVED:
        return f"{row['status']}_outcome"
    if row.get("status") != "failed" or row.get("stage") != "act":
        return None
    barrier = row.get("recovery_barrier")
    results = row.get("results")
    act = results.get("act") if isinstance(results, Mapping) else None
    explicit_unknown = (
        barrier is not None
        or row.get("error") == "act returned unsupported or missing status"
        or bool(row.get("provider_outcome_uncertain"))
        or (
            isinstance(act, Mapping)
            and (
                act.get("status") in {"unknown", "uncertain", "running", "submitted"}
                or act.get("outcome_uncertain") is True
            )
        )
    )
    if not explicit_unknown:
        return None
    # An existing strict receipt can affirmatively prove a pre-fix no-operation
    # wait. A durable recovery barrier is never discharged by this read-only guard.
    if barrier is None:
        import closer_pipeline as cp

        if cp._legacy_failed_act_wait_proof(path, target, row):
            return None
    return "unverified_act_outcome"


def legacy_unresolved(state_path: str | Path) -> tuple[dict[str, str], str | None]:
    """Targets the LEGACY ledger leaves unresolved, read-only.

    Returns ``({target: reason}, error)``. An unreadable ledger returns an error
    and the caller must fence everything: unknown is never clear.
    """
    path = Path(state_path)
    if not path.exists():
        return {}, None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        runs = doc.get("runs", {})
        carry = doc.get("carry", {})
        found: dict[str, str] = {}
        for run in runs.values():
            for target, row in (run.get("targets") or {}).items():
                if not isinstance(row, Mapping):
                    raise ValueError(f"legacy row for {target!r} is not an object")
                reason = _unresolved_effect(path, target, row)
                if reason:
                    found[canonical_target(target)] = "legacy_" + reason
        for target, row in carry.items():
            if not isinstance(row, Mapping):
                raise ValueError(f"legacy carry row for {target!r} is not an object")
            reason = _unresolved_effect(path, target, row)
            if reason:
                found[canonical_target(target)] = "legacy_" + reason
        return found, None
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        return {}, f"legacy ledger unreadable: {exc}"


def _fenced_row(reason: str, detail: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "status": "deferred",
        "stage": "prepare",
        "results": {},
        "deferred_reason": reason,
        "deferred_next_trigger": "next_round",
        "scoped_fence": True,
    }
    if detail:
        row["error"] = detail[:500]
    return row


# --------------------------------------------------------------------------- rollback guard


def scoped_rollback_report(state_path: str | Path) -> tuple[dict[str, str], list[str]]:
    """``({target: reason}, malformed)`` read from the scoped shards, read-only.

    A shard names its target only inside its own records, so a shard that cannot be read
    (or holds a malformed row) cannot be mapped to a target: it is listed in ``malformed``
    and the caller must treat EVERY target as affected. Unknown is never clear.
    A shard whose lease is currently held by a live scoped coordinator is running.
    """
    found: dict[str, str] = {}
    malformed: list[str] = []
    root = scoped_root(state_path)
    if not root.exists():
        return found, malformed
    base = root / "targets"
    if not (root / "contract.json").exists() or not base.is_dir():
        return found, ["scoped root without contract or targets directory"]
    try:
        import closer_pipeline as cp

        contract = json.loads((root / "contract.json").read_text(encoding="utf-8"))
        expected = {
            "schema": CONTRACT_SCHEMA,
            "layout": LAYOUT,
            "pipeline_schema": cp.SCHEMA,
            "coordination_gates": 1,
        }
        if not isinstance(contract, dict) or any(
            type(contract.get(k)) is not type(v) or contract.get(k) != v
            for k, v in expected.items()
        ):
            return found, ["incompatible scoped coordination contract"]
    except (OSError, ValueError) as exc:
        return found, [f"unreadable scoped coordination contract: {exc}"]
    for shard in sorted(p for p in base.iterdir() if p.is_dir()):
        try:
            lease_doc = None
            if (shard / "lease.json").exists():
                lease_doc = json.loads((shard / "lease.json").read_text(encoding="utf-8"))
                if not isinstance(lease_doc, dict) or not isinstance(lease_doc.get("target"), str):
                    raise ValueError("lease record without a target")
            lock_path = shard / "lease.lock"
            if lock_path.exists():
                with lock_path.open("a+") as probe:
                    try:
                        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
                    except BlockingIOError:
                        if lease_doc is None:
                            raise ValueError("lease held but its record is missing")
                        found[lease_doc["target"]] = "scoped_lease_held"
            state = shard / "state.json"
            if not state.exists():
                continue
            doc = json.loads(state.read_text(encoding="utf-8"))
            for run in doc["runs"].values():
                for target, row in run["targets"].items():
                    if not isinstance(row, dict):
                        raise ValueError(f"row for {target!r} is not an object")
                    reason = _unresolved_effect(state, target, row)
                    if reason:
                        found[canonical_target(target)] = "scoped_" + reason
            for target, row in doc.get("carry", {}).items():
                if not isinstance(row, Mapping):
                    raise ValueError(f"carry row for {target!r} is not an object")
                reason = _unresolved_effect(state, target, row)
                if reason:
                    found[canonical_target(target)] = "scoped_" + reason
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            malformed.append(f"{shard.name}: {type(exc).__name__}: {exc}"[:200])
    return found, malformed


def scoped_unresolved(state_path: str | Path) -> dict[str, str]:
    """Targets whose shard still holds a running/uncertain row; malformed shards are keyed
    ``unreadable_shard:<dir>`` (the rollback guard uses ``scoped_rollback_report``)."""
    found, malformed = scoped_rollback_report(state_path)
    return {
        **{t: r.split("_", 1)[1].split("_outcome")[0] for t, r in found.items()},
        **{f"unreadable_shard:{m.split(':', 1)[0]}": "unreadable_shard" for m in malformed},
    }


def rollback_fence(state_path: str | Path, targets) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Legacy-mode guard: fenced rows for assigned targets legacy must not touch, plus the
    runnable remainder. Legacy mode calls this itself; no global callback is injected."""
    ordered = list(targets)
    found, malformed = scoped_rollback_report(state_path)
    if malformed:
        reason = "scoped_shard_unreadable"
        rows = {t: _fenced_row(reason, "; ".join(malformed)) for t in ordered}
        return rows, []
    rows = {}
    for target in ordered:
        try:
            key = canonical_target(target)
        except ValueError:
            continue  # legacy validates identity itself
        if key in found:
            rows[target] = _fenced_row(found[key])
    return rows, [t for t in ordered if t not in rows]


@contextlib.contextmanager
def coordination_gate(state_path: str | Path, target: str, *, mode="resource_scoped"):
    """Stable pre-state-read target gate shared by both cooperating modes.

    Old installed writers do not speak this protocol: promotion requires their terminality.
    Never use elapsed time as evidence that such a writer has stopped.
    """
    path = Path(str(state_path) + ".coordination")
    path.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(canonical_target(target).encode()).hexdigest()
    with (path / name).open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            # Preserve legacy's existing wait-for-prior-round behavior for another
            # cooperating legacy caller. This is advisory only; flock still decides.
            handle.seek(0)
            incumbent = handle.read()
            if (
                mode != "legacy"
                or incumbent != "legacy"
                or not _poll_lock(handle, time.time() + RESOURCE_WAIT_SECONDS, time.time)
            ):
                raise ResourceBlocked("resource_contested") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(mode)
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class _CoordinationAdmission:
    """Internal live capability: exact target gates remain owned by the CLI stack."""

    def __init__(self, state_path):
        self.state_path = Path(state_path).resolve()
        self.runnable = set()
        self.active = True

    @contextlib.contextmanager
    def gate(self, state_path, target, *, mode="resource_scoped"):
        if (
            not self.active
            or Path(state_path).resolve() != self.state_path
            or canonical_target(target) not in self.runnable
        ):
            raise ResourceBlocked("resource_contested")
        yield


@contextlib.contextmanager
def coordination_admission(state_path, targets, *, mode="legacy"):
    """Acquire before any admission read; lower layers reuse only these live gates."""
    admission = _CoordinationAdmission(state_path)
    try:
        with contextlib.ExitStack() as stack:
            for target in sorted(set(targets), key=canonical_target):
                try:
                    stack.enter_context(coordination_gate(state_path, target, mode=mode))
                except ResourceBlocked:
                    continue
                admission.runnable.add(canonical_target(target))
            yield admission
    finally:
        admission.active = False


def admission_gate(admission, state_path, target, *, mode="resource_scoped"):
    if admission is None:
        return coordination_gate(state_path, target, mode=mode)
    if not isinstance(admission, _CoordinationAdmission):
        raise ResourceBlocked("invalid coordination admission")
    return admission.gate(state_path, target, mode=mode)


@contextlib.contextmanager
def mutation_reservation(state_path, target, context, resource_keys, *, owner, deadline, now):
    """Same mutation keys in either mode; unresolved aliases never disappear on rollback."""
    root = scoped_root(state_path)
    keys = default_resource_keys(target)
    if resource_keys is not None:
        extra = resource_keys(target, context)
        if extra is None or isinstance(extra, (str, bytes)):
            raise ResourceBlocked("resource_keys must return an iterable of keys")
        keys.extend(extra)
    else:
        # Without a prepared proposal we cannot prove branch/source disjointness.
        repo = canonical_target(target).split("#")[0]
        for key in unknown_reservations(root):
            if key.startswith(("unreadable:", "worktree:")) or (
                key.startswith(("issue:", "branch:"))
                and key.split(":", 1)[-1].split("#")[0].split("@")[0] == repo
            ):
                raise ResourceBlocked("unknown resource aliases require prepared operation keys")
    with reserve(root, keys, target=target, owner=owner, deadline=deadline, now=now):
        yield


def legacy_refusal_receipt(
    state_path: str | Path, targets, rows: Mapping[str, Any], deadline: float, now=time.time
) -> dict[str, Any]:
    stamp = now()
    return {
        "schema": "closer-pipeline-receipt/v1",
        "mode": "legacy_refused_by_scoped_state",
        "run_id": None,
        "deadline": deadline,
        "finished_at": stamp,
        "assignment": sorted(targets),
        "carry_targets": [],
        "targets": {t: dict(rows[t]) for t in sorted(rows)},
        "scoped_rollback": {"fenced_targets": sorted(rows), "drainable": 0},
    }


# --------------------------------------------------------------------------- reservations


def _reservation_base(root: Path) -> Path:
    # Coordination exists before the first scoped root. Cooperating legacy writers
    # must reserve aliases too, without opting their JSON ledger into scoped mode.
    if root.name.endswith(".scoped"):
        return root.with_name(root.name[: -len(".scoped")] + ".coordination") / "reservations"
    return root / "reservations"


def _reservation_bases(root: Path) -> list[Path]:
    return list(dict.fromkeys([_reservation_base(root), root / "reservations"]))


def _key_dir(root: Path, key: str) -> Path:
    return _reservation_base(root) / hashlib.sha256(key.encode()).hexdigest()[:24]


def _poll_lock(handle, deadline: float, now) -> bool:
    limit = min(float(deadline), now() + RESOURCE_WAIT_SECONDS)
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if now() >= limit:
                return False
            time.sleep(_POLL_SECONDS)


def _read_intent(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResourceBlocked(f"unreadable reservation intent {path.parent.name}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ResourceBlocked(f"malformed reservation intent {path.parent.name}")
    return doc


@contextlib.contextmanager
def reserve(
    root: Path,
    keys,
    *,
    target: str,
    owner: str,
    deadline: float,
    now: Callable[[], float] = time.time,
):
    """Short exclusive claims on ``keys`` around ONE effect, taken in lexical order.

    Durable kinds write intent before the effect starts and retain it after callback return.
    Only this executor's durably completed pipeline receipt may release its reservations.
    Historical unknowns require independent resource-bound reconciliation; another owner,
    even for the same target, cannot overwrite them. Reads and reasoning remain available.
    ``ResourceBlocked`` always means this effect was not started.
    """
    ordered = sorted({normalize_key(k) for k in keys})
    held: list[tuple[str, Any, Path]] = []
    started = False
    try:
        for key in ordered:
            directory = _key_dir(root, key)
            directory.mkdir(parents=True, exist_ok=True)
            handle = (directory / "lock").open("a+")
            if not _poll_lock(handle, deadline, now):
                handle.close()
                raise ResourceBlocked(f"resource {key} held by another effect")
            held.append((key, handle, directory / "intent.json"))
            for base in _reservation_bases(root):
                for filename in ("intent.json", "legacy-intent.json"):
                    intent = _read_intent(base / directory.name / filename)
                    if intent is not None:
                        raise ResourceBlocked(
                            f"resource {key} carries an unresolved effect of {intent.get('target')!r}"
                        )
        for key, _handle, path in held:
            if key.split(":", 1)[0] in DURABLE_KINDS:
                _atomic_write(
                    path,
                    {
                        "key": key,
                        "target": canonical_target(target),
                        "owner": owner,
                        "state": "in_flight",
                        "at": now(),
                    },
                )
        started = True
        yield
        # Keep the intent until the pipeline has durably recorded completion.
        # Returning from a callback proves neither success nor even a known result.
    except BaseException:
        if started:
            for key, _handle, path in held:
                if key.split(":", 1)[0] in DURABLE_KINDS and path.exists():
                    with contextlib.suppress(OSError):
                        _atomic_write(
                            path,
                            {
                                "key": key,
                                "target": canonical_target(target),
                                "owner": owner,
                                "state": "unknown",
                                "at": now(),
                            },
                        )
        raise
    finally:
        for _key, handle, _path in reversed(held):
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


@contextlib.contextmanager
def legacy_reserve(root, keys, *, target, owner, deadline, now=time.time):
    """Advertise a cooperating legacy executor under its existing JSON/runlock protocol.

    This is not a Core operation intent. The same live legacy executor may perform its
    next guarded operation while its durable legacy ledger advances. Other executors,
    including scoped ones, cannot enter. A crash or invalid stop retains the advertisement.
    Core intents are never adopted, including those with the same target/owner.
    """
    held = []
    try:
        for key in sorted({normalize_key(k) for k in keys}):
            directory = _key_dir(root, key)
            directory.mkdir(parents=True, exist_ok=True)
            handle = (directory / "lock").open("a+")
            if not _poll_lock(handle, deadline, now):
                handle.close()
                raise ResourceBlocked(f"resource {key} held by another executor")
            held.append((key, handle, directory))
            for base in _reservation_bases(root):
                if _read_intent(base / directory.name / "intent.json") is not None:
                    raise ResourceBlocked(f"resource {key} carries a Core operation intent")
                prior = _read_intent(base / directory.name / "legacy-intent.json")
                if prior is not None and (
                    prior.get("owner") != owner or prior.get("target") != canonical_target(target)
                ):
                    raise ResourceBlocked(f"resource {key} carries an unresolved legacy executor")
        for key, _handle, directory in held:
            if key.split(":", 1)[0] in DURABLE_KINDS:
                _atomic_write(
                    directory / "legacy-intent.json",
                    {
                        "key": key,
                        "owner": owner,
                        "target": canonical_target(target),
                        "state": "legacy_executor",
                        "at": now(),
                    },
                )
        yield
    finally:
        for _key, handle, _directory in reversed(held):
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()


def release_legacy_boundary(root, owner):
    """Remove only this legacy executor's advertisements after its durable valid stop.

    The caller must validate the existing native/legacy continuation proofs first.
    No Core intent is read as completion or removed here.
    """
    base = _reservation_base(root)
    for directory in sorted(base.iterdir()) if base.is_dir() else []:
        with (directory / "lock").open("a+") as handle:
            if not _poll_lock(handle, time.time() + 1.0, time.time):
                continue
            try:
                path = directory / "legacy-intent.json"
                intent = _read_intent(path)
                if intent is not None and intent.get("owner") == owner:
                    path.unlink()
                    _fsync_dir(directory)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def unknown_reservations(root: Path) -> dict[str, str]:
    """``{key: target}`` for every intent left behind (in_flight with no live holder is unknown)."""
    found: dict[str, str] = {}
    directories = [
        directory
        for base in _reservation_bases(root)
        for directory in (sorted(base.iterdir()) if base.is_dir() else [])
    ]
    for directory in directories:
        for filename in ("intent.json", "legacy-intent.json"):
            try:
                intent = _read_intent(directory / filename)
            except ResourceBlocked:
                found[f"unreadable:{directory.name}:{filename}"] = "?"
                continue
            if intent is not None:
                key = str(intent.get("key"))
                if key in found:
                    key = "historical:" + key
                found[key] = str(intent.get("target"))
    return found


def release_target(root: Path, target: str, *, owner: str) -> list[str]:
    """Release this executor's reservations after its durable completed receipt.

    Historical unknowns owned by a different executor need independent, resource-bound
    reconciliation. Completion of a newer attempt cannot silently drain them.
    """
    cleared = []
    owned = canonical_target(target)
    directories = [
        directory
        for base in _reservation_bases(root)
        for directory in (sorted(base.iterdir()) if base.is_dir() else [])
    ]
    for directory in directories:
        with (directory / "lock").open("a+") as handle:
            if not _poll_lock(handle, time.time() + 1.0, time.time):
                continue
            try:
                intent = _read_intent(directory / "intent.json")
                if (
                    intent is not None
                    and intent.get("target") == owned
                    and intent.get("owner") == owner
                ):
                    (directory / "intent.json").unlink()
                    _fsync_dir(directory)
                    cleared.append(str(intent.get("key")))
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    return cleared


# --------------------------------------------------------------------------- shared pools


@contextlib.contextmanager
def pool_slot(root: Path, lane: str, bound: int, *, deadline: float, now=time.time):
    """One of ``bound`` process-wide slots for ``lane`` (flock files shared by every process).

    Every coordinator must pass the same bound for a lane; the bounds are the existing
    ``max_*_workers`` arguments, not a new budget. ``ResourceBlocked``: no slot before the deadline.
    """
    directory = root / "pools" / lane
    directory.mkdir(parents=True, exist_ok=True)
    limit = min(float(deadline), now() + RESOURCE_WAIT_SECONDS)
    handle = None
    while handle is None:
        for index in range(bound):
            candidate = (directory / f"slot-{index}").open("a+")
            try:
                fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                handle = candidate
                break
            except BlockingIOError:
                candidate.close()
        if handle is None:
            if now() >= limit:
                raise ResourceBlocked(f"no {lane} pool slot before the deadline")
            time.sleep(_POLL_SECONDS)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# --------------------------------------------------------------------------- aggregate receipt


def _round_dir(root: Path) -> Path:
    return root / "rounds"


def write_round_receipt(root: Path, receipt: Mapping[str, Any]) -> Path:
    """Immutable create-only aggregate receipt, one file per round id."""
    directory = _round_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{receipt['round_id']}.json"
    temp = directory / f".{receipt['round_id']}.{os.getpid()}.{threading.get_ident()}.tmp"
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(receipt, stream, sort_keys=True, allow_nan=False, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temp, path)
    except FileExistsError:
        raise IncompatibleStateContract(f"round receipt {path.name} already exists") from None
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
    _fsync_dir(directory)
    return path


def validate_stop(
    state_path: str | Path,
    round_id: str,
    assignment_targets=None,
    *,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Validate ONE named round across its shards with the unchanged pipeline validator.

    The caller names its round; "latest globally" is not selectable. Each target is judged
    by ``closer_pipeline.validate_stop`` on its own shard, bound to the run id the round
    receipt recorded (a later run of that shard cannot stand in), so the shard's original
    deadline and resume receipts are what is validated. A fenced target (no run) is invalid
    and requires continuation. Empty or missing accounting is never valid.
    """
    import closer_pipeline as cp

    if not isinstance(round_id, str) or not re.fullmatch(r"[0-9a-f]{16,64}", round_id):
        return {"valid": False, "stop_valid": False, "reasons": ["invalid_round_id"]}
    root = scoped_root(state_path)
    path = _round_dir(root) / f"{round_id}.json"
    try:
        contract = json.loads((root / "contract.json").read_text(encoding="utf-8"))
        expected = {
            "schema": CONTRACT_SCHEMA,
            "layout": LAYOUT,
            "pipeline_schema": cp.SCHEMA,
            "coordination_gates": 1,
        }
        if not isinstance(contract, dict) or any(contract.get(k) != v for k, v in expected.items()):
            raise ValueError("incompatible scoped staging capability")
        receipt = json.loads(path.read_text(encoding="utf-8"))
        assignment = receipt["assignment"]
        run_ids = receipt["run_ids"]
        rows = receipt["targets"]
        if not isinstance(assignment, list) or not assignment or not isinstance(run_ids, dict):
            raise ValueError("empty or malformed assignment accounting")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"valid": False, "stop_valid": False, "reasons": [f"invalid_round_receipt:{exc}"]}
    reasons: list[str] = []
    if assignment_targets is not None and sorted(assignment_targets) != sorted(assignment):
        reasons.append("assignment_changed")
    if set(rows) != set(assignment):
        reasons.append("assignment_not_fully_accounted")
    continuation = False
    for target in assignment:
        run_id = run_ids.get(target)
        if not run_id:
            row = rows.get(target) or {}
            reasons.append(f"fenced_target:{target}:{row.get('deferred_reason', 'no_run')}")
            continuation = True
            continue
        shard_state = shard_dir(root, target) / "state.json"
        try:
            doc = cp._load(shard_state)
            chosen = doc["runs"][run_id]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            reasons.append(f"unreadable_shard_run:{target}:{type(exc).__name__}")
            continue
        created = float(chosen.get("created", 0))
        doc["runs"] = {
            k: v
            for k, v in doc["runs"].items()
            if isinstance(v, Mapping) and float(v.get("created", 0)) <= created
        }
        import tempfile as _tf

        with _tf.TemporaryDirectory() as scratch:
            copy = Path(scratch) / "state.json"
            copy.write_text(json.dumps(doc), encoding="utf-8")
            verdict = cp.validate_stop(copy, [target], now=now)
        if verdict.get("run_id") != run_id:
            reasons.append(f"run_binding_lost:{target}")
        if not verdict.get("valid"):
            reasons.extend(f"{target}:{r}" for r in verdict.get("reasons", ["invalid"]))
        continuation = continuation or bool(verdict.get("continuation_required"))
    unknown = unknown_reservations(root)
    if unknown:
        reasons.append("unresolved_resource_reservations")
        continuation = True
    valid = not reasons
    return {
        "valid": valid,
        "stop_valid": valid,
        "round_id": round_id,
        "run_ids": dict(run_ids),
        "continuation_required": continuation,
        "deadline": receipt.get("deadline"),
        "reasons": reasons,
    }


# --------------------------------------------------------------------------- coordinator


def run_scoped(
    state_path: str | Path,
    targets,
    *,
    contract_pin: Mapping[str, Any] | None = None,
    prepare,
    interpret,
    act,
    exception,
    reconcile=None,
    capacity_fn,
    deadline: float,
    priority_targets=(),
    max_io_workers: int = 4,
    max_low_workers: int = 2,
    max_exception_workers: int = 1,
    round_deadline_exception=None,
    round_capacity_exception=None,
    resource_keys=None,
    pool_lease=None,
    coordination_admission=None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Resource-scoped coordinator: one lease and one state shard per target.

    Each target runs the unchanged pipeline implementation against its own shard.
    The whole-target lease is held across agent work; callbacks run with no shared lock held.
    ``resource_keys(target, context) -> iterable of 'kind:value'`` names what ``act`` mutates
    (the PR, its source issue, branch, worktree); the target's own PR key and its repository's
    short serialisation key are always added. Only ``act`` takes reservations, so prepare,
    interpret and exception reasoning never wait on them. ``pool_lease(lane, target, context)``
    may return a context manager (the root's profile-pool reservation); without it the lane
    bounds are enforced across processes by ``pool_slot``.
    """
    from concurrent.futures import ThreadPoolExecutor

    import closer_pipeline as cp

    raw_list = list(targets)
    canonical = [canonical_target(t) for t in raw_list]
    if len(set(canonical)) != len(canonical):
        raise ValueError("targets must be unique non-empty strings (compared canonically)")
    if len(raw_list) > cp.MAX_TARGETS:
        raise ValueError(f"at most {cp.MAX_TARGETS} assigned targets fit the coordinator window")
    priority = frozenset(priority_targets)
    if not priority.issubset(raw_list):
        raise ValueError("priority targets must belong to the current assignment")
    bounds = {"io": max_io_workers, "low": max_low_workers, "exception": max_exception_workers}
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in bounds.values()):
        raise ValueError("pool ceilings must be positive integers")
    if resource_keys is not None and not callable(resource_keys):
        raise ValueError("resource_keys must be callable")

    started = now()
    root = open_root(state_path, pipeline_schema=cp.SCHEMA, pin=contract_pin)
    owner = f"{os.getpid()}:{threading.get_ident()}:{started}"
    round_id = hashlib.sha256(
        json.dumps([sorted(canonical), started, owner], sort_keys=True).encode()
    ).hexdigest()[:32]

    def gated(fn, lane):
        def call(target, ctx):
            if pool_lease is not None:
                lease_cm = pool_lease(lane, target, ctx)
            else:
                lease_cm = pool_slot(root, lane, bounds[lane], deadline=deadline, now=now)
            with lease_cm:
                return fn(target, ctx)

        return call

    def guarded_act(target, ctx):
        with mutation_reservation(
            state_path, target, ctx, resource_keys, owner=owner, deadline=deadline, now=now
        ):
            return act(target, ctx)

    results: dict[str, dict[str, Any]] = {}
    run_ids: dict[str, str] = {}
    run_deadlines: dict[str, float] = {}
    lock_wait: dict[str, float] = {}
    guard = threading.Lock()

    def work(target: str) -> None:
        try:
            with admission_gate(coordination_admission, state_path, target):
                work_locked(target)
        except ResourceBlocked as exc:
            with guard:
                results[target] = _fenced_row("resource_contested", str(exc))

    def work_locked(target: str) -> None:
        legacy_rows, legacy_error = legacy_unresolved(state_path)
        began = now()
        key = canonical_target(target)
        if legacy_error is not None:
            row = _fenced_row("legacy_state_unreadable", legacy_error)
        elif key in legacy_rows:
            row = _fenced_row(legacy_rows[key])
        else:
            try:
                lease = acquire(
                    root,
                    target,
                    owner=owner,
                    expires_at=float(deadline) + LEASE_COMMIT_GRACE_SECONDS,
                    now=now,
                )
            except CorruptShard as exc:
                lease = None
                row = _fenced_row("corrupt_shard", str(exc))
            else:
                row = None
            lock_wait[target] = now() - began
            if lease is None and row is None:
                row = _fenced_row("resource_contested")
            elif lease is not None:
                try:
                    with fenced(lease, now):
                        receipt = cp._run_pipeline_legacy(
                            lease.state_path,
                            [target],
                            prepare=gated(prepare, "io"),
                            interpret=gated(interpret, "low"),
                            act=gated(guarded_act, "io"),
                            exception=gated(exception, "exception"),
                            reconcile=gated(reconcile, "io") if reconcile else None,
                            capacity_fn=capacity_fn,
                            deadline=deadline,
                            priority_targets=[target] if target in priority else (),
                            max_io_workers=1,
                            max_low_workers=1,
                            max_exception_workers=1,
                            round_deadline_exception=round_deadline_exception,
                            round_capacity_exception=round_capacity_exception,
                            now=now,
                        )
                    found_row = (receipt.get("targets") or {}).get(target)
                    row = found_row or _fenced_row(str(receipt.get("reason") or "no_shard_receipt"))
                    if found_row is not None and receipt.get("run_id"):
                        run_ids[target] = receipt["run_id"]
                        run_deadlines[target] = receipt.get("deadline", deadline)
                    if found_row is not None and found_row.get("status") == "completed":
                        release_target(root, target, owner=owner)  # durable completion only
                except StaleLease as exc:
                    row = _fenced_row("stale_lease", str(exc))
                except Exception as exc:  # shard unreadable/incompatible: fence, never clear
                    row = _fenced_row("shard_error", f"{type(exc).__name__}: {exc}")
                finally:
                    lease.release()
        with guard:
            results[target] = row if row is not None else _fenced_row("missing_shard_result")

    ordered = sorted(raw_list, key=lambda t: (t not in priority, t))
    threads = max(1, min(len(ordered), sum(bounds.values())))
    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(work, ordered))
    finished = now()
    fenced_targets = sorted(t for t, r in results.items() if r.get("scoped_fence"))
    unknown = unknown_reservations(root)
    owners = set(canonical)
    aggregate = {
        "schema": "closer-pipeline-receipt/v1",
        "mode": "resource_scoped",
        "round_id": round_id,
        "run_ids": dict(sorted(run_ids.items())),
        "run_deadlines": dict(sorted(run_deadlines.items())),
        "shards": {t: shard_dir(root, t).name for t in sorted(raw_list)},
        "deadline": deadline,
        "elapsed_seconds": max(0.0, finished - started),
        "finished_at": finished,
        "overrun_seconds": max(0.0, finished - float(deadline)),
        "assignment": sorted(raw_list),
        "carry_targets": [],
        "targets": {t: results[t] for t in sorted(results)},
        "scoped": {
            "contract": CONTRACT_SCHEMA,
            "layout": LAYOUT,
            "fenced_targets": fenced_targets,
            "lease_wait_seconds": {t: lock_wait[t] for t in sorted(lock_wait)},
            "max_lease_wait_seconds": max(lock_wait.values(), default=0.0),
            "completed": sorted(t for t, r in results.items() if r.get("status") == "completed"),
            # blocking quantity AND drainable quantity, together (latched-gate rule)
            "unknown_reservations": dict(sorted(unknown.items())),
            "unknown_reservations_drainable": sum(1 for v in unknown.values() if v in owners),
        },
    }
    aggregate["receipt_path"] = str(write_round_receipt(root, aggregate))
    return aggregate
