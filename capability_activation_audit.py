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
DRIVER_MODULES = (
    "tick.py",
    "dispatcher.py",
    "orchestrate.sh",
    "router.py",
    "roles.py",
    "capacity.py",
    "backlog.py",
    "outcomes.py",
)

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
            accepted = {
                str(x).strip().lower()
                for x in getattr(importlib.import_module(module_name), attr, ()) or ()
            }
        except Exception:
            continue
        namespaces = {a.split(":", 1)[0] for a in accepted if ":" in a}
        deliberate = INTENTIONAL_EXCLUSIONS.get(cap_id, set())
        misses = sorted(
            f
            for f in fleet
            if ":" in f
            and f.split(":", 1)[0] in namespaces
            and f not in accepted
            and f not in deliberate
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
    for name in (
        "MECHANICAL_LABELS",
        "TESTGEN_LABELS",
        "EPIC_LABELS",
        "CODEMOD_LABELS",
        "CROSS_REPO_LABELS",
        "RUNTIME_AC_LABELS",
    ):
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
    for name in (
        "MECHANICAL_LABELS",
        "TESTGEN_LABELS",
        "EPIC_LABELS",
        "CODEMOD_LABELS",
        "CROSS_REPO_LABELS",
        "RUNTIME_AC_LABELS",
    ):
        vocab |= set(getattr(backlog, name, ()) or ())
    return sorted(lb for lb in vocab if backlog.classify([lb]) == task_type)


# A basename that could actually BE a module in this tree. `exp_abcd.py:followup ->
# synthesis_promotion.py:reconcile` tokenises the arrow as a token of its own, which probes for a
# file called `->.py`; harmless while only existence was asked, but reporting it as a MISSING module
# would fabricate a gap out of punctuation — the exact class of finding this module exists to avoid.
_MODULE_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*\.py\Z")


def _declared_modules(cap: dict) -> list[tuple[str, list[str]]]:
    """Per entrypoint token, the .py basenames it could name. Existence is NOT consulted.

    Split out of `_entrypoint_files` so the same parse answers two different questions: "which
    files ARE here" (below) and "which files did this row CLAIM" (`entrypoint_presence`). Two
    parses would drift, and the drift would be invisible — the second one would quietly disagree
    with the first about what a row even declared.
    """
    raw = str(cap.get("entrypoint") or "")
    out: list[tuple[str, list[str]]] = []
    for token in re.split(r"[\s,;]+|(?<=\.py)/", raw):
        token = token.strip()
        if not token:
            continue
        stem = token.split(":")[0]
        candidates = []
        if stem.endswith(".py"):
            candidates.append(Path(stem).name)
        else:
            # `dispatcher.offload` -> dispatcher.py ; `a.b.c` -> try each dotted prefix
            parts = stem.split(".")
            for i in range(len(parts), 0, -1):
                candidates.append(".".join(parts[:i]) + ".py")
        candidates = [c for c in candidates if _MODULE_NAME_RE.match(c)]
        if candidates:
            out.append((token, candidates))
    return out


def _entrypoint_files(cap: dict) -> list[Path]:
    """Local .py files named by the entrypoint declaration.

    Entrypoints come in three shapes and all three must resolve, or the audit invents defects:
    `watch.py`, `roles.py:run_triage_agent`, and `dispatcher.offload` (module.function, no `.py`).
    The third form produced a false `entrypoint_missing` for `offload` — a capability running
    196x/week — which is precisely the kind of fabricated finding this module exists to prevent.
    """
    out: list[Path] = []
    for _token, candidates in _declared_modules(cap):
        for name in candidates:
            path = HERE / name
            if path.exists() and path not in out:
                out.append(path)
                break
    return out


# --------------------------------------------------------- is the declared code even in this tree?
#
# THE DEFECT (observed 2026-08-22). The ledger row `evidence-acquisition` (entrypoint
# `evidence_acquisition.py:run`) sat in the SHARED machine-local ledger while its module existed
# only on an unmerged branch, in a sibling worktree. `verify.py` therefore went red in every OTHER
# worktree with three failures that named only the missing admission parts:
#
#     test_capability_admission  -> {'evidence-acquisition': ['caller_exists','heartbeat','fixture']}
#     test_capability_set_coverage::test_every_capability_has_a_recurrence_fixture
#     test_model_tier_resolution::test_every_capability_has_a_heartbeat_call_site
#
# All three read as "a row registered with no implementation — retire it, or waive it". Both of
# those actions would have DISCARDED finished, CI-green work. And CI cannot catch the confusion:
# `ci.yml` points ORCH_LOCAL_RUNTIME at an empty temp dir, so the ledger bootstraps to the rows the
# code declares and the extra row never exists there — main was green while local verify was red.
#
# The two situations look identical in that output and their fixes are OPPOSITE:
#   * module ABSENT from this tree -> WAIT OR MERGE. The declaration is fine; the code is elsewhere.
#   * module PRESENT but its caller/heartbeat/fixture missing -> FIX THE DECLARATION.
#
# This is structural, not a one-off: the ledger is shared per MACHINE ($ORCH_LOCAL_RUNTIME) while
# code is branch-isolated per WORKTREE, so any two concurrent sessions can produce it.
#
# DIAGNOSTIC ONLY, deliberately. Nothing here waives, retires, suppresses or skips anything — the
# three checks still fail, exactly as loudly. What changes is that the failure names which of the
# two situations it is, and where the code actually is.
ENTRYPOINT_PRESENT = "present"
ENTRYPOINT_ABSENT = "absent_here"
ENTRYPOINT_EXTERNAL = "external_repo"
ENTRYPOINT_UNDECLARED = "undeclared"


def _repo_root() -> Path:
    """The checkout that `.claude/worktrees/*` hangs off, whether we are IN it or in a worktree."""
    if HERE.parent.name == "worktrees" and HERE.parent.parent.name == ".claude":
        return HERE.parent.parent.parent
    return HERE


def sibling_checkouts() -> list[tuple[Path, str]]:
    """Other checkouts of THIS repo, where a module absent here may legitimately live.

    Filesystem only, on purpose. `git worktree list` would be authoritative, but git on this
    workspace's cloud-sync volume is the documented slow/corruption hazard, this runs inside a test
    assertion message, and a directory that git has forgotten about still holds the code we are
    trying to account for. A checkout is recognised by carrying `capabilities.py`, so an unrelated
    directory under `.claude/worktrees/` cannot masquerade as one.
    """
    root = _repo_root()
    here = HERE.resolve()
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()

    def _add(path: Path, label: str) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved == here or str(resolved) in seen:
            return
        if not (path / "capabilities.py").is_file():
            return
        seen.add(str(resolved))
        out.append((path, label))

    _add(root, "repo root")
    worktrees = root / ".claude" / "worktrees"
    if worktrees.is_dir():
        try:
            children = sorted(p for p in worktrees.iterdir() if p.is_dir())
        except OSError:
            children = []
        for child in children:
            _add(child, f".claude/worktrees/{child.name}")
    return out


def entrypoint_presence(cap: dict) -> dict:
    """Is the code this row DECLARES actually in this tree, and if not, where is it?

    Four states, because collapsing any two of them puts a reader on the wrong fix:
      * `present`       — at least one declared module resolves here. Missing admission parts are
                          then a DECLARATION problem, and the row is the thing to change.
      * `absent_here`   — the declaration names local modules and none of them exist here. The row
                          was registered by another checkout; this is wait-or-merge.
      * `external_repo` — the declaration names a path in a SIBLING REPOSITORY
                          (`Workflows/scripts/...`). Deliberate and long-standing; not a worktree
                          gap. Uses the same rule as the `entrypoint_external` defect below, which
                          now calls this function so the two cannot disagree.
      * `undeclared`    — the row names no module at all.

    `missing` is per TOKEN, not per candidate name: `dispatcher.offload` legitimately probes
    `dispatcher.offload.py` before `dispatcher.py`, so calling the first one "missing" would
    manufacture a gap out of the probe order. A row where SOME tokens resolve stays `present` —
    its code runs here, so its fix is still the declaration — but the unresolved tokens are
    reported in `missing` for a caller that wants them.
    """
    raw = str(cap.get("entrypoint") or "").strip()
    declared = _declared_modules(cap)
    if not raw or not declared:
        return {
            "state": ENTRYPOINT_UNDECLARED,
            "entrypoint": raw,
            "present": [],
            "missing": [],
            "found_in": [],
            "searched": [],
            "detail": "the row declares no entrypoint module at all",
        }

    present = [p.name for p in _entrypoint_files(cap)]
    missing = [
        {"token": token, "candidates": candidates}
        for token, candidates in declared
        if not any((HERE / name).exists() for name in candidates)
    ]
    if present:
        return {
            "state": ENTRYPOINT_PRESENT,
            "entrypoint": raw,
            "present": present,
            "missing": missing,
            "found_in": [],
            "searched": [],
            "detail": f"{', '.join(present)} present in this tree",
        }

    # A path with directory components names another REPOSITORY, not a module we failed to find.
    external = any(
        "/" in token and not token.startswith(("./", "/")) for token, _candidates in declared
    )
    if external:
        return {
            "state": ENTRYPOINT_EXTERNAL,
            "entrypoint": raw,
            "present": [],
            "missing": missing,
            "found_in": [],
            "searched": [],
            "detail": f"{raw} names a path in another repository",
        }

    wanted = [c for _token, candidates in declared for c in candidates]
    searched, found_in = [], []
    for path, label in sibling_checkouts():
        searched.append(label)
        hits = sorted({name for name in wanted if (path / name).is_file()})
        if hits:
            found_in.append({"checkout": label, "path": str(path), "modules": hits})
    names = ", ".join(dict.fromkeys(c for _token, candidates in declared for c in candidates[-1:]))
    return {
        "state": ENTRYPOINT_ABSENT,
        "entrypoint": raw,
        "present": [],
        "missing": missing,
        "found_in": found_in,
        "searched": searched,
        "detail": f"{names} is not in this tree",
    }


def absent_entrypoint_report(capability_ids, *, path: Path | None = None) -> dict:
    """Which of these rows declare code that is NOT in this tree, and where that code was found.

    `total` travels with `absent` on purpose: the runtime rule in CLAUDE.md is that a count must
    arrive next to what bounds it. "1 absent" reads as an emergency; "1 of 43 rows absent" reads
    as one session's in-flight branch, which is what it is.
    """
    # `load_declared`, NOT `load`: the writing loader bootstraps and RECONCILES ONTO DISK, and a
    # diagnostic printed inside an assertion message must never mutate shared machine-local state
    # as a side effect of explaining a failure. `load_declared` reconciles an in-memory copy and
    # writes nothing, and both sibling checks already read the ledger through it.
    ledger = capabilities.load_declared(path or capabilities.REG)
    wanted = [cid for cid in capability_ids if cid in ledger]
    absent = []
    for cid in wanted:
        verdict = entrypoint_presence(ledger[cid])
        if verdict["state"] == ENTRYPOINT_ABSENT:
            absent.append({"capability_id": cid, **verdict})
    return {"absent": absent, "checked": len(wanted), "total": len(ledger)}


def absent_entrypoint_note(capability_ids, *, path: Path | None = None, indent: str = "  ") -> str:
    """The diagnostic block, or '' when every row's code is here. Appended to a FAILURE, never a skip.

    One formatter for all three checks so they cannot tell three different stories about the same
    row — which is how "retire it" and "wait for the merge" ended up looking like the same finding.
    Returning '' when there is nothing to say keeps the ordinary declaration failure unchanged.
    """
    try:
        rep = absent_entrypoint_report(capability_ids, path=path)
    except Exception as exc:  # noqa: BLE001
        # A diagnostic must never convert the real assertion into an error about the diagnostic.
        return (
            f"\n{indent}[entrypoint-presence diagnostic unavailable: "
            f"{type(exc).__name__}: {exc}]"
        )
    if not rep["absent"]:
        return ""
    n = len(rep["absent"])
    # Both numbers in the same place, per the runtime rule in CLAUDE.md: "1 absent" reads as an
    # emergency, "1 of 43 ledger rows" reads as one session's in-flight branch, which is what it is.
    # Counted against the LEDGER rather than against the failing set, so the sentence reads the
    # same whether one row failed or twenty.
    verb, pronoun = ("declares", "Its") if n == 1 else ("declare", "Their")
    out = [
        "",
        f"{indent}MODULE ABSENT FROM THIS TREE — {n} of the {rep['total']} ledger rows {verb} an "
        f"entrypoint whose code",
        f"{indent}is not in this checkout, and is named above. {pronoun} "
        f"caller/heartbeat/fixture CANNOT be here either:",
    ]
    for row in rep["absent"]:
        out.append(
            f"{indent}  {row['capability_id']}  declares {row['entrypoint']} — " f"{row['detail']}"
        )
        if row["found_in"]:
            for hit in row["found_in"]:
                out.append(
                    f"{indent}      but {', '.join(hit['modules'])} IS present in "
                    f"{hit['checkout']}"
                )
        elif row["searched"]:
            out.append(
                f"{indent}      not found in {len(row['searched'])} sibling checkout(s) "
                f"either ({', '.join(row['searched'][:4])})"
            )
        else:
            out.append(f"{indent}      no sibling checkout to search from here")
    out += [
        f"{indent}This is WAIT-OR-MERGE, not fix-the-declaration. The capability ledger is SHARED "
        f"machine-local",
        f"{indent}state while code is branch-isolated per worktree, so a row another session "
        f"registered reads",
        f"{indent}exactly like a row with no implementation. Check the checkouts named above and "
        f"the repo's open",
        f"{indent}PRs BEFORE retiring the row or adding a WAIVERS entry — either one discards "
        f"finished work.",
        f"{indent}Any row NOT listed here has its module present, and for those the fix is the "
        f"declaration.",
    ]
    return "\n".join(out)


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
            if called in (
                "_capability_heartbeat",
                "production_heartbeat",
                "daily_heartbeat",
                "heartbeat",
                "_lane_capability_match",
            ):
                names.add(node.name)
                break
    return names


def _call_graph(path: Path) -> dict[str, set[str]]:
    """function -> functions it calls WITHIN the same module (bare-name calls only)."""
    try:
        tree = ast.parse(path.read_text(errors="ignore"))
    except (OSError, SyntaxError):
        return {}
    local = {
        n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
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
            return {
                "exists": True,
                "repo": repo,
                "workflow": workflow,
                "path": str(path),
                "root_origin": origin,
            }
    tried = [str(r / rel) for r, _ in _fleet_roots()]
    return {
        "exists": False,
        "repo": repo,
        "workflow": workflow,
        "path": tried[0] if tried else "",
        "tried": tried,
    }


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
    roots.append(
        (
            pathlib.Path.home() / "Library/CloudStorage/Dropbox/Learning/Code",
            "home-anchored-workspace",
        )
    )
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
        return {
            "status": "reachable",
            "via": ["dispatcher.build_prompt via TASK_TYPE_CAPABILITY (prompt-schema capability)"],
            "functions": ["build_prompt"],
        }
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
        return {
            "status": "reachable",
            "via": [
                "capability_outcome_bridge.ingest_external_ci_invocations "
                "(cross-repo CI observation)"
            ],
            "functions": ["ingest_external_ci_invocations"],
        }
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
            return {
                "status": "reachable",
                "via": [f"declared entrypoint {sorted(declared)[0]}"],
                "functions": sorted(hb_funcs),
            }

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
            return {
                "status": "off_path",
                "functions": sorted(hb_funcs),
                "detail": f"heartbeat only in {sorted(hb_funcs)}, but drivers call "
                f"{any_call[:2]} directly",
            }
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
    return any(
        _HEARTBEAT_CALL_RE.search(line) and not _HEARTBEAT_DEF_RE.match(line)
        for line in text.splitlines()
    )


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
            rf"^\s*export\s+{re.escape(HEARTBEAT_ENV_FLAG)}=1\b", line
        ):
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
    out = {
        "flag": HEARTBEAT_ENV_FLAG,
        "anchor": HEARTBEAT_EXPORT_ANCHOR,
        "drivers": {},
        "suppressed_modules": [],
        "invocations_before": 0,
        "invocations_after": 0,
        "invocations_deferred": 0,
        "anchor_present": False,
    }
    suppressed: dict[str, list[str]] = {}
    for driver in SHELL_DRIVERS:
        dpath = root / driver
        if not dpath.exists():
            continue
        text = dpath.read_text(errors="ignore")
        gate = shell_heartbeat_gate(text)
        if HEARTBEAT_EXPORT_ANCHOR in text:
            out["anchor_present"] = True
        emitting_before = sorted({mod for _, mod in gate["before"] if emits_heartbeat(root / mod)})
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
ADVISOR_REACH_BASELINE = frozenset(
    {
        "codemod-campaign",
        "cross-repo-coordination",
        "deliberate-break-verifier",
        "epic-decomposition",
        "testgen-lane",
    }
)
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
    except Exception as exc:  # noqa: BLE001
        return {
            "unreadable": f"capability_advisor unavailable ({type(exc).__name__})",
            "reachable": [],
            "by_capability": {},
            "task_types": [],
            "regressed": sorted(ADVISOR_REACH_BASELINE),
            "direct_entry": {},
            "direct_entry_targets": [],
            "direct_entry_only": [],
            "direct_entry_baseline": sorted(ADVISOR_DIRECT_ENTRY_BASELINE),
            "direct_entry_regressed": sorted(ADVISOR_DIRECT_ENTRY_BASELINE),
            "total_reachable_count": 0,
        }
    # ONE source for the direct-entry map: the advisor's own, which derives from the dispatcher.
    # Re-listing it here would be the second inventory this function exists to prevent.
    try:
        direct = dict(capability_advisor.direct_entry())
    except Exception:  # noqa: BLE001
        direct = {}
    direct_targets = {str(v) for v in direct.values() if v}
    by_capability: dict[str, list[str]] = {}
    for task_type in task_types:
        trigger = {
            "repository": ADVISOR_REACH_PROBE_REPO,
            "task_type": task_type,
            "lane": ADVISOR_REACH_PROBE_LANE,
        }
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
        return {
            "generated_at": time.time(),
            "repos": {},
            "unreadable": "gh CLI not installed; fleet label vocabulary unknown",
        }
    for full in getattr(backlog, "SUPPORTED_REPOS", []):
        try:
            proc = subprocess.run(
                ["gh", "label", "list", "--repo", full, "--limit", "300", "--json", "name"],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            continue  # unknown for this repo, exactly like a nonzero exit
        if proc.returncode != 0:
            continue
        try:
            index[full] = sorted(r["name"].strip().lower() for r in json.loads(proc.stdout or "[]"))
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
    wanted = {lb.lower() for lb in labels_producing(task_type)}
    repos = index.get("repos") or {}
    if not wanted or not repos:
        return {"labels": sorted(wanted), "repos_with": None, "repos_total": len(repos)}
    have = [r for r, labels in repos.items() if wanted & set(labels)]
    return {
        "labels": sorted(wanted),
        "repos_with": len(have),
        "repos_total": len(repos),
        "missing_in": sorted(r.split("/")[-1] for r in repos if r not in have)[:6],
    }


# --------------------------------------------------------------------------- the audit


def audit_capability(
    cap_id: str,
    cap: dict,
    *,
    emittable: set[str],
    templates: set[str],
    label_index: dict,
    vocab_gaps: dict | None = None,
    env_gate: dict | None = None,
    reach: dict | None = None,
) -> dict:
    """One capability: entry class, machinery verdict, named defects. Never a demand guess."""
    entry = entry_class(cap)
    defects: list[str] = []
    notes: list[str] = []
    row = {
        "capability_id": cap_id,
        "status": cap.get("status"),
        "entry_class": entry,
        "matcher": cap.get("matcher"),
        "entrypoint": cap.get("entrypoint"),
    }

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
                1, (cov.get("repos_total") or 12) // 6
            ):
                defects.append("label_absent_from_fleet")
                notes.append(
                    f"a label producing {value!r} exists in only "
                    f"{cov['repos_with']}/{cov['repos_total']} repos"
                )

    elif entry == ENTRY_DIRECT:
        hb = heartbeat_reachable(cap)
        row["heartbeat"] = hb
        if hb["status"] == "off_path":
            defects.append("heartbeat_off_path")
            notes.append(hb.get("detail", ""))
        elif hb["status"] == "no_heartbeat":
            defects.append("no_heartbeat")
            notes.append("entrypoint records nothing")
        elif hb["status"] == "no_caller":
            defects.append("entrypoint_no_caller")
            notes.append("no driver calls it")
        elif hb["status"] == "no_local_entrypoint":
            # A cross-repo entrypoint is not a MISSING file — reporting it as missing implies a
            # local defect and hides the real blocker, which is a change in another repository.
            # The external/missing split comes from `entrypoint_presence` rather than a second
            # inline `"/" in detail` test, so this row and the diagnostic the failing checks print
            # cannot classify the same declaration two different ways.
            presence = entrypoint_presence(cap)
            row["entrypoint_presence"] = presence
            detail = str(hb.get("detail") or "")
            external = presence["state"] == ENTRYPOINT_EXTERNAL
            caller = external_caller(cap)
            row["external_caller"] = caller
            if caller and caller.get("exists"):
                # The cross-repo caller HAS landed, so this is no longer blocked.
                notes.append(
                    f"cross-repo entrypoint, caller present: "
                    f"{caller['repo']}/.github/workflows/{caller['workflow']}.yml"
                )
            else:
                defects.append("entrypoint_external" if external else "entrypoint_missing")
                notes.append(detail)
                if caller:
                    notes.append(
                        f"awaiting caller {caller.get('workflow')} in " f"{caller.get('repo')}"
                    )
                # WHERE the code actually is, when it is somewhere. `entrypoint_missing` alone
                # reads as "registered with no implementation"; a named sibling checkout turns the
                # same row into "another session's in-flight branch", which is the opposite action.
                # ONE note, capped: worktrees come and go on an active fleet, and a row that grows
                # a note per checkout buries the defect it is explaining.
                hits = presence.get("found_in") or []
                if hits:
                    shown = ", ".join(h["checkout"] for h in hits[:3])
                    more = f" (+{len(hits) - 3} more)" if len(hits) > 3 else ""
                    notes.append(
                        f"module absent HERE but present in {shown}{more} — "
                        f"wait-or-merge, not retire"
                    )
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
            notes.append(
                "was nameable at the advisor front door and no longer is; a tightened "
                "matcher dropped it out without any diff saying so"
            )

    suppressed = set((env_gate or {}).get("suppressed_modules") or [])
    if suppressed:
        mine = sorted({p.name for p in _entrypoint_files(cap) if p.name in suppressed})
        if mine:
            defects.append("heartbeat_env_suppressed")
            notes.append(
                f"{', '.join(mine)} is invoked above the "
                f"{HEARTBEAT_ENV_FLAG} export, so its heartbeat records nothing"
            )
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

        _caps.production_heartbeat(
            "capability-activation-audit", event_type, ref="capability_activation_audit.audit"
        )
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
    rows = [
        audit_capability(
            cid,
            cap,
            emittable=emittable,
            templates=templates,
            label_index=index,
            vocab_gaps=vgaps,
            env_gate=env_gate,
            reach=reach,
        )
        for cid, cap in sorted(caps.items())
    ]
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
    entry = {
        "generated_at": rep["generated_at"],
        "total": rep["total"],
        "reachable": rep["reachable"],
        "blocked": rep["blocked"],
        "reachable_ids": sorted(rep["reachable_ids"]),
        "by_defect": {k: len(v) for k, v in rep["by_defect"].items()},
    }
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
        "defect_delta": {
            k: now_def.get(k, 0) - prev_def.get(k, 0)
            for k in sorted(set(now_def) | set(prev_def))
            if now_def.get(k, 0) != prev_def.get(k, 0)
        },
    }


