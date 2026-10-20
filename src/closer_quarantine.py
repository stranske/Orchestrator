"""Read-only, fail-closed resource census for independent scoped continuation.

This is scheduling evidence, never acceptance, an effect receipt, or authority to
retire an intent. Callers supply complete prepared aliases and hold their normal
coordination/resource locks while checking and using the result.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any

SCHEMA = "closer-quarantine-scope/v1"
MAX_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 10_000
TARGET = re.compile(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([0-9]+)")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{64}")


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _target(value: Any) -> str:
    match = TARGET.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("unbounded_target_identity")
    owner, repo, number = match.groups()
    if int(number) < 1:
        raise ValueError("unbounded_target_identity")
    return f"{owner.lower()}/{repo.lower()}#{int(number)}"


def _key(value: Any) -> str:
    from closer_resources import normalize_key

    normalized = normalize_key(value)
    kind, content = normalized.split(":", 1)
    if kind in {"pr", "issue"}:
        normalized = kind + ":" + _target(content)
    elif kind == "repo" and not REPO.fullmatch(content):
        raise ValueError("unbounded_repo_identity")
    elif kind == "branch":
        repo, _, branch = content.partition("@")
        if not REPO.fullmatch(repo) or any(ord(c) < 32 or ord(c) == 127 for c in branch):
            raise ValueError("unbounded_branch_identity")
    elif kind == "worktree":
        path = Path(content)
        if not path.is_absolute() or str(path.resolve()) != content:
            raise ValueError("aliased_worktree_identity")
    return normalized


WORKSPACE_SCHEMA = "closer-workspace-bindings/v1"


def workspace_bindings(value):
    """Separate old scope hints from prospective exact-target execution choices.

    Neither a current map nor its hash is evidence of an old callback's cwd.
    Flat maps retain their legacy conservative alias expansion.
    """
    envelope = isinstance(value, dict) and value.get("schema") == WORKSPACE_SCHEMA
    if envelope:
        if set(value) != {"schema", "historical", "execution"}:
            raise ValueError("unbounded_workspace_mapping")
        historical, execution = value["historical"], value["execution"]
    else:
        historical, execution = ({} if value is None else value), {}
    output = []
    for mapping, exact in ((historical, False), (execution, True)):
        if not isinstance(mapping, dict) or len(mapping) > MAX_ENTRIES:
            raise ValueError("unbounded_workspace_mapping")
        normalized = {}
        for target, location in mapping.items():
            if not isinstance(target, str) or not isinstance(location, str):
                raise ValueError("unbounded_workspace_mapping")
            name = _target(target) if "#" in target else _key("repo:" + target)[5:]
            if exact and "#" not in name:
                raise ValueError("execution_workspace_requires_exact_target")
            path = Path(location)
            if not path.is_absolute():
                raise ValueError("unbounded_workspace_mapping")
            resolved = str(path.resolve())
            if name in normalized and normalized[name] != resolved:
                raise ValueError("ambiguous_workspace_mapping")
            normalized[name] = resolved
        output.append(normalized)
    return output[0], output[1], envelope


def current_workspace_keys(target, keys, bindings):
    if bindings is None or bindings == {}:
        return set()
    historical, execution, _ = workspace_bindings(bindings)
    canonical = _target(target)
    if canonical in execution:
        # Exactly one checkout is selected by the worker. Generated repo aliases
        # must not silently add a second, unused canonical checkout.
        return {"worktree:" + execution[canonical]}
    names = {canonical, canonical.split("#")[0]}
    for key in keys:
        kind, value = key.split(":", 1)
        if kind in {"pr", "issue"}:
            names.update({value, value.split("#")[0]})
        elif kind in {"repo", "branch"}:
            names.add(value.split("@")[0])
    return {"worktree:" + historical[name] for name in names if name in historical}


def _live_finite_lease(lease, target, held, sampled_at):
    """Only a held exact lease can authorize a new finite shard's live rows."""
    from closer_frontier_policy import validate_frontier

    try:
        frontier = validate_frontier(lease.get("finite_frontier"))
        return (
            held is True
            and lease.get("authority") == "held-owner-epoch-lock/v1"
            and "expires_at" not in lease
            and frontier == lease.get("finite_frontier")
            and _target(lease.get("target")) == target
            and target in {_target(t) for t in frontier["targets"]}
            and isinstance(lease.get("owner"), str)
            and bool(lease["owner"])
            and type(lease.get("epoch")) is int
            and lease["epoch"] > 0
            and type(lease.get("acquired_at")) in {int, float}
            and math.isfinite(lease["acquired_at"])
            and lease["acquired_at"] <= sampled_at
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def resource_overlap(left, right):
    if left == right:
        return True
    if left.startswith("worktree:") and right.startswith("worktree:"):
        a, b = Path(left[9:]), Path(right[9:])
        if a.is_relative_to(b) or b.is_relative_to(a):
            return True

        # macOS can preserve different casing in resolve() for the same physical
        # directory. Compare directory identity without rewriting historical keys.
        def identity(path):
            try:
                stat = path.stat()
            except FileNotFoundError:
                return None
            return stat.st_dev, stat.st_ino

        a_id, b_id = identity(a), identity(b)
        return (
            a_id is not None and any(identity(parent) == a_id for parent in (b, *b.parents))
        ) or (b_id is not None and any(identity(parent) == b_id for parent in (a, *a.parents)))
    return False


class _Census:
    def __init__(self, path: Path, workspaces=None):
        self.path = path
        self.sources: dict[str, str | None] = {}
        self.reasons: list[str] = []
        self.items: list[dict[str, Any]] = []
        self.keys: set[str] = set()
        self.bytes = 0
        self.preparations: dict[tuple[str, Any], list[tuple[dict, Any]]] = {}
        self.observations: list[dict[str, Any]] = []
        self.raw_records: dict[str, Any] = {}
        self.workspaces = {}
        self.execution_workspaces = {}
        try:
            self.workspaces, self.execution_workspaces, _ = workspace_bindings(workspaces)
        except (TypeError, ValueError, AttributeError):
            self.reasons.append("unbounded_workspace_mapping")

    def mapped_keys(self, target, keys):
        names = {target, target.split("#")[0]}
        for key in keys:
            kind, value = key.split(":", 1)
            if kind in {"pr", "issue"}:
                names.update({value, value.split("#")[0]})
            elif kind in {"repo", "branch"}:
                names.add(value.split("@")[0])
        return {"worktree:" + self.workspaces[name] for name in names if name in self.workspaces}

    def evidence_root(self):
        suffix = ".pipeline.json"
        if not str(self.path).endswith(suffix):
            raise ValueError("unbound_evidence_root")
        return Path(str(self.path)[: -len(suffix)] + ".evidence")

    def retained(self, ref, kind):
        if (
            not isinstance(ref, dict)
            or not isinstance(ref.get("sha256"), str)
            or not SHA.fullmatch(ref["sha256"])
        ):
            raise ValueError("unverified_retained_reference")
        path = Path(ref["path"])
        directory = self.evidence_root() / kind
        if path.parent != directory or path.suffix != ".json" or path.is_symlink():
            raise ValueError("unconfined_retained_reference")
        if kind == "snapshots" and path.name != ref["sha256"] + ".json":
            raise ValueError("unbound_snapshot_name")
        doc = self.read(path)
        if self.sources[str(path)] != ref["sha256"]:
            raise ValueError("retained_reference_hash_mismatch")
        return doc

    def verified_snapshot(self, target, row):
        """Recover only actual producer files; a compact summary is insufficient."""
        import closer_evidence as evidence

        revision = row.get("revision")
        prepared = (
            row.get("results", {}).get("prepare", {})
            if isinstance(row.get("results"), dict)
            else {}
        )
        candidates = []
        if isinstance(prepared, dict) and prepared.get("snapshot_ref"):
            candidates.append((prepared["snapshot_ref"], prepared.get("packet_ref")))
        candidates.extend(self.preparations.get((target, revision), []))
        cache_row = (
            row if isinstance(row.get("evidence"), dict) else self.raw_records.get(target, {})
        )
        cached = (
            cache_row.get("evidence")
            if isinstance(cache_row, dict) and cache_row.get("revision") == revision
            else None
        )
        if isinstance(cached, dict) and cache_row.get("packet_ref"):
            # record_snapshot removes only these transport/cache flags. Probe
            # bounded actual content-addressed files, never invent a snapshot.
            variants = [cached]
            if "_local_cached" not in cached:
                variants.extend({**cached, "_local_cached": flag} for flag in (True, False, None))
            for value in variants:
                raw = json.dumps(
                    value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
                sha = hashlib.sha256(raw).hexdigest()
                candidates.append(
                    (
                        {
                            "path": str(self.evidence_root() / "snapshots" / (sha + ".json")),
                            "sha256": sha,
                        },
                        cache_row["packet_ref"],
                    )
                )
        for snapshot_ref, packet_ref in candidates:
            try:
                snap = self.retained(snapshot_ref, "snapshots")
                packet = self.retained(packet_ref, "manifests")
                if (
                    not isinstance(snap, dict)
                    or _target(snap.get("target")) != target
                    or not isinstance(revision, str)
                    or not revision
                    or snap.get("revision") != revision
                    or not evidence.validate_packet(packet)
                    or _target(packet["target"]) != target
                    or packet.get("head") != snap.get("head")
                    or packet.get("base") != snap.get("base")
                    or packet.get("subject_pr")
                    != (
                        snap.get("pr_target")
                        or (snap.get("target") if snap.get("kind") == "pr" else None)
                    )
                    or packet["bindings"].get("source") != evidence._hash(snap.get("sources"))
                ):
                    continue
                if isinstance(cached, dict) and any(
                    cached.get(key) != snap.get(key)
                    for key in (
                        "target",
                        "revision",
                        "pr_target",
                        "head",
                        "base",
                        "merge",
                        "head_branch",
                        "head_repository",
                        "sources",
                    )
                ):
                    continue
                return snap, {
                    "snapshot_ref": snapshot_ref,
                    "packet_ref": packet_ref,
                    "producer_version": packet["producer_version"],
                    "producer_sha256": packet["producer_sha256"],
                }
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue
        raise ValueError("complete_immutable_snapshot_unavailable")

    def exact_closure(self, target, row, *, native_queue=False, observation_only=False):
        """Return complete declared aliases, without changing the unknown outcome."""
        snap, proof = self.verified_snapshot(target, row)
        if row.get("provider_outcome_uncertain") not in (None, False):
            raise ValueError("original_provider_scope_unavailable")
        if snap.get("kind") != "pr" or any(
            not isinstance(snap.get(key), str) or not re.fullmatch(r"[0-9a-f]{40}", snap[key])
            for key in ("head", "base")
        ):
            raise ValueError("delivery_scope_incomplete")
        subject_pr = snap.get("pr_target")
        if subject_pr is None:
            subject_pr = snap["target"]
        pr = _target(subject_pr)
        sources = snap.get("sources")
        if not isinstance(sources, list) or not sources or len(sources) > MAX_ENTRIES:
            raise ValueError("source_scope_incomplete")
        if any(
            not isinstance(source, dict) or not isinstance(source.get("body"), str)
            for source in sources
        ):
            raise ValueError("source_scope_incomplete")
        if any(
            not isinstance(snap.get(key), str) or not snap[key] or snap[key] != snap[key].strip()
            for key in ("head_repository", "head_branch")
        ):
            raise ValueError("branch_scope_incomplete")
        source_targets = [_target(source["target"]) for source in sources]
        if len(set(source_targets)) != len(source_targets):
            raise ValueError("source_scope_ambiguous")
        keys = {
            "pr:" + target,
            "issue:" + target,
            "pr:" + pr,
            _key(f"branch:{snap['head_repository']}@{snap['head_branch']}"),
        }
        keys.update("issue:" + source for source in source_targets)
        operation = row.get("operation")
        remote = False
        if isinstance(operation, dict):
            from closer_queue import ACTION_NAMES

            if operation.get("action") not in ACTION_NAMES:
                raise ValueError("original_operation_action_unknown")
            binding = operation.get("binding")
            if (
                not isinstance(binding, dict)
                or any(
                    binding.get(key) != value
                    for key, value in {
                        "pr_target": subject_pr,
                        "head": snap["head"],
                        "merge": snap.get("merge"),
                    }.items()
                )
                or {_target(t) for t in binding.get("source_targets", [])} != set(source_targets)
            ):
                raise ValueError("original_operation_scope_mismatch")
            remote = operation.get("action") in {
                "merge",
                "initiate_verify",
                "post_merge_verify",
                "respond_review",
                "close_source",
            }
            # Unknown history with another binding cannot borrow the current observation.
            for prior in row.get("operation_history", []):
                if not isinstance(prior, dict) or prior.get("binding") != binding:
                    raise ValueError("historical_operation_scope_mismatch")
                if prior.get("action") not in {
                    "merge",
                    "initiate_verify",
                    "post_merge_verify",
                    "respond_review",
                    "close_source",
                }:
                    remote = False
        elif native_queue or row.get("stage") in {"act", "reconcile"}:
            raise ValueError("original_action_scope_unavailable")
        barrier = row.get("recovery_barrier")
        while isinstance(barrier, dict):
            if barrier.get("revision") != row.get("revision") or barrier.get("identity") != row.get(
                "identity"
            ):
                raise ValueError("historical_scope_identity_mismatch")
            barrier = barrier.get("prior_recovery_barrier")
        allowed_targets = {target, pr, *source_targets}
        allowed_repos = {name.split("#")[0] for name in allowed_targets}
        allowed_repos.add(snap["head_repository"].lower())
        for record in (row, operation):
            if not isinstance(record, dict):
                continue
            if "claim_targets" in record and (
                not isinstance(record["claim_targets"], list)
                or not {_target(name) for name in record["claim_targets"]}.issubset(allowed_targets)
            ):
                raise ValueError("original_claim_scope_mismatch")
            if "repo_keys" in record and (
                not isinstance(record["repo_keys"], list)
                or not {_key("repo:" + name)[5:] for name in record["repo_keys"]}.issubset(
                    allowed_repos
                )
            ):
                raise ValueError("original_repository_scope_mismatch")
        if not remote and not observation_only:
            # PR/head/source hashes do not bind an ORIGINAL execution cwd. A
            # current config (including a new target override) cannot narrow an
            # old unknown onto whichever checkout it selects today.
            raise ValueError("original_execution_workspace_unavailable")
        proof["action_contract"] = (
            "record_snapshot_observation_only"
            if observation_only
            else "bound_remote_github_operation" if remote else "bound_prepared_scope"
        )
        proof["filesystem_scope"] = (
            "excluded_by_record_snapshot_contract"
            if observation_only
            else "excluded_by_remote_action_contract" if remote else "authorized_workspace_mapping"
        )
        return keys, proof

    def read(self, path: Path) -> Any:
        if path.is_symlink() or path.resolve() != path:
            raise ValueError("aliased_source:" + str(path))
        if not path.exists():
            if str(path) in self.sources and self.sources[str(path)] is not None:
                self.reasons.append("source_changed_during_census:" + str(path))
                raise ValueError("source_changed_during_census")
            self.sources[str(path)] = None
            return None
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        sha = hashlib.sha256(raw).hexdigest()
        if str(path) in self.sources and self.sources[str(path)] != sha:
            self.reasons.append("source_changed_during_census:" + str(path))
            raise ValueError("source_changed_during_census")
        if str(path) not in self.sources:
            self.bytes += len(raw)
        if (
            len(raw) > MAX_FILE_BYTES
            or self.bytes > MAX_BYTES
            or (str(path) not in self.sources and len(self.sources) >= MAX_ENTRIES)
        ):
            self.reasons.append("census_bound_exceeded")
            raise ValueError("census_bound_exceeded")
        self.sources[str(path)] = sha
        return json.loads(raw)

    def directory(self, path: Path) -> list[Path]:
        if path.is_symlink() or path.resolve() != path:
            raise ValueError("aliased_directory:" + str(path))
        entries = sorted(path.iterdir()) if path.exists() else []
        if len(entries) > MAX_ENTRIES:
            raise ValueError("census_bound_exceeded")
        self.sources[str(path) + "/"] = _digest([p.name for p in entries])
        return entries

    def quarantine(self, target, row, source, run_id, reason):
        target = _target(target)
        repo = target.split("#")[0]
        closure, closure_reason = "repository_fallback", None
        try:
            keys, closure_proof = self.exact_closure(
                target, row, native_queue=run_id == "native_queue"
            )
            closure = "complete_immutable_snapshot"
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            keys = {"repo:" + repo, "pr:" + target, "issue:" + target}
            closure_proof = None
            closure_reason = str(exc)
        # A repository fallback is intentional when a row's historical metadata
        # is missing. Known cross-repository aliases are retained as well.
        if closure == "repository_fallback" and isinstance(row, dict):
            results = row.get("results")
            prepared = results.get("prepare") if isinstance(results, dict) else None
            snapshot = prepared.get("snapshot") if isinstance(prepared, dict) else None
            snapshots = [snapshot] if isinstance(snapshot, dict) else []
            try:
                retained_snapshot, _ = self.verified_snapshot(target, row)
                snapshots.append(retained_snapshot)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                pass
            for snapshot in snapshots:
                if snapshot.get("pr_target"):
                    keys.add("pr:" + _target(snapshot["pr_target"]))
                for item in snapshot.get("sources") or []:
                    keys.add("issue:" + _target(item["target"]))
                if snapshot.get("head_branch") and snapshot.get("head_repository"):
                    keys.add(
                        _key(f"branch:{snapshot['head_repository']}@{snapshot['head_branch']}")
                    )
                for name in ("workspace", "worktree"):
                    if snapshot.get(name):
                        keys.add(_key("worktree:" + snapshot[name]))
        if isinstance(row, dict):
            # Explicit ORIGINAL intent keys remain fences even when a verified
            # remote operation excludes additional filesystem inference.
            for record in (row, row.get("operation")):
                if isinstance(record, dict):
                    for value in record.get("resource_keys") or []:
                        keys.add(_key(value))
        if closure == "repository_fallback":
            for key in list(keys):
                kind, value = key.split(":", 1)
                if kind in {"pr", "issue", "branch"}:
                    keys.add("repo:" + value.split("@")[0].split("#")[0])
            keys.update(self.mapped_keys(target, keys))
        self.keys.update(keys)
        self.items.append(
            {
                "source_path": str(source),
                "source_sha256": self.sources[str(source)],
                "run_id": run_id,
                "target": target,
                "item_sha256": _digest(row),
                "reason": reason,
                "keys": sorted(keys),
                "closure": closure,
                "closure_proof": closure_proof,
                "closure_reason": closure_reason,
            }
        )
        if len(self.items) > MAX_ENTRIES:
            raise ValueError("census_bound_exceeded")

    def history(self, run, path=None):
        local = run.get("invalid_stop_history", [])
        if not isinstance(local, list) or any(not isinstance(item, dict) for item in local):
            raise ValueError("unbounded_shared_history")
        self.history_items(local, path or self.path)
        ref = run.get("invalid_stop_archive")
        if ref is None:
            return
        if not isinstance(ref, dict) or type(ref.get("count")) is not int or ref["count"] < 1:
            raise ValueError("unbounded_archive_reference")
        # Match the pipeline's content-addressed archive path, not an arbitrary
        # path supplied by a damaged historical row.
        from closer_pipeline import AUDIT_ARCHIVE_SCHEMA, _audit_archive_dir

        directory = _audit_archive_dir(path or self.path)
        if ref.get("schema") != AUDIT_ARCHIVE_SCHEMA:
            raise ValueError("unbounded_archive_schema")
        seen, count, sha = set(), 0, ref.get("sha256")
        while sha is not None:
            if not isinstance(sha, str) or not SHA.fullmatch(sha) or sha in seen:
                raise ValueError("unbounded_archive_chain")
            seen.add(sha)
            chunk_path = directory / (sha + ".json")
            chunk = self.read(chunk_path)
            if self.sources[str(chunk_path)] != sha or not isinstance(chunk, dict):
                raise ValueError("unbounded_archive_hash")
            entries = chunk.get("entries")
            if (
                chunk.get("schema") != AUDIT_ARCHIVE_SCHEMA
                or not isinstance(entries, list)
                or not entries
                or any(not isinstance(item, dict) for item in entries)
            ):
                raise ValueError("unbounded_archive_chunk")
            count += len(entries)
            if count > ref["count"] or count > MAX_ENTRIES:
                raise ValueError("unbounded_archive_count")
            self.history_items(entries, chunk_path)
            sha = chunk.get("previous_sha256")
        if count != ref["count"]:
            raise ValueError("unbounded_archive_count")

    def history_items(self, entries, source):
        for record in entries:
            if "targets" not in record:
                continue
            rows = record["targets"]
            if not isinstance(rows, dict):
                raise ValueError("unbounded_history_targets")
            for target, row in rows.items():
                if not isinstance(row, dict) or (
                    "item_sha256" in row
                    and (
                        not isinstance(row["item_sha256"], str)
                        or not SHA.fullmatch(row["item_sha256"])
                    )
                ):
                    self.quarantine(
                        target, row, source, "history", "malformed_anchored_item_history"
                    )

    def ledger(self):
        from closer_pipeline import SCHEMA as PIPELINE_SCHEMA
        from closer_resources import canonical_target

        doc = self.read(self.path)
        if self.sources.get(str(self.path)) is None:
            return
        if (
            not isinstance(doc, dict)
            or doc.get("schema") != PIPELINE_SCHEMA
            or not isinstance(doc.get("runs"), dict)
            or not isinstance(doc.get("carry"), dict)
        ):
            raise ValueError("unbounded_shared_ledger")
        for run_id, run in [*doc["runs"].items(), ("carry", {"targets": doc["carry"]})]:
            if not isinstance(run_id, str) or not isinstance(run, dict):
                raise ValueError("unbounded_run_identity")
            rows = run.get("targets")
            if not isinstance(rows, dict):
                raise ValueError("unbounded_run_targets")
            self.history(run)
            for target, row in rows.items():
                canonical_target(target)
                if isinstance(row, dict) and isinstance(row.get("results"), dict):
                    prepared = row["results"].get("prepare")
                    if isinstance(prepared, dict) and prepared.get("snapshot_ref"):
                        self.preparations.setdefault(
                            (canonical_target(target), row.get("revision")), []
                        ).append((prepared["snapshot_ref"], prepared.get("packet_ref")))
                reason = self.row_problem(self.path, run_id, target, row, doc)
                if reason:
                    self.quarantine(target, row, self.path, run_id, reason)
        from closer_resources import finite_unresolved

        for target, reason in finite_unresolved(doc).items():
            self.quarantine(target, None, self.path, "finite", reason)

    def row_problem(self, source, run_id, target, row, doc=None):
        problem = _row_problem(row)
        if problem:
            return problem
        if isinstance(row, dict) and row.get("deferred_reason") == "scope_quarantined":
            from closer_resources import valid_scope_refusal

            ref = row.get("scope_proof")
            if run_id == "carry":
                return self.carried_refusal(source, target, row, doc)
            if not valid_scope_refusal(source, target, row, run_id):
                return "unverified_scope_refusal"
            self.read(Path(ref["path"]))
            return None
        return None

    def carried_refusal(self, source, target, row, doc):
        """A full carry borrows only its exact, still-retained named origin."""
        from closer_resources import valid_scope_refusal

        try:
            ref = row["scope_proof"]
            proof_path = Path(ref["path"])
            base = source.resolve().parents[2] / "scope-admissions"
            if proof_path.is_symlink() or not proof_path.resolve().is_relative_to(base):
                return "unverified_scope_refusal_origin"
            proof = self.read(proof_path)
            if self.sources[str(proof_path)] != ref["sha256"]:
                return "unverified_scope_refusal_origin"
            origin_id = proof["binding"]["pipeline_run_id"]
            if not isinstance(origin_id, str) or not origin_id or origin_id == "carry":
                return "unverified_scope_refusal_origin"
            origin = doc["runs"][origin_id]["targets"][target]
            if (
                not isinstance(origin, dict)
                or _row_problem(origin)
                or origin != row
                or not valid_scope_refusal(source, target, origin, origin_id)
            ):
                return "unverified_scope_refusal_origin"
            return None
        except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
            return "unverified_scope_refusal_origin"

    def raw_queue(self):
        """Only the exact dispatcher raw sibling is part of this census."""
        suffix = ".pipeline.json"
        if not str(self.path).endswith(suffix) or str(self.path).endswith(".shadow.pipeline.json"):
            return
        source = Path(str(self.path)[: -len(suffix)])
        doc = self.read(source)
        if self.sources.get(str(source)) is None:
            return
        from closer_queue import SCHEMA as QUEUE_SCHEMA
        from closer_resources import canonical_target

        if (
            not isinstance(doc, dict)
            or doc.get("schema") != QUEUE_SCHEMA
            or not isinstance(doc.get("items"), dict)
            or not isinstance(doc.get("round"), dict)
        ):
            raise ValueError("unbounded_raw_queue")
        for target, row in doc["items"].items():
            canonical_target(target)
            if (
                isinstance(row, dict)
                and set(row) == {"evidence", "revision", "observation_freshness", "packet_ref"}
                and row["observation_freshness"] == "remote_snapshot"
            ):
                try:
                    snap, proof = self.verified_snapshot(_target(target), row)
                    if snap.get("kind") == "pr":
                        _, proof = self.exact_closure(_target(target), row, observation_only=True)
                    elif not (
                        snap.get("kind") == "issue"
                        and snap.get("head") is None
                        and snap.get("base") is None
                        and snap.get("pr_target") is None
                        and isinstance(snap.get("sources"), list)
                        and bool(snap["sources"])
                        and all(
                            isinstance(item, dict)
                            and isinstance(item.get("body"), str)
                            and _target(item.get("target")) == _target(target)
                            for item in snap["sources"]
                        )
                    ):
                        raise ValueError("unsupported_observation_scope")
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    pass
                else:
                    self.observations.append(
                        {
                            "target": _target(target),
                            "source_path": str(source),
                            "source_sha256": self.sources[str(source)],
                            "item_sha256": _digest(row),
                            "producer_contract": "record_snapshot_preserves_operation_and_history",
                            "closure_proof": proof,
                            "acceptance_authority": False,
                        }
                    )
                    continue
            safe = False
            if isinstance(row, dict):
                history = row.get("operation_history", [])
                operations = [row.get("operation"), *(history if isinstance(history, list) else [])]
                known = isinstance(history, list) and all(
                    op is None
                    or (
                        isinstance(op, dict)
                        and op.get("status") in {"completed", "confirmed_not_started", "abandoned"}
                    )
                    for op in operations
                )
                safe = known and (
                    row.get("status") == "complete"
                    or (
                        row.get("status") in {"deferred", "ineligible"}
                        and row.get("action") == "wait"
                        and row.get("eligible") is False
                        and row.get("operation") is None
                    )
                )
            if not safe:
                # Preserve the actual native row hash; augment aliases separately
                # rather than manufacturing a pipeline preparation or receipt.
                self.quarantine(target, row, source, "native_queue", "unresolved_native_item")
                if isinstance(row, dict):
                    aliases = set()
                    for claim in row.get("claim_targets") or []:
                        aliases.add("issue:" + _target(claim))
                    if self.items[-1]["closure"] == "repository_fallback":
                        for repo in row.get("repo_keys") or []:
                            aliases.add(_key("repo:" + repo))
                    operation = row.get("operation")
                    if isinstance(operation, dict):
                        binding = operation.get("binding") or {}
                        for claim in binding.get("source_targets") or []:
                            aliases.add("issue:" + _target(claim))
                    if self.items[-1]["closure"] == "repository_fallback":
                        for key in list(aliases):
                            kind, value = key.split(":", 1)
                            if kind in {"pr", "issue", "branch"}:
                                aliases.add("repo:" + value.split("@")[0].split("#")[0])
                        aliases.update(self.mapped_keys(_target(target), aliases))
                    self.keys.update(aliases)
                    self.items[-1]["keys"] = sorted(set(self.items[-1]["keys"]) | aliases)

    def raw_index(self):
        """Index exact sibling observations before recovering compact item scope."""
        suffix = ".pipeline.json"
        if not str(self.path).endswith(suffix) or str(self.path).endswith(".shadow.pipeline.json"):
            return
        source = Path(str(self.path)[: -len(suffix)])
        doc = self.read(source)
        if self.sources.get(str(source)) is None:
            return
        from closer_queue import SCHEMA as QUEUE_SCHEMA
        from closer_resources import canonical_target

        if (
            not isinstance(doc, dict)
            or doc.get("schema") != QUEUE_SCHEMA
            or not isinstance(doc.get("items"), dict)
            or not isinstance(doc.get("round"), dict)
        ):
            raise ValueError("unbounded_raw_queue")
        self.raw_records = {canonical_target(target): row for target, row in doc["items"].items()}

    def reservations(self):
        from closer_resources import DURABLE_KINDS, _reservation_bases, scoped_root

        for base in _reservation_bases(scoped_root(self.path)):
            for directory in self.directory(base):
                if not directory.is_dir() or directory.is_symlink():
                    raise ValueError("unbounded_reservation_directory")
                for filename in ("intent.json", "legacy-intent.json"):
                    source = directory / filename
                    ad = self.read(source)
                    if self.sources.get(str(source)) is None:
                        continue
                    if not isinstance(ad, dict):
                        raise ValueError("unbounded_reservation_record")
                    key = _key(ad.get("key"))
                    if (
                        key != ad.get("key")
                        or key.split(":", 1)[0] not in DURABLE_KINDS
                        or hashlib.sha256(key.encode()).hexdigest()[:24] != directory.name
                    ):
                        raise ValueError("unbounded_reservation_binding")
                    target = _target(ad.get("target"))
                    keys = {key} | self.mapped_keys(target, {key})
                    self.keys.update(keys)
                    self.items.append(
                        {
                            "source_path": str(source),
                            "source_sha256": self.sources[str(source)],
                            "target": target,
                            "item_sha256": _digest(ad),
                            "reason": "retained_resource_intent",
                            "keys": sorted(keys),
                        }
                    )

    def shards(self):
        """Bound corrupt historical shards by their independently checked lease.

        A held, unexpired lease identifies an exactly marked current callback;
        it never discharges an explicit barrier or an earlier interrupted row.
        No lock files are created and no lease is changed.
        """
        from closer_pipeline import SCHEMA as PIPELINE_SCHEMA
        from closer_resources import CONTRACT_SCHEMA, LAYOUT, canonical_target, scoped_root

        root = scoped_root(self.path)
        if not root.exists():
            self.sources[str(root) + "/"] = None
            return
        contract = self.read(root / "contract.json")
        expected = {
            "schema": CONTRACT_SCHEMA,
            "layout": LAYOUT,
            "pipeline_schema": PIPELINE_SCHEMA,
            "coordination_gates": 1,
        }
        if not isinstance(contract, dict) or any(
            type(contract.get(k)) is not type(v) or contract.get(k) != v
            for k, v in expected.items()
        ):
            raise ValueError("unbounded_scoped_contract")
        for directory in self.directory(root / "targets"):
            if not directory.is_dir() or directory.is_symlink():
                raise ValueError("unbounded_shard_directory")
            lease = self.read(directory / "lease.json")
            if not isinstance(lease, dict):
                raise ValueError("unbounded_shard_lease")
            target = canonical_target(lease.get("target"))
            if directory.name != hashlib.sha256(target.encode()).hexdigest()[:24]:
                raise ValueError("unbounded_shard_identity")
            source = directory / "state.json"
            # A just-acquired first lease may precede the first state write.
            lock = directory / "lease.lock"
            if lock.is_symlink() or lock.resolve() != lock:
                raise ValueError("aliased_shard_lock")
            held = False
            if lock.exists():
                with lock.open("rb") as stream:
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        held = True
                    else:
                        fcntl.flock(stream, fcntl.LOCK_UN)
            self.sources[str(lock) + ":held"] = str(held)
            finite_live = _live_finite_lease(lease, target, held, time.time())
            try:
                doc = self.read(source)
                if self.sources.get(str(source)) is None:
                    first_lease = (
                        held
                        and type(lease.get("epoch")) is int
                        and lease["epoch"] == 1
                        and isinstance(lease.get("owner"), str)
                        and bool(lease["owner"])
                        and all(
                            type(lease.get(key)) in {int, float} and math.isfinite(lease[key])
                            for key in ("acquired_at", "expires_at")
                        )
                        and lease["acquired_at"] <= time.time() <= lease["expires_at"]
                    )
                    if not first_lease and not (finite_live and lease["epoch"] == 1):
                        self.quarantine(target, None, source, "shard", "missing_shard_state")
                    continue
                if (
                    not isinstance(doc, dict)
                    or doc.get("schema") != PIPELINE_SCHEMA
                    or not isinstance(doc.get("runs"), dict)
                    or not isinstance(doc.get("carry"), dict)
                ):
                    raise ValueError("malformed_shard_state")
                acquired, expires = lease.get("acquired_at"), lease.get("expires_at")
                live = (
                    held
                    and isinstance(lease.get("owner"), str)
                    and bool(lease["owner"])
                    and type(lease.get("epoch")) is int
                    and lease["epoch"] > 0
                    and all(
                        type(v) in {int, float} and math.isfinite(v) for v in (acquired, expires)
                    )
                    and acquired <= time.time() <= expires
                )
                for run_id, run in [*doc["runs"].items(), ("carry", {"targets": doc["carry"]})]:
                    if not isinstance(run, dict) or not isinstance(run.get("targets"), dict):
                        raise ValueError("malformed_shard_run")
                    self.history(run, source)
                    for name, row in run["targets"].items():
                        if canonical_target(name) != target:
                            raise ValueError("shard_target_mismatch")
                        fresh = (
                            live
                            and isinstance(row, dict)
                            and isinstance(row.get("scope_lease"), dict)
                            and row["scope_lease"]
                            == {"target": target, "owner": lease["owner"], "epoch": lease["epoch"]}
                            and type(row["scope_lease"].get("epoch")) is int
                            and type(row.get("last_scheduled")) in {int, float}
                            and math.isfinite(row["last_scheduled"])
                            and acquired <= row["last_scheduled"] <= time.time()
                            and row.get("status") in {"queued", "running"}
                            and row.get("recovery_barrier") is None
                            and row.get("provider_outcome_uncertain") in (None, False)
                            and row.get("deferred_reason") != "scope_quarantined"
                        )
                        if not fresh and (
                            reason := self.row_problem(source, run_id, name, row, doc)
                        ):
                            self.quarantine(name, row, source, run_id, "shard_" + reason)
                # New explicit frontiers coexist with every historical run. An
                # interrupted/mismatched finite owner never inherits live status.
                frontiers = doc.get("frontiers", {})
                if not isinstance(frontiers, dict):
                    raise ValueError("malformed_finite_frontiers")
                for frontier_id, entry in frontiers.items():
                    from closer_frontier_policy import validate_frontier
                    from closer_resources import finite_unresolved

                    descriptor = validate_frontier(entry.get("finite_frontier"))
                    items = entry.get("items")
                    if (
                        entry.get("schema") != "closer-pipeline-frontier/v2"
                        or descriptor["frontier_id"] != frontier_id
                        or not isinstance(items, dict)
                        or len(items) != 1
                        or {_target(name) for name in items} != {target}
                    ):
                        raise ValueError("malformed_finite_shard")
                    unresolved = finite_unresolved({"frontiers": {frontier_id: entry}})
                    for name, row in items.items():
                        if canonical_target(name) != target or not isinstance(row, dict):
                            raise ValueError("finite_shard_target_mismatch")
                        active = (
                            finite_live
                            and descriptor == lease.get("finite_frontier")
                            and row.get("scope_lease")
                            == {"target": target, "owner": lease["owner"], "epoch": lease["epoch"]}
                            and type(row.get("last_scheduled")) in {int, float}
                            and math.isfinite(row["last_scheduled"])
                            and lease["acquired_at"] <= row["last_scheduled"] <= time.time()
                            and row.get("status") in {"pending", "running"}
                            and row.get("stage_status") != "unknown"
                            and row.get("recovery_barrier") is None
                            and row.get("provider_outcome_uncertain") in (None, False)
                        )
                        if active:
                            continue
                        if target in unresolved:
                            self.quarantine(
                                name, row, source, entry.get("run_id"), "finite_frontier_unresolved"
                            )
            except (ValueError, TypeError, KeyError, AttributeError):
                # The lease target and directory hash were checked independently.
                # A bad state/history cannot expand that known owner into all repos.
                if str(source) not in self.sources:
                    raise
                self.quarantine(target, None, source, "shard", "malformed_anchored_shard")


def _row_problem(row):
    if not isinstance(row, dict):
        return "malformed_item"
    if row.get("recovery_barrier") is not None:
        return "retained_recovery_barrier"
    if row.get("provider_outcome_uncertain") not in (None, False):
        return "provider_outcome_unknown"
    status, stage = row.get("status"), row.get("stage")
    if status in {"running", "uncertain"}:
        return "interrupted_or_uncertain_item"
    if status == "queued" and stage in {"exception", "act", "reconcile"}:
        return "unproved_queued_boundary"
    if status == "failed" and stage == "act":
        return "failed_action_boundary"
    if not isinstance(row.get("results", {}), dict):
        return "malformed_item_results"
    if row.get("error") in {
        "expired read deferral lacks a verified terminal resource boundary",
        "expired read retained a historical wait from another revision",
        "interrupted read lacks a verified terminal boundary",
    }:
        return "unverified_read_boundary"
    if status not in {"queued", "completed", "deferred", "pending", "failed"}:
        return "unknown_item_status"
    return None


def scope_snapshot(state_path: str | Path, *, workspaces=None) -> dict:
    """Census original bytes without ledger writes, callbacks or recovery.

    The short shared inventory lock observes atomic shard identity publication;
    it never covers model or action work. Callers retain their normal alias gates.
    A corrupt shared history remains globally unbounded.
    """
    original = Path(state_path).absolute()
    path = original.resolve()
    census = _Census(path, workspaces)
    if original.is_symlink():
        census.reasons.append("aliased_state_leaf")
    from closer_resources import inventory_lock, scoped_root

    try:
        if not path.parent.is_dir():
            raise ValueError("missing_source_parent")
        with inventory_lock(scoped_root(path)):
            for collect in (
                census.raw_index,
                census.ledger,
                census.raw_queue,
                census.reservations,
                census.shards,
            ):
                try:
                    collect()
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                    RecursionError,
                ) as exc:
                    census.reasons.append(type(exc).__name__ + ":" + str(exc))
    except (OSError, ValueError):
        census.reasons.append("inventory_lock_unavailable")
    value = {
        "schema": SCHEMA,
        "state_path": str(path),
        "quarantine_keys": sorted(census.keys),
        "global_reasons": sorted(set(census.reasons)),
        "items": census.items,
        "observations": census.observations,
        "source_hashes": dict(sorted(census.sources.items())),
        "workspaces": dict(sorted(census.workspaces.items())),
        "execution_workspaces": dict(sorted(census.execution_workspaces.items())),
        "acceptance_authority": False,
        "global_stop_authority": False,
    }
    return {**value, "snapshot_sha256": _digest(value)}


def scope_problem(snapshot: dict, target: str, resource_keys) -> str | None:
    """Check complete prepared aliases; never interpret missing scope as disjoint.

    The integrator must prove inventory completeness from its retained prepared
    snapshot (including configured workspaces), not from this iterable alone.
    """
    try:
        if (
            snapshot.get("schema") != SCHEMA
            or snapshot.get("snapshot_sha256")
            != _digest({k: v for k, v in snapshot.items() if k != "snapshot_sha256"})
            or snapshot.get("acceptance_authority") is not False
            or snapshot.get("global_stop_authority") is not False
        ):
            return "invalid_scope_snapshot"
        if (
            scope_snapshot(
                snapshot["state_path"],
                workspaces={
                    "schema": WORKSPACE_SCHEMA,
                    "historical": snapshot.get("workspaces", {}),
                    "execution": snapshot.get("execution_workspaces", {}),
                },
            )
            != snapshot
        ):
            return "scope_sources_changed"
        if snapshot["global_reasons"]:
            return "unbounded_scope:" + snapshot["global_reasons"][0]
        if not isinstance(resource_keys, (list, tuple, set, frozenset)):
            return "incomplete_resource_inventory"
        keys = {_key(key) for key in resource_keys}
        try:
            canonical = _target(target)
        except ValueError:
            return "unbounded_target_identity" if snapshot["quarantine_keys"] else None
        repo = canonical.split("#")[0]
        if not ({"pr:" + canonical, "issue:" + canonical} & keys) or "repo:" + repo not in keys:
            return "incomplete_resource_inventory"
        mapped = current_workspace_keys(
            canonical,
            keys,
            {
                "schema": WORKSPACE_SCHEMA,
                "historical": snapshot["workspaces"],
                "execution": snapshot.get("execution_workspaces", {}),
            },
        )
        if not mapped.issubset(keys):
            return "incomplete_resource_inventory"
        for key in keys:
            kind, value = key.split(":", 1)
            if kind in {"pr", "issue", "branch"}:
                linked_repo = value.split("#")[0] if kind != "branch" else value.split("@")[0]
                if "repo:" + linked_repo not in keys:
                    return "incomplete_resource_inventory"
            if any(resource_overlap(key, held) for held in snapshot["quarantine_keys"]):
                return "quarantined_resource:" + key
        return None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return "invalid_resource_scope"


def selftest() -> None:
    """Exercise real census wiring using only a self-owned temporary ledger."""
    import tempfile

    from closer_pipeline import SCHEMA as PIPELINE_SCHEMA

    passed = 0
    with tempfile.TemporaryDirectory(prefix="closer-quarantine-selftest-") as directory:
        root = Path(directory).resolve()
        path = root / "pipeline.json"
        original = json.dumps(
            {
                "schema": PIPELINE_SCHEMA,
                "runs": {
                    "original": {
                        "targets": {
                            "owner/held#1": {"status": "queued", "stage": "exception"},
                        }
                    }
                },
                "carry": {},
            }
        ).encode()
        path.write_bytes(original)
        snapshot = scope_snapshot(path)
        assert not snapshot["global_reasons"]
        assert "repo:owner/held" in snapshot["quarantine_keys"]
        passed += 1
        assert scope_problem(snapshot, "owner/held#2", ["pr:owner/held#2", "repo:owner/held"])
        passed += 1
        healthy = ["pr:owner/healthy#2", "repo:owner/healthy"]
        assert scope_problem(snapshot, "owner/healthy#2", healthy) is None
        passed += 1
        altered = {**snapshot, "quarantine_keys": []}
        assert scope_problem(altered, "owner/healthy#2", healthy) == "invalid_scope_snapshot"
        passed += 1
        assert path.read_bytes() == original
        passed += 1
        malformed = root / "malformed.json"
        malformed.write_bytes(b"{")
        broken = scope_snapshot(malformed)
        assert broken["global_reasons"] and scope_problem(broken, "owner/healthy#2", healthy)
        assert malformed.read_bytes() == b"{" and path.read_bytes() == original
        passed += 1
    print(f"closer_quarantine selftest: {passed} passed")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true", required=True)
    parser.parse_args()
    selftest()
