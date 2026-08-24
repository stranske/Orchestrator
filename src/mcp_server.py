#!/usr/bin/env python3
"""mcp_server.py — expose the Orchestrator to any MCP client (item 16l, 2026-07-08).

Line-delimited JSON-RPC over stdio (the MCP stdio transport), zero dependencies. Registered with
Claude Code via `claude mcp add --scope user orchestrator -- python3 <this file>`, which makes the
fleet steerable from ANY session on this machine: check capacity, read the fleet summary and route
weights, list/answer owner questions, look up resume hints.

Deliberately SAFE surface: read-only tools plus exactly three bounded actions —
answer_owner_question (feeds the 16h decision loop), record_owner_question, and capability_decline
(append-only evidence that an offered capability was rejected, and why). No dispatch, no
claims, no config mutation through this door; steering that mutates the fleet stays with the CLIs.
`--selftest` drives the server as a subprocess through a real initialize/tools/list/tools/call
round-trip."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import cast

ORCH = Path(__file__).resolve().parent
sys.path.insert(0, str(ORCH))

import feedback  # noqa: E402 — resolvable only after the sys.path.insert above

HANDOFF = Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff"))
STATE_DIR = Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator"))
PROTOCOL_VERSION = "2025-06-18"


def _decline_kinds() -> list[str]:
    """The decline vocabulary, read from its owner so the MCP enum cannot drift from it.

    Imported here rather than at module scope for the same reason `capability_advice` imports the
    advisor lazily: a capability-registry problem must never take down the capacity/fleet reads. A
    hardcoded list would be a second inventory of the thing this function exists to mirror.
    """
    try:
        import capability_propensity

        return sorted(capability_propensity.DECLINE_KINDS)
    except Exception:  # noqa: BLE001
        return []


TOOLS = [
    {
        "name": "capacity_status",
        "description": "Current per-agent capacity/policy snapshot (this tick's capacity.json).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fleet_summary",
        "description": "Orchestrator health: last tick, backlog/experiment stamps, DB volumes.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "route_weights",
        "description": "Learned routing order for a task type (posterior/score/n_obs per agent).",
        "inputSchema": {
            "type": "object",
            "properties": {"task_type": {"type": "string"}},
            "required": ["task_type"],
        },
    },
    {
        "name": "capability_advice",
        "description": (
            "Is the Orchestrator useful for this task, and which capabilities apply? Ask at task "
            "initiation and again when the work changes shape. Returns useful=false freely — that "
            "is a normal answer, not a failure. Advice is NOT dispatch: each capability reports "
            "dispatch_ready, and today none is, so treat results as candidates to consider."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "the task in plain words"},
                "repository": {"type": "string"},
                "skill": {
                    "type": "string",
                    "description": "skill that surfaced this work, if any; recorded so the "
                    "skill->capability association is learned over time",
                },
                "surface": {
                    "type": "string",
                    "description": "the skill or automation asking, optionally with a phase "
                    "(e.g. 'repo-audit:phase-3'). Selects that surface's "
                    "DECLARED capability binding — a small named set that "
                    "answers even when the task wording does not classify. "
                    "A long multi-phase process should pass its phase, "
                    "because the capabilities that apply differ per phase.",
                },
                "repo_path": {
                    "type": "string",
                    "description": "an absolute path to a checkout of `repository`, if "
                    "you have one. Lets a capability's declared repo-fact "
                    "precondition actually be EVALUATED — e.g. whether "
                    "this repository has an observable surface at all, "
                    "which is what frontend-verifier requires. Without it "
                    "the answer says the precondition was NOT EVALUATED "
                    "and names this field as the missing input; it never "
                    "guesses, and a failed precondition never withholds "
                    "or reorders the offer.",
                },
                "previous": {
                    "type": "object",
                    "description": "the prior capability_advice result; when supplied, the "
                    "response adds reask{} saying whether the work has "
                    "changed enough to be worth re-consulting",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "capability_decline",
        "description": (
            "Record that a capability capability_advice OFFERED was deliberately NOT used, and why. "
            "Pass the experiment_id from the advice you are responding to. A decline is NOT a "
            "negative verdict: it never touches the usefulness posterior, because the capability "
            "did not run. It makes 'offered and rejected on stated grounds' distinguishable from "
            "'never considered' — which were byte-identical in the ledger before this existed — and "
            "repeated declines at one surface propose demoting that binding. A reason is REQUIRED."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "capability_id": {
                    "type": "string",
                    "description": "the offered capability you are turning down",
                },
                "experiment_id": {
                    "type": "string",
                    "description": "the `experiment_id` from the capability_advice "
                    "response (an 'advice:<digest>' string)",
                },
                "reason": {
                    "type": "string",
                    "description": "why this capability was the wrong tool for THIS work. "
                    "Refused when blank — an unexplained decline is "
                    "indistinguishable from inattention",
                },
                "surface": {
                    "type": "string",
                    "description": "the surface declining, matching the one you passed to "
                    "capability_advice (e.g. 'repo-audit:phase-2'). Optional, "
                    "but a decline without it cannot feed demotion",
                },
                "kind": {
                    "type": "string",
                    "enum": sorted(_decline_kinds()),
                    "description": "WHICH KIND of decline, because the kinds imply opposite "
                    "fixes. wrong_match = it does not fit this work (the "
                    "binding is wrong). scope_too_small = a correct match "
                    "declared too broadly. no_landing_zone = a CORRECT match "
                    "the deliverable shape made impossible (e.g. a test "
                    "generator in a read-only audit) — this never counts "
                    "against the capability. gated_off = held behind a "
                    "deliberate switch or shadow status. deferred = wanted, "
                    "not affordable. Omitted means 'unspecified', which is "
                    "recorded and can never demote a binding.",
                },
            },
            "required": ["capability_id", "experiment_id", "reason"],
        },
    },
    {
        "name": "capability_associations",
        "description": (
            "Learned skill->capability and task_type->capability associations, accumulated from "
            "real advisory matches. Empty until observations exist — nothing here is hand-declared."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "owner_questions",
        "description": "Open owner questions (FYI — work already proceeds on stated defaults).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "answer_owner_question",
        "description": "Answer an open owner question; the answer steers future dispatches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question_id": {"type": "string"},
                "answer": {"type": "string"},
            },
            "required": ["question_id", "answer"],
        },
    },
    {
        "name": "record_owner_question",
        "description": "Record a product-level question with the default being proceeded on.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "default": {"type": "string"},
                "repo": {"type": "string"},
                "expires_days": {"type": "number"},
            },
            "required": ["question", "default"],
        },
    },
    {
        "name": "resume_hint",
        "description": "Stored CLI resume identifier + paste-ready resume command for a run_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
]


def _read_json_file(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _fleet_summary() -> dict:
    heartbeat = _read_json_file(HANDOFF / "orchestrator.json") or {}
    hb_ts = int(heartbeat.get("generated_at") or 0)
    with feedback._conn() as c:
        volumes = {
            table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "runs",
                "outcomes",
                "costs",
                "evaluations",
                "human_calibration",
                "owner_questions",
                "resume_tokens",
            )
        }
    stamps = {}
    for stamp in (
        "last-relearn",
        "last-periodic-report",
        "last-range-rollout",
        "last-ship-gate",
        "last-ledger-reconcile",
    ):
        p = STATE_DIR / f".{stamp}"
        stamps[stamp] = int(p.stat().st_mtime) if p.exists() else None
    return {
        "heartbeat_age_s": (int(time.time()) - hb_ts) if hb_ts else None,
        "db_volumes": volumes,
        "cadence_stamps": stamps,
    }


def _call_tool(name: str, args: dict):
    if name == "capacity_status":
        return _read_json_file(HANDOFF / "capacity.json") or {"error": "no capacity snapshot"}
    if name == "fleet_summary":
        return _fleet_summary()
    if name == "route_weights":
        return {
            "task_type": args["task_type"],
            "weights": feedback.current_weights(str(args["task_type"])),
        }
    if name == "capability_advice":
        # Imported lazily so a capability-registry problem can never take down capacity/fleet reads.
        import capability_advisor

        result = capability_advisor.advise(
            str(args["task"]),
            repository=str(args.get("repository") or ""),
            skill=str(args.get("skill") or ""),
            # Without this the declared binding is unreachable from the MCP tool, which is the only
            # way the skills call the advisor -- the callers would name a surface nothing read.
            surface=str(args.get("surface") or ""),
            # Same rule for the precondition input: a declared condition the caller cannot supply an
            # answer for is a condition nothing evaluates, which is the defect being fixed.
            repo_path=str(args.get("repo_path") or ""),
        )
        previous = args.get("previous")
        if isinstance(previous, dict):
            # Answers "was this worth re-asking?" so a caller can stay quiet when nothing changed.
            result["reask"] = capability_advisor.should_reask(
                previous,
                {
                    "task": str(args["task"]),
                    "repository": str(args.get("repository") or ""),
                    "skill": str(args.get("skill") or ""),
                    "surface": str(args.get("surface") or ""),
                    "capabilities_ready": result.get("dispatch_ready_count") or 0,
                },
            )
        return result
    if name == "capability_decline":
        import capability_propensity

        recorded = capability_propensity.record_decline(
            str(args["capability_id"]),
            str(args["experiment_id"]),
            reason=str(args.get("reason") or ""),
            surface=str(args.get("surface") or ""),
            kind=str(args.get("kind") or capability_propensity.DECLINE_KIND_DEFAULT),
        )
        kind = str(args.get("kind") or capability_propensity.DECLINE_KIND_DEFAULT)
        return {
            "recorded": bool(recorded),
            "capability_id": str(args["capability_id"]),
            "experiment_id": str(args["experiment_id"]),
            "surface": str(args.get("surface") or "") or None,
            # SAY WHAT IT DID NOT DO, in the response. A caller that believes it just scored the
            # capability has been misinformed by a successful call.
            "affects_propensity": False,
            "attributable_to_surface": bool(args.get("surface")),
            "kind": kind,
            "can_demote_the_binding": capability_propensity.decline_kind_demotable(kind),
            "kind_implies_fix": capability_propensity.DECLINE_KINDS[kind]["fix"],
            "note": (
                "recorded as a reasoned rejection; the usefulness posterior is untouched "
                "because the capability never ran"
            ),
            "already_recorded": not recorded,
        }
    if name == "capability_associations":
        import capability_advisor

        return capability_advisor.learned_associations()
    if name == "owner_questions":
        feedback.expire_owner_questions()
        return {"open": feedback.open_owner_questions()}
    if name == "answer_owner_question":
        ok = feedback.answer_owner_question(str(args["question_id"]), str(args["answer"]))
        return {"answered": ok}
    if name == "record_owner_question":
        return feedback.record_owner_question(
            str(args["question"]),
            str(args["default"]),
            repo=args.get("repo"),
            expires_days=float(args.get("expires_days") or 7),
        )
    if name == "resume_hint":
        hint = feedback.resume_hint(str(args["run_id"]))
        return hint or {"error": f"no resume token for {args['run_id']}"}
    raise ValueError(f"unknown tool: {name}")


def _handle(msg: dict):
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        client_version = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": client_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "orchestrator", "version": "1.0.0"},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            result = _call_tool(params.get("name") or "", params.get("arguments") or {})
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(result, indent=2, default=str)}
                    ],
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": f"error: {exc}"}],
                    "isError": True,
                },
            }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if msg_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return None


def serve() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = _handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response, default=str) + "\n")
            sys.stdout.flush()
    return 0


def _selftest_advice_schema_matches_advise() -> None:
    """Every keyword `advise()` accepts that a CALLER would set must be reachable through this tool.

    Written after shipping the exact opposite: seven skills were edited to pass `surface`, and the
    tool had no such parameter, so all seven edits were inert. A caller naming a field the callee
    drops is silent -- the request succeeds and the field vanishes. The MCP tool is the ONLY way the
    skills reach the advisor, so a gap here makes every skill-side binding unreachable.
    """
    import inspect

    import capability_advisor

    tool = next(t for t in TOOLS if t["name"] == "capability_advice")
    schema = cast(dict, tool["inputSchema"])
    advertised = set(schema["properties"])
    sig = inspect.signature(capability_advisor.advise)
    # Caller-settable = keyword-only, minus the internals a remote caller must never drive.
    internal = {"record", "path", "lane", "context"}
    callable_kw = {
        n for n, prm in sig.parameters.items() if prm.kind is prm.KEYWORD_ONLY and n not in internal
    }
    missing = sorted(callable_kw - advertised)
    assert not missing, (
        f"capability_advice does not advertise {missing}, so a caller setting them is silently "
        f"ignored. Add them to the inputSchema AND pass them through in _call_tool."
    )

    # ADVERTISED IS NOT FORWARDED, read off the AST of the handler's own `advise(...)` call. The
    # spot-check below covers `surface`; this covers EVERY caller-settable field at once, so the next
    # parameter cannot be advertised and dropped. Containment rather than equality: the tool also
    # advertises `previous`, which drives `should_reask` and is deliberately not an `advise()` field.
    forwarded = _forwarded_args(_call_tool, "advise")
    unforwarded = sorted((callable_kw | {"task"}) - forwarded)
    assert not unforwarded, (
        f"capability_advice advertises {unforwarded} and never passes them to advise(); the request "
        f"succeeds and the field vanishes, which is exactly the failure this selftest exists for."
    )

    # ...and advertised is not enough: it must actually be FORWARDED. Assert the FORWARDING, never
    # which capabilities come back -- `advise()` only returns bound capabilities that exist in the
    # ledger, and the ledger is machine-local (43 rows on the owner's machine, 14 on a clean runner).
    # The first version of this asserted `adversarial-review` was returned, passed locally and went
    # red on CI: a machine-local assertion inside the guard written to stop a different recurrence.
    got = _call_tool(
        "capability_advice", {"task": "xyzzy plugh frobnicate", "surface": "repo-audit:phase-3"}
    )
    assert got.get("surface") == "repo-audit:phase-3", got.get("surface")
    assert "bound_capabilities" in got, "the tool must report which capabilities were bound"
    # A phase declared NO_BINDING must come back empty through the tool. This one is ledger-
    # independent in the safe direction: suppression yields {} regardless of what is registered.
    empty = _call_tool(
        "capability_advice", {"task": "xyzzy plugh frobnicate", "surface": "repo-audit:phase-1"}
    )
    assert (empty.get("bound_capabilities") or []) == [], empty
    assert empty.get("surface") == "repo-audit:phase-1", empty.get("surface")
    print(
        "mcp_server advice-schema selftest: OK (every caller-settable advise() field is advertised "
        "and forwarded)"
    )


def _forwarded_args(handler, callee: str) -> set[str]:
    """Which `args[...]` keys actually reach `callee(...)` inside `handler`. From the AST.

    Positional arguments are read through their `args["key"]` subscript and keyword arguments
    through their names, so this answers "what did the caller's field become" rather than "does the
    word appear somewhere nearby" — the difference between a guard and decoration.
    """
    import ast
    import inspect as _inspect

    tree = ast.parse(_inspect.getsource(handler))
    keys: set[str] = set()

    def _subscript_key(node) -> str | None:
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Subscript)
                and isinstance(sub.value, ast.Name)
                and sub.value.id == "args"
                and isinstance(sub.slice, ast.Constant)
            ):
                return str(sub.slice.value)
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != callee:
            continue
        for positional in node.args:
            key = _subscript_key(positional)
            if key:
                keys.add(key)
        for kw in node.keywords:
            if kw.arg:
                keys.add(kw.arg)
    return keys


def _selftest_decline_schema_matches_record_decline() -> None:
    """The same guard as above, for the decline verb — because the same mistake is available again.

    `record_decline` has a REQUIRED keyword (`reason`) and an attribution keyword (`surface`) that
    silently degrades the evidence when dropped: the decline is still written, still readable, and
    can no longer feed `propose_demotions`. That is the worst possible failure shape — a successful
    call that quietly produces weaker evidence — so both the advertisement and the FORWARDING are
    asserted.

    Every probe below is a REFUSAL path, so this selftest writes nothing to any ledger. Asserting
    forwarding through a successful write would mean writing a fake decline into the live ledger of
    whatever machine runs the suite, which is the mislabeled-trial mistake this project already made
    once with `record_usefulness`.
    """
    import inspect

    import capability_propensity

    tool = next(t for t in TOOLS if t["name"] == "capability_decline")
    schema = cast(dict, tool["inputSchema"])
    advertised = set(schema["properties"])
    sig = inspect.signature(capability_propensity.record_decline)
    internal = {"path", "metadata"}
    callable_kw = {
        n for n, prm in sig.parameters.items() if prm.kind is prm.KEYWORD_ONLY and n not in internal
    }
    missing = sorted(callable_kw - advertised)
    assert not missing, (
        f"capability_decline does not advertise {missing}, so a caller setting them is silently "
        f"ignored. Add them to the inputSchema AND pass them through in _call_tool."
    )
    # The positional arguments must be reachable too, under the names the tool advertises.
    assert {"capability_id", "experiment_id"} <= advertised, sorted(advertised)
    required_schema = cast(dict, tool["inputSchema"])
    assert set(required_schema["required"]) == {
        "capability_id",
        "experiment_id",
        "reason",
    }, tool

    # ADVERTISED IS NOT FORWARDED. `surface` is the dangerous one: dropping it still returns a
    # SUCCESSFUL call that writes a weaker decline, so no refusal probe can see the loss. Assert it
    # against the CALL ITSELF — code vs code, no ledger, so it runs on any machine and catches
    # exactly the original defect (callers passing a field the callee silently discarded).
    #
    # Read off the AST, not the source text. A substring search over the handler body was written
    # first and DID NOT DISCRIMINATE: deleting `surface=` from the call left the word in the
    # response dict, so the break stayed green. A guard that cannot fail is decoration.
    forwarded = _forwarded_args(_call_tool, "record_decline")
    assert forwarded == advertised, (
        f"capability_decline advertises {sorted(advertised)} and forwards {sorted(forwarded)} to "
        f"record_decline; the difference is silently dropped and the call still succeeds"
    )

    def _fails(args) -> str:
        try:
            _call_tool("capability_decline", args)
        except Exception as exc:  # noqa: BLE001
            return str(exc)
        raise AssertionError(f"capability_decline accepted {args!r}")

    # `reason` is forwarded: blank reaches record_decline's own refusal, which names the reason.
    assert "requires a reason" in _fails(
        {"capability_id": "x", "experiment_id": "advice:abc", "reason": "   "}
    )
    # `experiment_id` is forwarded: a non-advisory id reaches the prefix check.
    assert "advice:" in _fails(
        {"capability_id": "x", "experiment_id": "not-an-advice-ref", "reason": "why"}
    )
    # `capability_id` is forwarded: an id no ledger can hold reaches the ledger's own guard. This
    # deliberately does NOT assert which capabilities exist -- the ledger is machine-local (43 rows
    # on the owner's machine, 14 on a clean runner) and asserting its contents inside a guard about
    # schema agreement is the exact cross-machine mistake the sibling selftest documents.
    assert "unknown capability" in _fails(
        {
            "capability_id": "__mcp-selftest-absent-capability__",
            "experiment_id": "advice:0000000000ff",
            "reason": "probe, writes nothing",
        }
    )
    print(
        "mcp_server decline-schema selftest: OK (every caller-settable record_decline field is "
        "advertised AND read by the handler; every probe is a refusal, so nothing was written)"
    )


def _selftest() -> None:
    import subprocess
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="mcp-selftest-"))
    env = dict(
        os.environ,
        ORCH_FEEDBACK_DB=str(tmp / "t.db"),
        HANDOFF_DIR=str(tmp),
        ORCH_STATE_DIR=str(tmp),
    )
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "record_owner_question",
                "arguments": {"question": "Ship it?", "default": "yes"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "owner_questions", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        },
    ]
    payload = "".join(json.dumps(r) + "\n" for r in requests)
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    by_id = {m.get("id"): m for m in lines}
    assert by_id[1]["result"]["serverInfo"]["name"] == "orchestrator", by_id[1]
    names = {t["name"] for t in by_id[2]["result"]["tools"]}
    assert {
        "capacity_status",
        "fleet_summary",
        "owner_questions",
        "answer_owner_question",
        "resume_hint",
        "route_weights",
    } <= names, names
    assert '"status": "open"' in by_id[3]["result"]["content"][0]["text"], by_id[3]
    assert "Ship it?" in by_id[4]["result"]["content"][0]["text"], by_id[4]
    assert by_id[5]["result"].get("isError") is True, by_id[5]
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    print(
        "mcp_server.py selftest: OK (initialize, tools/list, tools/call round-trip, "
        "question record/list through the MCP door, unknown-tool isError)"
    )


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        _selftest_advice_schema_matches_advise()
        _selftest_decline_schema_matches_record_decline()
        raise SystemExit(0)
    raise SystemExit(serve())
