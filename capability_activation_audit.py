#!/usr/bin/env python3
"""capability_activation_audit.py — CAN each capability fire at all, and is anything routed to it?

WHY THIS EXISTS. Every previous attempt to answer "which capabilities are useful?" answered a
different question — "which have been dispatched?" — and got it wrong in the same way each time.
`offload` read `no_matching_work` while running 196x/week. `runtime-ac-checks` read "no demand"
while 87% of worked issues carry acceptance criteria. `epic-decomposition` read "no demand" while
the OWNER was doing the decomposition by hand. A dispatch count cannot distinguish "nobody needs
this" from "the trigger physically cannot fire", and those demand opposite responses.

So this module never asks whether a capability ran. It asks, mechanically, whether it COULD:

  1. What is its ENTRY CLASS?  task-routed / directly-entered / gated. Asking a `{task_type}`
     question of a `{"kind":"transport"}` capability is a category error, and 26 of 34 capabilities
     are in that position — which is why the inventory's `no_matching_work` was meaningless for
     most of the fleet.
  2. Given that class, is the MACHINERY intact? Each class has different failure modes, checked
     separately and named separately (see DEFECT_CLASSES).
  3. Only then: is there DEMAND? Measured from fleet evidence, and reported alongside — never used
     to conclude a capability is unwanted when the machinery is broken.

EVERY DEFECT IT DETECTS WAS FOUND BY HAND FIRST. The checks are the generalisation of six real
defects: a NameError hidden by a bare `except`, a heartbeat stranded in `main()`, a task_type no
classifier can emit, a prompt template that does not exist, a label the fleet spells differently,
and an entrypoint with no caller. A hand-found defect that this audit cannot see is a gap in the
audit, and that is the standard to hold it to.

IT PERSISTS SNAPSHOTS so progress is measurable rather than asserted. `--progress` diffs the
current audit against history: what moved to reachable, what regressed, what is unchanged.

    python3 capability_activation_audit.py              # scorecard
    python3 capability_activation_audit.py --json
    python3 capability_activation_audit.py --progress   # movement vs the last snapshots
    python3 capability_activation_audit.py --snapshot   # record today's state
    python3 capability_activation_audit.py --selftest
"""
from __future__ import annotations

import pathlib

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import backlog
import capabilities

HERE = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator"))
SNAPSHOT_PATH = STATE_DIR / "capability-activation-history.json"
MAX_SNAPSHOTS = 60

# Modules that actually drive a tick. A directly-entered capability is only reachable if its
# heartbeat sits on a path one of these can reach.
DRIVER_MODULES = ("tick.py", "dispatcher.py", "orchestrate.sh", "router.py", "roles.py",
                  "capacity.py", "backlog.py", "outcomes.py")

ENTRY_TASK_ROUTED = "task_routed"
ENTRY_DIRECT = "directly_entered"
ENTRY_GATED = "gated"
ENTRY_UNKNOWN = "unknown"

# Each defect is named so a fix can be aimed at it, and so progress can be counted per class.
DEFECT_CLASSES = {
    "task_type_not_emittable": "classify() can never produce the task_type this capability matches",
    "no_prompt_template": "no PROMPT_TEMPLATES entry, so there is nothing to dispatch",
    "label_absent_from_fleet": "the labels that would produce its task_type exist in ~no repo",
    "heartbeat_off_path": "its code runs, but the heartbeat is unreachable from any driver",
    "heartbeat_env_suppressed": "a shell driver invokes its entrypoint ABOVE the "
                                "ORCH_CAPABILITY_HEARTBEATS export, so the heartbeat call runs "
                                "and records nothing",
    "no_heartbeat": "the entrypoint records nothing at all",
    "entrypoint_no_caller": "no driver module calls the entrypoint",
    "entrypoint_missing": "the declared entrypoint file does not exist",
    "entrypoint_external": "the entrypoint lives in another repository; activation needs a "
                           "change there, not here",
    "trigger_shape_mismatch": "kind-matched, so a task_type trigger can never reach it",
    "vocabulary_mismatch": "the fleet uses a label in this namespace that the code does not accept",
    "advisor_reach_regression": "it used to be nameable from a free-text task at the advisor front "
                               "door and no longer is; a tightened matcher dropped it out of the "
                               "front door with nothing reporting it",
}

# Code-side label vocabularies that gate behaviour, and the capability each one gates. A fleet label
# sharing a namespace prefix but absent from the set is a NEAR MISS -- the exact shape of the
# `risk:major` defect: adversarial.HIGH_STAKES_LABELS accepts `risk:critical` and `risk:high`, the
# fleet writes `risk:major`, and high_stakes_reason() therefore returned None for every genuinely
# high-stakes issue. Nothing about a dispatch count could reveal that.
GATE_VOCABULARIES = (
    ("adversarial-review", "adversarial", "HIGH_STAKES_LABELS"),
    ("runtime-ac-checks", "backlog", "RUNTIME_AC_LABELS"),
    ("epic-decomposition", "backlog", "EPIC_LABELS"),
    ("codemod-campaign", "backlog", "CODEMOD_LABELS"),
    ("cross-repo-coordination", "backlog", "CROSS_REPO_LABELS"),
    ("testgen-lane", "backlog", "TESTGEN_LABELS"),
)


# Fleet labels that are DELIBERATELY not accepted, so the near-miss detector does not report a
# correct exclusion as a defect. `risk:low`/`risk:medium`/`risk:minor` share the `risk:` namespace
# with `risk:major` but are genuinely not high-stakes — flagging them would push toward spending
# multiple reviewer seats on routine work, the opposite of the intent.
INTENTIONAL_EXCLUSIONS = {
    "adversarial-review": {"risk:low", "risk:medium", "risk:minor"},
}


def vocabulary_gaps(index: dict) -> dict:
    """Fleet labels that share a namespace with a code vocabulary but are not accepted by it."""
    import importlib
    fleet = set()
    for labels in (index.get("repos") or {}).values():
        fleet |= set(labels)
    out: dict[str, list[str]] = {}
    for cap_id, module_name, attr in GATE_VOCABULARIES:
        try:
            accepted = {str(x).strip().lower()
                        for x in getattr(importlib.import_module(module_name), attr, ()) or ()}
        except Exception:
            continue
        namespaces = {a.split(":", 1)[0] for a in accepted if ":" in a}
        deliberate = INTENTIONAL_EXCLUSIONS.get(cap_id, set())
        misses = sorted(
            f for f in fleet
            if ":" in f and f.split(":", 1)[0] in namespaces
            and f not in accepted and f not in deliberate
        )
        if misses:
            out[cap_id] = misses
    return out


# --------------------------------------------------------------------------- entry class

def entry_class(cap: dict) -> str:
    """How is this capability ENTERED? The wrong question is worse than no answer."""
    matcher = cap.get("matcher") or {}
    if matcher.get("field") == "task_type":
        return ENTRY_TASK_ROUTED
    if matcher.get("kind") == "env" or cap.get("gate_reason"):
        return ENTRY_GATED
    if "kind" in matcher:
        return ENTRY_DIRECT
    return ENTRY_UNKNOWN


# --------------------------------------------------------------------------- shared helpers

def emittable_task_types() -> set[str]:
    """Every task_type `backlog.classify()` can produce, by brute force over its own vocabulary."""
    vocab: set[str] = set()
    for name in ("MECHANICAL_LABELS", "TESTGEN_LABELS", "EPIC_LABELS", "CODEMOD_LABELS",
                 "CROSS_REPO_LABELS", "RUNTIME_AC_LABELS"):
        vocab |= set(getattr(backlog, name, ()) or ())
    out = {backlog.classify([])}
    for label in vocab:
        out.add(backlog.classify([label]))
    return out


def _prompt_templates() -> set[str]:
    try:
        import dispatcher
        return set(dispatcher.PROMPT_TEMPLATES)
    except Exception:
        return set()


def labels_producing(task_type: str) -> list[str]:
    """Which labels would make classify() emit this task_type."""
    vocab: set[str] = set()
    for name in ("MECHANICAL_LABELS", "TESTGEN_LABELS", "EPIC_LABELS", "CODEMOD_LABELS",
                 "CROSS_REPO_LABELS", "RUNTIME_AC_LABELS"):
        vocab |= set(getattr(backlog, name, ()) or ())
    return sorted(l for l in vocab if backlog.classify([l]) == task_type)


# The separators WITHIN one entrypoint declaration: whitespace (which is what splits the ` -> `
# form the ledger uses), `,`, `;`, and a `/` that follows a `.py` (the `a.py/b.py` form naming two
# modules). Defined once because the resolver below and the presence diagnosis further down must
# read the SAME declaration — a second, independent parse would eventually diagnose a file the
# resolver never looked for, and a fabricated finding is exactly what this module prevents.
ENTRYPOINT_SEP_RE = re.compile(r"[\s,;]+|(?<=\.py)/")
# A token only NAMES a module when its candidate is a plausible module filename. This is what
# keeps the `->` in `a.py:f -> b.py:g` out of the diagnosis: it splits off as its own token, and
# `->.py` is not a module name.
_MODULE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.py$")


def _entrypoint_declarations(cap: dict) -> list[dict]:
    """One record per module the entrypoint declaration names.

    Entrypoints come in shapes that ALL must resolve, or the audit invents defects: `watch.py`,
    `roles.py:run_triage_agent`, `dispatcher.offload` (module.function, no `.py`), `a.py/b.py`,
    `a.py:f -> b.py:g`, and `Workflows/scripts/x.py` (another repository). The `module.function`
    form produced a false `entrypoint_missing` for `offload` — a capability running 196x/week —
    which is precisely the kind of fabricated finding this module exists to prevent.

    Keys: `token` as written; `candidates`, the filenames to probe, most specific first; `module`,
    the reportable filename, or None when the token names no module at all; and `external`, set
    when the token carries a directory component, so the file is declared to live outside this
    flat module tree — a cross-repo entrypoint is NOT a locally missing file.
    """
    out: list[dict] = []
    for token in ENTRYPOINT_SEP_RE.split(str(cap.get("entrypoint") or "")):
        token = token.strip()
        if not token:
            continue
        stem = token.split(":")[0]
        if stem.endswith(".py"):
            candidates = [Path(stem).name]
        else:
            # `dispatcher.offload` -> dispatcher.py ; `a.b.c` -> try each dotted prefix
            parts = stem.split(".")
            candidates = [".".join(parts[:i]) + ".py" for i in range(len(parts), 0, -1)]
        named = [c for c in candidates if _MODULE_NAME_RE.match(c)]
        out.append({"token": token, "candidates": candidates,
                    "module": named[-1] if named else None,
                    "external": "/" in stem and not stem.startswith(("./", "/"))})
    return out


