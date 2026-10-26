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
import math
import os
import re
import stat
import tempfile
import threading
import time
import weakref
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


class FinitePoolUncertain(RuntimeError):
    """A bounded finite request returned with an owned process group unverified."""

    def __init__(self, fence):
        super().__init__("finite request process cleanup is uncertain")
        self.finite_pool_fence = fence


class ScopeBlocked(ResourceBlocked):
    """A fresh scope proof refused the callback before any provider or effect started."""

    def __init__(self, reason, proof):
        super().__init__(reason)
        self.proof = proof


_NATIVE_INTERPRET_WRAPPERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def native_interpretation_callback(callback):
    """Resolve only wrappers created here; never trust callable attributes."""
    return _NATIVE_INTERPRET_WRAPPERS.get(callback, callback)


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


_INVENTORY_REQUEST = threading.local()
_INVENTORY_OWNERS = threading.local()


@contextlib.contextmanager
def inventory_request(deadline, *, now=time.time):
    """Carry one finite request window through nested shard journal commits."""
    previous = getattr(_INVENTORY_REQUEST, "window", None)
    previous_limit = getattr(_INVENTORY_REQUEST, "monotonic_limit", None)
    sampled_at, monotonic_at = now(), time.monotonic()
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
        for value in (deadline, sampled_at, monotonic_at)
    ):
        raise ResourceBlocked("finite request window requires finite numeric clocks and deadline")
    limit = monotonic_at + max(0.0, deadline - sampled_at)
    if not math.isfinite(limit):
        raise ResourceBlocked("finite request monotonic deadline is not finite")
    if previous is not None:
        deadline = min(deadline, previous[0])
    if previous_limit is not None:
        limit = min(limit, previous_limit)
    _INVENTORY_REQUEST.window = (deadline, now)
    _INVENTORY_REQUEST.monotonic_limit = limit
    try:
        yield
    finally:
        _INVENTORY_REQUEST.window = previous
        _INVENTORY_REQUEST.monotonic_limit = previous_limit


def finite_request_remaining() -> float | None:
    """Return this active request's remaining time without starting a new window."""
    window = getattr(_INVENTORY_REQUEST, "window", None)
    if window is None:
        return None
    deadline, now = window
    sampled_at, monotonic_at = now(), time.monotonic()
    limit = getattr(_INVENTORY_REQUEST, "monotonic_limit", None)
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
        for value in (deadline, sampled_at, monotonic_at, limit)
    ):
        raise ResourceBlocked("finite request window has an invalid clock or deadline")
    if not isinstance(limit, (int, float)):
        raise ResourceBlocked("finite request window has no monotonic deadline")
    return max(0.0, min(deadline - sampled_at, limit - monotonic_at))


