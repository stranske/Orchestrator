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
    "no_heartbeat": "the entrypoint records nothing at all",
    "entrypoint_no_caller": "no driver module calls the entrypoint",
    "entrypoint_missing": "the declared entrypoint file does not exist",
    "entrypoint_external": "the entrypoint lives in another repository; activation needs a "
                           "change there, not here",
    "trigger_shape_mismatch": "kind-matched, so a task_type trigger can never reach it",
    "vocabulary_mismatch": "the fleet uses a label in this namespace that the code does not accept",
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


def _entrypoint_files(cap: dict) -> list[Path]:
    """Local .py files named by the entrypoint declaration.

    Entrypoints come in three shapes and all three must resolve, or the audit invents defects:
    `watch.py`, `roles.py:run_triage_agent`, and `dispatcher.offload` (module.function, no `.py`).
    The third form produced a false `entrypoint_missing` for `offload` — a capability running
    196x/week — which is precisely the kind of fabricated finding this module exists to prevent.
    """
    raw = str(cap.get("entrypoint") or "")
    out: list[Path] = []
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
        for name in candidates:
            path = HERE / name
            if path.exists() and path not in out:
                out.append(path)
                break
    return out


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
    for full in getattr(backlog, "SUPPORTED_REPOS", []):
        proc = subprocess.run(["gh", "label", "list", "--repo", full, "--limit", "300",
                               "--json", "name"], capture_output=True, text=True, timeout=120)
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
                     label_index: dict, vocab_gaps: dict | None = None) -> dict:
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
            detail = str(hb.get("detail") or "")
            external = "/" in detail and not detail.startswith(("./", "/"))
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
    rows = [audit_capability(cid, cap, emittable=emittable, templates=templates,
                             label_index=index, vocab_gaps=vgaps)
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
          "heartbeat off-path vs no-heartbeat vs reachable, progress + regression tracking)")


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