def _entrypoint_files(cap: dict) -> list[Path]:
    """Local .py files named by the entrypoint declaration (shapes: `_entrypoint_declarations`)."""
    out: list[Path] = []
    for decl in _entrypoint_declarations(cap):
        for name in decl["candidates"]:
            path = HERE / name
            if path.exists() and path not in out:
                out.append(path)
                break
    return out


# --------------------------------------------------------------------------- tree presence
#
# WHY THIS EXISTS. The capability ledger is SHARED machine-local state (`$ORCH_LOCAL_RUNTIME`),
# while code is branch-isolated. So any branch that registers a capability turns every SIBLING
# branch's `python3 verify.py` red — and the three capability gates named that failure with a
# bare capability id:
#
#     test_capability_admission.py  -> {'X': ['caller_exists', 'heartbeat', 'fixture']}
#     test_capability_set_coverage.py -> ['X']
#     test_model_tier_resolution.py   -> ['X']
#
# which is indistinguishable from the genuine defect those gates exist to catch: a row registered
# with no implementation at all. On 2026-08-22 that ambiguity cost a full misdiagnosed session.
# The verdict was "registered without its implementation", and the proposed remedies were to
# RETIRE a live capability's ledger row or to mask it with a WAIVER. The module existed the whole
# time on an unmerged branch, and it carried a hard dependency on a `capabilities.unblock()` guard
# from that branch's parent commit — so the waiver would have hidden a latched-gate bug.
#
# Note what the misdiagnosis rested on: `git log --all --oneline -- <file>` came back EMPTY, and
# emptiness was read as proof. It was empty because the branch's ref had not been fetched. So the
# pointer below says to fetch first and says that silence proves nothing.
#
# The facts needed to tell the two cases apart were ALREADY here — `_entrypoint_files` above, and
# the `entrypoint_missing` / `entrypoint_external` defect classes. They just never reached the
# failure text. This is the wiring, and it NEVER converts a failure into a skip: the gate still
# fails, it just says WHICH failure it is.

ENTRYPOINT_PRESENT = "present_in_tree"
ENTRYPOINT_ABSENT = "absent_from_tree"
ENTRYPOINT_EXTERNAL = "declared_in_another_repo"
ENTRYPOINT_UNDECLARED = "no_entrypoint_declared"


def entrypoint_presence(cap: dict) -> dict:
    """Is the code this ledger row NAMES actually in this working tree?

    Four states, because three of them demand different actions: fetch and check a branch
    (`absent_from_tree`), change another repository (`declared_in_another_repo`), fix the row
    (`no_entrypoint_declared`), or fix the capability (`present_in_tree` — a genuine defect).
    """
    decls = [d for d in _entrypoint_declarations(cap) if d["module"]]
    present = [d["module"] for d in decls if (HERE / d["module"]).exists()]
    absent = [d for d in decls if d["module"] not in present]
    row = {"capability_id": cap.get("capability_id"),
           "entrypoint": str(cap.get("entrypoint") or ""),
           "present": present,
           "absent": [d["module"] for d in absent if not d["external"]],
           "external": [d["token"] for d in absent if d["external"]]}
    if not decls:
        row["state"] = ENTRYPOINT_UNDECLARED
    elif row["absent"]:
        row["state"] = ENTRYPOINT_ABSENT
    elif row["external"]:
        row["state"] = ENTRYPOINT_EXTERNAL
    else:
        row["state"] = ENTRYPOINT_PRESENT
    return row


def entrypoint_diagnosis(capability_ids, *, missing: dict | None = None,
                         ledger: dict | None = None) -> str:
    """Is this gate red about THIS TREE, or about the capability? Text for the failure message.

    Callers PREPEND it to their own assertion text, so the tree-level explanation is read first
    and survives the 400-character truncation the hand-rolled gate runners in
    `test_capability_admission.py` / `test_capability_set_coverage.py` apply.

    `missing` maps capability_id -> the parts the caller found absent, so the message can say the
    thing that would have ended the misdiagnosis in one line: those parts are DOWNSTREAM of the
    absent file, not independent evidence of a badly-declared capability.

    Deliberately pure — it never shells out to git. A `git log --all` executed here would report
    an unfetched branch as "nothing found", which is the precise mistake that produced the wrong
    verdict; printing the command with its caveat cannot make that mistake for the reader.
    """
    ids = [str(c) for c in (capability_ids or [])]
    if not ids:
        return ""
    try:
        # load_declared, NEVER load: `load()` defaults to create=True, which reconciles AND
        # PERSISTS. A diagnosis printed inside a failing assertion must not mutate the shared
        # ledger it is describing. `test_capabilities.py` enforces that rule for every file
        # carrying a selftest, and it caught exactly this line.
        rows = ledger if ledger is not None else capabilities.load_declared(capabilities.REG)
    except Exception:                                              # noqa: BLE001
        # The diagnosis must never break the failure it is explaining.
        return ("could not read the capability ledger, so entrypoint presence is unknown for "
                f"{ids}.\n")
    out: list[str] = []
    for cap_id in ids:
        cap = dict((rows or {}).get(cap_id) or {})
        cap.setdefault("capability_id", cap_id)
        pres = entrypoint_presence(cap)
        parts = ", ".join((missing or {}).get(cap_id) or [])
        if pres["state"] == ENTRYPOINT_ABSENT:
            named = ", ".join(pres["absent"])
            out.append(f"{cap_id}: entrypoint {named} is NOT in this tree.")
            out.append("  The ledger is shared machine-local state; the code may live on an "
                       "unmerged branch.")
            out.append(f"  Check: git log --all --oneline -- {pres['absent'][0]}"
                       "   (fetch first: an unfetched branch reads as empty)")
            if parts:
                out.append(f"  Its missing parts ({parts}) follow from that absent file, not "
                           "from a defect in it.")
        elif pres["state"] == ENTRYPOINT_EXTERNAL:
            out.append(f"{cap_id}: entrypoint {', '.join(pres['external'])} is declared in "
                       "ANOTHER repository, not this tree.")
            out.append("  Activation needs a change there; `git log --all` here will never "
                       "find it.")
        elif pres["state"] == ENTRYPOINT_UNDECLARED:
            out.append(f"{cap_id}: the ledger row declares no entrypoint, so there is no file "
                       "to look for.")
        else:
            out.append(f"{cap_id}: entrypoint {', '.join(pres['present'])} IS in this tree"
                       + (f" — a genuine admission defect (missing: {parts})."
                          if parts else " — a genuine admission defect."))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- static analysis

def _heartbeat_functions(path: Path) -> set[str]:
    """Functions in `path` whose body reaches a capability heartbeat call."""
    try:
        tree = ast.parse(path.read_text(errors="ignore"))
    except (OSError, SyntaxError):
        return set()
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            called = getattr(func, "id", None) or getattr(func, "attr", None) or ""
            if called in ("_capability_heartbeat", "production_heartbeat", "daily_heartbeat",
                          "heartbeat", "_lane_capability_match"):
                names.add(node.name)
                break
    return names


def _call_graph(path: Path) -> dict[str, set[str]]:
    """function -> functions it calls WITHIN the same module (bare-name calls only)."""
    try:
        tree = ast.parse(path.read_text(errors="ignore"))
    except (OSError, SyntaxError):
        return {}
    local = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    graph: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                name = getattr(sub.func, "id", None)
                if name in local:
                    called.add(name)
        graph[node.name] = called
    return graph


def _reaches(start: str, graph: dict[str, set[str]], targets: set[str]) -> bool:
    """Does `start` transitively reach any function in `targets`?

    Needed because a heartbeat commonly lives in a PRIVATE HELPER. `roles.py` fires its heartbeats
    from `_role_capability_event`, while `tick.py` calls `activate_tick_triage` — so a direct-call
    check reported `off_path` for `role-triage`, a capability with 688 recorded invocations. A false
    "blocked" is less dangerous than a false pass, but it is still wrong, and it would have sent me
    chasing a defect that did not exist.
    """
    if start in targets:
        return True
    seen, stack = set(), [start]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for callee in graph.get(current, ()):  # noqa: SIM118
            if callee in targets:
                return True
            if callee not in seen:
                stack.append(callee)
    return False


