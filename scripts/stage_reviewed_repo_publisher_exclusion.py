#!/usr/bin/env python3
"""Create, but never install, an exclusion-safe publisher successor bundle.

The reviewed-repo publisher is an external local automation, not this Git
worktree.  This tool reads a captured controller directory and creates an
immutable staging directory containing a content-addressed continuity helper,
the matching router and accountability sources, and a CAS apply recipe.  It
never writes beneath ``--controller-root``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

EXCLUDED_REPOSITORIES = frozenset({"stranske/collab-admin"})
PIN_PATH = re.compile(r"^_CONTINUITY_PATH = '([^']+)'$", re.M)
PIN_SHA = re.compile(r"^_CONTINUITY_SHA256 = '([0-9a-f]{64})'$", re.M)


class StageError(ValueError):
    """The captured publisher did not have the exact supported shape."""


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def mode(path: Path) -> int:
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or path.is_symlink():
        raise StageError(f"regular non-symlink file required: {path}")
    return stat.S_IMODE(value.st_mode)


def source(path: Path) -> bytes:
    mode(path)
    return path.read_bytes()


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise StageError(f"unsupported {label} anchor")
    return text.replace(old, new)


def successor_helper(raw: bytes) -> bytes:
    """Add fail-closed excluded-locator checks to the pinned helper source."""
    text = raw.decode("utf-8")
    anchor = 'LANES = {"closer", "orphan", "maintenance"}\n'
    support = '''LANES = {"closer", "orphan", "maintenance"}
EXCLUDED_REPOSITORIES = frozenset({"stranske/collab-admin"})


def excluded_target(target: Any) -> bool:
    """True only for a syntactically exact owner-excluded target."""
    if not isinstance(target, str):
        return False
    repo, marker, _number = target.rpartition("#")
    return bool(marker and repo.casefold() in EXCLUDED_REPOSITORIES)


def excluded_locator(target: Any, row: Any = None) -> bool:
    """Reject a cached source target or its cached delivery/parent locator."""
    return excluded_target(target) or (
        isinstance(row, Mapping) and excluded_target(row.get("pr_target"))
    )
'''
    text = _replace_once(text, anchor, support, "helper constants")
    text = _replace_once(
        text,
        '    candidates = copy.deepcopy(dict(carried))\n    for seed in seeds:\n        target = seed.get("target")\n        if isinstance(target, str) and TARGET.fullmatch(target):\n            candidates.setdefault(target, copy.deepcopy(seed))\n',
        '    candidates = {\n        target: copy.deepcopy(old)\n        for target, old in dict(carried).items()\n        if not excluded_locator(target, old)\n    }\n    for seed in seeds:\n        target = seed.get("target")\n        if (\n            isinstance(target, str)\n            and TARGET.fullmatch(target)\n            and not excluded_locator(target, seed)\n        ):\n            candidates.setdefault(target, copy.deepcopy(seed))\n',
        "helper plan candidate filtering",
    )
    text = _replace_once(
        text,
        "    for target, row in prior.items():\n        if not isinstance(row, Mapping):\n            continue\n",
        "    for target, row in prior.items():\n        if not isinstance(row, Mapping) or excluded_locator(target, row):\n            continue\n",
        "helper persisted seed filtering",
    )
    text = _replace_once(
        text,
        '    open_targets = {f"{router.inventory.repo(n)}#{n[\'number\']}" for n in prs.get("nodes", [])}\n    carry = dict(state.get("delivery_continuity", {}))\n',
        '    open_targets = {\n        f"{router.inventory.repo(n)}#{n[\'number\']}"\n        for n in prs.get("nodes", [])\n        if not excluded_target(f"{router.inventory.repo(n)}#{n[\'number\']}")\n    }\n    carry = {\n        target: row\n        for target, row in dict(state.get("delivery_continuity", {})).items()\n        if not excluded_locator(target, row)\n    }\n',
        "helper continuity carry filtering",
    )
    text = _replace_once(
        text,
        '    for node in prs.get("nodes", []):\n        target = f"{router.inventory.repo(node)}#{node[\'number\']}"\n        owner = router.pr_owner(node)\n',
        '    for node in prs.get("nodes", []):\n        target = f"{router.inventory.repo(node)}#{node[\'number\']}"\n        if excluded_target(target):\n            continue\n        owner = router.pr_owner(node)\n',
        "helper fresh seed filtering",
    )
    text = _replace_once(
        text,
        '    def read_node(target: str) -> dict:\n        repo, number = target.split("#")\n',
        '    def read_node(target: str) -> dict:\n        if excluded_target(target):\n            raise ValueError("owner-excluded continuity target")\n        repo, number = target.split("#")\n',
        "helper pre-fetch target guard",
    )
    text = _replace_once(
        text,
        '            node = read_node(subject)\n            links = (node.get("closingIssuesReferences") or {}).get("nodes")\n',
        '            if excluded_target(subject):\n                raise ValueError("owner-excluded continuity delivery locator")\n            node = read_node(subject)\n            links = (node.get("closingIssuesReferences") or {}).get("nodes")\n',
        "helper parent delivery guard",
    )
    text = _replace_once(
        text,
        '        for source_node in linked.get("nodes", []):\n            source_holds.setdefault(target, set()).update(source_labels(source_node))\n',
        '        for source_node in linked.get("nodes", []):\n            source_target = f"{router.inventory.repo(source_node)}#{source_node[\'number\']}"\n            if excluded_target(source_target):\n                continue\n            source_holds.setdefault(target, set()).update(source_labels(source_node))\n',
        "helper linked parent filtering",
    )
    return text.encode("utf-8")


def successor_router(raw: bytes, helper_path: Path, helper_sha: str) -> bytes:
    text = raw.decode("utf-8")
    text = _replace_once(
        text,
        "STALE_OWNER_SECONDS = 2700  # 45-minute controller round budget\n",
        """STALE_OWNER_SECONDS = 2700  # 45-minute controller round budget
