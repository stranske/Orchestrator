"""Carry already-owned delivery objectives through merge in the controller's state.

Planning only: no native queue, effect, acceptance judgment or alternative owner.
The caller keeps its existing demand lock and publishes normal assignments.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

TARGET = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")
SHA = re.compile(r"^[0-9a-f]{40}$")
LANES = {"closer", "orphan", "maintenance"}
# Memory/record bounds, not execution-time budgets or evidence necessity gates.
MAX_DEMAND_STATE_BYTES = 16 * 1024 * 1024
MAX_PROVENANCE_BYTES = 64 * 1024 * 1024


def _read_state_bytes(path: Any) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(MAX_DEMAND_STATE_BYTES + 1)
    if len(raw) > MAX_DEMAND_STATE_BYTES:
        raise ValueError("demand-state JSON size bound exceeded")
    return raw


def _provenance_sha(path: Any) -> str:
    import stat

    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_PROVENANCE_BYTES:
        raise ValueError("candidate provenance file size/type bound exceeded")
    result, total = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_PROVENANCE_BYTES:
                raise ValueError("candidate provenance growth bound exceeded")
            result.update(chunk)
    return result.hexdigest()


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def plan(
    carried: Mapping[str, Any],
    seeds: list[dict],
    *,
    observe: Callable[[str], Mapping[str, Any]],
    policy: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    now: float,
) -> dict:
    """Reobserve exact locators. Old ownership never authorizes a current effect.

    `observe` supplies actual source/PR/head/merge and an explicit association.
    `policy` is the existing controller owner/hold/dependency policy, not a model.
    Final resolution needs exact head/merge-bound evidence supplied by the observer.
    """
    candidates = copy.deepcopy(dict(carried))
    for seed in seeds:
        target = seed.get("target")
        if isinstance(target, str) and TARGET.fullmatch(target):
            candidates.setdefault(target, copy.deepcopy(seed))
    rows, ownership, retained, retired, failures = [], {}, {}, [], []
    for target, old in sorted(candidates.items()):
        if not TARGET.fullmatch(target) or not isinstance(old, dict):
            raise ValueError("malformed continuity locator")
        try:
            fresh = dict(observe(target))
            subject = fresh.get("pr_target")
            if (
                fresh.get("target") != target
                or not isinstance(subject, str)
                or not TARGET.fullmatch(subject)
                or fresh.get("association_verified") is not True
                or (old.get("pr_target") not in (None, subject))
                or fresh.get("state") != "MERGED"
                or not SHA.fullmatch(str(fresh.get("head", "")))
                or not SHA.fullmatch(str(fresh.get("merge", "")))
            ):
                raise ValueError("fresh delivery identity/association incomplete or changed")
            decision = dict(policy(target, fresh))
            owner = decision.get("owner")
            if owner not in LANES:
                raise ValueError("current owner removed or unsupported")
            resolution = fresh.get("final_resolution")
            if (
                isinstance(resolution, Mapping)
                and all(
                    resolution.get(k) == fresh.get(k)
                    for k in ("target", "pr_target", "head", "merge")
                )
                and resolution.get("verified") is True
                and resolution.get("status") == "resolved"
            ):
                retired.append({"target": target, "resolution": dict(resolution)})
                continue
            # Source closure and workflow completion intentionally do not retire a row.
            held = decision.get("held") is not False or decision.get("dependency") is not None
            state = "held" if held else "active"
            action = (
                decision.get("reason")
                if held
                else "Complete merge-bound verification and source acceptance"
            )
            row = {
                "target": target,
                "pr_target": subject,
                "head": fresh["head"],
                "merge": fresh["merge"],
                "lane": owner,
                "delivery_owner": owner,
                "execution_state": state,
                "action": action or "Current delivery admission unresolved",
                "fingerprint": decision.get("fingerprint")
                or digest({k: fresh.get(k) for k in ("target", "pr_target", "head", "merge")}),
                "eligibility_reason": "owned_delivery_continuity",
                "observed_at": now,
                "continuity_provenance": copy.deepcopy(old.get("provenance", {})),
            }
            for key in ("diagnosed_fingerprint", "unchanged_since", "due"):
                if key in decision.get("normal_metadata", {}):
                    row[key] = decision["normal_metadata"][key]
            rows.append(row)
            ownership[target] = {
                "owner": owner,
                "state": state,
                "action": row["action"],
                "via": [],
                "delivery_pr": subject,
            }
            retained[target] = {
                "target": target,
                "pr_target": subject,
                "owner": owner,
                "head": fresh["head"],
                "merge": fresh["merge"],
                "provenance": row["continuity_provenance"],
                "verifier_material": fresh.get("verifier_material", {}),
                "verifier_material_epoch": fresh.get("verifier_material_epoch"),
                "diagnostic_coverage": decision.get("diagnostic_coverage"),
            }
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            # Keep exact prior locator, not active authority, on incomplete reads.
            retained[target] = {
                **old,
                "last_failure": {
                    "reason": str(exc),
                    "observed_at": now,
                    "requirement": "fresh authoritative delivery identity, owner and holds",
                    "next_trigger": "fresh complete source/PR observation or scoped owner disposition",
                },
            }
            owner = old.get("owner")
            if owner in LANES:
                row = {
                    "target": target,
                    "lane": owner,
                    "delivery_owner": owner,
                    "execution_state": "held",
                    "action": "Reobserve delivery continuity: " + str(exc),
                    "eligibility_reason": "delivery_continuity_unresolved",
                    "observed_at": now,
                }
                rows.append(row)
                ownership[target] = {
                    "owner": owner,
                    "state": "held",
                    "action": row["action"],
                    "via": [],
                }
            failures.append({"target": target, "reason": str(exc), "status": "unresolved"})
    return {
        "rows": rows,
        "ownership": ownership,
        "carry": retained,
        "retired": retired,
        "failures": failures,
    }


def merge_plan(
    result: dict, routes: dict, observations: dict, ownership: dict, waits: list
) -> None:
    """Add only absent objectives; an explicit current route/hold always wins."""
    present = {r["target"] for group in routes.values() for r in group}
    present.update(r["target"] for r in waits)
    present.update(observations)
    for row in result["rows"]:
        if row["target"] in present:
            continue
        if row["execution_state"] == "active":
            routes[row["lane"]].append(row)
        else:
            waits.append(row)
        observations[row["target"]] = row
        ownership[row["target"]] = result["ownership"][row["target"]]


def prepare_controller(
    router: Any,
    state: dict,
    prs: dict,
    now: float,
    terminal: dict | None = None,
    blocker_context: tuple | None = None,
) -> dict:
    """Use existing controller GitHub transport and owner policy; no native effects.

    The source of candidates is existing persisted demand state plus previously
    observed current open PRs. Optional backfill locators must first be reviewed
    into the same state by its owner; this API never imports a task cohort.
    """
    seeds = []
    prior = state.get("targets", {})
    ownership = state.get("delivery_ownership", {})
    for target, row in prior.items():
        owner = ownership.get(target, {}).get("owner") or row.get("delivery_owner")
        if owner in LANES and SHA.fullmatch(str(row.get("head", ""))):
            seeds.append(
                {
                    "target": target,
                    "pr_target": row.get("pr_target"),
                    "owner": owner,
                    "provenance": {
                        "kind": "prior_controller_owner",
                        "fingerprint": row.get("fingerprint"),
                    },
                }
            )
    open_targets = {f"{router.inventory.repo(n)}#{n['number']}" for n in prs.get("nodes", [])}
    carry = dict(state.get("delivery_continuity", {}))
    # Existing open inventory owns those rows; remember locators for the next tick.
    for node in prs.get("nodes", []):
        target = f"{router.inventory.repo(node)}#{node['number']}"
        owner = router.pr_owner(node)
        if owner in LANES:
            carry.setdefault(
                target,
                {
                    "target": target,
                    "pr_target": target,
                    "owner": owner,
                    "provenance": {"kind": "controller_pr_inventory"},
                },
            )
    deferred = {k: v for k, v in carry.items() if k in open_targets}
    carry = {k: v for k, v in carry.items() if k not in open_targets}
    seeds = [s for s in seeds if s["target"] not in open_targets]
    nodes = {}

    def read_node(target: str) -> dict:
        repo, number = target.split("#")
        owner, name = repo.split("/")
        remainder = router.QUERY.split("... on PullRequest{", 1)[1]
        depth = 1
        end = None
        for index, char in enumerate(remainder):
            depth += (char == "{") - (char == "}")
            if depth == 0:
                end = index
                break
        if end is None:
            raise ValueError("controller PR query shape changed")
        fields = remainder[:end]
        fields = fields.replace(
            "closingIssuesReferences(first:20){nodes{number repository{nameWithOwner}}}",
            "closingIssuesReferences(first:20){pageInfo{hasNextPage} nodes{number state "
            "labels(first:100){nodes{name}} repository{nameWithOwner}}}",
        )
        fields += " comments(last:100){nodes{id body createdAt author{login}}}"
        query = (
            "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
            "issueOrPullRequest(number:$number){__typename ... on Issue{state body number url "
            "labels(first:100){nodes{name}} repository{nameWithOwner}} "
            "... on PullRequest{state mergedAt mergeCommit{oid} " + fields + "}}}}"
        )
        code, stdout, stderr = router.inventory.run(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                "query=" + query,
                "-f",
                "owner=" + owner,
                "-f",
                "name=" + name,
                "-F",
                "number=" + number,
            ],
            timeout=60,
        )
        if code:
            raise RuntimeError(stderr.strip() or "continuity GitHub read failed")
        doc = json.loads(stdout)
        if doc.get("errors"):
            raise ValueError("continuity GraphQL errors")
        node = doc["data"]["repository"]["issueOrPullRequest"]
        if f"{router.inventory.repo(node)}#{node['number']}".lower() != target.lower():
            raise ValueError("fresh target identity mismatch")
        return node

    locators = {**{s["target"]: s for s in seeds}, **carry}
    source_holds = {}

    def observe(target: str) -> dict:
        source = read_node(target)
        subject = target
        node = source
        if source.get("__typename") == "Issue":
            subject = locators[target].get("pr_target")
            if not isinstance(subject, str) or not TARGET.fullmatch(subject) or subject == target:
                raise ValueError(
                    "source delivery association requires authenticated pending receipt"
                )
            node = read_node(subject)
            links = (node.get("closingIssuesReferences") or {}).get("nodes")
            if not isinstance(links, list) or target.lower() not in {
                f"{router.inventory.repo(n)}#{n['number']}".lower() for n in links
            }:
                raise ValueError("current source/delivery association is absent or ambiguous")
            source_holds[target] = {n["name"] for n in source.get("labels", {}).get("nodes", [])}
        if node.get("__typename") != "PullRequest":
            raise ValueError("delivery object is not a pull request")
        linked = node.get("closingIssuesReferences") or {}
        if (linked.get("pageInfo") or {}).get("hasNextPage"):
            raise ValueError("linked source hold inventory incomplete")
        source_holds.setdefault(target, set()).update(
            label["name"]
            for source_node in linked.get("nodes", [])
            for label in (source_node.get("labels") or {}).get("nodes", [])
        )
        old = locators[target]
        baseline = old.get("verifier_material")
        material = dict(baseline or {})
        for comment in (node.get("comments") or {}).get("nodes", []):
            body = comment.get("body")
            if (
                isinstance(body, str)
                and body.startswith("## Provider Comparison Report")
                and (comment.get("author") or {}).get("login")
                in {"github-actions[bot]", "github-actions"}
                and isinstance(comment.get("id"), str)
                and node.get("mergedAt")
                and comment.get("createdAt", "") >= node["mergedAt"]
            ):
                material[comment["id"]] = digest(body)
        epoch = old.get("verifier_material_epoch")
        # First observation is a baseline, never a fabricated new-response event.
        if baseline is not None and material != baseline:
            epoch = digest(material)
        if epoch is not None:
            node["_continuity_verifier_material"] = epoch
        nodes[target] = node
        return {
            "target": target,
            "pr_target": subject,
            "state": node["state"],
            "head": node.get("headRefOid"),
            "merge": (node.get("mergeCommit") or {}).get("oid"),
            "association_verified": True,
            "verifier_material": material,
            "verifier_material_epoch": epoch,
        }

    held, challenge, retry = (
        blocker_context if blocker_context is not None else router.inventory.scoped_blocker_states()
    )

    def policy(target: str, fresh: Mapping[str, Any]) -> dict:
        node = nodes[target]
        subject = f"{router.inventory.repo(node)}#{node['number']}"
        local_state, local_terminal = state, terminal or {}
        if subject != target:
            local_state = copy.deepcopy(state)
            if target in local_state.get("targets", {}):
                local_state["targets"][subject] = local_state["targets"][target]
            local_terminal = copy.deepcopy(terminal or {})
            coverage = local_terminal.get("coverage_json", {})
            if target in coverage:
                coverage[subject] = coverage[target]
            if local_terminal.get("selected_target") == target:
                local_terminal["selected_target"] = subject
        retained_coverage = locators[target].get("diagnostic_coverage")
        if isinstance(retained_coverage, dict) and subject not in local_terminal.get(
            "coverage_json", {}
        ):
            local_terminal = copy.deepcopy(local_terminal)
            local_terminal.setdefault("coverage_json", {})[subject] = retained_coverage
        normal_routes, normal_observations, normal_waits, normal_ignored = router.build_routes(
            [node], {}, local_state, local_terminal, now, held, challenge, retry
        )
        normal = normal_observations.get(subject)
        if not isinstance(normal, dict):
            raise ValueError("normal controller did not classify exact delivery")
        lane = normal.get("execution_state")
        reason = normal.get("action")
        owner = router.pr_owner(node)
        ordinary_route = any(row.get("target") == subject for row in normal_routes.get(owner, []))
        labels = source_holds.get(target, set()) | {
            n["name"] for n in node.get("labels", {}).get("nodes", [])
        }
        paused = bool(
            labels
            & {
                "agents:auto-pilot-pause",
                "agents:paused",
                "agent:paused",
                "do-not-merge",
                "hold",
            }
        )
        return {
            "owner": owner,
            "held": not ordinary_route
            or paused
            or lane != "active"
            or target in held
            or target in challenge
            or target in retry,
            "dependency": (
                "maintenance owner must use its existing campaign admission"
                if router.pr_owner(node) == "maintenance"
                else None
            ),
            "fingerprint": normal.get("fingerprint"),
            "normal_metadata": {
                k: normal[k]
                for k in ("diagnosed_fingerprint", "unchanged_since", "due")
                if k in normal
            },
            "diagnostic_coverage": {
                k: v
                for k, v in local_terminal.get("coverage_json", {}).get(subject, {}).items()
                if k
                in {
                    "head",
                    "fingerprint",
                    "execution_error",
                    "park_until_material_change",
                    "outcome_class",
                    "due",
                    "action",
                }
            },
            "reason": "Current source/PR hold" if paused else reason,
        }

    result = plan(carry, seeds, observe=observe, policy=policy, now=now)
    result["carry"].update(deferred)
    return result


def plan_enrollment(state_path: Any, candidates: list[dict]) -> dict:
    """Dry plan to enroll authenticated locators, never executable assignments."""
    from pathlib import Path

    path = Path(state_path).resolve()
    raw = _read_state_bytes(path)
    state = json.loads(raw)
    if not isinstance(state, dict):
        raise ValueError("demand state must be an object")
    enrolled = {}
    for candidate in candidates:
        target, subject = candidate.get("target"), candidate.get("pr_target")
        if not isinstance(target, str) or not TARGET.fullmatch(target):
            raise ValueError("invalid enrollment target")
        if subject is not None and (not isinstance(subject, str) or not TARGET.fullmatch(subject)):
            raise ValueError("invalid delivery locator")
        refs = candidate.get("provenance_refs")
        if not isinstance(refs, list) or not refs:
            raise ValueError("candidate locator needs retained provenance")
        for ref in refs:
            if _provenance_sha(Path(ref["path"])) != ref["sha256"]:
                raise ValueError("candidate provenance bytes changed")
        if target in enrolled:
            raise ValueError("duplicate enrollment target")
        # No admission/held/ready/PASS/model/action state can enter this API.
        enrolled[target] = {
            "target": target,
            "pr_target": subject,
            "provenance": {"kind": "reviewed_locator_enrollment", "refs": copy.deepcopy(refs)},
        }
        old = state.get("delivery_continuity", {}).get(target)
        if old is not None and old.get("pr_target") not in (None, subject):
            raise ValueError("conflicting existing delivery locator")
    return {
        "schema": "closer-continuity-enrollment/v1",
        "state_path": str(path),
        "expected_state_sha256": hashlib.sha256(raw).hexdigest(),
        "locators": enrolled,
    }


def apply_enrollment(state_path: Any, prepared: Mapping[str, Any]) -> dict:
    """Exact CAS under the controller's existing lock; no native state/assignments."""
    import fcntl
    import os
    import tempfile
    from pathlib import Path

    path = Path(state_path).resolve()
    if prepared.get("schema") != "closer-continuity-enrollment/v1" or prepared.get(
        "state_path"
    ) != str(path):
        raise ValueError("enrollment plan target differs")
    with (path.parent / ".demand.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raw = _read_state_bytes(path)
        if hashlib.sha256(raw).hexdigest() != prepared.get("expected_state_sha256"):
            raise ValueError("demand-state CAS changed; prepare again")
        # Revalidate provenance and locator-only shape immediately before publication.
        locators = prepared.get("locators")
        if not isinstance(locators, dict):
            raise ValueError("malformed enrollment plan")
        candidates = [
            {
                "target": target,
                "pr_target": row.get("pr_target"),
                "provenance_refs": row.get("provenance", {}).get("refs"),
            }
            for target, row in locators.items()
        ]
        checked = plan_enrollment(path, candidates)
        if checked != dict(prepared):
            raise ValueError("enrollment plan changed or includes admission authority")
        state = json.loads(raw)
        for target, row in locators.items():
            existing = state.setdefault("delivery_continuity", {}).get(target)
            if existing is None:
                state["delivery_continuity"][target] = row
            elif existing.get("pr_target") is None and row.get("pr_target") is not None:
                existing["pr_target"] = row["pr_target"]
                existing.setdefault("enrollment_provenance", []).append(row["provenance"])
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".continuity-")
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(state, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if Path(temporary).stat().st_size > MAX_DEMAND_STATE_BYTES:
                raise ValueError("enrolled demand-state JSON size bound exceeded")
            os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {
        "status": "enrolled_locators_only",
        "targets": sorted(locators),
        "state_sha256": hashlib.sha256(_read_state_bytes(path)).hexdigest(),
    }