def _callers_of(module_stem: str, func_names: set[str]) -> list[str]:
    """Driver modules that call `module_stem.<func>` for any func, or run it as a CLI."""
    found = []
    for driver in DRIVER_MODULES:
        dpath = HERE / driver
        if not dpath.exists():
            continue
        text = dpath.read_text(errors="ignore")
        if driver.endswith(".sh"):
            # A COMMENT is not a caller. orchestrate.sh line 42 merely says
            # "See research_scheduler.py + tick.research_tick", and matching that reported a
            # main()-stranded heartbeat as reachable — a false PASS, the worst failure this audit
            # can have. Require an actual invocation on a non-comment line.
            invoked = any(
                re.search(rf"(?:python3?|\bexec\b)[^\n#]*\b{re.escape(module_stem)}\.py\b", line)
                for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
            if invoked:
                found.append(f"{driver} (CLI)")
            continue
        for fn in func_names or {"*"}:
            if fn == "*":
                if re.search(rf"\b{re.escape(module_stem)}\.", text):
                    found.append(driver)
                    break
            elif re.search(rf"\b{re.escape(module_stem)}\.{re.escape(fn)}\s*\(", text):
                found.append(f"{driver}:{fn}")
                break
    return found


def _declared_functions(cap: dict) -> set[str]:
    """Function names named by the entrypoint declaration, e.g. `roles.py:run_triage_agent`."""
    out = set()
    for token in re.split(r"[\s,;]+", str(cap.get("entrypoint") or "")):
        if ":" in token:
            name = token.split(":", 1)[1].strip()
            if name and name.replace("_", "").isalnum():
                out.add(name)
    return out


def external_caller(cap: dict) -> dict | None:
    """For a cross-repo capability, does its declared CI caller actually exist?

    `entrypoint_external` says "activation needs a change in another repository". That is only a
    BLOCKER while the change is outstanding — once the caller lands, the capability can fire and
    reporting it as blocked would be the same false-negative this module exists to prevent.

    A `{"kind": "ci_workflow", "name": X}` matcher names the workflow, and the entrypoint's first
    path segment names the sibling repo (`Workflows/scripts/...`), so both are checkable offline
    against the local checkout.
    """
    matcher = cap.get("matcher") or {}
    if matcher.get("kind") != "ci_workflow":
        return None
    workflow = str(matcher.get("name") or "").strip()
    entry = str(cap.get("entrypoint") or "")
    repo = entry.split("/", 1)[0].strip() if "/" in entry else ""
    if not workflow or not repo:
        return {"exists": False, "detail": "matcher or entrypoint does not name a repo/workflow"}
    rel = pathlib.Path(repo) / ".github" / "workflows" / f"{workflow}.yml"
    for root, origin in _fleet_roots():
        path = root / rel
        if path.exists():
            return {"exists": True, "repo": repo, "workflow": workflow,
                    "path": str(path), "root_origin": origin}
    tried = [str(r / rel) for r, _ in _fleet_roots()]
    return {"exists": False, "repo": repo, "workflow": workflow,
            "path": tried[0] if tried else "", "tried": tried}


def _fleet_roots() -> list[tuple[pathlib.Path, str]]:
    """Candidate directories that hold the sibling fleet repos, best first.

    WHY THIS IS NOT JUST `HERE.parent`. It was, and that was wrong in the place that matters most:
    launchd runs the MIRROR at ~/.codex/orchestrator-mirror, whose parent is ~/.codex, which holds
    no fleet checkout. So byte-identical code scored 37 of 37 in the canonical tree and 36 of 37 in
    the mirror -- the live system would have reported docs-drift-fix-agent blocked forever, while
    the tree I was editing said it was fine. A verdict that depends on WHERE it ran is exactly the
    failure this module exists to detect, so it must not be one this module commits.

    The home-anchored candidate matches the idiom already in keepalive_outcomes.py:30.
    model_profile_trial_bridge.py carried the identical latent bug in `DEFAULT_WORKFLOWS_ROOT`; it
    now uses this same resolution order in its own `_fleet_roots`/`resolve_workflows_root`, so if a
    third caller needs it, extract one shared helper rather than adding a fourth spelling.
    """
    roots: list[tuple[pathlib.Path, str]] = []
    env = os.environ.get("ORCH_FLEET_ROOT")
    if env:
        roots.append((pathlib.Path(env).expanduser(), "ORCH_FLEET_ROOT"))
    roots.append((HERE.parent, "sibling-of-module"))
    roots.append((pathlib.Path.home() / "Library/CloudStorage/Dropbox/Learning/Code",
                  "home-anchored-workspace"))
    seen, out = set(), []
    for r, origin in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            out.append((r, origin))
    return out


def heartbeat_reachable(cap: dict) -> dict:
    """Is this capability's heartbeat reachable from a driver, or stranded?

    THE DEFECT THIS GENERALISES: `offload`'s heartbeat raised NameError on every call (no import,
    swallowed by `except Exception: pass`), and `research_scheduler` / `watch` / `repo_knowledge`
    put theirs inside `main()` while the tick calls their FUNCTIONS directly, so main() never runs.
    Both look identical from a dispatch count: zero.
    """
    # PROMPT-SCHEMA CAPABILITIES ARE CREDITED BY THE DISPATCHER, NOT BY THEIR OWN MODULE -- and
    # asking this question of their entrypoint is the same category error as asking an observer for
    # a delivery outcome. The lane modules are SCHEMAS: `PROMPT_TEMPLATES` says "produce strict JSON
    # matching codemod_lane.py" and an agent returns conforming output. Verified 2026-08-21 that
    # every non-test reference to those modules outside themselves is a prompt-template string, a
    # doc, or a registry mention -- no code calls their functions, so their `main()` heartbeat can
    # never fire and MOVING it has nowhere to move to. The real executed path is
    # `dispatcher.build_prompt`, which records a `match` for exactly these via TASK_TYPE_CAPABILITY.
    #
    # DECLARED, NOT INFERRED: the source is dispatcher's own mapping, so a capability is credited
    # here only because the dispatcher really does credit it. Nothing is guessed from naming.
    try:
        import dispatcher as _dispatcher
        _lane_credited = set((_dispatcher.TASK_TYPE_CAPABILITY or {}).values())
    except Exception:
        _lane_credited = set()
    if cap.get("capability_id") in _lane_credited:
        return {"status": "reachable",
                "via": ["dispatcher.build_prompt via TASK_TYPE_CAPABILITY (prompt-schema capability)"],
                "functions": ["build_prompt"]}
    # CROSS-REPO CAPABILITIES ARE CREDITED BY THE BRIDGE, for the same reason and by the same rule.
    # `docs-drift-fix-agent`'s entrypoint lives in the Workflows repo, so asking whether a heartbeat
    # sits on ITS path is unanswerable -- the file is in another repository.
    # `capability_outcome_bridge.ingest_external_ci_invocations` observes that repo's completed
    # workflow runs and records the invocation here, which IS the executed path for this shape.
    # Declared from the bridge's own EXTERNAL_CI_CAPABILITIES mapping, never inferred.
    try:
        import capability_outcome_bridge as _bridge
        _ci_credited = set(_bridge.EXTERNAL_CI_CAPABILITIES or {})
    except Exception:
        _ci_credited = set()
    if cap.get("capability_id") in _ci_credited:
        return {"status": "reachable",
                "via": ["capability_outcome_bridge.ingest_external_ci_invocations "
                        "(cross-repo CI observation)"],
                "functions": ["ingest_external_ci_invocations"]}
    files = _entrypoint_files(cap)
    if not files:
        return {"status": "no_local_entrypoint", "detail": str(cap.get("entrypoint") or "")[:60]}
    for path in files:
        stem = path.stem
        hb_funcs = _heartbeat_functions(path)
        if not hb_funcs:
            continue
        # A heartbeat only in main() needs the module to be run as a CLI by a driver.
        # Any function that TRANSITIVELY reaches a heartbeat counts as heartbeat-bearing, so a
        # driver calling a public entry point that delegates to a private helper is reachable.
        graph = _call_graph(path)
        bearing = {fn for fn in graph if _reaches(fn, graph, hb_funcs)} | hb_funcs

        # THE DECLARED ENTRY POINT IS AUTHORITATIVE. `roles.py:run_triage_agent` names the function
        # the capability is entered through; if THAT reaches a heartbeat, the capability is credited
        # whenever it is used — regardless of whether a driver calls it by bare name (roles are
        # dispatched through a registry, which no bare-name AST scan can follow). Ignoring the
        # `:function` suffix reported `off_path` for all five role capabilities, including
        # `role-triage` with 688 recorded invocations.
        declared = _declared_functions(cap) & set(graph)
        if declared and (declared & bearing):
            return {"status": "reachable", "via": [f"declared entrypoint {sorted(declared)[0]}"],
                    "functions": sorted(hb_funcs)}

        non_main = {f for f in bearing if f != "main"}
        callers = _callers_of(stem, non_main) if non_main else []
        cli = [c for c in _callers_of(stem, {"*"}) if "(CLI)" in c]
        if callers:
            return {"status": "reachable", "via": callers[:3], "functions": sorted(hb_funcs)}
        if "main" in hb_funcs and cli:
            return {"status": "reachable", "via": cli[:2], "functions": ["main"]}
        # Its code may still run — via direct function calls that bypass the heartbeat.
        any_call = _callers_of(stem, {"*"})
        if any_call:
            return {"status": "off_path", "functions": sorted(hb_funcs),
                    "detail": f"heartbeat only in {sorted(hb_funcs)}, but drivers call "
                              f"{any_call[:2]} directly"}
        return {"status": "no_caller", "functions": sorted(hb_funcs)}
    return {"status": "no_heartbeat", "files": [p.name for p in files]}


# ------------------------------------------------------------------ heartbeat ENABLEMENT (env gate)
#
# `heartbeat_reachable` above answers "can a driver reach the heartbeat CALL?". That is not the same
# question as "will the heartbeat RECORD anything?", and until 2026-08-22 nothing owned the second
# one. `capabilities.production_heartbeat` / `daily_heartbeat` return False immediately unless
# ORCH_CAPABILITY_HEARTBEATS=1 is in the CHILD process's environment, and only orchestrate.sh
# exports it. So a shell invocation placed ABOVE that export reaches the call and records NOTHING —
# reachable and silent at the same time.
#
# MEASURED, not hypothetical (2026-08-22, orchestrate.sh before this was fixed): the export sat 19
# lines below `frontend_verify.py --doctor`, whose invocation is the frontend-verifier capability's
# only tick caller. `heartbeat_reachable` reported it `reachable` (main() heartbeat + a shell CLI
# invocation), the firing monitor reported `never fired`, and both were right — flipping
# ORCH_FRONTEND_VERIFY_START_BROWSER=1 would have recorded nothing and read as "the switch did not
# help". `capacity.py` was in the same position, making `windowed-capacity-policy`'s declared cadence
# ("every tick, capacity.build at the top of the tick") false: its evidence came only from later
# in-process callers.
#
# Reported with BOTH numbers, per the latched-gate runtime rule: `suppressed` alone cannot be read,
# because zero suppressed looks identical whether the ordering is correct or the parse found no
# invocations at all. `invocations_after` is the denominator that tells those apart.
HEARTBEAT_ENV_FLAG = "ORCH_CAPABILITY_HEARTBEATS"
HEARTBEAT_EXPORT_ANCHOR = "ORCH-ANCHOR: heartbeat-export"
SHELL_DRIVERS = tuple(d for d in DRIVER_MODULES if d.endswith(".sh"))

# A call, never a definition. `capabilities.py` DEFINES both helpers and calls neither, so matching
# the bare name would report the validation gate — which must stay above the export — as a defect.
_HEARTBEAT_CALL_RE = re.compile(r"\b(?:production_heartbeat|daily_heartbeat)\s*\(")
_HEARTBEAT_DEF_RE = re.compile(r"^\s*def\s+(?:production_heartbeat|daily_heartbeat)\b")
_SHELL_FUNC_OPEN_RE = re.compile(r"^\s*(?:function\s+)?[A-Za-z_][A-Za-z0-9_]*\s*\(\)\s*\{")
_SHELL_PY_INVOKE_RE = re.compile(r"(?:python3?|\bexec\b)")
_SHELL_PY_MODULE_RE = re.compile(r"([A-Za-z0-9_]+)\.py")


def emits_heartbeat(path: Path) -> bool:
    """Does this module CALL a production heartbeat? (A definition is not a call.)"""
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return False
    return any(_HEARTBEAT_CALL_RE.search(line) and not _HEARTBEAT_DEF_RE.match(line)
               for line in text.splitlines())


def shell_heartbeat_gate(text: str) -> dict:
    """Split a shell driver's python invocations around the heartbeat export. PURE.

    Three buckets, because two would lie:
      * `before`   — invoked above the export, so any heartbeat it emits is discarded.
      * `after`    — invoked below it; heartbeats land. This is the denominator.
      * `deferred` — inside a shell FUNCTION body, so the position of the definition says nothing
                     about when it runs (`_gh_gate` is defined near the top and called throughout).
                     Reported rather than guessed; calling a deferred invocation `before` would
                     manufacture a defect out of a definition.
    """
    export_line: int | None = None
    before: list[tuple[int, str]] = []
    after: list[tuple[int, str]] = []
    deferred: list[tuple[int, str]] = []
    depth = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        # Classify with the depth the line ITSELF sits at, then update. A one-line function
        # (`_gh_gate() { ...python3 gh_capacity.py...; }`) opens and closes on the same line, so
        # updating first would return depth to 0 and bucket its body as top-level `before` — the
        # exact false positive this bucket exists to avoid.
        opens = depth == 0 and bool(_SHELL_FUNC_OPEN_RE.match(line))
        in_function = depth > 0 or opens
        if opens:
            depth = line.count("{") - line.count("}")
        elif depth > 0:
            depth += line.count("{") - line.count("}")
        depth = max(depth, 0)
        if not stripped or stripped.startswith("#"):
            continue
        if export_line is None and re.search(
                rf"^\s*export\s+{re.escape(HEARTBEAT_ENV_FLAG)}=1\b", line):
            export_line = lineno
            continue
        if not _SHELL_PY_INVOKE_RE.search(line.split("#", 1)[0]):
            continue
        # Deduped per line: a single invocation whose warn message repeats the module name
        # (`python3 "$ORCH/capacity.py" || echo "warn: capacity.py failed"`) is ONE invocation, and
        # counting it twice would inflate the denominator these buckets exist to make readable.
        for mod in dict.fromkeys(_SHELL_PY_MODULE_RE.findall(line.split("#", 1)[0])):
            entry = (lineno, f"{mod}.py")
            if in_function:
                deferred.append(entry)
            elif export_line is None:
                before.append(entry)
            else:
                after.append(entry)
    return {"export_line": export_line, "before": before, "after": after, "deferred": deferred}


def heartbeat_env_gate(*, here: Path | None = None) -> dict:
    """Which heartbeat-emitting modules does a shell driver invoke before enabling heartbeats?

    `suppressed_modules` is the defect set. `invocations_after` is published alongside it so a zero
    can be read: 0 suppressed of 40 invocations is a correct ordering, 0 of 0 is a broken parse.
    """
    root = here or HERE
    out = {"flag": HEARTBEAT_ENV_FLAG, "anchor": HEARTBEAT_EXPORT_ANCHOR, "drivers": {},
           "suppressed_modules": [], "invocations_before": 0, "invocations_after": 0,
           "invocations_deferred": 0, "anchor_present": False}
    suppressed: dict[str, list[str]] = {}
    for driver in SHELL_DRIVERS:
        dpath = root / driver
        if not dpath.exists():
            continue
        text = dpath.read_text(errors="ignore")
        gate = shell_heartbeat_gate(text)
        if HEARTBEAT_EXPORT_ANCHOR in text:
            out["anchor_present"] = True
        emitting_before = sorted({
            mod for _, mod in gate["before"] if emits_heartbeat(root / mod)})
        out["drivers"][driver] = {
            "export_line": gate["export_line"],
            "invocations_before": len(gate["before"]),
            "invocations_after": len(gate["after"]),
            "invocations_deferred": len(gate["deferred"]),
            "heartbeat_emitters_before": emitting_before,
        }
        out["invocations_before"] += len(gate["before"])
        out["invocations_after"] += len(gate["after"])
        out["invocations_deferred"] += len(gate["deferred"])
        for mod in emitting_before:
            suppressed.setdefault(mod, []).append(driver)
    out["suppressed_modules"] = sorted(suppressed)
    out["suppressed_by_driver"] = {k: sorted(v) for k, v in sorted(suppressed.items())}
    return out


# ------------------------------------------------------- advisor reach (span of the FRONT DOOR)
#
# `capability_advisor.advise` (and the `capability_advice` MCP tool) is the front door: it turns a
# free-text task into a list of capabilities. A capability no free-text task can NAME is invisible
# there — and that is a property of its MATCHER SHAPE, not of demand. `capabilities._matches_trigger`
# is handed {repository, task_type, lane} and fails closed against a kind-based matcher, by design.
# Measured 2026-08-22 over the 41-row ledger: FIVE capabilities are nameable from a task type, all
# five carrying `{"field": "task_type", ...}`; the other 36 carry kind-shaped matchers (tick_phase 9,
# role 5, env 4, then singletons). The advisor names a sixth, `offload`, through a hardcoded
# direct-entry map inside `advise()`.
#
# WHY THIS IS AUDITED. Reach has already SHRUNK in silence. The learned skill->capability
# associations still hold observations for `docs-drift-fix-agent` (2) and `adversarial-review` (1) —
# both alive, both once reachable, which is how those observations exist. Each was later retuned to
# a kind-shaped matcher (`ci_workflow`, `closer_gate`), correctly, and each thereby dropped out of
# the front door with nothing reporting it. A measuring instrument whose own span narrows unobserved
# is a latched gate inside the gauge.
#
# THE BASELINE IS A FLOOR, NOT A TARGET, AND MUST NOT BE CHASED. Reach may grow freely. It may not
# shrink without appearing in a diff: a matcher legitimately tightened must drop its name from this
# set in the same change, which puts the decision in review instead of in silence. Widening
# `TASK_SIGNALS` or loosening a matcher TO MOVE THIS NUMBER is forbidden — it would corrupt the
# learned associations, which is the exact trap of measuring the thing you are optimising.
#
# DECLARED reach only, and the direct-entry set is tracked SEPARATELY below. `offload` was left out
# of this baseline on 2026-08-22 because it was reachable only through a HARDCODED map inside
# `advise()`, and a hardcode cannot shrink quietly — deleting it IS a diff. That premise died the
# same day: #20 replaced the hardcode with `capability_advisor.direct_entry()`, DERIVED from
# `dispatcher.TASK_TYPE_CAPABILITY`. Derived reach can now shrink with no diff in either module —
# drop an entry from the dispatcher's map and the front door narrows silently. So the exemption that
# was correct for a literal is exactly wrong for a derivation, and the direct set gets its own
# baseline rather than no baseline.
ADVISOR_REACH_BASELINE = frozenset({
    "codemod-campaign",
    "cross-repo-coordination",
    "deliberate-break-verifier",
    "epic-decomposition",
    "testgen-lane",
})
# Targets reachable ONLY through the derived direct-entry map — never through a declared matcher.
# `runtime-ac-checks` is the case that proves the point: the dispatcher has routed `runtime_ac` to it
# all along while the advisor named `deliberate-break-verifier` for the same work, and nothing
# reported the disagreement because neither side measured the other.
ADVISOR_DIRECT_ENTRY_BASELINE = frozenset({"offload", "runtime-ac-checks"})
ADVISOR_REACH_PROBE_REPO = "stranske/Ready"
ADVISOR_REACH_PROBE_LANE = "opener"


def advisor_reach(caps: dict[str, dict]) -> dict:
    """Which capabilities can a free-text task NAME through their declared matcher? PURE.

    Pure over an already-loaded ledger dict on purpose: no second `capabilities.load`, so this can
    never take a writing load of the live ledger, and the selftest can drive it with three rows.
    """
    try:
        import capability_advisor
        task_types = list(capability_advisor.TASK_SIGNALS)
    except Exception as exc:                                   # noqa: BLE001
        return {"unreadable": f"capability_advisor unavailable ({type(exc).__name__})",
                "reachable": [], "by_capability": {}, "task_types": [],
                "regressed": sorted(ADVISOR_REACH_BASELINE),
                "direct_entry": {}, "direct_entry_targets": [], "direct_entry_only": [],
                "direct_entry_baseline": sorted(ADVISOR_DIRECT_ENTRY_BASELINE),
                "direct_entry_regressed": sorted(ADVISOR_DIRECT_ENTRY_BASELINE),
                "total_reachable_count": 0}
    # ONE source for the direct-entry map: the advisor's own, which derives from the dispatcher.
    # Re-listing it here would be the second inventory this function exists to prevent.
    try:
        direct = dict(capability_advisor.direct_entry())
    except Exception:                                          # noqa: BLE001
        direct = {}
    direct_targets = {str(v) for v in direct.values() if v}
    by_capability: dict[str, list[str]] = {}
    for task_type in task_types:
        trigger = {"repository": ADVISOR_REACH_PROBE_REPO, "task_type": task_type,
                   "lane": ADVISOR_REACH_PROBE_LANE}
        for cap_id, cap in sorted(caps.items()):
            if cap.get("status") in {"retired", "superseded"}:
                continue
            ok, _reasons = capabilities._matches_trigger(cap, trigger)
            if ok:
                by_capability.setdefault(cap_id, []).append(task_type)
    reachable = sorted(by_capability)
    return {
        "reachable": reachable,
        "by_capability": {k: sorted(v) for k, v in sorted(by_capability.items())},
        "task_types": task_types,
        # BOTH numbers again: "5 reachable" is only readable next to the total it is drawn from.
        "reachable_count": len(reachable),
        "capability_count": len(caps),
        "baseline": sorted(ADVISOR_REACH_BASELINE),
        "regressed": sorted(ADVISOR_REACH_BASELINE - set(reachable)),
        # THE DERIVED HALF. Reported next to the declared half because "5 reachable" and
        # "7 reachable" are both true of different populations, and two disagreeing reach numbers in
        # two modules is how a parallel inventory starts.
        "direct_entry": dict(sorted(direct.items())),
        "direct_entry_targets": sorted(direct_targets),
        "direct_entry_only": sorted(direct_targets - set(reachable)),
        "direct_entry_baseline": sorted(ADVISOR_DIRECT_ENTRY_BASELINE),
        "direct_entry_regressed": sorted(ADVISOR_DIRECT_ENTRY_BASELINE - direct_targets),
        "total_reachable_count": len(set(reachable) | direct_targets),
    }


# --------------------------------------------------------------------------- fleet vocabulary

def _fleet_label_index(*, use_cache: bool = True) -> dict:
    """Labels that EXIST per repo. Cached: 12 API calls is too slow for a hot path."""
    cache = STATE_DIR / "fleet-label-index.json"
    if use_cache and cache.exists():
        try:
            blob = json.loads(cache.read_text())
            if time.time() - float(blob.get("generated_at") or 0) < 7 * 86400:
                return blob
        except (OSError, ValueError):
            pass
    index = {}
    # A FAILED gh call already means "unknown for this repo" (unauthenticated, offline, no
    # access), and the loop skips it. An ABSENT gh binary meant an uncaught FileNotFoundError
    # that took the whole audit down — same information, opposite outcome. Named here, and it
    # short-circuits: with no gh at all there is nothing to ask 12 times.
    if not shutil.which("gh"):
        return {"generated_at": time.time(), "repos": {},
                "unreadable": "gh CLI not installed; fleet label vocabulary unknown"}
    for full in getattr(backlog, "SUPPORTED_REPOS", []):
        try:
            proc = subprocess.run(["gh", "label", "list", "--repo", full, "--limit", "300",
                                   "--json", "name"], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            continue                       # unknown for this repo, exactly like a nonzero exit
        if proc.returncode != 0:
            continue
        try:
            index[full] = sorted(r["name"].strip().lower()
                                 for r in json.loads(proc.stdout or "[]"))
        except ValueError:
            continue
    blob = {"generated_at": time.time(), "repos": index}
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(blob, indent=2))
    except OSError:
        pass
    return blob


def label_coverage(task_type: str, index: dict) -> dict:
    """In how many repos does a label that produces this task_type actually exist?"""
    wanted = {l.lower() for l in labels_producing(task_type)}
    repos = index.get("repos") or {}
    if not wanted or not repos:
        return {"labels": sorted(wanted), "repos_with": None, "repos_total": len(repos)}
    have = [r for r, labels in repos.items() if wanted & set(labels)]
    return {"labels": sorted(wanted), "repos_with": len(have), "repos_total": len(repos),
            "missing_in": sorted(r.split("/")[-1] for r in repos if r not in have)[:6]}


# --------------------------------------------------------------------------- the audit

def audit_capability(cap_id: str, cap: dict, *, emittable: set[str], templates: set[str],
                     label_index: dict, vocab_gaps: dict | None = None,
                     env_gate: dict | None = None, reach: dict | None = None) -> dict:
    """One capability: entry class, machinery verdict, named defects. Never a demand guess."""
    entry = entry_class(cap)
    defects: list[str] = []
    notes: list[str] = []
    row = {"capability_id": cap_id, "status": cap.get("status"), "entry_class": entry,
           "matcher": cap.get("matcher"), "entrypoint": cap.get("entrypoint")}

    if entry == ENTRY_TASK_ROUTED:
        values = (cap.get("matcher") or {}).get("value") or []
        row["task_types"] = list(values)
        for value in values:
            if value not in emittable:
                defects.append("task_type_not_emittable")
                notes.append(f"classify() can never emit {value!r}")
            if value not in templates:
                defects.append("no_prompt_template")
                notes.append(f"no PROMPT_TEMPLATES[{value!r}]")
            cov = label_coverage(value, label_index)
            row.setdefault("label_coverage", {})[value] = cov
            if cov.get("repos_with") == 0:
                defects.append("label_absent_from_fleet")
                notes.append(f"no repo carries a label producing {value!r}")
            elif (cov.get("repos_with") or 0) and cov["repos_with"] <= max(
                    1, (cov.get("repos_total") or 12) // 6):
                defects.append("label_absent_from_fleet")
                notes.append(f"a label producing {value!r} exists in only "
                             f"{cov['repos_with']}/{cov['repos_total']} repos")

    elif entry == ENTRY_DIRECT:
        hb = heartbeat_reachable(cap)
        row["heartbeat"] = hb
        if hb["status"] == "off_path":
            defects.append("heartbeat_off_path"); notes.append(hb.get("detail", ""))
        elif hb["status"] == "no_heartbeat":
            defects.append("no_heartbeat"); notes.append("entrypoint records nothing")
        elif hb["status"] == "no_caller":
            defects.append("entrypoint_no_caller"); notes.append("no driver calls it")
        elif hb["status"] == "no_local_entrypoint":
            # A cross-repo entrypoint is not a MISSING file — reporting it as missing implies a
            # local defect and hides the real blocker, which is a change in another repository.
            # The external test comes from `entrypoint_presence` so this branch and the failure
            # text the capability gates print cannot drift into disagreeing about which case a
            # capability is in — that disagreement IS the misdiagnosis they exist to prevent.
            detail = str(hb.get("detail") or "")
            external = entrypoint_presence(cap)["state"] == ENTRYPOINT_EXTERNAL
            caller = external_caller(cap)
            row["external_caller"] = caller
            if caller and caller.get("exists"):
                # The cross-repo caller HAS landed, so this is no longer blocked.
                notes.append(f"cross-repo entrypoint, caller present: "
                             f"{caller['repo']}/.github/workflows/{caller['workflow']}.yml")
            else:
                defects.append("entrypoint_external" if external else "entrypoint_missing")
                notes.append(detail)
                if caller:
                    notes.append(f"awaiting caller {caller.get('workflow')} in "
                                 f"{caller.get('repo')}")
        # A kind-matcher is correct for this class, but the INVENTORY must not ask it a
        # task_type question — that is what produced years of meaningless `no_matching_work`.
        notes.append("kind-matched: judge by whether its code path runs, not by work matching")

    elif entry == ENTRY_GATED:
        flag = (cap.get("matcher") or {}).get("name")
        row["gate_flag"] = flag
        row["gate_encoded"] = bool(capabilities.gate_policy(cap)["encoded"])
        if flag:
            notes.append(f"held by {flag}; flipping it is the activation decision")
        if not row["gate_encoded"] and cap.get("gate_reason"):
            notes.append("gate threshold is not machine-checkable")

    gaps = (vocab_gaps or {}).get(cap_id)
    if gaps:
        defects.append("vocabulary_mismatch")
        notes.append(f"fleet uses {', '.join(gaps[:4])}, which its label set does not accept")
        row["vocabulary_gaps"] = gaps

    # Entry-class agnostic on purpose: a mis-ordered shell invocation silences a gated capability's
    # producer exactly as thoroughly as a directly-entered one's, and the frontend-verifier instance
    # was BOTH (kind-matched, and held behind ORCH_FRONTEND_VERIFY_START_BROWSER).
    # ADVISOR REACH is recorded on EVERY row, reachable or not, because "the advisor never
    # recommended it" has two causes with opposite fixes: nobody asked for that work, versus the
    # front door structurally cannot name it. Without this column those two read identically —
    # standing rule 6 ("no demand" vs "could not fire") showing up in a new place.
    if reach is not None:
        row["advisor_reachable"] = cap_id in set(reach.get("reachable") or [])
        row["advisor_task_types"] = (reach.get("by_capability") or {}).get(cap_id) or []
        if cap_id in set(reach.get("regressed") or []):
            defects.append("advisor_reach_regression")
            notes.append("was nameable at the advisor front door and no longer is; a tightened "
                         "matcher dropped it out without any diff saying so")

    suppressed = set((env_gate or {}).get("suppressed_modules") or [])
    if suppressed:
        mine = sorted({p.name for p in _entrypoint_files(cap) if p.name in suppressed})
        if mine:
            defects.append("heartbeat_env_suppressed")
            notes.append(f"{', '.join(mine)} is invoked above the "
                         f"{HEARTBEAT_ENV_FLAG} export, so its heartbeat records nothing")
            row["heartbeat_env_suppressed"] = mine

    row["defects"] = sorted(set(defects))
    row["notes"] = [n for n in notes if n]
    row["reachable"] = not row["defects"]
    return row


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Record that this audit ran, at its own code path.

    Lazy import + never raises + inert outside an active tick, matching the sibling modules. Added
    because the audit's FIRST run flagged itself with `no_heartbeat` -- which is the instrument
    working: it holds itself to the same standard it applies to everything else.
    """
    try:
        import capabilities as _caps
        _caps.production_heartbeat("capability-activation-audit", event_type,
                                   ref="capability_activation_audit.audit")
    except Exception:
        pass


def audit(*, path=None, use_cache: bool = True) -> dict:
    _capability_heartbeat()
    caps = capabilities.load(path or capabilities.REG)
    emittable = emittable_task_types()
    templates = _prompt_templates()
    index = _fleet_label_index(use_cache=use_cache)
    vgaps = vocabulary_gaps(index)
    env_gate = heartbeat_env_gate()
    reach = advisor_reach(caps)
    rows = [audit_capability(cid, cap, emittable=emittable, templates=templates,
                             label_index=index, vocab_gaps=vgaps, env_gate=env_gate, reach=reach)
            for cid, cap in sorted(caps.items())]
    by_defect: dict[str, list[str]] = {}
    for row in rows:
        for defect in row["defects"]:
            by_defect.setdefault(defect, []).append(row["capability_id"])
    by_entry: dict[str, int] = {}
    for row in rows:
        by_entry[row["entry_class"]] = by_entry.get(row["entry_class"], 0) + 1
    reachable = [r["capability_id"] for r in rows if r["reachable"]]
    return {
        "generated_at": int(time.time()),
        "total": len(rows),
        "reachable": len(reachable),
        "blocked": len(rows) - len(reachable),
        "reachable_ids": reachable,
        "by_entry_class": by_entry,
        "by_defect": {k: sorted(v) for k, v in sorted(by_defect.items())},
        "emittable_task_types": sorted(emittable),
        # Published with BOTH numbers so a zero can be read: `suppressed_modules: []` with
        # `invocations_after: 0` is a broken parse, not a clean ordering.
        "heartbeat_env_gate": env_gate,
        "advisor_reach": reach,
        "rows": rows,
    }


# --------------------------------------------------------------------------- progress over time

def load_history(path: Path | None = None) -> list[dict]:
    p = path or SNAPSHOT_PATH
    if not p.exists():
        return []
    try:
        blob = json.loads(p.read_text())
        return list(blob.get("snapshots") or [])
    except (OSError, ValueError):
        return []


def record_snapshot(rep: dict, *, path: Path | None = None) -> dict:
    """Persist the headline numbers so progress is measured, not asserted."""
    p = path or SNAPSHOT_PATH
    history = load_history(p)
    entry = {"generated_at": rep["generated_at"], "total": rep["total"],
             "reachable": rep["reachable"], "blocked": rep["blocked"],
             "reachable_ids": sorted(rep["reachable_ids"]),
             "by_defect": {k: len(v) for k, v in rep["by_defect"].items()}}
    history.append(entry)
    history = history[-MAX_SNAPSHOTS:]
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"snapshots": history}, indent=2))
    except OSError as exc:
        return {"recorded": False, "error": str(exc)}
    return {"recorded": True, "snapshots": len(history)}


def progress(rep: dict, *, path: Path | None = None) -> dict:
    """Movement since the last snapshot: gained, regressed, still blocked."""
    history = load_history(path)
    if not history:
        return {"baseline": None, "detail": "no snapshots yet — run --snapshot to start tracking"}
    prev = history[-1]
    prev_ids = set(prev.get("reachable_ids") or [])
    now_ids = set(rep["reachable_ids"])
    prev_def = prev.get("by_defect") or {}
    now_def = {k: len(v) for k, v in rep["by_defect"].items()}
    return {
        "baseline": prev.get("generated_at"),
        "snapshots": len(history),
        "reachable_then": prev.get("reachable"),
        "reachable_now": rep["reachable"],
        "gained": sorted(now_ids - prev_ids),
        "regressed": sorted(prev_ids - now_ids),
        "defect_delta": {k: now_def.get(k, 0) - prev_def.get(k, 0)
                         for k in sorted(set(now_def) | set(prev_def))
                         if now_def.get(k, 0) != prev_def.get(k, 0)},
    }


# --------------------------------------------------------------------------- rendering

def format_scorecard(rep: dict, prog: dict | None = None) -> str:
    pct = 100 * rep["reachable"] / rep["total"] if rep["total"] else 0
    lines = [
        "# Capability activation scorecard", "",
        f"  CAN FIRE:  {rep['reachable']:>3} of {rep['total']}  ({pct:.0f}%)",
        f"  BLOCKED:   {rep['blocked']:>3}", "",
        "  entry classes: " + ", ".join(f"{k}={v}" for k, v in sorted(rep["by_entry_class"].items())),
        "",
    ]
    if prog and prog.get("baseline"):
        age = (rep["generated_at"] - int(prog["baseline"])) / 86400
        lines += [f"  vs last snapshot ({age:.1f}d ago): "
                  f"{prog['reachable_then']} -> {prog['reachable_now']} reachable"]
        if prog["gained"]:
            lines.append(f"    GAINED:    {', '.join(prog['gained'])}")
        if prog["regressed"]:
            lines.append(f"    REGRESSED: {', '.join(prog['regressed'])}")
        if prog["defect_delta"]:
            lines.append("    defect delta: " + ", ".join(
                f"{k} {v:+d}" for k, v in prog["defect_delta"].items()))
        lines.append("")
    elif prog:
        lines += [f"  {prog.get('detail')}", ""]

    lines += ["## Blocked, by defect — each line is a fix with a target", ""]
    for defect, ids in rep["by_defect"].items():
        lines.append(f"  {defect}  ({len(ids)})")
        lines.append(f"      {DEFECT_CLASSES.get(defect, '')}")
        for cap_id in ids:
            lines.append(f"      - {cap_id}")
        lines.append("")
    if not rep["by_defect"]:
        lines += ["  none — every capability can fire", ""]
    lines += ["## Per capability", "",
              "| Capability | Entry | Can fire | Defects |", "|---|---|---|---|"]
    for row in rep["rows"]:
        lines.append(f"| {row['capability_id']} | {row['entry_class']} | "
                     f"{'yes' if row['reachable'] else 'NO'} | "
                     f"{', '.join(row['defects']) or '—'} |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- selftest

def _selftest() -> None:
    import tempfile

    # ENTRY CLASS is the load-bearing distinction: asking a task_type question of a kind-matched
    # capability is what made `no_matching_work` meaningless for 26 of 34 capabilities.
    assert entry_class({"matcher": {"field": "task_type", "value": ["testgen"]}}) == ENTRY_TASK_ROUTED
    assert entry_class({"matcher": {"kind": "transport", "name": "offload"}}) == ENTRY_DIRECT
    assert entry_class({"matcher": {"kind": "env", "name": "ORCH_X"}}) == ENTRY_GATED
    assert entry_class({"matcher": {}, "gate_reason": "held"}) == ENTRY_GATED
    assert entry_class({}) == ENTRY_UNKNOWN

    # PROMPT-SCHEMA CREDITING is narrow and driven by the dispatcher's own mapping. It exists
    # because a lane SCHEMA never executes -- its `main()` heartbeat has nowhere to move to -- while
    # `dispatcher.build_prompt` really does credit it on every routed dispatch. Both halves matter:
    # a capability in the mapping is credited, and one that is NOT in it still has to earn
    # reachability the normal way, or this becomes a blanket excuse for a genuinely dead module.
    import dispatcher as _d
    _mapped = set((_d.TASK_TYPE_CAPABILITY or {}).values())
    assert "codemod-campaign" in _mapped, _mapped
    assert heartbeat_reachable({"capability_id": "codemod-campaign",
                                "entrypoint": "codemod_lane.py"})["status"] == "reachable"
    # NARROWNESS CONTROL: `deliberate-break-verifier` is the same shape but is NOT in the mapping,
    # and must keep reporting a real defect -- local_verify.py genuinely has no caller.
    assert "deliberate-break-verifier" not in _mapped, _mapped
    assert heartbeat_reachable({"capability_id": "deliberate-break-verifier",
                                "entrypoint": "local_verify.py"})["status"] != "reachable"
    # CROSS-REPO CREDITING, same rule and same narrowness.
    import capability_outcome_bridge as _bridge
    _ci = set(_bridge.EXTERNAL_CI_CAPABILITIES or {})
    assert "docs-drift-fix-agent" in _ci, _ci
    assert heartbeat_reachable({"capability_id": "docs-drift-fix-agent",
                                "entrypoint": "Workflows/scripts/docs_drift_fix_agent.py"}
                               )["status"] == "reachable"
    assert heartbeat_reachable({"capability_id": "not-declared-anywhere",
                                "entrypoint": "Other/repo/script.py"}
                               )["status"] == "no_local_entrypoint"

    # classify() reachability, derived from backlog's own vocabulary rather than hardcoded.
    em = emittable_task_types()
    assert "testgen" in em and "codemod" in em and "implement" in em
    assert "docs" not in em, "docs labels map to mechanical; if this changes, the docs fix landed"
    assert "review" not in em, "classify() cannot emit review; if this changes, the fix landed"
    assert backlog.classify(["docs"]) == "mechanical"      # the reason docs is unreachable
    assert "testing" in labels_producing("testgen")

    idx = {"repos": {"o/a": ["testing", "bug"], "o/b": ["bug"], "o/c": ["bug"]}}
    cov = label_coverage("testgen", idx)
    assert cov["repos_with"] == 1 and cov["repos_total"] == 3, cov

    tmpl = _prompt_templates()
    assert "testgen" in tmpl and "docs" not in tmpl

    # A task-routed capability whose task_type cannot be emitted is BLOCKED, and says why.
    row = audit_capability("x", {"matcher": {"field": "task_type", "value": ["docs"]},
                                 "status": "generated"},
                           emittable=em, templates=tmpl, label_index=idx)
    assert not row["reachable"]
    assert "task_type_not_emittable" in row["defects"] and "no_prompt_template" in row["defects"]

    # A healthy task-routed capability is reachable.
    ok = audit_capability("t", {"matcher": {"field": "task_type", "value": ["testgen"]},
                                "status": "generated"},
                          emittable=em, templates=tmpl,
                          label_index={"repos": {f"o/{i}": ["testing"] for i in range(12)}})
    assert ok["reachable"], ok

    # HEARTBEAT REACHABILITY — the generalisation of the offload NameError and the main()-stranded
    # heartbeats. Build a fake module whose heartbeat sits only in main(), with no CLI caller.
    with tempfile.TemporaryDirectory(prefix="cap-audit-") as td:
        global HERE
        saved_here = HERE
        try:
            HERE = Path(td)
            (HERE / "lonely.py").write_text(
                "def _capability_heartbeat():\n    import capabilities\n"
                "    capabilities.production_heartbeat('x','invocation')\n"
                "def main(argv):\n    _capability_heartbeat()\n")
            (HERE / "tick.py").write_text("import lonely\nlonely.helper()\n")
            hb = heartbeat_reachable({"entrypoint": "lonely.py"})
            assert hb["status"] == "off_path", hb          # runs, but credit cannot land
            # ...and with a driver calling the heartbeat-bearing function, it IS reachable.
            (HERE / "tick.py").write_text("import lonely\nlonely._capability_heartbeat()\n")
            hb2 = heartbeat_reachable({"entrypoint": "lonely.py"})
            assert hb2["status"] == "reachable", hb2
            # A module with no heartbeat at all is a distinct, named defect.
            (HERE / "silent.py").write_text("def go():\n    return 1\n")
            assert heartbeat_reachable({"entrypoint": "silent.py"})["status"] == "no_heartbeat"
            # A main()-only heartbeat IS reachable when a shell driver runs it as a CLI.
            (HERE / "orchestrate.sh").write_text('python3 "$ORCH/lonely.py" --run\n')
            (HERE / "tick.py").write_text("x = 1\n")
            assert heartbeat_reachable({"entrypoint": "lonely.py"})["status"] == "reachable"
        finally:
            HERE = saved_here

    # HEARTBEAT ENABLEMENT — reachable is NOT the same as recorded. Synthetic fixture on purpose
    # (the real orchestrate.sh is now correctly ordered, so asserting against it would only prove
    # today's file and would go on passing if the ordering regressed in a way the parse missed).
    # Both directions, so a regression to a hardcoded verdict fails here.
    with tempfile.TemporaryDirectory(prefix="cap-envgate-") as td:
        root = Path(td)
        (root / "producer.py").write_text(
            "import capabilities\n"
            "def go():\n    capabilities.production_heartbeat('p','invocation')\n")
        # DEFINES the helpers and calls neither — the shape of capabilities.py, which MUST be
        # allowed above the export because it is the validation gate that authorises heartbeats.
        (root / "gatekeeper.py").write_text(
            "def production_heartbeat(a, b):\n    return False\n"
            "def daily_heartbeat(a, b):\n    return False\n")
        assert emits_heartbeat(root / "producer.py")
        assert not emits_heartbeat(root / "gatekeeper.py"), "a definition is not a call"

        bad = ('python3 "$ORCH/gatekeeper.py" --validate\n'
               'python3 "$ORCH/producer.py" --run\n'
               "export ORCH_CAPABILITY_HEARTBEATS=1\n"
               'python3 "$ORCH/producer.py" --run-again\n')
        (root / "orchestrate.sh").write_text(bad)
        broken = heartbeat_env_gate(here=root)
        assert broken["suppressed_modules"] == ["producer.py"], broken
        assert broken["invocations_before"] == 2 and broken["invocations_after"] == 1, broken
        assert not broken["anchor_present"], broken

        # REVERT: move the export above the producer and the defect must clear — and the
        # denominator must stay non-zero, so a clean verdict is distinguishable from a dead parse.
        good = (f"# {HEARTBEAT_EXPORT_ANCHOR}\n"
                'python3 "$ORCH/gatekeeper.py" --validate\n'
                "export ORCH_CAPABILITY_HEARTBEATS=1\n"
                'python3 "$ORCH/producer.py" --run\n')
        (root / "orchestrate.sh").write_text(good)
        fixed = heartbeat_env_gate(here=root)
        assert fixed["suppressed_modules"] == [], fixed
        assert fixed["invocations_after"] >= 1, "a zero denominator cannot be read as clean"
        assert fixed["anchor_present"], fixed

        # A one-line shell FUNCTION is deferred, not `before`: the definition's position says
        # nothing about when it runs, and calling it `before` invents a defect (`_gh_gate` defines
        # `python3 gh_capacity.py` at the top of orchestrate.sh and is called throughout).
        (root / "orchestrate.sh").write_text(
            '_g() { python3 "$ORCH/producer.py" --gate; }\n'
            "export ORCH_CAPABILITY_HEARTBEATS=1\n"
            'python3 "$ORCH/gatekeeper.py" --x\n')
        fn = heartbeat_env_gate(here=root)
        assert fn["suppressed_modules"] == [], fn
        assert fn["invocations_deferred"] == 1, fn

        # A COMMENT is not an invocation.
        (root / "orchestrate.sh").write_text(
            '# python3 "$ORCH/producer.py" --run\n'
            "export ORCH_CAPABILITY_HEARTBEATS=1\n"
            'python3 "$ORCH/producer.py" --run\n')
        cm = heartbeat_env_gate(here=root)
        assert cm["suppressed_modules"] == [], cm
        assert cm["invocations_before"] == 0, cm

        # And the defect must reach the capability ROW, or the audit reports a clean sheet while
        # the producer records nothing.
        (root / "orchestrate.sh").write_text(bad)
        gate = heartbeat_env_gate(here=root)
        saved_here = HERE
        try:
            HERE = root
            row = audit_capability("p", {"matcher": {"kind": "tick_phase", "name": "p"},
                                         "entrypoint": "producer.py", "status": "generated"},
                                   emittable=em, templates=tmpl, label_index=idx, env_gate=gate)
        finally:
            HERE = saved_here
        assert "heartbeat_env_suppressed" in row["defects"], row
        assert not row["reachable"], row

    # ADVISOR REACH — the span of the front door, and whether it has narrowed. Synthetic ledger, so
    # this exercises the mechanism rather than today's ledger contents.
    synthetic = {
        "task-shaped": {"capability_id": "task-shaped", "status": "generated",
                        "matcher": {"field": "task_type", "operator": "in",
                                    "value": ["testgen"]}},
        "kind-shaped": {"capability_id": "kind-shaped", "status": "generated",
                        "matcher": {"kind": "tick_phase", "name": "x"}},
        "retired-task-shaped": {"capability_id": "retired-task-shaped", "status": "retired",
                                "matcher": {"field": "task_type", "operator": "in",
                                            "value": ["testgen"]}},
    }
    reach = advisor_reach(synthetic)
    assert reach["reachable"] == ["task-shaped"], reach
    assert reach["by_capability"]["task-shaped"] == ["testgen"], reach
    assert reach["capability_count"] == 3, "the denominator must travel with the count"
    # THE DERIVED HALF must be reported and must agree with the advisor's own total. Two modules
    # publishing different "reach" numbers for the same front door is a parallel inventory.
    import capability_advisor
    assert reach["direct_entry"] == dict(sorted(capability_advisor.direct_entry().items())), reach
    assert reach["total_reachable_count"] == len(
        set(reach["reachable"]) | set(reach["direct_entry_targets"])), reach
    # A derived direct-entry map CAN shrink with no diff in either module (drop an entry from
    # dispatcher.TASK_TYPE_CAPABILITY and the front door narrows in silence), which is why the
    # derived set has its own baseline. Simulate the shrink: an empty map must regress.
    import unittest.mock as _mock
    with _mock.patch.object(capability_advisor, "direct_entry", lambda: {}):
        shrunk = advisor_reach(synthetic)
    assert shrunk["direct_entry_regressed"] == sorted(ADVISOR_DIRECT_ENTRY_BASELINE), shrunk
    assert shrunk["direct_entry_targets"] == [], shrunk
    # A kind-shaped matcher failing to match is CORRECT, not a defect: it is entered directly.
    kind_row = audit_capability("kind-shaped", synthetic["kind-shaped"], emittable=em,
                                templates=tmpl, label_index=idx, reach=reach)
    assert kind_row["advisor_reachable"] is False, kind_row
    assert "advisor_reach_regression" not in kind_row["defects"], kind_row
    # ...but a capability IN THE BASELINE that is no longer reachable is a silent narrowing, and
    # must surface. Simulate it by naming a baseline member the synthetic ledger cannot reach.
    victim = sorted(ADVISOR_REACH_BASELINE)[0]
    regressed = advisor_reach({victim: {"capability_id": victim, "status": "generated",
                                        "matcher": {"kind": "ci_workflow", "name": "y"}}})
    assert regressed["regressed"], regressed
    reg_row = audit_capability(victim, {"capability_id": victim, "status": "generated",
                                        "matcher": {"kind": "ci_workflow", "name": "y"},
                                        "entrypoint": "nothing.py"},
                               emittable=em, templates=tmpl, label_index=idx, reach=regressed)
    assert "advisor_reach_regression" in reg_row["defects"], reg_row
    # An unreadable advisor must report the whole baseline as regressed rather than an empty,
    # reassuring result — silence must never read as a pass.
    assert advisor_reach({})["regressed"] == sorted(ADVISOR_REACH_BASELINE)

    # EXTERNAL CALLER. `entrypoint_external` is a blocker only while the cross-repo change is
    # OUTSTANDING. Once the caller lands, continuing to report "blocked" would be the same
    # false-negative this module exists to prevent.
    with tempfile.TemporaryDirectory(prefix="cap-ext-") as td4:
        saved4 = HERE
        try:
            sibling = Path(td4) / "SiblingRepo"
            (sibling / ".github" / "workflows").mkdir(parents=True)
            globals()["HERE"] = Path(td4) / "Orchestrator"
            (Path(td4) / "Orchestrator").mkdir()
            cap = {"matcher": {"kind": "ci_workflow", "name": "maint-99-thing"},
                   "entrypoint": "SiblingRepo/scripts/thing.py"}
            # Caller absent -> not reachable.
            got = external_caller(cap)
            assert got and got["exists"] is False, got
            # Caller present -> reachable.
            (sibling / ".github" / "workflows" / "maint-99-thing.yml").write_text("name: x\n")
            got2 = external_caller(cap)
            assert got2 and got2["exists"] is True, got2
            assert got2["repo"] == "SiblingRepo" and got2["workflow"] == "maint-99-thing"
            # A non-ci_workflow matcher is not this check's business.
            assert external_caller({"matcher": {"kind": "transport"}}) is None

            # LOCATION INDEPENDENCE. This is the assertion whose absence let byte-identical code
            # score 37 of 37 here and 36 of 37 in the mirror that launchd actually runs: HERE.parent
            # was the ONLY candidate root, and ~/.codex has no fleet checkout. With HERE pointed at
            # a temp dir that contains no fleet repo, a REAL fleet capability must still resolve via
            # the home-anchored candidate — otherwise the verdict depends on where it ran.
            import capabilities as _caps_mod
            _real_cap = _caps_mod.load(_caps_mod.REG).get("docs-drift-fix-agent")
            if _real_cap:
                anywhere = external_caller(_real_cap)
                assert anywhere and anywhere["exists"] is True, \
                    f"a real fleet caller must resolve from ANY cwd, not just the canonical tree: {anywhere}"
                assert anywhere.get("root_origin") == "home-anchored-workspace", anywhere

            # An explicit override wins, so a relocated workspace stays checkable.
            _saved_env = os.environ.get("ORCH_FLEET_ROOT")
            try:
                os.environ["ORCH_FLEET_ROOT"] = td4
                ov = external_caller(cap)
                assert ov and ov["exists"] is True and ov["root_origin"] == "ORCH_FLEET_ROOT", ov
            finally:
                if _saved_env is None:
                    os.environ.pop("ORCH_FLEET_ROOT", None)
                else:
                    os.environ["ORCH_FLEET_ROOT"] = _saved_env
        finally:
            globals()["HERE"] = saved4

    # DECLARED ENTRYPOINT + TRANSITIVE HELPER. Both were missing and together produced a false
    # `off_path` for all five role capabilities, including role-triage with 688 invocations:
    # heartbeats live in a private helper, and the entry function is dispatched via a registry
    # rather than called by bare name from a driver.
    with tempfile.TemporaryDirectory(prefix="cap-decl-") as td3:
        saved3 = HERE
        try:
            globals()["HERE"] = Path(td3)
            (Path(td3) / "rolesy.py").write_text(
                "def _event():\n    import capabilities\n"
                "    capabilities.production_heartbeat('x','match')\n"
                "def run_thing_agent():\n    _event()\n"
                "def unrelated():\n    return 1\n")
            (Path(td3) / "tick.py").write_text("import rolesy\nrolesy.unrelated()\n")
            hb = heartbeat_reachable({"entrypoint": "rolesy.py:run_thing_agent"})
            assert hb["status"] == "reachable", hb
            assert "run_thing_agent" in str(hb.get("via")), hb
            # A declared entry that does NOT reach a heartbeat is still correctly off_path.
            hb2 = heartbeat_reachable({"entrypoint": "rolesy.py:unrelated"})
            assert hb2["status"] == "off_path", hb2
        finally:
            globals()["HERE"] = saved3
    assert _declared_functions({"entrypoint": "roles.py:run_triage_agent"}) == {"run_triage_agent"}
    assert _declared_functions({"entrypoint": "watch.py"}) == set()

    # VOCABULARY MISMATCH: the fleet writes `risk:major`; adversarial.HIGH_STAKES_LABELS accepts
    # only `risk:critical`/`risk:high`, so high_stakes_reason() returned None for every genuinely
    # high-stakes issue. A near miss in a shared namespace is the generalisable signal.
    # `risk:major` was the original defect and is now ACCEPTED, so testing it would be vacuous.
    # Use a namespace sibling the code still (correctly) rejects, so the detector stays tested.
    # `risk:major` is now accepted and risk:low/medium/minor are DELIBERATE exclusions, so both
    # would be vacuous. Use an unseen namespace sibling so the near-miss detector stays tested.
    fake_index = {"repos": {"o/a": ["risk:severe", "bug"], "o/b": ["risk:severe"]}}
    gaps = vocabulary_gaps(fake_index)
    assert "adversarial-review" in gaps and "risk:severe" in gaps["adversarial-review"], gaps
    # A DELIBERATE exclusion must not be reported as a gap — flagging risk:low would push toward
    # spending reviewer seats on routine work.
    assert "adversarial-review" not in vocabulary_gaps(
        {"repos": {"o/a": ["risk:low", "risk:minor"]}}), "deliberate exclusion reported as a gap"
    # And the FIXED label must no longer be reported as a gap — proof the fix landed.
    assert "risk:major" not in vocabulary_gaps(
        {"repos": {"o/a": ["risk:major"]}}).get("adversarial-review", []), \
        "risk:major should now be accepted by HIGH_STAKES_LABELS"
    # A label in a namespace the code never uses is NOT a mismatch (no false positives).
    assert not vocabulary_gaps({"repos": {"o/a": ["colour:blue", "bug"]}}), \
        "unrelated namespace flagged as a mismatch"
    # And an accepted label produces no gap.
    assert "adversarial-review" not in vocabulary_gaps({"repos": {"o/a": ["risk:critical"]}})

    # Both audit bugs found on first live run, pinned so they cannot return.
    # (a) `module.function` entrypoints must resolve, or a running capability reads as missing.
    assert [p.name for p in _entrypoint_files({"entrypoint": "dispatcher.offload"})] \
        == ["dispatcher.py"], "module.function entrypoint did not resolve"
    assert [p.name for p in _entrypoint_files({"entrypoint": "roles.py:run_triage_agent"})] \
        == ["roles.py"]
    assert _entrypoint_files({"entrypoint": "Workflows/scripts/nope.py"}) == []

    # ENTRYPOINT PRESENCE — the two cases a bare capability id cannot tell apart. The ledger is
    # shared machine-local state while code is branch-isolated, so a SIBLING branch's registration
    # produces the same red as a row declared with no implementation at all. Reading the first as
    # the second cost a full session on 2026-08-22, and the remedies proposed for a live
    # capability were to retire its ledger row or mask it with a waiver.
    with tempfile.TemporaryDirectory(prefix="cap-entrypoint-") as td3:
        saved_here = HERE
        try:
            globals()["HERE"] = Path(td3)
            (Path(td3) / "present_lane.py").write_text("# in the tree\n")
            here_cap = {"capability_id": "here-cap", "entrypoint": "present_lane.py:run"}
            gone_cap = {"capability_id": "gone-cap", "entrypoint": "absent_lane.py:run"}
            away_cap = {"capability_id": "away-cap", "entrypoint": "Elsewhere/away_lane.py"}
            bare_cap = {"capability_id": "bare-cap", "entrypoint": None}
            assert entrypoint_presence(here_cap)["state"] == ENTRYPOINT_PRESENT
            assert entrypoint_presence(gone_cap)["state"] == ENTRYPOINT_ABSENT
            assert entrypoint_presence(gone_cap)["absent"] == ["absent_lane.py"]
            assert entrypoint_presence(away_cap)["state"] == ENTRYPOINT_EXTERNAL
            assert entrypoint_presence(bare_cap)["state"] == ENTRYPOINT_UNDECLARED
            # A part-resolving declaration is a TREE problem, not a pass: `a.py/b.py` with only
            # one half present must still name the missing half.
            half = {"capability_id": "half-cap", "entrypoint": "present_lane.py/absent_lane.py"}
            assert entrypoint_presence(half)["state"] == ENTRYPOINT_ABSENT
            assert entrypoint_presence(half)["absent"] == ["absent_lane.py"], entrypoint_presence(half)
            # The `->` between two declarations names no module and must not be diagnosed as one.
            arrow = {"capability_id": "arrow", "entrypoint": "present_lane.py:a -> present_lane.py:b"}
            assert entrypoint_presence(arrow)["state"] == ENTRYPOINT_PRESENT, entrypoint_presence(arrow)

            led = {"here-cap": here_cap, "gone-cap": gone_cap, "away-cap": away_cap}
            miss = {"here-cap": ["fixture"], "gone-cap": ["caller_exists", "heartbeat", "fixture"]}

            # BRANCH 1 — the file is absent: say so, and point at the branch check WITH its caveat.
            # The caveat is the load-bearing half: the wrong verdict rested on `git log --all`
            # returning nothing for a branch whose ref had never been fetched.
            gone = entrypoint_diagnosis(["gone-cap"], missing=miss, ledger=led)
            assert "absent_lane.py is NOT in this tree" in gone, gone
            assert "unmerged branch" in gone, gone
            assert "git log --all --oneline -- absent_lane.py" in gone, gone
            assert "fetch first" in gone, gone
            # ...and the missing parts are named as a CONSEQUENCE, not as independent evidence.
            assert "follow from that absent file" in gone, gone
            # The hand-rolled gate runners print only `str(exc)[:400]`, which is WHY the diagnosis
            # is prepended rather than appended. Pin that one capability's diagnosis fits inside
            # that budget, or the fix is invisible in the harness verify.py runs these gates in.
            assert len(gone) <= 400, len(gone)

            # BRANCH 2 — the file is present: same gate, opposite conclusion, and it must NOT
            # mention a branch, or the text sends a reader hunting for code in front of them.
            here = entrypoint_diagnosis(["here-cap"], missing=miss, ledger=led)
            assert "present_lane.py IS in this tree" in here, here
            assert "genuine admission defect (missing: fixture)" in here, here
            assert "branch" not in here and "NOT in this tree" not in here, here

            # A cross-repo entrypoint is neither case: `git log --all` here can never find it.
            away = entrypoint_diagnosis(["away-cap"], ledger=led)
            assert "ANOTHER repository" in away and "never find it" in away, away

            # NOTHING HERE SKIPS, and nothing is swallowed. An id the ledger does not hold still
            # produces text naming it, and a passing caller gets nothing prepended.
            assert entrypoint_diagnosis([]) == ""
            assert "gone-cap" in entrypoint_diagnosis(["gone-cap"], ledger={})
            # A diagnosis must never break the failure it is explaining.
            saved_load = capabilities.load_declared
            try:
                def _boom(*a, **k):
                    raise OSError("ledger unreadable")
                capabilities.load_declared = _boom
                broke = entrypoint_diagnosis(["gone-cap"])
                assert "could not read the capability ledger" in broke, broke
            finally:
                capabilities.load_declared = saved_load
        finally:
            globals()["HERE"] = saved_here
    # (b) a mention inside a shell COMMENT is not a caller — matching it reported a
    # main()-stranded heartbeat as reachable, which is a false PASS.
    with tempfile.TemporaryDirectory(prefix="cap-cmt-") as td2:
        saved = HERE
        try:
            globals()["HERE"] = Path(td2)
            (Path(td2) / "lonely2.py").write_text(
                "def _capability_heartbeat():\n    pass\n"
                "def main(argv):\n    _capability_heartbeat()\n")
            (Path(td2) / "tick.py").write_text("import lonely2\nlonely2.helper()\n")
            (Path(td2) / "orchestrate.sh").write_text(
                "# See lonely2.py for details\necho hi\n")
            hb = heartbeat_reachable({"entrypoint": "lonely2.py"})
            assert hb["status"] == "off_path", f"comment counted as a caller: {hb}"
            # A real invocation on a non-comment line IS a caller.
            (Path(td2) / "orchestrate.sh").write_text(
                "# See lonely2.py for details\npython3 \"$ORCH/lonely2.py\" --run\n")
            assert heartbeat_reachable({"entrypoint": "lonely2.py"})["status"] == "reachable"
        finally:
            globals()["HERE"] = saved

    # PROGRESS: snapshots must show movement, and a regression must be visible as such.
    with tempfile.TemporaryDirectory(prefix="cap-hist-") as td:
        hp = Path(td) / "h.json"
        r1 = {"generated_at": 1000, "total": 3, "reachable": 1, "blocked": 2,
              "reachable_ids": ["a"], "by_defect": {"no_heartbeat": ["b", "c"]}}
        assert record_snapshot(r1, path=hp)["recorded"]
        r2 = {"generated_at": 2000, "total": 3, "reachable": 2, "blocked": 1,
              "reachable_ids": ["a", "b"], "by_defect": {"no_heartbeat": ["c"]}}
        p = progress(r2, path=hp)
        assert p["gained"] == ["b"] and p["regressed"] == [], p
        assert p["defect_delta"] == {"no_heartbeat": -1}, p
        # A regression is reported, not smoothed over.
        record_snapshot(r2, path=hp)
        r3 = dict(r2, generated_at=3000, reachable=1, reachable_ids=["a"],
                  by_defect={"no_heartbeat": ["b", "c"]})
        p3 = progress(r3, path=hp)
        assert p3["regressed"] == ["b"], p3
        assert p3["defect_delta"] == {"no_heartbeat": 1}, p3
        assert progress(r2, path=Path(td) / "absent.json")["baseline"] is None

    print("capability_activation_audit.py selftest: OK (entry classes, emittable task types, "
          "heartbeat off-path vs no-heartbeat vs reachable, heartbeat env-suppression both "
          "directions, entrypoint present vs absent-from-tree vs another-repo, "
          "advisor reach + narrowing, progress + regression tracking)")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--progress", action="store_true", help="movement vs recorded snapshots")
    ap.add_argument("--snapshot", action="store_true", help="record today's state")
    ap.add_argument("--no-cache", action="store_true", help="re-read fleet labels from GitHub")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    rep = audit(use_cache=not args.no_cache)
    prog = progress(rep) if (args.progress or not args.json) else None
    if args.snapshot:
        rep["snapshot"] = record_snapshot(rep)
    if args.json:
        print(json.dumps({**rep, "progress": prog} if prog else rep, indent=2))
    else:
        print(format_scorecard(rep, prog), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