EXCLUDED_REPOSITORIES = frozenset({"stranske/collab-admin"})


def excluded_target(target: Any) -> bool:
    if not isinstance(target, str):
        return False
    repo, marker, _number = target.rpartition("#")
    return bool(marker and repo.casefold() in EXCLUDED_REPOSITORIES)


def excluded_node(node: Any) -> bool:
    if not isinstance(node, dict):
        return True
    repository = node.get("repository") or {}
    return str(repository.get("nameWithOwner") or "").casefold() in EXCLUDED_REPOSITORIES


def filtered_cached_mapping(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        target: row
        for target, row in value.items()
        if not excluded_target(target)
        and not (isinstance(row, dict) and excluded_target(row.get("pr_target")))
    }


def filtered_state(state: dict) -> dict:
    result = dict(state)
    for key in ("targets", "delivery_continuity", "delivery_ownership", "accountability"):
        result[key] = filtered_cached_mapping(state.get(key))
    return result
""",
        "router exclusion helpers",
    )
    text = _replace_once(
        text,
        "    for pr in prs:\n        target = f\"{inventory.repo(pr)}#{pr['number']}\"\n",
        "    for pr in prs:\n        if excluded_node(pr):\n            continue\n        target = f\"{inventory.repo(pr)}#{pr['number']}\"\n",
        "router PR routing filter",
    )
    text = _replace_once(
        text,
        '    for item in guard.get("actionable_candidates", []):\n        lane = issue_owner(item)\n',
        '    for item in guard.get("actionable_candidates", []):\n        if str(item.get("repository") or "").casefold() in EXCLUDED_REPOSITORIES:\n            continue\n        lane = issue_owner(item)\n',
        "router guard candidate filter",
    )
    text = _replace_once(
        text,
        "    for item in guard.get(\"challenge_due\", []):\n        target = f\"{item['repository']}#{item['number']}\"\n",
        "    for item in guard.get(\"challenge_due\", []):\n        target = f\"{item['repository']}#{item['number']}\"\n        if excluded_target(target):\n            continue\n",
        "router guard challenge filter",
    )
    old_path = PIN_PATH.search(text)
    old_sha = PIN_SHA.search(text)
    if old_path is None or old_sha is None:
        raise StageError("router continuity pin missing")
    text = text[: old_path.start(1)] + str(helper_path) + text[old_path.end(1) :]
    # Re-search because the previous string replacement changes source offsets.
    old_sha = PIN_SHA.search(text)
    assert old_sha is not None
    text = text[: old_sha.start(1)] + helper_sha + text[old_sha.end(1) :]
    text = _replace_once(
        text,
        "        state = load(STATE, {})\n        if args.record_human_alert:\n",
        "        state = load(STATE, {})\n        routing_state = filtered_state(state)\n        if args.record_human_alert:\n",
        "router cached-state boundary",
    )
    text = _replace_once(
        text,
        "                    __import__(__name__), state, prs, now, terminal, (held, challenge, retry))\n",
        "                    __import__(__name__), routing_state, prs, now, terminal, (held, challenge, retry))\n",
        "router continuity filtered state",
    )
    text = _replace_once(
        text,
        '            (prs or {}).get("nodes", []), guard or {}, state, terminal, now, held, challenge, retry, running)\n',
        '            (prs or {}).get("nodes", []), guard or {}, routing_state, terminal, now, held, challenge, retry, running)\n',
        "router route filtered state",
    )
    text = _replace_once(
        text,
        "            state.get('accountability', {}), terminal.get('coverage_json') or {}, observations, now)\n",
        "            routing_state.get('accountability', {}), terminal.get('coverage_json') or {}, observations, now)\n",
        "router accountability filtered state",
    )
    text = _replace_once(
        text,
        "            human_due = accountability.unknown_due(state.get('accountability', {}), now,\n",
        "            human_due = accountability.unknown_due(routing_state.get('accountability', {}), now,\n",
        "router error accountability filtering",
    )
    text = _replace_once(
        text,
        'def relay_eligible(lane):\n    assignment = load(ROOT / (lane + "-assignment.json"), {})\n    generated = inventory.parse_iso(assignment.get("generated_at") or "")\n    return bool(generated and time.time() - generated <= 2 * 3600 and\n                assignment.get("targets") and not assignment.get("errors"))\n',
        'def relay_eligible(lane):\n    assignment = load(ROOT / (lane + "-assignment.json"), {})\n    generated = inventory.parse_iso(assignment.get("generated_at") or "")\n    targets = assignment.get("targets") or []\n    return bool(generated and time.time() - generated <= 2 * 3600 and targets\n                and not assignment.get("errors")\n                and not any(excluded_target(row.get("target")) for row in targets if isinstance(row, dict)))\n',
        "router stale assignment filtering",
    )
    text = _replace_once(
        text,
        "        target = request['target']\n        import followthrough_control\n",
        "        target = request['target']\n        if excluded_target(target):\n            return {'errors': ['Owner-excluded target cannot be followed through']}\n        import followthrough_control\n",
        "router followthrough request filtering",
    )
    text = _replace_once(
        text,
        "            alerts = [r for r in plan.get('human_deadline_due', []) if r.get('new_alert_due') and\n                      time.time() - ((state.get('accountability') or {}).get(r['target']) or {}).get('last_human_alert_at', 0) >= accountability.REMINDER_SECONDS]\n",
        "            alerts = [r for r in plan.get('human_deadline_due', [])\n                      if not excluded_target(r.get('target')) and r.get('new_alert_due') and\n                      time.time() - ((routing_state.get('accountability') or {}).get(r['target']) or {}).get('last_human_alert_at', 0) >= accountability.REMINDER_SECONDS]\n",
        "router cached human-alert filtering",
    )
    text = _replace_once(
        text,
        "            repair_alerts = [r for r in plan.get('repair_budget_decisions', [])\n                             if r.get('new_alert_due') and (r['request_id'] not in state.get('repair_budget_alerts', {}) or\n                             time.time() - state['repair_budget_alerts'][r['request_id']] >= accountability.REMINDER_SECONDS)]\n",
        "            repair_alerts = [r for r in plan.get('repair_budget_decisions', [])\n                             if not excluded_target(r.get('target')) and r.get('new_alert_due')\n                             and (r['request_id'] not in state.get('repair_budget_alerts', {}) or\n                             time.time() - state['repair_budget_alerts'][r['request_id']] >= accountability.REMINDER_SECONDS)]\n",
        "router cached repair-alert filtering",
    )
    return text.encode("utf-8")


def successor_accountability(raw: bytes) -> bytes:
    text = raw.decode("utf-8")
    text = _replace_once(
        text,
        "REMINDER_SECONDS = 24 * 3600\n",
        """REMINDER_SECONDS = 24 * 3600
