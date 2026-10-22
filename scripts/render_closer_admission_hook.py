#!/usr/bin/env python3
"""Render a reviewed demand-router candidate only; never publish live settings."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

PRE = "        routes, observations, waits, ignored = build_routes(\n"
POST = "        records, recoveries, human_due, exclusions = accountability.evaluate(\n"
SAVE = '            state["targets"] = observations\n'
MARKER = "# pinned delivery-continuity hook"


def render(source: str, module: Path, expected_sha256: str) -> str:
    module = module.resolve()
    if hashlib.sha256(module.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("continuity module hash mismatch")
    if MARKER in source or any(source.count(hunk) != 1 for hunk in (PRE, POST, SAVE)):
        raise ValueError("unexpected or already rendered demand-router shape")
    loader = f"""{MARKER}
import importlib.util as _continuity_import
_CONTINUITY_PATH = {str(module)!r}
_CONTINUITY_SHA256 = {expected_sha256!r}


def _continuity_module():
    path = Path(_CONTINUITY_PATH)
    if hashlib.sha256(path.read_bytes()).hexdigest() != _CONTINUITY_SHA256:
        raise RuntimeError("delivery continuity source changed; retain canonical assignment")
    spec = _continuity_import.spec_from_file_location("_pinned_delivery_continuity", path)
    module = _continuity_import.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _continuity_held(state, reason):
    original = state.get("delivery_continuity", {{}})
    carry = dict(original) if isinstance(original, dict) else original
    targets = state.get("targets", {{}})
    targets = targets if isinstance(targets, dict) else {{}}
    ownership_state = state.get("delivery_ownership", {{}})
    ownership_state = ownership_state if isinstance(ownership_state, dict) else {{}}
    if isinstance(carry, dict):
        for target, row in targets.items():
            if isinstance(row, dict) and row.get("head") and isinstance(row.get("delivery_owner"), str) and row.get("delivery_owner") in {{"closer", "orphan", "maintenance"}}:
                carry.setdefault(target, {{"target": target, "pr_target": row.get("pr_target"), "owner": row["delivery_owner"]}})
    rows, ownership = [], {{}}
    for target, raw in (carry.items() if isinstance(carry, dict) else []):
        old = raw if isinstance(raw, dict) else {{}}
        known = ownership_state.get(target, {{}})
        known = known if isinstance(known, dict) else {{}}
        owner = old.get("owner")
        if not isinstance(owner, str):
            owner = known.get("owner")
        if isinstance(target, str) and "#" in target and isinstance(owner, str) and owner in {{"closer", "orphan", "maintenance"}}:
            rows.append({{"target": target, "lane": owner, "delivery_owner": owner,
                         "execution_state": "held", "action": "Continuity integrity wait: " + reason}})
            ownership[target] = {{"owner": owner, "state": "held", "via": [], "action": rows[-1]["action"]}}
    return {{"rows": rows, "ownership": ownership, "carry": carry}}

"""
    # The existing main holds .demand.lock across every insertion and publication.
    source = source.replace("def main(argv=None):\n", loader + "def main(argv=None):\n")
    fingerprint_hunk = (
        "    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()\n"
    )
    if source.count(fingerprint_hunk) != 1:
        raise ValueError("normal controller fingerprint shape changed")
    source = source.replace(
        fingerprint_hunk,
        '    if pr.get("_continuity_verifier_material") is not None:\n'
        '        value.append(pr["_continuity_verifier_material"])\n' + fingerprint_hunk,
    )
    source = source.replace(
        PRE,
        """        continuity = None
        if not errors:
            try:
                continuity = _continuity_module().prepare_controller(
                    __import__(__name__), state, prs, now, terminal, (held, challenge, retry))
                warnings.extend("Delivery continuity " + failure["target"] + ": " + failure["reason"]
                                for failure in continuity.get("failures", []))
            except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
                warnings.append("Delivery continuity held: " + str(exc))
                continuity = _continuity_held(state, str(exc))
""" + PRE,
    )
    source = source.replace(
        POST,
        """        if not errors and continuity is not None:
            try:
                _continuity_module().merge_plan(continuity, routes, observations, ownership, waits)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
                warnings.append("Delivery continuity held: " + str(exc))
                continuity = _continuity_held(state, str(exc))
                for row in continuity["rows"]:
                    if row["target"] not in observations:
                        waits.append(row)
                        observations[row["target"]] = row
                        ownership[row["target"]] = continuity["ownership"][row["target"]]
""" + POST,
    )
    source = source.replace(
        SAVE,
        """            if continuity is not None:
                state["delivery_continuity"] = continuity["carry"]
                state["delivery_continuity_failures"] = continuity.get(
                    "failures", state.get("delivery_continuity_failures", []))
""" + SAVE,
    )
    compile(source, "<rendered-demand-router>", "exec")
    return source


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--router", type=Path, required=True)
    parser.add_argument("--module", type=Path, required=True)
    parser.add_argument("--module-sha256", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.resolve() in {args.router.resolve(), args.module.resolve()}:
        raise ValueError("output must be a distinct staged candidate")
    args.out.write_text(render(args.router.read_text(), args.module, args.module_sha256))


if __name__ == "__main__":
    main()