@contextlib.contextmanager
def inventory_lock(root, *, exclusive=False, deadline=None, now=time.time):
    """Bind short metadata/state commits to a census; never held across target work."""
    root = Path(root)
    lock = root.with_name(root.name + ".inventory.lock")
    if not exclusive and not lock.exists():
        # Pure inventory of an old/absent root creates nothing. New writers
        # publish this lock before exposing any new shard directory.
        yield
        return
    if exclusive:
        root.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+" if exclusive else "r") as handle:
        window = getattr(_INVENTORY_REQUEST, "window", None)
        if deadline is None and window is not None:
            deadline, now = window
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if deadline is None:
            fcntl.flock(handle, mode)
        else:
            if (
                not isinstance(deadline, (int, float))
                or isinstance(deadline, bool)
                or not math.isfinite(deadline)
            ):
                raise ResourceBlocked("inventory lock requires a finite request deadline")
            # Wall time is the original request's authority; monotonic time also
            # bounds the wait if the wall clock moves backwards while contended.
            monotonic_limit = time.monotonic() + max(0.0, deadline - now())
            while True:
                try:
                    fcntl.flock(handle, mode | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if now() >= deadline or time.monotonic() >= monotonic_limit:
                        raise ResourceBlocked("inventory lock exceeded finite request window")
                    time.sleep(min(_POLL_SECONDS, max(0.0, monotonic_limit - time.monotonic())))
        token = object()
        owners = getattr(_INVENTORY_OWNERS, "active", {})
        _INVENTORY_OWNERS.active = owners
        owners[token] = (root.resolve(), os.getpid())
        try:
            yield token
        finally:
            owners.pop(token, None)
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def inventory_census(root, *, owner=None):
    """Read within an explicitly supplied live inventory owner, or acquire normally.

    A capability is valid only while its originating lock is held by this thread
    and process for this exact physical root. Ordinary locks stay non-reentrant.
    """
    if owner is None:
        with inventory_lock(root):
            yield
        return
    if getattr(_INVENTORY_OWNERS, "active", {}).get(owner) != (Path(root).resolve(), os.getpid()):
        raise ResourceBlocked("invalid caller-owned inventory census")
    yield


def open_root(state_path, *, pipeline_schema, pin=None, deadline=None, now=time.time):
    with inventory_lock(scoped_root(state_path), exclusive=True, deadline=deadline, now=now):
        return _open_root(state_path, pipeline_schema=pipeline_schema, pin=pin)


def _open_root(
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
    def __init__(
        self,
        directory: Path,
        target: str,
        owner: str,
        epoch: int,
        handle,
        expires_at,
        finite_frontier=None,
    ):
        self.directory = directory
        self.target = target
        self.owner = owner
        self.epoch = epoch
        self.finite_frontier = finite_frontier
        self.expires_at = float(expires_at) if finite_frontier is None else None
        self._handle = handle
        self.state_path = directory / "state.json"

    def check(self, now: Callable[[], float] = time.time) -> None:
        if self._handle is None or self._handle.closed:
            raise StaleLease(f"{self.target}: lease released")
        if self.finite_frontier is None:
            expires_at = self.expires_at
            if expires_at is None or now() > expires_at:
                raise StaleLease(f"{self.target}: lease expired at {expires_at}")
        try:
            doc = json.loads((self.directory / "lease.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StaleLease(f"{self.target}: lease record unreadable: {exc}") from exc
        if doc.get("epoch") != self.epoch or doc.get("owner") != self.owner:
            raise StaleLease(f"{self.target}: lease superseded by {doc.get('owner')!r}")
        if self.finite_frontier is not None and (
            doc.get("finite_frontier") != self.finite_frontier
            or type(doc.get("epoch")) is not int
            or doc.get("target") != canonical_target(self.target)
            or doc.get("authority") != "held-owner-epoch-lock/v1"
            or "expires_at" in doc
        ):
            raise StaleLease(f"{self.target}: finite frontier lease identity changed")

    def release(self) -> None:
        if self.finite_frontier is not None:
            try:
                with inventory_lock(
                    self.directory.parents[1], exclusive=True, deadline=time.time()
                ):
                    self._release()
            except ResourceBlocked:
                # The callback has ended. Unlock without waiting; its durable
                # running row/intents retain UNKNOWN and cannot be replayed.
                self._release()
            return
        with inventory_lock(self.directory.parents[1], exclusive=True):
            self._release()

    def _release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None and not handle.closed:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def acquire(
    root,
    target,
    *,
    owner,
    expires_at=None,
    now=time.time,
    finite_frontier=None,
    request_deadline=None,
):
    if finite_frontier is not None:
        from closer_frontier_policy import validate_frontier

        finite_frontier = validate_frontier(finite_frontier)
        if expires_at is not None or target not in finite_frontier["targets"]:
            raise ValueError("finite target lease requires its selected frontier and no duration")
        if request_deadline is None:
            request_deadline = now() + RESOURCE_WAIT_SECONDS
    elif expires_at is None:
        raise ValueError("legacy target lease requires its original expiry")
    with inventory_lock(root, exclusive=True, deadline=request_deadline, now=now):
        return _acquire(
            root,
            target,
            owner=owner,
            expires_at=expires_at,
            now=now,
            finite_frontier=finite_frontier,
        )


def _acquire(
    root: Path,
    target: str,
    *,
    owner: str,
    expires_at: float | None,
    now: Callable[[], float] = time.time,
    finite_frontier=None,
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
                **(
                    {"expires_at": expires_at}
                    if finite_frontier is None
                    else {
                        "finite_frontier": finite_frontier,
                        "authority": "held-owner-epoch-lock/v1",
                    }
                ),
            },
        )
    except BaseException:
        handle.close()
        raise
    return Lease(directory, target, owner, epoch, handle, expires_at, finite_frontier)


# state-file path -> commit fence; consulted by closer_pipeline._save before replace. Keys are
# per-shard paths and a shard is only ever fenced by its flock holder, so entries never collide;
# registration under a mutex refuses a duplicate instead of overwriting another thread's fence.
SAVE_FENCES: dict[str, Callable[[], None]] = {}
FENCE_IDENTITIES: dict[str, dict[str, Any]] = {}
_FENCE_GUARD = threading.Lock()


def fence_for(path: str | Path) -> Callable[[], None] | None:
    with _FENCE_GUARD:
        return SAVE_FENCES.get(str(path))


def fence_identity(path):
    with _FENCE_GUARD:
        identity = FENCE_IDENTITIES.get(str(path))
        return dict(identity) if identity is not None else None


@contextlib.contextmanager
def fenced(lease: Lease, now: Callable[[], float] = time.time):
    key = str(lease.state_path)
    with _FENCE_GUARD:
        if key in SAVE_FENCES:
            raise StaleLease(f"{lease.target}: shard already fenced by another holder")
        SAVE_FENCES[key] = lambda: lease.check(now)
        FENCE_IDENTITIES[key] = {
            "target": canonical_target(lease.target),
            "owner": lease.owner,
            "epoch": lease.epoch,
        }
    try:
        yield
    finally:
        with _FENCE_GUARD:
            SAVE_FENCES.pop(key, None)
            FENCE_IDENTITIES.pop(key, None)


_UNRESOLVED = {"running", "uncertain"}


def _unresolved_effect(path: Path, target: str, row: Mapping[str, Any]) -> str | None:
    """Mode switches cannot turn an explicit unknown effect into fresh authority."""
    if row.get("status") in _UNRESOLVED:
        return f"{row['status']}_outcome"
    if row.get("status") == "queued" and row.get("stage") == "exception":
        return "unverified_exception_outcome"
    # A scheduler stage/status change cannot discharge an explicitly retained
    # quarantine or a provider's unknown outcome. Both mode directions use this
    # same per-target predicate before admitting any callback.
    barrier = row.get("recovery_barrier")
    if barrier is not None:
        if isinstance(barrier, Mapping):
            if barrier.get("schema") == "closer-unverified-read-barrier/v1":
                return "unverified_read_outcome"
            if barrier.get("schema") == "closer-unverified-act-barrier/v1":
                return "unverified_act_outcome"
        return "unverified_recovery_outcome"
    uncertainty = row.get("provider_outcome_uncertain")
    if uncertainty is not None and uncertainty is not False:
        return "provider_uncertain_outcome"
    if row.get("error") in {
        "expired read deferral lacks a verified terminal resource boundary",
        "expired read retained a historical wait from another revision",
        "interrupted read lacks a verified terminal boundary",
    }:
        return "unverified_read_outcome"
    if row.get("status") != "failed" or row.get("stage") != "act":
        return None
    results = row.get("results")
    act = results.get("act") if isinstance(results, Mapping) else None
    explicit_unknown = row.get("error") == "act returned unsupported or missing status" or (
        isinstance(act, Mapping)
        and (
            act.get("status") in {"unknown", "uncertain", "running", "submitted"}
            or act.get("outcome_uncertain") is True
        )
    )
    if not explicit_unknown:
        return None
    # An existing strict receipt can affirmatively prove a pre-fix no-operation
    # wait. A durable recovery barrier is never discharged by this read-only guard.
    import closer_pipeline as cp

    if cp._legacy_failed_act_wait_proof(path, target, row):
        return None
    return "unverified_act_outcome"


def finite_unresolved(doc):
    """A mode switch cannot replay any unfinished frozen frontier item."""
    from closer_frontier_policy import TERMINAL_STATES, validate_frontier

    entries = doc.get("frontiers", {})
    if not isinstance(entries, dict):
        raise ValueError("malformed finite frontier namespace")
    found = {}
    for frontier_id, entry in entries.items():
        descriptor = validate_frontier(entry.get("finite_frontier"))
        items = entry.get("items")
        if (
            entry.get("schema") != "closer-pipeline-frontier/v2"
            or descriptor["frontier_id"] != frontier_id
            or not isinstance(items, dict)
            or not items
            or not set(items).issubset(descriptor["targets"])
        ):
            raise ValueError("malformed finite frontier history")
        for target, row in items.items():
            if not isinstance(row, dict):
                raise ValueError("malformed finite frontier item")
            act = row.get("results", {}).get("act")
            if (
                row.get("status") not in TERMINAL_STATES
                or row.get("stage_status") == "unknown"
                or row.get("recovery_barrier") is not None
                or row.get("provider_outcome_uncertain") not in (None, False)
                or (
                    isinstance(act, Mapping)
                    and (
                        act.get("status") in {"unknown", "uncertain", "running", "submitted"}
                        or act.get("outcome_uncertain") is True
                    )
                )
            ):
                found[canonical_target(target)] = "finite_frontier_unresolved"
    return found


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
        found.update(finite_unresolved(doc))
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
            found.update(finite_unresolved(doc))
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
def mutation_reservation(
    state_path, target, context, resource_keys, *, owner, deadline, now, scope_check=None
):
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
    with reserve(
        root,
        keys,
        target=target,
        owner=owner,
        deadline=deadline,
        now=now,
        scope_check=scope_check,
    ):
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


def _poll_lock(handle, deadline: float, now, *, shared=False) -> bool:
    limit = min(float(deadline), now() + RESOURCE_WAIT_SECONDS)
    while True:
        try:
            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
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
def _workspace_locks(root, keys, *, deadline, now):
    """Shared ancestor/exclusive leaf locks exclude overlapping writable trees.

    Siblings share only SH ancestor locks, so their actual effects can overlap.
    This namespace is separate from durable intents and their receipt cleanup.
    """
    modes = {}

    def add(path, shared):
        identities = ["path:" + str(path)]
        try:
            stat = path.stat()
        except FileNotFoundError:
            pass
        else:
            identities.append(f"inode:{stat.st_dev}:{stat.st_ino}")
        for identity in identities:
            modes[identity] = modes.get(identity, True) and shared

    for key in keys:
        if not key.startswith("worktree:"):
            continue
        path = Path(key[9:])
        if not path.is_absolute() or path.resolve() != path:
            raise ResourceBlocked("workspace lock requires canonical physical identity")
        for ancestor in path.parents:
            add(ancestor, True)
        add(path, False)
    handles = []
    directory = _reservation_base(Path(root)).parent / "workspace-locks"
    try:
        if modes:
            directory.mkdir(parents=True, exist_ok=True)
        for path, shared in sorted(modes.items()):
            lock = directory / (hashlib.sha256(path.encode()).hexdigest() + ".lock")
            handle = lock.open("a+")
            if not _poll_lock(handle, deadline, now, shared=shared):
                handle.close()
                raise ResourceBlocked(f"workspace {path} overlaps another callback")
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _check_workspace_intents(root, keys, *, legacy_owner=None, target=None):
    """Called under hierarchy locks: a completed callback still retains its intent."""
    import closer_quarantine

    workspaces = [key for key in keys if key.startswith("worktree:")]
    if not workspaces:
        return
    for base in _reservation_bases(Path(root)):
        for directory in sorted(base.iterdir()) if base.is_dir() else []:
            for filename in ("intent.json", "legacy-intent.json"):
                intent = _read_intent(directory / filename)
                if intent is None:
                    continue
                try:
                    key = normalize_key(intent["key"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ResourceBlocked("malformed retained workspace intent") from exc
                if not key.startswith("worktree:"):
                    continue
                path = Path(key[9:])
                if not path.is_absolute() or path.resolve() != path:
                    raise ResourceBlocked("retained workspace intent identity is aliased")
                if not any(
                    closer_quarantine.resource_overlap(key, candidate) for candidate in workspaces
                ):
                    continue
                if (
                    filename == "legacy-intent.json"
                    and legacy_owner is not None
                    and intent.get("owner") == legacy_owner
                    and intent.get("target") == canonical_target(target)
                ):
                    continue
                raise ResourceBlocked(f"workspace {key[9:]} carries an unresolved effect")


def _scope_keys(target, context, resource_keys, workspaces=None, *, infer_workspaces=True):
    keys = default_resource_keys(target)
    if resource_keys is not None:
        extra = resource_keys(target, context)
        if extra is None or isinstance(extra, (str, bytes)):
            raise ResourceBlocked("resource_keys must return an iterable of keys")
        keys.extend(extra)
    normalized = {normalize_key(k) for k in keys}
    # A retained item with incomplete history conservatively fences its repository,
    # including references reached through another PR's source/branch aliases.
    for key in list(normalized):
        kind, value = key.split(":", 1)
        if kind in {"pr", "issue", "branch"}:
            normalized.add(normalize_key("repo:" + value.split("#")[0].split("@")[0]))
    import closer_quarantine

    if infer_workspaces:
        normalized.update(closer_quarantine.current_workspace_keys(target, normalized, workspaces))
    return sorted(normalized)


@contextlib.contextmanager
def scope_locks(root, keys, *, deadline, now):
    """Use the effect locks for read admission without advertising a fictitious effect."""
    keys = list(keys)
    handles = []
    hierarchy = contextlib.ExitStack()
    try:
        hierarchy.enter_context(_workspace_locks(root, keys, deadline=deadline, now=now))
        for key in sorted(set(keys)):
            directory = _key_dir(root, normalize_key(key))
            directory.mkdir(parents=True, exist_ok=True)
            handle = (directory / "lock").open("a+")
            if not _poll_lock(handle, deadline, now):
                handle.close()
                raise ResourceBlocked(f"resource {key} held by another callback")
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        hierarchy.close()


@contextlib.contextmanager
def reserve(
    root: Path,
    keys,
    *,
    target: str,
    owner: str,
    deadline: float,
    now: Callable[[], float] = time.time,
    scope_check=None,
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
    hierarchy = contextlib.ExitStack()
    try:
        hierarchy.enter_context(_workspace_locks(root, ordered, deadline=deadline, now=now))
        for key in ordered:
            directory = _key_dir(root, key)
            directory.mkdir(parents=True, exist_ok=True)
            handle = (directory / "lock").open("a+")
            if not _poll_lock(handle, deadline, now):
                handle.close()
                raise ResourceBlocked(f"resource {key} held by another effect")
            held.append((key, handle, directory / "intent.json"))
        _check_workspace_intents(root, ordered)
        if scope_check is not None:
            scope_check(ordered)
        for key, _handle, path in held:
            for base in _reservation_bases(root):
                for filename in ("intent.json", "legacy-intent.json"):
                    intent = _read_intent(base / path.parent.name / filename)
                    if intent is not None:
                        raise ResourceBlocked(
                            f"resource {key} carries an unresolved effect of {intent.get('target')!r}"
                        )
        writer_pgid = os.getpgrp()
        for key, _handle, path in held:
            if key.split(":", 1)[0] in DURABLE_KINDS:
                _atomic_write(
                    path,
                    {
                        "key": key,
                        "target": canonical_target(target),
                        "owner": owner,
                        "state": "in_flight",
                        # Fence terminal reconciliation against this writer's group.
                        # Detached effect children should update process_group when spawned.
                        "process_group": writer_pgid,
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
                        prior = _read_intent(path) or {}
                        _atomic_write(
                            path,
                            {
                                "key": key,
                                "target": canonical_target(target),
                                "owner": owner,
                                "state": "unknown",
                                "process_group": prior.get("process_group", os.getpgrp()),
                                "at": now(),
                            },
                        )
        raise
    finally:
        for _key, handle, _path in reversed(held):
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        hierarchy.close()


@contextlib.contextmanager
def legacy_reserve(root, keys, *, target, owner, deadline, now=time.time, causal_binding=None):
    """Advertise a cooperating legacy executor under its existing JSON/runlock protocol.

    This is not a Core operation intent. The same live legacy executor may perform its
    next guarded operation while its durable legacy ledger advances. Other executors,
    including scoped ones, cannot enter. A crash or invalid stop retains the advertisement.
    Core intents are never adopted, including those with the same target/owner.
    """
    if not isinstance(causal_binding, dict) or not (
        causal_binding.get("pipeline_run_id")
        or (causal_binding.get("native_state_path") and causal_binding.get("operation_id"))
    ):
        raise ResourceBlocked("legacy_reservation_requires_causal_binding")
    held = []
    ordered = sorted({normalize_key(k) for k in keys})
    hierarchy = contextlib.ExitStack()
    try:
        hierarchy.enter_context(_workspace_locks(root, ordered, deadline=deadline, now=now))
        for key in ordered:
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
        _check_workspace_intents(root, ordered, legacy_owner=owner, target=target)
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
                        **({"causal_binding": dict(causal_binding)} if causal_binding else {}),
                    },
                )
        yield
    finally:
        for _key, handle, _directory in reversed(held):
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        hierarchy.close()


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

    def retain(key: str, target: str) -> None:
        # Both intent kinds can survive in both reservation roots. Keep every
        # advertisement so receipt counts cannot mistake a partial drain for a
        # cleared latch; unreadable intents need the same collision handling.
        while key in found:
            key = "historical:" + key
        found[key] = target

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
                retain(f"unreadable:{directory.name}:{filename}", "?")
                continue
            if intent is not None:
                retain(str(intent.get("key")), str(intent.get("target")))
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
                try:
                    intent = _read_intent(directory / "intent.json")
                except ResourceBlocked:
                    # Preserve and report this unknown via unknown_reservations;
                    # it cannot prevent releasing separately proven owned keys.
                    continue
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


def _release_known_shard_boundary(
    root, state_path, target, *, owner, now, run_id=None, progress=False, finite_frontier=None
):
    """Drain only this executor's intent after its durable known target boundary.

    The target lease remains held. Unknown outcomes and quarantines never gain
    authority from a callback's return value or an unverified `validated` flag.
    """
    import closer_pipeline as cp

    try:
        if finite_frontier is not None:
            if progress or run_id is None:
                return False
            if canonical_target(target) in finite_unresolved(cp._load(state_path)):
                return False
            verdict = cp.validate_stop(
                state_path,
                [target],
                now=now,
                expected_run_id=run_id,
                finite_frontier=finite_frontier,
                frontier_target=target,
            )
            if verdict.get("valid") is not True or verdict.get("run_id") != run_id:
                return False
            release_target(root, target, owner=owner)
            return True
        doc = cp._load(state_path)
        current_id, run = max(doc["runs"].items(), key=lambda pair: pair[1]["created"])
        if run_id is not None and current_id != run_id:
            return False
        if run.get("assignment") != [target]:
            return False
        row = run["targets"][target]
        if (
            row.get("recovery_barrier") is not None
            or row.get("error") is not None
            or (
                row.get("provider_outcome_uncertain") is not None
                and row.get("provider_outcome_uncertain") is not False
            )
            or row.get("stage") == "exception"
            or row.get("status") in {"uncertain", "failed", "parked"}
        ):
            return False
        act = row.get("results", {}).get("act")
        if isinstance(act, Mapping) and (
            act.get("status") in {"unknown", "uncertain", "running", "submitted"}
            or act.get("outcome_uncertain") is True
        ):
            return False
        if progress:
            if not (
                row.get("stage") == "prepare"
                and row.get("status") in {"queued", "running"}
                and row.get("known_completed_action") is True
                and isinstance(act, Mapping)
                and act.get("status") == "completed"
                and isinstance(row.get("progress_key"), str)
                and row["progress_key"]
                and act.get("progress_key") == row["progress_key"]
            ):
                return False
        else:
            if _unresolved_effect(Path(state_path), target, row) is not None:
                return False
            if row.get("status") not in {"completed", "pending", "deferred"}:
                return False
            verdict = cp._validate_stop_legacy(state_path, [target], now=now)
            if verdict.get("run_id") != current_id or verdict.get("valid") is not True:
                return False
        release_target(root, target, owner=owner)
        return True
    except (OSError, ValueError, TypeError, KeyError, ResourceBlocked):
        return False


# --------------------------------------------------------------------------- shared pools

_pool_slot_local = threading.local()


def _pool_slots():
    slots = getattr(_pool_slot_local, "slots", None)
    if slots is None:
        slots = []
        _pool_slot_local.slots = slots
    return slots


class _FiniteExecutionContext:
    """Same-process borrow of an original request and already-held pool slots."""

    def __init__(self, target, frontier, window, monotonic_limit, slots):
        self.pid = os.getpid()
        self.target = target
        self.frontier = frontier
        self.window = window
        self.monotonic_limit = monotonic_limit
        self.slots = tuple(slots)


def _validate_execution_slots(slots, target, frontier):
    for item in slots:
        record, handle = item["record"], item["handle"]
        if (
            handle.closed
            or canonical_target(record.get("target", "")) != canonical_target(target)
            or record.get("frontier_id") != frontier["frontier_id"]
        ):
            raise ResourceBlocked("finite executor pool ownership differs from target/frontier")


def capture_finite_execution_context(target, frontier):
    """Capture references only; never renew a window or acquire another lease."""
    from closer_frontier_policy import validate_frontier

    frontier = validate_frontier(frontier)
    if target not in frontier["targets"]:
        raise ResourceBlocked("finite executor target is outside the frontier")
    slots = tuple(_pool_slots())
    window = getattr(_INVENTORY_REQUEST, "window", None)
    limit = getattr(_INVENTORY_REQUEST, "monotonic_limit", None)
    if slots and window is None:
        raise ResourceBlocked("finite executor pool has no original request window")
    if window is not None:
        finite_request_remaining()  # validate the original clocks without extending them
    _validate_execution_slots(slots, target, frontier)
    return _FiniteExecutionContext(target, frontier, window, limit, slots)


@contextlib.contextmanager
def finite_execution_context(context, *, target, frontier, deadline, now=time.time):
    """Restore exact request authority in one joined queue worker, then restore its TLS.

    Held handles remain owned by the waiting coordinator. This context never
    closes/unlocks them, claims capacity, or publishes lease ownership.
    """
    from closer_frontier_policy import validate_frontier

    frontier = validate_frontier(frontier)
    if (
        not isinstance(context, _FiniteExecutionContext)
        or context.pid != os.getpid()
        or context.target != target
        or context.frontier != frontier
    ):
        raise ResourceBlocked("finite executor context target/frontier/process differs")
    _validate_execution_slots(context.slots, target, frontier)
    previous_window = getattr(_INVENTORY_REQUEST, "window", None)
    previous_limit = getattr(_INVENTORY_REQUEST, "monotonic_limit", None)
    previous_slots = getattr(_pool_slot_local, "slots", None)
    if previous_slots and (
        len(previous_slots) != len(context.slots)
        or any(a is not b for a, b in zip(previous_slots, context.slots))
    ):
        raise ResourceBlocked("finite executor thread already holds another pool context")
    restored_window = context.window
    restored_limit = context.monotonic_limit
    if previous_window is not None:
        finite_request_remaining()  # validate the worker's already-active bound
        if restored_window is None:
            restored_window, restored_limit = previous_window, previous_limit
        else:
            if restored_window[1] is not previous_window[1]:
                raise ResourceBlocked("finite executor cannot replace another request clock")
            restored_window = (min(restored_window[0], previous_window[0]), restored_window[1])
            restored_limit = min(restored_limit, previous_limit)
    borrowed = []
    try:
        for item in sorted(context.slots, key=id):
            lock = item.setdefault("executor_borrow_lock", threading.Lock())
            if not lock.acquire(blocking=False):
                raise ResourceBlocked("finite executor pool is already borrowed")
            borrowed.append(lock)
        # A closed/rebound owner cannot race capture into a new worker.
        _validate_execution_slots(context.slots, target, frontier)
        _INVENTORY_REQUEST.window = restored_window
        _INVENTORY_REQUEST.monotonic_limit = restored_limit
        _pool_slot_local.slots = list(context.slots)  # same item/record/FD references
        original_now = restored_window[1] if restored_window is not None else now
        with inventory_request(deadline, now=original_now):
            yield
    finally:
        _INVENTORY_REQUEST.window = previous_window
        _INVENTORY_REQUEST.monotonic_limit = previous_limit
        _pool_slot_local.slots = previous_slots
        for lock in reversed(borrowed):
            lock.release()


def _write_pool_record(handle, record):
    """Atomically publish metadata beside the stable inode holding the flock."""
    record_path = Path(f"{handle.name}.json")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{record_path.name}.", suffix=".tmp", dir=record_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, record_path)
        _fsync_dir(record_path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def finite_pool_prepare_request(observation):
    """Persist a fresh request intent before each owned transport is spawned."""
    if not isinstance(observation, Mapping) or not all(
        isinstance(observation.get(key), str) and observation.get(key)
        for key in ("run_id", "agent", "profile_id", "finite_frontier_id", "finite_frontier_target")
    ):
        raise ResourceBlocked("finite request prelaunch identity is incomplete")
    active = _pool_slots()
    if not active:
        raise ResourceBlocked("finite request has no held pool reservation")
    # Validate the whole held set before changing any journal. A request with
    # an assigned run_id may already have spawned without a recorded birth.
    for item in active:
        record = item["record"]
        if (
            record.get("frontier_id") != observation["finite_frontier_id"]
            or record.get("target") != observation["finite_frontier_target"]
            or record.get("status") not in {"starting", "resolved"}
            or (record.get("status") == "starting" and record.get("run_id") is not None)
        ):
            raise ResourceBlocked("finite request does not match a reusable held pool reservation")
    for item in active:
        record = item["record"]
        record.update(
            {
                "status": "starting",
                "run_id": observation["run_id"],
                "agent": observation["agent"],
                "profile_id": observation["profile_id"],
                "process_group": None,
                "process_birth": None,
                "request_started_at": time.time(),
            }
        )
        _write_pool_record(item["handle"], record)


def finite_pool_process_started(observation):
    """Durably bind all active finite pool reservations to the exact observed child."""
    if not isinstance(observation, Mapping) or observation.get("source") not in {
        "trusted-popen-transport/v1",
        "trusted-bounded-run/v1",
        "trusted-artifact-runner/v1",
    }:
        raise ResourceBlocked("finite pool process start lacks trusted transport identity")
    pgid = observation.get("process_group") or observation.get("pid")
    if (
        type(pgid) is not int
        or pgid <= 1
        or observation.get("pid") != pgid
        or not all(
            isinstance(observation.get(key), str) and observation.get(key)
            for key in ("run_id", "agent", "profile_id", "os_started")
        )
    ):
        raise ResourceBlocked("finite pool process start identity is incomplete")
    changed = 0
    for item in _pool_slots():
        record = item["record"]
        if record.get("status") != "starting":
            continue
        if record.get("run_id") != observation.get("run_id"):
            continue
        if record.get("agent") != observation.get("agent") or record.get(
            "profile_id"
        ) != observation.get("profile_id"):
            continue
        if observation.get("finite_frontier_id", record.get("frontier_id")) != record.get(
            "frontier_id"
        ):
            continue
        if observation.get("finite_frontier_target", record.get("target")) != record.get("target"):
            continue
        record.update(
            {
                "status": "uncertain",
                "process_group": pgid,
                "run_id": observation["run_id"],
                "agent": observation["agent"],
                "profile_id": observation["profile_id"],
                **(
                    {"process_birth": observation["os_started"]}
                    if observation.get("os_started")
                    else {}
                ),
            }
        )
        _write_pool_record(item["handle"], record)
        changed += 1
    if not changed:
        raise ResourceBlocked("finite provider spawned without a matching durable pool reservation")


def finite_pool_process_finished(observation):
    """Release durable reservation only after positive proof of the original group's absence."""
    pgid = observation.get("process_group") or observation.get("pid")
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise ResourceBlocked("finite process-group termination could not be verified") from exc
    else:
        raise ResourceBlocked("finite process group is still present")
    matched = 0
    for item in _pool_slots():
        record = item["record"]
        if record.get("status") not in {"starting", "uncertain"}:
            continue
        if record.get("status") == "uncertain" and record.get("process_group") != pgid:
            continue
        if record.get("status") == "starting" and record.get("process_group") is not None:
            continue
        if record.get("run_id") != observation.get("run_id"):
            continue
        if record.get("agent") != observation.get("agent") or record.get(
            "profile_id"
        ) != observation.get("profile_id"):
            continue
        if record.get("frontier_id") != observation.get("finite_frontier_id") or record.get(
            "target"
        ) != observation.get("finite_frontier_target"):
            raise ResourceBlocked("finite pool completion does not match original run")
        record.update(
            {
                "status": "resolved",
                "process_group": pgid,
                "run_id": observation.get("run_id"),
                "agent": observation.get("agent", record.get("agent")),
                "profile_id": observation.get("profile_id", record.get("profile_id")),
                "resolved_at": time.time(),
                "resolution": "process_group_absent",
            }
        )
        _write_pool_record(item["handle"], record)
        matched += 1
    if not matched:
        raise ResourceBlocked("finite process completion has no matching pool reservation")


def finite_pool_has_active_reservations():
    return bool(_pool_slots())


def finite_pool_current_binding():
    active = _pool_slots()
    if not active:
        return None
    records = [item["record"] for item in active]
    frontiers = {record.get("frontier_id") for record in records}
    targets = {record.get("target") for record in records}
    if len(frontiers) != 1 or len(targets) != 1:
        raise ResourceBlocked("active finite pool reservations disagree on target binding")
    return {"frontier_id": next(iter(frontiers)), "target": next(iter(targets))}


def finite_pool_no_start(observation=None):
    """Clear only current-thread reservations still in prelaunch state."""
    for item in _pool_slots():
        record = item["record"]
        if record.get("status") == "starting" and (
            (observation is None and record.get("run_id") is None)
            or (
                isinstance(observation, Mapping)
                and record.get("run_id") == observation.get("run_id")
                and record.get("frontier_id") == observation.get("finite_frontier_id")
                and record.get("target") == observation.get("finite_frontier_target")
            )
        ):
            record.update(
                {"status": "resolved", "resolved_at": time.time(), "resolution": "trusted_no_spawn"}
            )
            _write_pool_record(item["handle"], record)


@contextlib.contextmanager
def pool_slot(
    root: Path,
    lane: str,
    bound: int,
    *,
    deadline: float,
    now=time.time,
    finite_frontier: bool = False,
    reservation: Mapping[str, str] | None = None,
):
    """One of ``bound`` process-wide slots for ``lane`` (flock files shared by every process).

    Every coordinator must pass the same bound for a lane; the bounds are the existing
    ``max_*_workers`` arguments, not a new budget. ``ResourceBlocked``: no slot before the deadline.
    """
    directory = root / "pools" / lane
    directory.mkdir(parents=True, exist_ok=True)
    limit = min(float(deadline), now() + RESOURCE_WAIT_SECONDS)

    def read_fence(handle):
        record_path = Path(f"{handle.name}.json")
        if not record_path.exists():
            handle.seek(0)
            if handle.read():
                raise ResourceBlocked(f"{lane} pool lock contains an unsupported inline record")
            return None
        try:
            fence = json.loads(record_path.read_text(encoding="utf-8"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ResourceBlocked(f"{lane} pool slot has unreadable uncertainty fence") from exc
        if not isinstance(fence, dict) or fence.get("schema") != "finite-pool-uncertainty/v1":
            raise ResourceBlocked(f"{lane} pool slot has unsupported uncertainty fence")
        return fence

    def group_absent(pgid):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except OSError:
            return False
        return False

    def slot_available(candidate):
        try:
            fence = read_fence(candidate)
            if not fence:
                return True
            if fence.get("status") == "resolved":
                return True
            pgid = fence.get("process_group")
            if (
                fence.get("status") != "uncertain"
                or type(pgid) is not int
                or pgid <= 1
                or fence.get("lane") != lane
                or not all(
                    isinstance(fence.get(key), str) and fence.get(key)
                    for key in (
                        "run_id",
                        "agent",
                        "profile_id",
                        "target",
                        "frontier_id",
                        "process_birth",
                    )
                )
                or not group_absent(pgid)
            ):
                return False
            fence.update(
                {"status": "resolved", "resolved_at": now(), "resolution": "process_group_absent"}
            )
            _write_pool_record(candidate, fence)
            return True
        except (OSError, ResourceBlocked, TypeError, ValueError):
            return False

    handle = None
    while handle is None:
        for index in range(bound):
            candidate = (directory / f"slot-{index}").open("a+")
            try:
                fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                if slot_available(candidate):
                    handle = candidate
                    break
                fcntl.flock(candidate.fileno(), fcntl.LOCK_UN)
            except BlockingIOError:
                pass
            finally:
                if candidate is not handle:
                    candidate.close()
        if handle is None:
            if now() >= limit:
                raise ResourceBlocked(f"no {lane} pool slot before the deadline")
            time.sleep(_POLL_SECONDS)

    try:
        if finite_frontier:
            required = ("reservation_id", "target", "frontier_id", "agent", "profile_id")
            if not isinstance(reservation, Mapping) or not all(
                isinstance(reservation.get(key), str) and reservation.get(key) for key in required
            ):
                raise ResourceBlocked("finite pool slot requires an exact prelaunch reservation")
            starting = {
                "schema": "finite-pool-uncertainty/v1",
                "status": "starting",
                "lane": lane,
                **{key: reservation[key] for key in required},
                "process_group": None,
                "run_id": None,
                "process_birth": None,
                "observed_at": now(),
            }
            _write_pool_record(handle, starting)
            item = {"handle": handle, "record": starting}
            _pool_slots().append(item)
            try:
                yield
            finally:
                with contextlib.suppress(ValueError):
                    _pool_slots().remove(item)
        else:
            yield
    except BaseException as exc:
        fence = getattr(exc, "finite_pool_fence", None)
        if finite_frontier and isinstance(fence, dict):
            pgid = fence.get("process_group")
            if (
                type(pgid) is not int
                or pgid <= 1
                or fence.get("cleanup_terminal") is not False
                or not all(
                    isinstance(fence.get(key), str) and fence.get(key)
                    for key in (
                        "run_id",
                        "agent",
                        "profile_id",
                        "target",
                        "frontier_id",
                        "process_birth",
                    )
                )
            ):
                raise ResourceBlocked(
                    f"{lane} pool slot uncertainty lacks exact cleanup evidence"
                ) from exc
            saved = {
                "schema": "finite-pool-uncertainty/v1",
                "status": "uncertain",
                "lane": lane,
                "process_group": pgid,
                "run_id": fence.get("run_id"),
                "agent": fence.get("agent"),
                "profile_id": fence.get("profile_id"),
                "target": fence.get("target"),
                "process_birth": fence.get("process_birth"),
                "frontier_id": fence.get("frontier_id"),
                "observed_at": now(),
                "cleanup_error": fence.get("cleanup_error"),
            }
            _write_pool_record(handle, saved)
        raise
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# --------------------------------------------------------------------------- aggregate receipt


def _round_dir(root: Path) -> Path:
    return root / "rounds"


def valid_scope_refusal(state_path, target, item, run_id):
    """Validate a named, immutable pre-callback refusal, never a provider outcome."""
    try:
        state = Path(state_path).resolve()
        ref = item["scope_proof"]
        path = Path(ref["path"])
        # Scoped shards have a fixed root/targets/<identity>/state.json layout.
        base = state.parents[2] / "scope-admissions"
        if path.is_symlink() or not path.resolve().is_relative_to(base):
            return False
        raw = path.read_bytes()
        if len(raw) > 4 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            return False
        proof = json.loads(raw)
        binding = proof["binding"]
        snapshot = proof["snapshot"]
        expected_hash = snapshot["snapshot_sha256"]
        value = {k: v for k, v in snapshot.items() if k != "snapshot_sha256"}
        actual_hash = hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        return (
            proof.get("schema") == "closer-scoped-admission/v1"
            and proof.get("admitted") is False
            and proof.get("callback_started") is False
            and proof.get("legacy_stop_authority") is False
            and bool(proof.get("reason"))
            and proof.get("target") == target
            and proof.get("stage") == item.get("stage")
            and Path(binding.get("state_path", "")).resolve() == state
            and binding.get("pipeline_run_id") == run_id
            and all(binding.get(k) == item.get(k) for k in ("revision", "identity", "work_key"))
            and snapshot.get("schema") == "closer-quarantine-scope/v1"
            and expected_hash == actual_hash
            and proof.get("input_sha256")
            == hashlib.sha256(
                json.dumps(
                    item.get("results", {}),
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest()
            and item.get("recovery_barrier") is None
            and item.get("provider_outcome_uncertain") in (None, False)
            and item.get("stage") != "reconcile"
        )
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        return False


def valid_initial_scope_refusal(state_path, target, item, round_id):
    """Positive gate evidence that this target never entered a pipeline shard."""
    try:
        ref = item["scope_proof"]
        path = Path(ref["path"])
        base = scoped_root(Path(state_path).resolve()) / "scope-admissions" / round_id
        if path.is_symlink() or path.resolve().parent != base:
            return False
        raw = path.read_bytes()
        if len(raw) > 4 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            return False
        proof = json.loads(raw)
        snapshot = proof["snapshot"]
        digest = hashlib.sha256(
            json.dumps(
                {k: v for k, v in snapshot.items() if k != "snapshot_sha256"},
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        return (
            item.get("scoped_fence") is True
            and item.get("deferred_reason") == "scope_quarantined"
            and item.get("results") == {}
            and proof.get("schema") == "closer-scoped-admission/v1"
            and proof.get("round_id") == round_id
            and proof.get("target") == target
            and proof.get("stage") == "target_admission"
            and proof.get("callback_started") is False
            and proof.get("admitted") is False
            and bool(proof.get("reason"))
            and proof.get("legacy_stop_authority") is False
            and snapshot.get("snapshot_sha256") == digest
            and snapshot.get("acceptance_authority") is False
            and snapshot.get("global_stop_authority") is False
            and Path(snapshot["state_path"]).resolve() == Path(state_path).resolve()
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


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


ROUND_RECEIPT_MAX_BYTES = 32 * 1024 * 1024


def read_round_receipt(path: Path) -> tuple[dict[str, Any], str]:
    """Read one regular immutable receipt, hashing exactly the bounded parsed bytes."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("no-follow receipt reads unavailable")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > ROUND_RECEIPT_MAX_BYTES:
            raise ValueError("round receipt exceeds regular-file bound")
        raw = stream.read(ROUND_RECEIPT_MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if (
        len(raw) > ROUND_RECEIPT_MAX_BYTES
        or len(raw) != before.st_size
        or any(getattr(before, key) != getattr(after, key) for key in fields)
        or any(getattr(after, key) != getattr(current, key) for key in fields)
        or not stat.S_ISREG(current.st_mode)
    ):
        raise ValueError("round receipt changed during bounded read")
    receipt = json.loads(raw)
    if not isinstance(receipt, dict):
        raise ValueError("round receipt is not an object")
    return receipt, hashlib.sha256(raw).hexdigest()


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
        receipt, receipt_hash = read_round_receipt(path)
        if (
            receipt.get("schema") != "closer-pipeline-receipt/v1"
            or receipt.get("mode") != "resource_scoped"
            or receipt.get("round_id") != round_id
        ):
            raise ValueError("round identity mismatch")
        assignment = receipt["assignment"]
        run_ids = receipt["run_ids"]
        rows = receipt["targets"]
        frontier = receipt.get("finite_frontier")
        if frontier is not None:
            from closer_frontier_policy import validate_frontier

            frontier = validate_frontier(frontier)
            if sorted(frontier["targets"]) != assignment:
                raise ValueError("finite round assignment differs from frozen frontier")
        if (
            not isinstance(assignment, list)
            or not assignment
            or not isinstance(run_ids, dict)
            or not isinstance(rows, dict)
            or any(not isinstance(target, str) for target in assignment)
            or len(set(assignment)) != len(assignment)
        ):
            raise ValueError("empty or malformed assignment accounting")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"valid": False, "stop_valid": False, "reasons": [f"invalid_round_receipt:{exc}"]}
    reasons: list[str] = []
    if assignment_targets is not None and sorted(assignment_targets) != sorted(assignment):
        reasons.append("assignment_changed")
    if set(rows) != set(assignment):
        reasons.append("assignment_not_fully_accounted")
    continuation = False
    admitted_reasons = []
    held_targets = []
    unsettled_rows = []
    containment_accounted = (
        set(rows) == set(assignment)
        and set(run_ids).issubset(set(assignment))
        and (assignment_targets is None or sorted(assignment_targets) == sorted(assignment))
    )
    for target in assignment:
        run_id = run_ids.get(target)
        if not run_id:
            row = rows.get(target) or {}
            reasons.append(f"fenced_target:{target}:{row.get('deferred_reason', 'no_run')}")
            held_targets.append(target)
            continuation = True
            containment_accounted = containment_accounted and valid_initial_scope_refusal(
                state_path, target, row, round_id
            )
            continue
        shard_state = shard_dir(root, target) / "state.json"
        try:
            doc = cp._load(shard_state)
            admitted_row = (
                doc["runs"][run_id]["targets"][target]
                if frontier is None
                else doc["frontiers"][frontier["frontier_id"]]["items"][target]
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            reasons.append(f"unreadable_shard_run:{target}:{type(exc).__name__}")
            admitted_reasons.append(reasons[-1])
            containment_accounted = False
            continue
        unresolved = _unresolved_effect(shard_state, target, admitted_row)
        if frontier is not None and canonical_target(target) in finite_unresolved(doc):
            unresolved = "finite_unknown_outcome"
        if unresolved:
            admitted_reasons.append(f"unresolved_admitted_outcome:{target}:{unresolved}")
        # Native receipts bind their original shard location. Select this exact
        # recorded run in place, never relocate its proof or substitute latest.
        verdict = (
            cp._validate_stop_legacy(shard_state, [target], now=now, expected_run_id=run_id)
            if frontier is None
            else cp.validate_stop(
                shard_state,
                [target],
                now=now,
                expected_run_id=run_id,
                finite_frontier=frontier,
                frontier_target=target,
            )
        )
        if verdict.get("run_id") != run_id:
            reasons.append(f"run_binding_lost:{target}")
            admitted_reasons.append(reasons[-1])
        if not verdict.get("valid"):
            invalid = [f"{target}:{r}" for r in verdict.get("reasons", ["invalid"])]
            reasons.extend(invalid)
            admitted_reasons.extend(invalid)
        if unresolved or verdict.get("run_id") != run_id or not verdict.get("valid"):
            # A later mutation of a formerly known result is not evidence that
            # this named terminal round contained its effects. Bind the exact
            # row captured by the immutable aggregate, then require independent
            # census coverage below; no uncertainty is discharged here.
            containment_accounted = containment_accounted and rows.get(target) == admitted_row
            unsettled_rows.append((target, shard_state, run_id, admitted_row))
        continuation = continuation or bool(verdict.get("continuation_required"))
    scoped_completion_valid = not reasons
    unknown = unknown_reservations(root)
    if unknown:
        reasons.append("unresolved_resource_reservations")
        continuation = True
    import closer_quarantine

    retained = closer_quarantine.scope_snapshot(state_path)
    if retained.get("global_reasons") or retained.get("quarantine_keys"):
        reasons.append("retained_historical_quarantine")
        continuation = True
    quarantined_admitted_items = []
    for target, source, run_id, row in unsettled_rows:
        try:
            item_hash = closer_quarantine._digest(row)
            matches = [
                item
                for item in retained.get("items", [])
                if item.get("source_path") == str(source)
                and item.get("run_id") == run_id
                and item.get("target") == canonical_target(target).lower()
                and item.get("item_sha256") == item_hash
                and item.get("keys")
            ]
        except (TypeError, ValueError):
            matches = []
        if len(matches) != 1:
            containment_accounted = False
        else:
            item = matches[0]
            # File hashes may change when disjoint rows are committed. The
            # regenerated census still verifies those files; the stable proof
            # binds this exact retained row and its conflict closure instead.
            quarantined_admitted_items.append(
                {
                    key: item[key]
                    for key in ("source_path", "run_id", "target", "item_sha256", "keys")
                }
            )
    valid = not reasons
    return {
        "valid": valid,
        "stop_valid": valid,
        "global_stop_valid": valid,
        "scoped_completion_valid": scoped_completion_valid,
        "admitted_domain_valid": bool(run_ids)
        and not admitted_reasons
        and set(rows) == set(assignment)
        and set(run_ids) == set(assignment) - set(held_targets)
        and (assignment_targets is None or sorted(assignment_targets) == sorted(assignment)),
        "admitted_domain_reasons": admitted_reasons,
        "scoped_containment_valid": bool(unsettled_rows)
        and containment_accounted
        and not retained.get("global_reasons")
        and len(quarantined_admitted_items) == len(unsettled_rows),
        "quarantined_admitted_items": quarantined_admitted_items,
        "not_started_domain_valid": not run_ids
        and set(rows) == set(assignment)
        and (assignment_targets is None or sorted(assignment_targets) == sorted(assignment))
        and all(
            valid_initial_scope_refusal(state_path, target, rows[target], round_id)
            for target in assignment
        ),
        "held_targets": held_targets,
        "historical_quarantine": retained,
        "round_id": round_id,
        "receipt_sha256": receipt_hash,
        "run_ids": dict(run_ids),
        "continuation_required": continuation,
        **(
            {"deadline": receipt["deadline"]}
            if "finite_frontier" not in receipt and "deadline" in receipt
            else {}
        ),
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
    deadline: float | None = None,
    finite_frontier=None,
    request_timeout_seconds: float = 120.0,
    cancelled_fn=None,
    paused_fn=None,
    priority_targets=(),
    max_io_workers: int = 4,
    max_low_workers: int = 2,
    max_exception_workers: int = 1,
    max_target_workers: int | None = None,
    round_deadline_exception=None,
    round_capacity_exception=None,
    resource_keys=None,
    pool_lease=None,
    coordination_admission=None,
    quarantine_workspaces=None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Resource-scoped coordinator: one lease and one state shard per target.

    Each target runs the unchanged pipeline implementation against its own shard.
    The whole-target lease is held across agent work; callbacks run with no shared lock held.
    ``resource_keys(target, context) -> iterable of 'kind:value'`` names what ``act`` mutates
    (the PR, its source issue, branch, worktree); the target's own PR key and its repository's
    short serialisation key are always added. Reasoning takes alias locks and a fresh
    quarantine proof without writing effect intents. Registered native read stages omit
    inferred checkout claims; authenticated deterministic interpretation uses IO capacity.
    Only ``act`` writes intents.
    ``pool_lease(lane, target, context)``
    may return a context manager (the root's profile-pool reservation); without it the lane
    bounds are enforced across processes by ``pool_slot``.
    """
    from concurrent.futures import ThreadPoolExecutor

    import closer_pipeline as cp

    raw_list = list(targets)
    legacy_deadline: float | None = None
    if finite_frontier is not None:
        cp._require_dispatcher_finite_callbacks(
            {
                "prepare": prepare,
                "interpret": interpret,
                "exception": exception,
                "act": act,
                "reconcile": reconcile,
            }
        )
        from closer_frontier_policy import request_window, validate_frontier

        finite_frontier = validate_frontier(finite_frontier, targets=raw_list)
        request_window(now(), request_timeout_seconds)
        if deadline is not None:
            raise ValueError("finite frontier cannot inherit an aggregate deadline")
    else:
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
            raise ValueError("legacy scoped execution requires a finite deadline")
        legacy_deadline = float(deadline)
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
    if max_target_workers is not None and (
        isinstance(max_target_workers, bool)
        or not isinstance(max_target_workers, int)
        or max_target_workers < 1
    ):
        raise ValueError("target worker ceiling must be a positive integer")
    if resource_keys is not None and not callable(resource_keys):
        raise ValueError("resource_keys must be callable")

    started = now()
    root = open_root(
        state_path,
        pipeline_schema=cp.SCHEMA,
        pin=contract_pin,
        deadline=now() + request_timeout_seconds if finite_frontier is not None else None,
        now=now,
    )
    owner = f"{os.getpid()}:{threading.get_ident()}:{started}"
    round_id = hashlib.sha256(
        json.dumps([sorted(canonical), started, owner], sort_keys=True).encode()
    ).hexdigest()[:32]

    def callback_deadline(context):
        if finite_frontier is None:
            return deadline
        # The pipeline supplies ONE original request window for this callback.
        # Waiting for its pool/alias claims consumes that same finite window.
        value = context.get("deadline")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ResourceBlocked("finite callback has no bounded request window")
        from closer_frontier_policy import request_is_live

        if not request_is_live({"deadline": value}, now()):
            raise ResourceBlocked("finite callback request window expired")
        if (cancelled_fn and cancelled_fn()) or (paused_fn and paused_fn()):
            raise ResourceBlocked("finite callback cancelled or paused")
        return value

    def native_remote_stage(target, context, stage):
        # The shared native helper authenticates original callback identities.
        # A caller-supplied callback or proposal cannot opt into reduced scope.
        import closer_dispatcher

        callback = {"prepare": prepare, "interpret": interpret}.get(stage)
        return closer_dispatcher.native_stage_remote_only(target, context, stage, callback)

    def check_scope(
        target,
        stage,
        keys,
        context=None,
        refusal=None,
        request_deadline=None,
        native_remote_expected=False,
    ):
        if finite_frontier is not None and context is not None:
            request_deadline = callback_deadline(context)
        with inventory_lock(root, deadline=request_deadline, now=now):
            return check_scope_locked(target, stage, keys, context, refusal, native_remote_expected)

    def remote_act(target, context):
        # Finite entry already requires the exact registered production callbacks.
        # Revalidate both before reservation and under inventory/resource locks.
        if finite_frontier is None:
            return False
        capability = getattr(act, "__globals__", {}).get("_remote_only_effect")
        return callable(capability) and capability(target, context) is True

    def check_scope_locked(
        target, stage, keys, context=None, refusal=None, native_remote_expected=False
    ):
        import closer_quarantine

        purpose = (
            "observation"
            if finite_frontier is not None and stage in {"target_admission", "prepare", "interpret"}
            else "effect"
        )
        if native_remote_expected:
            purpose = "remote_observation"
        if stage == "act" and context is not None and remote_act(target, context):
            purpose = "remote_effect"
        snapshot = closer_quarantine.scope_snapshot(state_path, workspaces=quarantine_workspaces)
        problem = closer_quarantine.scope_problem(snapshot, target, keys, purpose=purpose)
        # Unrelated shards can commit while we hold our aliases. Retry only the
        # read-only census, never a callback, when that invalidates its hash.
        for _ in range(2):
            if problem != "scope_sources_changed":
                break
            snapshot = closer_quarantine.scope_snapshot(
                state_path, workspaces=quarantine_workspaces
            )
            problem = closer_quarantine.scope_problem(snapshot, target, keys, purpose=purpose)
        if native_remote_expected and not native_remote_stage(
            target, context or {}, "prepare" if stage == "target_admission" else stage
        ):
            problem = "native_remote_contract_changed"
        problem = refusal or problem
        if (
            problem is None
            and resource_keys is None
            and stage not in {"prepare", "target_admission"}
            and snapshot.get("quarantine_keys")
        ):
            problem = "complete_prepared_resource_inventory_required"
        # Retain the exact source census independently of the legacy ledger. A
        # disjoint admission never changes that ledger's stop verdict or deadline.
        directory = root / "scope-admissions" / round_id
        directory.mkdir(parents=True, exist_ok=True)
        proof = {
            "schema": "closer-scoped-admission/v1",
            "round_id": round_id,
            "target": target,
            "stage": stage,
            "purpose": purpose,
            "historical_overlaps": {
                "candidate_keys": sorted(
                    key
                    for key in keys
                    if any(
                        closer_quarantine.resource_overlap(key, held)
                        for held in snapshot["quarantine_keys"]
                    )
                ),
                "quarantine_keys": sorted(
                    held
                    for held in snapshot["quarantine_keys"]
                    if any(closer_quarantine.resource_overlap(key, held) for key in keys)
                ),
            },
            "keys": keys,
            "snapshot": snapshot,
            "admitted": problem is None,
            "reason": problem,
            "observed_at": now(),
            "legacy_stop_authority": False,
            "callback_started": False,
            "input_sha256": hashlib.sha256(
                json.dumps(
                    {k: (context or {})[k] for k in cp.STAGES if k in (context or {})},
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest(),
            "binding": {
                k: (context or {}).get(k)
                for k in ("state_path", "pipeline_run_id", "revision", "identity", "work_key")
            },
        }
        name = hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()
        _atomic_write(directory / (name + ".json"), proof)
        if problem:
            raise ScopeBlocked(
                problem,
                {
                    "path": str(directory / (name + ".json")),
                    "sha256": hashlib.sha256(
                        (directory / (name + ".json")).read_bytes()
                    ).hexdigest(),
                },
            )

    def scope_guard(fn, stage):
        def call(target, ctx):
            # Preparation is native read-only discovery. Its initial gate fences
            # the target/repository; its returned immutable packet supplies all
            # aliases before any model, effect, or reconciliation callback.
            remote = native_remote_stage(target, ctx, stage)
            keys = _scope_keys(
                target,
                ctx,
                None if stage == "prepare" else resource_keys,
                quarantine_workspaces,
                infer_workspaces=not remote,
            )
            with contextlib.ExitStack() as stack:
                try:
                    stack.enter_context(
                        scope_locks(root, keys, deadline=callback_deadline(ctx), now=now)
                    )
                except ResourceBlocked as exc:
                    check_scope(
                        target,
                        stage,
                        keys,
                        ctx,
                        refusal=str(exc),
                        native_remote_expected=remote,
                    )
                check_scope(target, stage, keys, ctx, native_remote_expected=remote)
                callback_deadline(ctx)
                return fn(target, ctx)

        return call

    def gated(fn, lane, *, native_interpret=False):
        def call(target, ctx):
            remote = native_interpret and native_remote_stage(target, ctx, "interpret")
            selected_lane = "io" if remote else lane
            if pool_lease is not None:
                lease_cm = pool_lease(selected_lane, target, ctx)
            else:
                reservation = None
                if finite_frontier is not None:
                    reservation_id = hashlib.sha256(
                        f"{root.resolve()}\0{selected_lane}\0{finite_frontier['frontier_id']}\0{target}".encode()
                    ).hexdigest()
                    reservation = {
                        "reservation_id": reservation_id,
                        "target": target,
                        "frontier_id": finite_frontier["frontier_id"],
                        "agent": "closer-dispatcher",
                        "profile_id": selected_lane,
                    }
                lease_cm = pool_slot(
                    root,
                    selected_lane,
                    bounds[selected_lane],
                    deadline=callback_deadline(ctx),
                    now=now,
                    finite_frontier=finite_frontier is not None,
                    reservation=reservation,
                )
            with lease_cm:
                try:
                    callback_deadline(ctx)
                    if remote and not native_remote_stage(target, ctx, "interpret"):
                        raise ResourceBlocked("native_remote_contract_changed")
                    if finite_frontier is not None:
                        with inventory_request(ctx["deadline"], now=now):
                            result = fn(target, ctx)
                    else:
                        return fn(target, ctx)
                except Exception:
                    if finite_frontier is not None:
                        # Only an unassigned prelaunch record proves no owned
                        # request was prepared. Bound/uncertain requests stay.
                        finite_pool_no_start()
                    raise
                if finite_frontier is not None:
                    # Successful callbacks that made no owned process request
                    # prove this initial lane reservation had no spawn. A per-
                    # request starting/uncertain row is never cleared here.
                    finite_pool_no_start()
                    return result

        if native_interpret:
            _NATIVE_INTERPRET_WRAPPERS[call] = interpret
        return call

    def guarded_act(target, ctx):
        keys = _scope_keys(
            target,
            ctx,
            resource_keys,
            quarantine_workspaces,
            infer_workspaces=not remote_act(target, ctx),
        )
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(
                    mutation_reservation(
                        state_path,
                        target,
                        ctx,
                        lambda _t, _c: keys,
                        owner=owner,
                        deadline=callback_deadline(ctx),
                        now=now,
                        scope_check=lambda ordered: check_scope(target, "act", ordered, ctx),
                    )
                )
            except ScopeBlocked:
                raise
            except ResourceBlocked as exc:
                check_scope(target, "act", keys, ctx, refusal=str(exc))
            callback_deadline(ctx)
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
        began = now()
        admission_deadline = (
            began + request_timeout_seconds if finite_frontier is not None else None
        )
        row: dict[str, Any] | None
        try:
            remote_prepare = native_remote_stage(target, {}, "prepare")
            check_scope(
                target,
                "target_admission",
                _scope_keys(
                    target,
                    {},
                    None,
                    quarantine_workspaces,
                    infer_workspaces=not remote_prepare,
                ),
                request_deadline=admission_deadline,
                native_remote_expected=remote_prepare,
            )
        except ScopeBlocked as exc:
            row = _fenced_row("scope_quarantined", str(exc))
            row["scope_proof"] = exc.proof
        else:
            try:
                if finite_frontier is None:
                    assert legacy_deadline is not None
                    expires_at = legacy_deadline + LEASE_COMMIT_GRACE_SECONDS
                else:
                    expires_at = None
                lease = acquire(
                    root,
                    target,
                    owner=owner,
                    **(
                        {"expires_at": expires_at}
                        if finite_frontier is None
                        else {"finite_frontier": finite_frontier}
                    ),
                    request_deadline=admission_deadline,
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

                    def known_progress_prepare(name, context):
                        lease.check(now)
                        _release_known_shard_boundary(
                            root,
                            lease.state_path,
                            target,
                            owner=owner,
                            now=now,
                            progress=True,
                            finite_frontier=finite_frontier,
                        )
                        return scope_guard(prepare, "prepare")(name, context)

                    with fenced(lease, now):
                        if finite_frontier is not None:
                            assert admission_deadline is not None
                            # Retain the OUTER shared bounds, not the per-shard
                            # runner's single-worker ceiling. Historical rows
                            # without this observation are never backfilled.
                            context_body = {
                                "schema": "closer-shared-lane-context/v1",
                                "root": str(root.resolve()),
                                "state_path": str(lease.state_path.resolve()),
                                "target": target,
                                "frontier_sha256": cp._digest(finite_frontier),
                                "bounds": dict(bounds),
                            }
                            context = {**context_body, "sha256": cp._digest(context_body)}
                            with inventory_request(admission_deadline, now=now):
                                with cp._locked(
                                    lease.state_path,
                                    timeout_seconds=max(0.001, admission_deadline - now()),
                                ):
                                    retained = cp._load(lease.state_path)
                                    entries = retained.setdefault("frontiers", {})
                                    entry = entries.setdefault(
                                        finite_frontier["frontier_id"],
                                        {
                                            "schema": "closer-pipeline-frontier/v2",
                                            "run_id": cp._digest(
                                                [
                                                    "finite-frontier/v2",
                                                    finite_frontier["frontier_id"],
                                                    finite_frontier["owner"],
                                                ]
                                            )[:32],
                                            "finite_frontier": finite_frontier,
                                            "created_at": now(),
                                            "items": {},
                                        },
                                    )
                                    if entry.get("finite_frontier") != finite_frontier:
                                        raise ResourceBlocked(
                                            "shared lane frontier context differs"
                                        )
                                    existing = entry.setdefault("items", {}).get(target)
                                    if existing is None:
                                        entry["items"][target] = {
                                            "status": "pending",
                                            "stage": "prepare",
                                            "results": {},
                                            "metrics": {"requests": 0},
                                            "shared_lane_context": context,
                                        }
                                        cp._save(lease.state_path, retained)
                                    elif (
                                        existing.get("shared_lane_context") is not None
                                        and existing["shared_lane_context"] != context
                                    ):
                                        raise ResourceBlocked("shared lane original bounds changed")
                        runner = (
                            cp._run_pipeline_legacy
                            if finite_frontier is None
                            else cp._run_pipeline_finite
                        )
                        receipt = runner(
                            lease.state_path,
                            [target],
                            prepare=gated(known_progress_prepare, "io"),
                            interpret=gated(
                                scope_guard(interpret, "interpret"), "low", native_interpret=True
                            ),
                            act=gated(guarded_act, "io"),
                            exception=gated(scope_guard(exception, "exception"), "exception"),
                            reconcile=(
                                gated(scope_guard(reconcile, "reconcile"), "io")
                                if reconcile
                                else None
                            ),
                            capacity_fn=capacity_fn,
                            **(
                                {
                                    "deadline": deadline,
                                    "priority_targets": [target] if target in priority else (),
                                    "round_deadline_exception": round_deadline_exception,
                                    "round_capacity_exception": round_capacity_exception,
                                }
                                if finite_frontier is None
                                else {
                                    "finite_frontier": finite_frontier,
                                    "frontier_target": target,
                                    "request_timeout_seconds": request_timeout_seconds,
                                    "cancelled_fn": cancelled_fn,
                                    "paused_fn": paused_fn,
                                }
                            ),
                            max_io_workers=1,
                            max_low_workers=1,
                            max_exception_workers=1,
                            now=now,
                        )
                    found_row = (receipt.get("targets") or {}).get(target)
                    row = found_row or _fenced_row(str(receipt.get("reason") or "no_shard_receipt"))
                    if found_row is not None and receipt.get("run_id"):
                        run_ids[target] = receipt["run_id"]
                        receipt_deadline = receipt.get("deadline")
                        if isinstance(receipt_deadline, (int, float)) and not isinstance(
                            receipt_deadline, bool
                        ):
                            run_deadlines[target] = float(receipt_deadline)
                        elif finite_frontier is None and legacy_deadline is not None:
                            run_deadlines[target] = legacy_deadline
                    if found_row is not None and receipt.get("run_id"):
                        lease.check(now)
                        _release_known_shard_boundary(
                            root,
                            lease.state_path,
                            target,
                            owner=owner,
                            now=now,
                            run_id=receipt["run_id"],
                            finite_frontier=finite_frontier,
                        )
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
    if max_target_workers is not None:
        threads = min(threads, max_target_workers)
    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(work, ordered))
    finished = now()
    overrun_seconds = None
    if finite_frontier is None:
        assert legacy_deadline is not None
        overrun_seconds = max(0.0, finished - legacy_deadline)
    fenced_targets = sorted(t for t, r in results.items() if r.get("scoped_fence"))
    unknown = unknown_reservations(root)
    owners = set(canonical)
    aggregate = {
        "schema": "closer-pipeline-receipt/v1",
        "mode": "resource_scoped",
        "round_id": round_id,
        "run_ids": dict(sorted(run_ids.items())),
        **(
            {"run_deadlines": dict(sorted(run_deadlines.items()))}
            if finite_frontier is None
            else {}
        ),
        "shards": {t: shard_dir(root, t).name for t in sorted(raw_list)},
        **(
            {"deadline": deadline}
            if finite_frontier is None
            else {
                "finite_frontier": finite_frontier,
                "request_timeout_seconds": request_timeout_seconds,
            }
        ),
        "elapsed_seconds": max(0.0, finished - started),
        "finished_at": finished,
        **({"overrun_seconds": overrun_seconds} if overrun_seconds is not None else {}),
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
    receipt_path = write_round_receipt(root, aggregate)
    aggregate["receipt_path"] = str(receipt_path)
    _stored_receipt, aggregate["receipt_sha256"] = read_round_receipt(receipt_path)
    return aggregate


LEGACY_RECONCILIATION_SCHEMA = "closer-legacy-advertisement-reconciliation/v1"


def _reconciliation_bytes(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing_or_aliased_file:{path}")
    return path.read_bytes()


def _reconciliation_record(path):
    raw = _reconciliation_bytes(path)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"invalid_record:{path}")
    return raw, value


def _reconciliation_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _no_operation_wait(path, target, row):
    """Pure, deliberately narrow subset of the native inner-wait contract.

    No holder lookup, provider inspection, claim adoption, or native ledger write.
    Only evidence-refusal waits with no operation history can retire an advertisement.
    """
    import closer_pipeline as pipeline
    import closer_queue as queue

    act = row["results"]["act"]
    snapshot = row["results"]["prepare"]["snapshot"]
    proof = act["resource_evidence"]["inner_wait"]
    native_path = path.parent / f"{path.stem}.actions" / f"{pipeline._digest(target)}.json"
    raw, native = _reconciliation_record(native_path)
    round_state = native["round"]
    receipt = round_state["receipt"]
    item = native["items"][target]
    reason, trigger = proof["reason"], proof["next_trigger"]
    expected_proof = {
        "schema": pipeline.INNER_WAIT_SCHEMA,
        "target": target,
        "revision": row["revision"],
        "head": snapshot.get("head"),
        "reason": reason,
        "next_trigger": trigger,
        "inner_path": str(native_path),
        "receipt_sha256": pipeline._digest(receipt),
    }
    if (
        row.get("status") != "deferred"
        or row.get("stage") != "act"
        or row.get("error") is not None
        or row.get("recovery_barrier") is not None
        or row.get("provider_outcome_uncertain")
        or row.get("known_completed_action") is True
        or not isinstance(row.get("revision"), str)
        or not row["revision"]
        or (snapshot.get("head") is None and snapshot.get("kind") != "issue")
        or (
            snapshot.get("head") is not None
            and (
                not isinstance(snapshot["head"], str)
                or not re.fullmatch(r"[0-9a-f]{40}", snapshot["head"])
            )
        )
        or native_path.parent.is_symlink()
        or snapshot.get("target") != target
        or snapshot.get("revision") != row["revision"]
        or row.get("identity")
        != hashlib.sha256(
            json.dumps(
                [target, snapshot.get("head"), snapshot.get("merge")], sort_keys=True
            ).encode()
        ).hexdigest()
        or proof != expected_proof
        or act.get("status") != "deferred"
        or act.get("reason") != "validated_inner_wait"
        or act.get("acceptance_authority") is not False
        or act.get("next_trigger") != trigger
        or act.get("queue_receipt") != receipt
        or reason not in pipeline.EVIDENCE_REFUSAL_REASONS
        or trigger != pipeline.EVIDENCE_REFUSAL_TRIGGER
        or native.get("schema") != queue.SCHEMA
        or set(native["items"]) != {target}
        or round_state.get("status") != "finished"
        or round_state.get("assignment_targets") != [target]
        or round_state.get("source_revisions", {}).get(target) != row["revision"]
        or receipt.get("schema") != queue.RECEIPT_SCHEMA
        or receipt.get("valid") is not True
        or receipt.get("stop_valid") is not True
        or receipt.get("progress") != []
        or receipt.get("eligible_remaining") != []
        or item.get("operation") is not None
        or item.get("operation_history") != []
        or item.get("evidence", {}).get("head") != snapshot.get("head")
        or receipt.get("accounting") != [queue._account_row(target, item)]
    ):
        raise ValueError("native_no_operation_wait_proof_mismatch")
    accounting = receipt["accounting"][0]
    if any(
        accounting.get(key) != value
        for key, value in {
            "target": target,
            "revision": row["revision"],
            "action": "wait",
            "eligible": False,
            "requires_coordinator": False,
            "reason": reason,
            "next_trigger": trigger,
            "observation_freshness": "fresh",
            "operation_id": None,
            "operation_status": None,
        }.items()
    ) or accounting.get("status") not in {"deferred", "ineligible"}:
        raise ValueError("native_accounting_mismatch")

    def forbidden_holder(_key):
        raise ValueError("unexpected_claim_lookup")

    checked = queue._validate_data(
        native,
        receipt,
        assignment_targets=[target],
        now_value=receipt["finished_at"],
        holder_fn=forbidden_holder,
    )
    if not checked.get("valid") or not checked.get("stop_valid"):
        raise ValueError("invalid_native_receipt")
    return native_path, raw


def legacy_plan_target_for_intent(state_path, *, run_id, intent_target):
    """Map an intent target to the exact original run assignment string."""
    path = Path(state_path).absolute()
    import closer_pipeline as pipeline

    _, state = _reconciliation_record(path)
    if state.get("schema") != pipeline.SCHEMA:
        raise ValueError("unsupported_pipeline_schema")
    run = state["runs"][run_id]
    wanted = canonical_target(str(intent_target))
    for assigned in run["assignment"]:
        try:
            if canonical_target(assigned) == wanted:
                return assigned
        except ValueError:
            if assigned == intent_target:
                return assigned
    raise ValueError("target_not_in_original_run")


def plan_legacy_reconciliation(state_path, *, run_id, owner, target):
    """Read-only plan; historical advertisements lacking causal IDs remain held.

    A plan is not a global stop, acceptance proof, or permission to retry an effect.
    Apply rechecks the exact plan under all original locks before archiving any bytes.
    """
    path = Path(state_path).absolute()
    plan = {
        "schema": LEGACY_RECONCILIATION_SCHEMA,
        "state_path": str(path),
        "run_id": run_id,
        "owner": owner,
        "target": target,
        "acceptance_authority": False,
        "global_stop_authority": False,
        "reasons": [],
        "advertisements": [],
    }
    try:
        import closer_pipeline as pipeline

        raw, state = _reconciliation_record(path)
        if state.get("schema") != pipeline.SCHEMA:
            raise ValueError("unsupported_pipeline_schema")
        run = state["runs"][run_id]
        if target not in run["assignment"]:
            raise ValueError("target_not_in_original_run")
        row = run["targets"][target]
        native_path, native_raw = _no_operation_wait(path, target, row)
        plan["pipeline_bytes"] = raw.decode()
        plan["native_path"] = str(native_path)
        plan["native_bytes"] = native_raw.decode()
        binding = {
            "pipeline_run_id": run_id,
            "revision": row["revision"],
            "identity": row["identity"],
            "work_key": row["work_key"],
        }
        if any(not isinstance(v, str) or not v for v in binding.values()):
            raise ValueError("invalid_causal_run_binding")
        root = scoped_root(path)
        for base in _reservation_bases(root):
            if base.is_symlink() or base.parent.is_symlink():
                raise ValueError("aliased_reservation_base")
            for directory in sorted(base.iterdir()) if base.is_dir() else []:
                intent_path = directory / "legacy-intent.json"
                if not intent_path.exists():
                    continue
                intent_raw, intent = _reconciliation_record(intent_path)
                if intent.get("owner") != owner or intent.get("target") != canonical_target(target):
                    continue
                key = intent["key"]
                if (
                    normalize_key(key) != key
                    or key.split(":", 1)[0] not in DURABLE_KINDS
                    or directory.name != hashlib.sha256(key.encode()).hexdigest()[:24]
                    or directory.is_symlink()
                    or intent.get("state") != "legacy_executor"
                ):
                    raise ValueError("advertisement_identity_mismatch")
                if intent.get("causal_binding") != binding:
                    plan["reasons"].append("missing_or_mismatched_causal_run_binding")
                for candidate_base in _reservation_bases(root):
                    if (candidate_base / directory.name / "intent.json").exists():
                        raise ValueError("core_intent_retained")
                    other = candidate_base / directory.name / "legacy-intent.json"
                    if other != intent_path and other.exists():
                        raise ValueError("other_resource_advertisement_retained")
                plan["advertisements"].append(
                    {"path": str(intent_path), "bytes": intent_raw.decode(), "key": key}
                )
        if not plan["advertisements"]:
            raise ValueError("no_matching_advertisements")
        keys = [item["key"] for item in plan["advertisements"]]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate_resource_advertisements")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        plan["reasons"].append(str(exc))
    plan["reasons"] = sorted(set(plan["reasons"]))
    plan["eligible"] = not plan["reasons"]
    return {**plan, "plan_sha256": _reconciliation_digest(plan)}


def _exact_reconciliation_plan(path, run_id, owner, target, expected_hash):
    """Resume only the same immutable audit, with every retired byte still present."""
    current = plan_legacy_reconciliation(path, run_id=run_id, owner=owner, target=target)
    if current["eligible"] and current["plan_sha256"] == expected_hash:
        return current
    audit_dir = _reservation_base(scoped_root(path)).parent / "reconciliation-audit"
    if audit_dir.is_symlink() or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ResourceBlocked("invalid_reconciliation_audit_identity")
    audit_path = audit_dir / f"{expected_hash}.json"
    try:
        _, prior = _reconciliation_record(audit_path)
        content = {k: v for k, v in prior.items() if k != "plan_sha256"}
        if (
            prior.get("plan_sha256") != expected_hash
            or _reconciliation_digest(content) != expected_hash
            or prior.get("eligible") is not True
            or current["reasons"] not in ([], ["no_matching_advertisements"])
            or any(
                current.get(k) != v
                for k, v in prior.items()
                if k not in {"advertisements", "reasons", "eligible", "plan_sha256"}
            )
        ):
            raise ValueError("reconciliation_audit_binding_changed")
        remaining = []
        for index, ad in enumerate(prior["advertisements"]):
            source = Path(ad["path"])
            allowed = {
                base / hashlib.sha256(ad["key"].encode()).hexdigest()[:24] / "legacy-intent.json"
                for base in _reservation_bases(scoped_root(path))
            }
            if source not in allowed or source.parent.is_symlink() or source.is_symlink():
                raise ValueError("invalid_audited_advertisement_path")
            for base in _reservation_bases(scoped_root(path)):
                if (base / source.parent.name / "intent.json").exists():
                    raise ValueError("core_intent_retained")
                other = base / source.parent.name / "legacy-intent.json"
                if other != source and other.exists():
                    raise ValueError("other_resource_advertisement_retained")
            destination = audit_dir / f"{expected_hash}-{index}.retired"
            if source.exists():
                if destination.exists() or _reconciliation_bytes(source) != ad["bytes"].encode():
                    raise ValueError("advertisement_changed_since_audit")
                remaining.append(ad)
            elif _reconciliation_bytes(destination) != ad["bytes"].encode():
                raise ValueError("retired_advertisement_changed")
        if remaining != current["advertisements"]:
            raise ValueError("advertisement_set_changed_since_audit")
        return prior
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ResourceBlocked("reconciliation_plan_held_or_changed") from exc


def apply_legacy_reconciliation(state_path, *, run_id, owner, target, expected_plan_sha256):
    """Explicit exact-plan CAS: archive before retiring only proven no-operation ads.

    Lock contention is a refusal, never evidence that a holder is dead. No locks
    are created by this API. A crash after an archive rename leaves immutable
    evidence. An exact-hash restart resumes only while all original queue/native
    bytes and every still-present or archived advertisement remain unchanged.
    """
    path = Path(state_path).absolute()
    initial = _exact_reconciliation_plan(path, run_id, owner, target, expected_plan_sha256)
    lock_paths = [path.with_name(path.name + ".runlock"), path.with_name(path.name + ".lock")]
    lock_paths += [
        Path(ad["path"]).parent / "lock"
        for ad in sorted(initial["advertisements"], key=lambda ad: ad["key"])
    ]
    lock_paths += [Path(initial["native_path"] + ".lock")]
    with contextlib.ExitStack() as stack:
        for lock_path in lock_paths:
            if lock_path.is_symlink():
                raise ResourceBlocked("aliased_reconciliation_lock")
            handle = stack.enter_context(lock_path.open("r+"))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ResourceBlocked(f"live_reconciliation_holder:{lock_path}") from exc
        current = _exact_reconciliation_plan(path, run_id, owner, target, expected_plan_sha256)
        if current != initial:
            raise ResourceBlocked("reconciliation_plan_changed_under_locks")
        audit_dir = _reservation_base(scoped_root(path)).parent / "reconciliation-audit"
        audit_dir.mkdir(exist_ok=True)
        if audit_dir.is_symlink():
            raise ResourceBlocked("aliased_reconciliation_audit")
        audit_path = audit_dir / f"{expected_plan_sha256}.json"
        encoded = (json.dumps(current, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
        try:
            fd = os.open(audit_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        except FileExistsError:
            if _reconciliation_bytes(audit_path) != encoded:
                raise ResourceBlocked("reconciliation_audit_mismatch") from None
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        with audit_path.open("rb") as stream:
            os.fsync(stream.fileno())
        _fsync_dir(audit_dir)
        # Audit is durable before the first removal. Each exact original file is
        # retired by atomic rename, retaining its bytes even after a partial crash.
        retired = []
        for index, ad in enumerate(current["advertisements"]):
            source = Path(ad["path"])
            destination = audit_dir / f"{expected_plan_sha256}-{index}.retired"
            if not source.exists() and _reconciliation_bytes(destination) == ad["bytes"].encode():
                retired.append(str(destination))
                continue
            if _reconciliation_bytes(source) != ad["bytes"].encode():
                raise ResourceBlocked("advertisement_changed_before_retirement")
            if destination.exists() or destination.is_symlink():
                raise ResourceBlocked("retirement_destination_exists")
            os.rename(source, destination)
            _fsync_dir(source.parent)
            _fsync_dir(audit_dir)
            retired.append(str(destination))
        return {
            "status": "retired",
            "audit_path": str(audit_path),
            "retired": retired,
            "acceptance_authority": False,
            "global_stop_authority": False,
        }


TERMINAL_RECONCILIATION_SCHEMA = "closer-terminal-owner-reconciliation/v1"


def _terminal_owner(intent, filename):
    """Absence, never age, proves a recorded local owner terminal."""
    owner = intent.get("owner")
    if filename == "legacy-intent.json" and owner == "legacy":
        return  # Historical legacy records have only the original lock identity.
    match = re.fullmatch(r"(?:(?:raw-)?legacy:)?([1-9][0-9]*):.+", str(owner))
    if not match:
        raise ResourceBlocked("owner_identity_unknown")
    pid = int(match.group(1))
    if "process_group" in intent:
        pgid = intent["process_group"]
    else:
        # Historical Core intents omitted process_group. Fall back to the recorded
        # pid so those advertisements remain drainable; plan/apply still require
        # fresh remote and native-writer terminality. New writers record the group.
        pgid = pid
    for probe, identity in ((os.kill, pid), (os.killpg, pgid)):
        if type(identity) is not int or identity <= 0:
            raise ResourceBlocked("owner_process_group_unknown")
        try:
            probe(identity, 0)
        except ProcessLookupError:
            continue
        except OSError as exc:
            raise ResourceBlocked("owner_liveness_unknown") from exc
        raise ResourceBlocked("owner_still_alive")


@contextlib.contextmanager
def _terminal_locks(state_path, advertisements):
    # A dead coordinator may have surviving children. Hold both original ledger
    # locks and every resource lock throughout the fresh observation and CAS.
    paths = {state_path.with_name(state_path.name + suffix) for suffix in (".lock", ".runlock")}
    paths.update(Path(row["path"]).parent / "lock" for row in advertisements)
    with contextlib.ExitStack() as stack:
        for path in sorted(paths):
            if path.is_symlink():
                raise ResourceBlocked("aliased_reconciliation_lock")
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            handle = stack.enter_context(os.fdopen(fd, "r+"))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ResourceBlocked("live_reconciliation_holder") from exc
        yield


def _terminal_advertisements(state_path, target):
    rows = []
    for base in _reservation_bases(scoped_root(state_path)):
        if base.is_symlink() or base.parent.is_symlink():
            raise ResourceBlocked("aliased_reservation_base")
        for directory in sorted(base.iterdir()) if base.is_dir() else []:
            for filename in ("intent.json", "legacy-intent.json"):
                path = directory / filename
                if not path.exists():
                    continue
                raw, intent = _reconciliation_record(path)
                if canonical_target(intent.get("target")) != target:
                    continue
                key = intent.get("key")
                if (
                    normalize_key(key) != key
                    or key.split(":", 1)[0] not in DURABLE_KINDS
                    or directory.name != hashlib.sha256(key.encode()).hexdigest()[:24]
                    or directory.is_symlink()
                    or intent.get("state") not in {"in_flight", "unknown", "legacy_executor"}
                ):
                    raise ResourceBlocked("advertisement_identity_mismatch")
                _terminal_owner(intent, filename)
                rows.append(
                    {
                        "path": str(path),
                        "bytes": raw.decode(),
                        "key": key,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                )
    if not rows:
        raise ResourceBlocked("no_matching_advertisements")
    return rows


def _terminal_remote_observation(target):
    # Reuse the production snapshot and all-head native writer rail. Source
    # issues also need every referenced PR, including open/unmerged deliveries.
    import closer_dispatcher
    import review_writer_gate

    github = closer_dispatcher.GitHub(deadline=time.time() + 120)
    snapshot = github.snapshot(target)
    pr_target = snapshot.get("pr_target") or (target if snapshot.get("kind") == "pr" else None)
    inventory = snapshot.get("delivery_links")
    targets = {pr_target} if pr_target else set()
    if inventory is not None or snapshot.get("kind") == "issue":
        if not isinstance(inventory, dict) or inventory.get("complete") is not True:
            raise ResourceBlocked("delivery_inventory_incomplete")
        candidates = inventory.get("candidates")
        if not isinstance(candidates, list):
            raise ResourceBlocked("delivery_inventory_malformed")
        targets.update(candidates)
        repo, number = target.split("#")
        timeline = review_writer_gate._pages(
            github.api, f"repos/{repo}/issues/{number}/timeline?per_page=100"
        )
        for event in timeline:
            if not isinstance(event, dict):
                raise ResourceBlocked("delivery_timeline_malformed")
            if event.get("event") != "cross-referenced":
                continue
            source = event.get("source")
            if not isinstance(source, dict) or not isinstance(source.get("issue"), dict):
                raise ResourceBlocked("delivery_timeline_malformed")
            issue = source["issue"]
            if "pull_request" not in issue:
                continue
            match = re.fullmatch(
                r"https://api\.github\.com/repos/([\w.-]+/[\w.-]+)/issues/([1-9][0-9]*)",
                str(issue.get("url")),
            )
            if not match:
                raise ResourceBlocked("delivery_timeline_malformed")
            targets.add(f"{match[1]}#{match[2]}")
    proofs = []

    def native_api(endpoint):
        value = github.api(endpoint)
        if "/jobs?" not in endpoint:
            return value
        if not isinstance(value, dict) or not isinstance(value.get("jobs"), list):
            raise ResourceBlocked("implementation_jobs_malformed")
        for job in value["jobs"]:
            if not isinstance(job, dict):
                raise ResourceBlocked("implementation_jobs_malformed")
            steps = job.get("steps", [])
            if not isinstance(steps, list) or any(not isinstance(s, dict) for s in steps):
                raise ResourceBlocked("implementation_steps_malformed")
            implementation = [
                s
                for s in steps
                if re.fullmatch(r"Run (Codex|Claude|Cursor|Muse|Gemini|Vibe)", str(s.get("name")))
                or s.get("name") == "Commit and push changes"
            ]
            named = re.search(
                r"\brun (codex|claude|cursor|muse|gemini|vibe)\b", str(job.get("name")), re.I
            )
            if (implementation or named) and (
                job.get("status") != "completed"
                or any(s.get("status") != "completed" for s in implementation)
            ):
                raise ResourceBlocked("implementation_writer_not_terminal")
        return value

    for linked in sorted(targets):
        if not isinstance(linked, str) or not closer_dispatcher.TARGET.fullmatch(linked):
            raise ResourceBlocked("delivery_target_malformed")
        repo, number = linked.split("#")
        if repo.lower() in closer_dispatcher.EXCLUDED:
            raise ResourceBlocked("delivery_target_excluded")
        proof = review_writer_gate.writer_terminality(repo, int(number), native_api)
        if proof.get("terminal") is not True:
            raise ResourceBlocked("native_writer_not_terminal:" + str(proof.get("reason")))
        proofs.append({"target": linked, **proof})
    proof = {
        "terminal": True,
        "evidence": [row for item in proofs for row in item.get("evidence", [])],
        "targets": proofs,
    }
    return {"snapshot": snapshot, "native_writer": proof}


def _terminal_plan(state_path, target, advertisements, observe):
    observation = observe(target)
    if _terminal_advertisements(state_path, target) != advertisements:
        raise ResourceBlocked("advertisement_changed_during_observation")
    if not isinstance(observation, dict):
        raise ResourceBlocked("remote_observation_malformed")
    snapshot = observation.get("snapshot", {})
    if not isinstance(snapshot, dict) or not isinstance(observation.get("native_writer"), dict):
        raise ResourceBlocked("remote_observation_malformed")
    if (
        canonical_target(snapshot.get("target")) != target
        or snapshot.get("kind") not in {"pr", "issue"}
        or snapshot.get("state") not in {"OPEN", "CLOSED", "MERGED"}
        or not snapshot.get("revision")
        or snapshot.get("revision") == "unknown"
        or snapshot.get("_local_cached")
        or observation.get("native_writer", {}).get("terminal") is not True
    ):
        raise ResourceBlocked("remote_observation_unknown")
    # observed_at is transport freshness, not a remote-state revision. Apply
    # obtains a NEW observation under locks and compares the actual stable bytes.
    observation = {
        **observation,
        "snapshot": {k: v for k, v in snapshot.items() if k != "observed_at"},
    }
    content = {
        "schema": TERMINAL_RECONCILIATION_SCHEMA,
        "state_path": str(state_path),
        "target": target,
        "advertisements": advertisements,
        "observation": observation,
        "eligible": True,
        "acceptance_authority": False,
        "global_stop_authority": False,
        "effect_retry_authority": False,
        "reasons": [],
    }
    return {**content, "plan_sha256": _reconciliation_digest(content)}


def plan_terminal_reconciliation(state_path, *, target, observe=None):
    """Read-only remote reconciliation plan for terminal local owners.

    This retires resource advertisements, not operations or pipeline history.
    Unknown effects retain their original operation ledger and retry barriers.
    No acceptance, global stop or continuation authority is inferred.
    """
    path, target = Path(state_path).absolute(), canonical_target(target)
    try:
        rows = _terminal_advertisements(path, target)
        with _terminal_locks(path, rows):
            if _terminal_advertisements(path, target) != rows:
                raise ResourceBlocked("advertisement_changed_under_locks")
            return _terminal_plan(path, target, rows, observe or _terminal_remote_observation)
    except (OSError, ValueError, TypeError, AttributeError, ResourceBlocked, RuntimeError) as exc:
        return {
            "schema": TERMINAL_RECONCILIATION_SCHEMA,
            "eligible": False,
            "advertisements": [],
            "reasons": [str(exc)],
            "acceptance_authority": False,
            "global_stop_authority": False,
            "effect_retry_authority": False,
        }


def apply_terminal_reconciliation(state_path, *, target, expected_plan_sha256, observe=None):
    """Fresh observation + exact-byte CAS; archive each intent before retirement.

    There is no automatic effect retry. A crash after the audit write and before
    the first rename resumes from that same immutable audit when the plan hash
    and advertisement bytes still match (observed_at is transport freshness).
    After the first rename, the retired intent leaves the scan, so the plan hash
    changes and a rerun raises reconciliation_plan_changed. Plan the remaining
    bytes afresh; the new audit does not replace the prior audit or retired bytes.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256):
        raise ResourceBlocked("invalid_reconciliation_audit_identity")
    path, target = Path(state_path).absolute(), canonical_target(target)
    rows = _terminal_advertisements(path, target)
    with _terminal_locks(path, rows):
        if _terminal_advertisements(path, target) != rows:
            raise ResourceBlocked("advertisement_changed_under_locks")
        current = _terminal_plan(path, target, rows, observe or _terminal_remote_observation)
        if current["plan_sha256"] != expected_plan_sha256:
            raise ResourceBlocked("reconciliation_plan_changed")
        audit_dir = _reservation_base(scoped_root(path)).parent / "reconciliation-audit"
        if audit_dir.is_symlink():
            raise ResourceBlocked("aliased_reconciliation_audit")
        existed = audit_dir.exists()
        audit_dir.mkdir(exist_ok=True)
        if audit_dir.is_symlink():
            raise ResourceBlocked("aliased_reconciliation_audit")
        if not existed:
            # Persist the new directory entry before any retired rename lands.
            _fsync_dir(audit_dir.parent)
        audit_path = audit_dir / f"{expected_plan_sha256}.json"
        encoded = (
            json.dumps({**current, "observed_at": time.time()}, sort_keys=True, indent=2) + "\n"
        ).encode()
        # Fsync a temp file, then link it into place so a crash cannot leave a
        # torn read-only audit that latches every identical plan hash. An
        # identical audit already present (crash between publish and rename)
        # resumes; always fsync the audit directory before any intent moves.
        fd, temp = tempfile.mkstemp(prefix=".audit-", suffix=".tmp", dir=audit_dir)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, 0o444)
            os.link(temp, audit_path)
        except FileExistsError:
            _, prior = _reconciliation_record(audit_path)
            prior_body = {k: v for k, v in prior.items() if k != "observed_at"}
            if prior_body != current:
                raise ResourceBlocked("reconciliation_audit_mismatch") from None
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)
        _fsync_dir(audit_dir)
        retired = []
        for index, row in enumerate(current["advertisements"]):
            source = Path(row["path"])
            destination = audit_dir / f"{expected_plan_sha256}-{index}.retired"
            if not source.exists() and _reconciliation_bytes(destination) == row["bytes"].encode():
                retired.append(str(destination))
                continue
            if _reconciliation_bytes(source) != row["bytes"].encode():
                raise ResourceBlocked("advertisement_changed_before_retirement")
            if destination.exists() or destination.is_symlink():
                raise ResourceBlocked("retirement_destination_exists")
            os.rename(source, destination)
            _fsync_dir(source.parent)
            _fsync_dir(audit_dir)
            retired.append(str(destination))
        return {
            "status": "retired",
            "audit_path": str(audit_path),
            "retired": retired,
            "acceptance_authority": False,
            "global_stop_authority": False,
            "effect_retry_authority": False,
        }


def _reconciliation_main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Plan exact no-operation legacy advertisement reconciliation"
    )
    parser.add_argument("--state", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--terminal-owner", action="store_true")
    parser.add_argument("--owner")
    parser.add_argument("--target", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args(argv)
    if args.apply and not args.expected_plan_sha256:
        parser.error("--apply requires --expected-plan-sha256")
    try:
        if args.terminal_owner:
            result = (
                apply_terminal_reconciliation(
                    args.state, target=args.target, expected_plan_sha256=args.expected_plan_sha256
                )
                if args.apply
                else plan_terminal_reconciliation(args.state, target=args.target)
            )
            print(json.dumps(result, sort_keys=True))
            return 0 if result.get("eligible", result.get("status") == "retired") else 78
        if not args.run_id or not args.owner:
            parser.error("legacy reconciliation requires --run-id and --owner")
        common = {"run_id": args.run_id, "owner": args.owner, "target": args.target}
        result = (
            apply_legacy_reconciliation(
                args.state, **common, expected_plan_sha256=args.expected_plan_sha256
            )
            if args.apply
            else plan_legacy_reconciliation(args.state, **common)
        )
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("eligible", result.get("status") == "retired") else 78
    except (OSError, ValueError, KeyError, RuntimeError, ResourceBlocked) as exc:
        print(json.dumps({"status": "held", "reason": str(exc), "acceptance_authority": False}))
        return 78


if __name__ == "__main__":
    raise SystemExit(_reconciliation_main())