EXCLUDED_REPOSITORIES = frozenset({"stranske/collab-admin"})


def excluded_target(target):
    if not isinstance(target, str):
        return False
    repo, marker, _number = target.rpartition("#")
    return bool(marker and repo.casefold() in EXCLUDED_REPOSITORIES)


def excluded_node(node):
    repository = node.get("repository") or {} if isinstance(node, dict) else {}
    return str(repository.get("nameWithOwner") or "").casefold() in EXCLUDED_REPOSITORIES


def filtered_mapping(value):
    return {
        target: row
        for target, row in (value or {}).items()
        if not excluded_target(target)
        and not (isinstance(row, dict) and excluded_target(row.get("pr_target")))
    }
""",
        "accountability exclusion helpers",
    )
    text = _replace_once(
        text,
        "    for node in (pr.get('closingIssuesReferences') or {}).get('nodes', []):\n        linked.add(identity(node))\n",
        "    for node in (pr.get('closingIssuesReferences') or {}).get('nodes', []):\n        target = identity(node)\n        if not excluded_target(target):\n            linked.add(target)\n",
        "accountability GraphQL parent filtering",
    )
    text = _replace_once(
        text,
        '        linked.add(f"{match.group(1) or match.group(2) or repo}#{match.group(3)}")\n',
        '        target = f"{match.group(1) or match.group(2) or repo}#{match.group(3)}"\n        if not excluded_target(target):\n            linked.add(target)\n',
        "accountability body parent filtering",
    )
    text = _replace_once(
        text,
        "    linked_progress = {}\n    for pr in prs:\n",
        "    prs = [node for node in prs if not excluded_node(node)]\n    issues = [node for node in issues if not excluded_node(node)]\n    prior = filtered_mapping(prior)\n    observations = filtered_mapping(observations)\n    human = {}\n    for value in guard.get(\"human_attention\") or []:\n        key = f\"{value.get('repository')}#{value.get('number')}\"\n        if not excluded_target(key):\n            human[key] = value\n    linked_progress = {}\n    for pr in prs:\n",
        "accountability pre-routing filtering",
    )
    text = _replace_once(
        text,
        "    human = {f\"{i['repository']}#{i['number']}\": i for i in guard.get('human_attention', [])}\n    records, recoveries, human_due, exclusions = {}, [], [], []\n",
        "    records, recoveries, human_due, exclusions = {}, [], [], []\n",
        "accountability guarded-human replacement",
    )
    text = _replace_once(
        text,
        "    due = []\n    for old in prior.values():\n",
        "    due = []\n    for target, old in prior.items():\n        if excluded_target(target):\n            continue\n",
        "accountability error-path filtering",
    )
    return text.encode("utf-8")


def _atomic_write(path: Path, raw: bytes, file_mode: int) -> None:
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, file_mode)


def stage(controller_root: Path, output: Path) -> dict[str, Any]:
    """Build a create-only stage and return its signed-by-hash recipe."""
    root = controller_root.resolve()
    output = output.resolve()
    if output.exists():
        raise StageError("staging destination already exists")
    controller = root / "controller.py"
    router = root / "demand_router.py"
    accountability = root / "accountability.py"
    router_raw = source(router)
    path_match, sha_match = PIN_PATH.search(router_raw.decode()), PIN_SHA.search(
        router_raw.decode()
    )
    if path_match is None or sha_match is None:
        raise StageError("captured router has no supported continuity pin")
    old_helper = Path(path_match.group(1))
    if old_helper.parent.resolve() != root or not old_helper.name.startswith(
        "delivery-continuity-"
    ):
        raise StageError("captured helper is outside the controller root")
    helper_raw = source(old_helper)
    if digest(helper_raw) != sha_match.group(1):
        raise StageError("captured helper digest differs from router pin")
    helper_new = successor_helper(helper_raw)
    helper_sha = digest(helper_new)
    helper_name = f"delivery-continuity-{helper_sha}.py"
    helper_path = root / helper_name
    router_new = successor_router(router_raw, helper_path, helper_sha)
    accountability_raw = source(accountability)
    accountability_new = successor_accountability(accountability_raw)
    inputs = {
        "controller.py": (controller, source(controller), mode(controller)),
        "demand_router.py": (router, router_raw, mode(router)),
        "accountability.py": (accountability, accountability_raw, mode(accountability)),
        old_helper.name: (old_helper, helper_raw, mode(old_helper)),
    }
    payloads = {
        helper_name: (helper_new, mode(old_helper)),
        "accountability.py": (accountability_new, mode(accountability)),
        "demand_router.py": (router_new, mode(router)),
    }
    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".publisher-exclusion-", dir=parent))
    try:
        payload_dir = temporary / "payload"
        payload_dir.mkdir()
        for name, (raw, file_mode) in payloads.items():
            _atomic_write(payload_dir / name, raw, file_mode)
        recipe = {
            "schema": "reviewed-repo-publisher-exclusion-stage/v1",
            "controller_root": str(root),
            "lock": str(root / ".demand.lock"),
            "excluded_repositories": sorted(EXCLUDED_REPOSITORIES),
            "preimages": {
                name: {"path": str(path), "sha256": digest(raw), "mode": file_mode}
                for name, (path, raw, file_mode) in inputs.items()
            },
            "operations": [
                {
                    "kind": "create_only",
                    "target": helper_name,
                    "payload": helper_name,
                    "sha256": helper_sha,
                    "mode": mode(old_helper),
                },
                {
                    "kind": "replace_cas",
                    "target": "accountability.py",
                    "payload": "accountability.py",
                    "sha256": digest(accountability_new),
                    "mode": mode(accountability),
                },
                {
                    "kind": "replace_cas",
                    "target": "demand_router.py",
                    "payload": "demand_router.py",
                    "sha256": digest(router_new),
                    "mode": mode(router),
                },
            ],
        }
        _atomic_write(
            temporary / "apply-recipe.json",
            json.dumps(recipe, indent=2, sort_keys=True).encode() + b"\n",
            0o600,
        )
        os.replace(temporary, output)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        for path in sorted(temporary.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        temporary.rmdir() if temporary.exists() else None
        raise
    return recipe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--controller-root", required=True)
    parser.add_argument("--out", required=True, help="new staging directory; must not exist")
    args = parser.parse_args(argv)
    recipe = stage(Path(args.controller_root), Path(args.out))
    print(
        json.dumps(
            {
                "stage": str(Path(args.out).resolve()),
                "helper": recipe["operations"][0]["target"],
                "helper_sha256": recipe["operations"][0]["sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