# --------------------------------------------------------------------------- rendering


def format_scorecard(rep: dict, prog: dict | None = None) -> str:
    pct = 100 * rep["reachable"] / rep["total"] if rep["total"] else 0
    lines = [
        "# Capability activation scorecard",
        "",
        f"  CAN FIRE:  {rep['reachable']:>3} of {rep['total']}  ({pct:.0f}%)",
        f"  BLOCKED:   {rep['blocked']:>3}",
        "",
        "  entry classes: "
        + ", ".join(f"{k}={v}" for k, v in sorted(rep["by_entry_class"].items())),
        "",
    ]
    if prog and prog.get("baseline"):
        age = (rep["generated_at"] - int(prog["baseline"])) / 86400
        lines += [
            f"  vs last snapshot ({age:.1f}d ago): "
            f"{prog['reachable_then']} -> {prog['reachable_now']} reachable"
        ]
        if prog["gained"]:
            lines.append(f"    GAINED:    {', '.join(prog['gained'])}")
        if prog["regressed"]:
            lines.append(f"    REGRESSED: {', '.join(prog['regressed'])}")
        if prog["defect_delta"]:
            lines.append(
                "    defect delta: "
                + ", ".join(f"{k} {v:+d}" for k, v in prog["defect_delta"].items())
            )
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
    lines += [
        "## Per capability",
        "",
        "| Capability | Entry | Can fire | Defects |",
        "|---|---|---|---|",
    ]
    for row in rep["rows"]:
        lines.append(
            f"| {row['capability_id']} | {row['entry_class']} | "
            f"{'yes' if row['reachable'] else 'NO'} | "
            f"{', '.join(row['defects']) or '—'} |"
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- selftest


def _selftest() -> None:
    import tempfile

    # ENTRY CLASS is the load-bearing distinction: asking a task_type question of a kind-matched
    # capability is what made `no_matching_work` meaningless for 26 of 34 capabilities.
    assert (
        entry_class({"matcher": {"field": "task_type", "value": ["testgen"]}}) == ENTRY_TASK_ROUTED
    )
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
    assert (
        heartbeat_reachable({"capability_id": "codemod-campaign", "entrypoint": "codemod_lane.py"})[
            "status"
        ]
        == "reachable"
    )
    # NARROWNESS CONTROL: `deliberate-break-verifier` is the same shape but is NOT in the mapping,
    # and must keep reporting a real defect -- local_verify.py genuinely has no caller.
    assert "deliberate-break-verifier" not in _mapped, _mapped
    assert (
        heartbeat_reachable(
            {"capability_id": "deliberate-break-verifier", "entrypoint": "local_verify.py"}
        )["status"]
        != "reachable"
    )
    # CROSS-REPO CREDITING, same rule and same narrowness.
    import capability_outcome_bridge as _bridge

    _ci = set(_bridge.EXTERNAL_CI_CAPABILITIES or {})
    assert "docs-drift-fix-agent" in _ci, _ci
    assert (
        heartbeat_reachable(
            {
                "capability_id": "docs-drift-fix-agent",
                "entrypoint": "Workflows/scripts/docs_drift_fix_agent.py",
            }
        )["status"]
        == "reachable"
    )
    assert (
        heartbeat_reachable(
            {"capability_id": "not-declared-anywhere", "entrypoint": "Other/repo/script.py"}
        )["status"]
        == "no_local_entrypoint"
    )

    # classify() reachability, derived from backlog's own vocabulary rather than hardcoded.
    em = emittable_task_types()
    assert "testgen" in em and "codemod" in em and "implement" in em
    assert "docs" not in em, "docs labels map to mechanical; if this changes, the docs fix landed"
    assert "review" not in em, "classify() cannot emit review; if this changes, the fix landed"
    assert backlog.classify(["docs"]) == "mechanical"  # the reason docs is unreachable
    assert "testing" in labels_producing("testgen")

    idx = {"repos": {"o/a": ["testing", "bug"], "o/b": ["bug"], "o/c": ["bug"]}}
    cov = label_coverage("testgen", idx)
    assert cov["repos_with"] == 1 and cov["repos_total"] == 3, cov

    tmpl = _prompt_templates()
    assert "testgen" in tmpl and "docs" not in tmpl

    # A task-routed capability whose task_type cannot be emitted is BLOCKED, and says why.
    row = audit_capability(
        "x",
        {"matcher": {"field": "task_type", "value": ["docs"]}, "status": "generated"},
        emittable=em,
        templates=tmpl,
        label_index=idx,
    )
    assert not row["reachable"]
    assert "task_type_not_emittable" in row["defects"] and "no_prompt_template" in row["defects"]

    # A healthy task-routed capability is reachable.
    ok = audit_capability(
        "t",
        {"matcher": {"field": "task_type", "value": ["testgen"]}, "status": "generated"},
        emittable=em,
        templates=tmpl,
        label_index={"repos": {f"o/{i}": ["testing"] for i in range(12)}},
    )
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
                "def main(argv):\n    _capability_heartbeat()\n"
            )
            (HERE / "tick.py").write_text("import lonely\nlonely.helper()\n")
            hb = heartbeat_reachable({"entrypoint": "lonely.py"})
            assert hb["status"] == "off_path", hb  # runs, but credit cannot land
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
            "def go():\n    capabilities.production_heartbeat('p','invocation')\n"
        )
        # DEFINES the helpers and calls neither — the shape of capabilities.py, which MUST be
        # allowed above the export because it is the validation gate that authorises heartbeats.
        (root / "gatekeeper.py").write_text(
            "def production_heartbeat(a, b):\n    return False\n"
            "def daily_heartbeat(a, b):\n    return False\n"
        )
        assert emits_heartbeat(root / "producer.py")
        assert not emits_heartbeat(root / "gatekeeper.py"), "a definition is not a call"

        bad = (
            'python3 "$ORCH/gatekeeper.py" --validate\n'
            'python3 "$ORCH/producer.py" --run\n'
            "export ORCH_CAPABILITY_HEARTBEATS=1\n"
            'python3 "$ORCH/producer.py" --run-again\n'
        )
        (root / "orchestrate.sh").write_text(bad)
        broken = heartbeat_env_gate(here=root)
        assert broken["suppressed_modules"] == ["producer.py"], broken
        assert broken["invocations_before"] == 2 and broken["invocations_after"] == 1, broken
        assert not broken["anchor_present"], broken

        # REVERT: move the export above the producer and the defect must clear — and the
        # denominator must stay non-zero, so a clean verdict is distinguishable from a dead parse.
        good = (
            f"# {HEARTBEAT_EXPORT_ANCHOR}\n"
            'python3 "$ORCH/gatekeeper.py" --validate\n'
            "export ORCH_CAPABILITY_HEARTBEATS=1\n"
            'python3 "$ORCH/producer.py" --run\n'
        )
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
            'python3 "$ORCH/gatekeeper.py" --x\n'
        )
        fn = heartbeat_env_gate(here=root)
        assert fn["suppressed_modules"] == [], fn
        assert fn["invocations_deferred"] == 1, fn

        # A COMMENT is not an invocation.
        (root / "orchestrate.sh").write_text(
            '# python3 "$ORCH/producer.py" --run\n'
            "export ORCH_CAPABILITY_HEARTBEATS=1\n"
            'python3 "$ORCH/producer.py" --run\n'
        )
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
            row = audit_capability(
                "p",
                {
                    "matcher": {"kind": "tick_phase", "name": "p"},
                    "entrypoint": "producer.py",
                    "status": "generated",
                },
                emittable=em,
                templates=tmpl,
                label_index=idx,
                env_gate=gate,
            )
        finally:
            HERE = saved_here
        assert "heartbeat_env_suppressed" in row["defects"], row
        assert not row["reachable"], row

    # ADVISOR REACH — the span of the front door, and whether it has narrowed. Synthetic ledger, so
    # this exercises the mechanism rather than today's ledger contents.
    synthetic = {
        "task-shaped": {
            "capability_id": "task-shaped",
            "status": "generated",
            "matcher": {"field": "task_type", "operator": "in", "value": ["testgen"]},
        },
        "kind-shaped": {
            "capability_id": "kind-shaped",
            "status": "generated",
            "matcher": {"kind": "tick_phase", "name": "x"},
        },
        "retired-task-shaped": {
            "capability_id": "retired-task-shaped",
            "status": "retired",
            "matcher": {"field": "task_type", "operator": "in", "value": ["testgen"]},
        },
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
        set(reach["reachable"]) | set(reach["direct_entry_targets"])
    ), reach
    # A derived direct-entry map CAN shrink with no diff in either module (drop an entry from
    # dispatcher.TASK_TYPE_CAPABILITY and the front door narrows in silence), which is why the
    # derived set has its own baseline. Simulate the shrink: an empty map must regress.
    import unittest.mock as _mock

    with _mock.patch.object(capability_advisor, "direct_entry", lambda: {}):
        shrunk = advisor_reach(synthetic)
    assert shrunk["direct_entry_regressed"] == sorted(ADVISOR_DIRECT_ENTRY_BASELINE), shrunk
    assert shrunk["direct_entry_targets"] == [], shrunk
    # A kind-shaped matcher failing to match is CORRECT, not a defect: it is entered directly.
    kind_row = audit_capability(
        "kind-shaped",
        synthetic["kind-shaped"],
        emittable=em,
        templates=tmpl,
        label_index=idx,
        reach=reach,
    )
    assert kind_row["advisor_reachable"] is False, kind_row
    assert "advisor_reach_regression" not in kind_row["defects"], kind_row
    # ...but a capability IN THE BASELINE that is no longer reachable is a silent narrowing, and
    # must surface. Simulate it by naming a baseline member the synthetic ledger cannot reach.
    victim = sorted(ADVISOR_REACH_BASELINE)[0]
    regressed = advisor_reach(
        {
            victim: {
                "capability_id": victim,
                "status": "generated",
                "matcher": {"kind": "ci_workflow", "name": "y"},
            }
        }
    )
    assert regressed["regressed"], regressed
    reg_row = audit_capability(
        victim,
        {
            "capability_id": victim,
            "status": "generated",
            "matcher": {"kind": "ci_workflow", "name": "y"},
            "entrypoint": "nothing.py",
        },
        emittable=em,
        templates=tmpl,
        label_index=idx,
        reach=regressed,
    )
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
            cap = {
                "matcher": {"kind": "ci_workflow", "name": "maint-99-thing"},
                "entrypoint": "SiblingRepo/scripts/thing.py",
            }
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
                assert (
                    anywhere and anywhere["exists"] is True
                ), f"a real fleet caller must resolve from ANY cwd, not just the canonical tree: {anywhere}"
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
                "def unrelated():\n    return 1\n"
            )
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
        {"repos": {"o/a": ["risk:low", "risk:minor"]}}
    ), "deliberate exclusion reported as a gap"
    # And the FIXED label must no longer be reported as a gap — proof the fix landed.
    assert "risk:major" not in vocabulary_gaps({"repos": {"o/a": ["risk:major"]}}).get(
        "adversarial-review", []
    ), "risk:major should now be accepted by HIGH_STAKES_LABELS"
    # A label in a namespace the code never uses is NOT a mismatch (no false positives).
    assert not vocabulary_gaps(
        {"repos": {"o/a": ["colour:blue", "bug"]}}
    ), "unrelated namespace flagged as a mismatch"
    # And an accepted label produces no gap.
    assert "adversarial-review" not in vocabulary_gaps({"repos": {"o/a": ["risk:critical"]}})

    # Both audit bugs found on first live run, pinned so they cannot return.
    # (a) `module.function` entrypoints must resolve, or a running capability reads as missing.
    assert [p.name for p in _entrypoint_files({"entrypoint": "dispatcher.offload"})] == [
        "dispatcher.py"
    ], "module.function entrypoint did not resolve"
    assert [p.name for p in _entrypoint_files({"entrypoint": "roles.py:run_triage_agent"})] == [
        "roles.py"
    ]
    assert _entrypoint_files({"entrypoint": "Workflows/scripts/nope.py"}) == []
    # The split-out parse must not fabricate a module out of punctuation: the `->` in
    # `exp_abcd.py:followup -> synthesis_promotion.py:reconcile` is a separator, and reporting it
    # as a MISSING module would be a defect invented by the diagnostic itself.
    assert [
        t
        for t, _c in _declared_modules(
            {"entrypoint": "exp_abcd.py:followup -> synthesis_promotion.py:reconcile"}
        )
    ] == ["exp_abcd.py:followup", "synthesis_promotion.py:reconcile"]

    # ---- MODULE ABSENT FROM THIS TREE vs. PRESENT-BUT-INCOMPLETE (2026-08-22) ----------------
    # The two have OPPOSITE fixes — wait-or-merge versus fix-the-declaration — and until this
    # existed they produced byte-identical output. So the test is the flip itself: create the
    # module and the verdict must become `present` with an EMPTY note; delete it and the verdict
    # must return to `absent_here` with a note that names where the code actually is. A verdict
    # that cannot be made to change is not measuring anything.
    with tempfile.TemporaryDirectory(prefix="cap-absent-") as td3:
        droot = Path(td3)
        wt = droot / ".claude" / "worktrees"
        here_tree, other = wt / "mine", wt / "theirs"
        for tree in (droot, here_tree, other):
            tree.mkdir(parents=True, exist_ok=True)
            (tree / "capabilities.py").write_text("# a checkout is recognised by this file\n")
        (other / "brand_new.py").write_text("def run():\n    pass\n")
        ledger_path = droot / "capabilities.json"
        rows = {
            "absent-row": {
                **capabilities._blank_capability("absent-row"),
                "entrypoint": "brand_new.py:run",
            },
            "present-row": {
                **capabilities._blank_capability("present-row"),
                "entrypoint": "capabilities.py",
            },
            "external-row": {
                **capabilities._blank_capability("external-row"),
                "entrypoint": "Workflows/scripts/elsewhere.py",
            },
            "undeclared-row": {
                **capabilities._blank_capability("undeclared-row"),
                "entrypoint": None,
            },
        }
        capabilities.save(rows, ledger_path)
        saved_here = HERE
        try:
            globals()["HERE"] = here_tree

            # Each state is distinct, and the two that mean "not here" are NOT merged: a sibling
            # repo's path is a deliberate cross-repo declaration, not a worktree gap.
            assert entrypoint_presence(rows["absent-row"])["state"] == ENTRYPOINT_ABSENT
            assert entrypoint_presence(rows["present-row"])["state"] == ENTRYPOINT_PRESENT
            assert entrypoint_presence(rows["external-row"])["state"] == ENTRYPOINT_EXTERNAL
            assert entrypoint_presence(rows["undeclared-row"])["state"] == ENTRYPOINT_UNDECLARED

            # The sibling checkout is FOUND and NAMED — a bare "absent" would still read as
            # "retire it", which is the wrong action and the whole reason this exists.
            v = entrypoint_presence(rows["absent-row"])
            assert [h["checkout"] for h in v["found_in"]] == [".claude/worktrees/theirs"], v
            assert v["found_in"][0]["modules"] == ["brand_new.py"], v
            # And this tree is never offered as the place the code might be.
            assert ".claude/worktrees/mine" not in v["searched"], v

            # The report scopes to the rows it was ASKED about, and carries the denominator.
            rep = absent_entrypoint_report(sorted(rows), path=ledger_path)
            assert [r["capability_id"] for r in rep["absent"]] == ["absent-row"], rep
            assert rep["checked"] == 4 and rep["total"] == 4, rep
            assert absent_entrypoint_report(["present-row"], path=ledger_path)["absent"] == []

            # The note must say WHICH situation this is, WHERE the code is, and what NOT to do.
            note = absent_entrypoint_note(sorted(rows), path=ledger_path)
            for phrase in (
                "ABSENT FROM THIS TREE",
                "absent-row",
                ".claude/worktrees/theirs",
                "WAIT-OR-MERGE",
                "WAIVERS",
            ):
                assert phrase in note, (phrase, note)
            # It must NOT accuse a row whose module is right here.
            assert "present-row" not in note and "external-row" not in note, note
            # Silence when there is nothing to say, so an ordinary declaration failure reads
            # exactly as it did before.
            assert absent_entrypoint_note(["present-row"], path=ledger_path) == ""

            # ---- DELIBERATE BREAK -> REVERT ---------------------------------------------------
            # BREAK: land the module in this tree. The verdict must flip to `present` and the
            # note must fall silent, because the fix is now the declaration, not a merge.
            (here_tree / "brand_new.py").write_text("def run():\n    pass\n")
            assert (
                entrypoint_presence(rows["absent-row"])["state"] == ENTRYPOINT_PRESENT
            ), "a module that IS in this tree must not be reported absent"
            assert absent_entrypoint_note(sorted(rows), path=ledger_path) == "", (
                "the note must go quiet once the code is here — otherwise it sends a reader to "
                "wait for a merge that already happened"
            )
            # REVERT: take it away again and the original verdict must come back exactly.
            (here_tree / "brand_new.py").unlink()
            assert entrypoint_presence(rows["absent-row"])["state"] == ENTRYPOINT_ABSENT
            assert (
                absent_entrypoint_note(sorted(rows), path=ledger_path) == note
            ), "the diagnostic must be a function of the tree, not of call order"

            # DIAGNOSTIC ONLY. It reports; it must not be able to shrink a failing set or turn a
            # red into a skip. Proven on the real shape: the note is APPENDED to a message about
            # a non-empty `missing` list, and that list is untouched by the append.
            missing = sorted(rows)
            message = f"{len(missing)} capability(ies) have NO recurrence fixture: {missing}"
            assert sorted(rows) == missing, "the note's inputs must not be mutated"
            assert not isinstance(absent_entrypoint_note(missing, path=ledger_path), bool)
            assert message in (message + absent_entrypoint_note(missing, path=ledger_path))

            # An UNREADABLE ledger must degrade to a visible note, never to an exception that
            # replaces the real assertion with a complaint about the diagnostic.
            broken = absent_entrypoint_note(sorted(rows), path=droot / "not-a-ledger-dir")
            assert broken == "" or "diagnostic unavailable" in broken, broken
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
                "def main(argv):\n    _capability_heartbeat()\n"
            )
            (Path(td2) / "tick.py").write_text("import lonely2\nlonely2.helper()\n")
            (Path(td2) / "orchestrate.sh").write_text("# See lonely2.py for details\necho hi\n")
            hb = heartbeat_reachable({"entrypoint": "lonely2.py"})
            assert hb["status"] == "off_path", f"comment counted as a caller: {hb}"
            # A real invocation on a non-comment line IS a caller.
            (Path(td2) / "orchestrate.sh").write_text(
                '# See lonely2.py for details\npython3 "$ORCH/lonely2.py" --run\n'
            )
            assert heartbeat_reachable({"entrypoint": "lonely2.py"})["status"] == "reachable"
        finally:
            globals()["HERE"] = saved

    # PROGRESS: snapshots must show movement, and a regression must be visible as such.
    with tempfile.TemporaryDirectory(prefix="cap-hist-") as td:
        hp = Path(td) / "h.json"
        r1 = {
            "generated_at": 1000,
            "total": 3,
            "reachable": 1,
            "blocked": 2,
            "reachable_ids": ["a"],
            "by_defect": {"no_heartbeat": ["b", "c"]},
        }
        assert record_snapshot(r1, path=hp)["recorded"]
        r2 = {
            "generated_at": 2000,
            "total": 3,
            "reachable": 2,
            "blocked": 1,
            "reachable_ids": ["a", "b"],
            "by_defect": {"no_heartbeat": ["c"]},
        }
        p = progress(r2, path=hp)
        assert p["gained"] == ["b"] and p["regressed"] == [], p
        assert p["defect_delta"] == {"no_heartbeat": -1}, p
        # A regression is reported, not smoothed over.
        record_snapshot(r2, path=hp)
        r3 = dict(
            r2,
            generated_at=3000,
            reachable=1,
            reachable_ids=["a"],
            by_defect={"no_heartbeat": ["b", "c"]},
        )
        p3 = progress(r3, path=hp)
        assert p3["regressed"] == ["b"], p3
        assert p3["defect_delta"] == {"no_heartbeat": 1}, p3
        assert progress(r2, path=Path(td) / "absent.json")["baseline"] is None

    print(
        "capability_activation_audit.py selftest: OK (entry classes, emittable task types, "
        "heartbeat off-path vs no-heartbeat vs reachable, heartbeat env-suppression both "
        "directions, advisor reach + narrowing, progress + regression tracking, "
        "entrypoint absent-here vs present vs external-repo vs undeclared with the "
        "create/delete flip and a diagnostic that cannot suppress a failure)"
    )


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
