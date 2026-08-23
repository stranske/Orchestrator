#!/usr/bin/env python3
"""mcp_server.py — expose the Orchestrator to any MCP client (item 16l, 2026-07-08).

Line-delimited JSON-RPC over stdio (the MCP stdio transport), zero dependencies. Registered with
Claude Code via `claude mcp add --scope user orchestrator -- python3 <this file>`, which makes the
fleet steerable from ANY session on this machine: check capacity, read the fleet summary and route
weights, list/answer owner questions, look up resume hints.

Deliberately SAFE surface: read-only tools plus exactly two bounded actions —
answer_owner_question (feeds the 16h decision loop) and record_owner_question. No dispatch, no
claims, no config mutation through this door; steering that mutates the fleet stays with the CLIs.
`--selftest` drives the server as a subprocess through a real initialize/tools/list/tools/call
round-trip."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ORCH = Path(__file__).resolve().parent
sys.path.insert(0, str(ORCH))

import feedback  # noqa: E402

HANDOFF = Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff"))
STATE_DIR = Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator"))
PROTOCOL_VERSION = "2025-06-18"

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
                "skill": {"type": "string",
                          "description": "skill that surfaced this work, if any; recorded so the "
                                         "skill->capability association is learned over time"},
                "surface": {"type": "string",
                            "description": "the skill or automation asking, optionally with a phase "
                                           "(e.g. 'repo-audit:phase-3'). Selects that surface's "
                                           "DECLARED capability binding — a small named set that "
                                           "answers even when the task wording does not classify. "
                                           "A long multi-phase process should pass its phase, "
                                           "because the capabilities that apply differ per phase."},
                "previous": {"type": "object",
                             "description": "the prior capability_advice result; when supplied, the "
                                            "response adds reask{} saying whether the work has "
                                            "changed enough to be worth re-consulting"},
            },
            "required": ["task"],
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
            for table in ("runs", "outcomes", "costs", "evaluations",
                          "human_calibration", "owner_questions", "resume_tokens")
        }
    stamps = {}
    for stamp in ("last-relearn", "last-periodic-report", "last-range-rollout",
                  "last-ship-gate", "last-ledger-reconcile"):
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
        return {"task_type": args["task_type"],
                "weights": feedback.current_weights(str(args["task_type"]))}
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
        )
        previous = args.get("previous")
        if isinstance(previous, dict):
            # Answers "was this worth re-asking?" so a caller can stay quiet when nothing changed.
            result["reask"] = capability_advisor.should_reask(previous, {
                "task": str(args["task"]),
                "repository": str(args.get("repository") or ""),
                "skill": str(args.get("skill") or ""),
                "surface": str(args.get("surface") or ""),
                "capabilities_ready": result.get("dispatch_ready_count") or 0,
            })
        return result
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
            str(args["question"]), str(args["default"]),
            repo=args.get("repo"), expires_days=float(args.get("expires_days") or 7),
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
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": client_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "orchestrator", "version": "1.0.0"},
        }}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            result = _call_tool(params.get("name") or "", params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}],
            }}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": f"error: {exc}"}], "isError": True,
            }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if msg_id is not None:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
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
    advertised = set(tool["inputSchema"]["properties"])
    sig = inspect.signature(capability_advisor.advise)
    # Caller-settable = keyword-only, minus the internals a remote caller must never drive.
    internal = {"record", "path", "lane", "context"}
    callable_kw = {n for n, prm in sig.parameters.items()
                   if prm.kind is prm.KEYWORD_ONLY and n not in internal}
    missing = sorted(callable_kw - advertised)
    assert not missing, (
        f"capability_advice does not advertise {missing}, so a caller setting them is silently "
        f"ignored. Add them to the inputSchema AND pass them through in _call_tool.")

    # ...and advertised is not enough: it must actually be FORWARDED. Assert the FORWARDING, never
    # which capabilities come back -- `advise()` only returns bound capabilities that exist in the
    # ledger, and the ledger is machine-local (43 rows on the owner's machine, 14 on a clean runner).
    # The first version of this asserted `adversarial-review` was returned, passed locally and went
    # red on CI: a machine-local assertion inside the guard written to stop a different recurrence.
    got = _call_tool("capability_advice", {"task": "xyzzy plugh frobnicate",
                                           "surface": "repo-audit:phase-3"})
    assert got.get("surface") == "repo-audit:phase-3", got.get("surface")
    assert "bound_capabilities" in got, "the tool must report which capabilities were bound"
    # A phase declared NO_BINDING must come back empty through the tool. This one is ledger-
    # independent in the safe direction: suppression yields {} regardless of what is registered.
    empty = _call_tool("capability_advice", {"task": "xyzzy plugh frobnicate",
                                            "surface": "repo-audit:phase-1"})
    assert (empty.get("bound_capabilities") or []) == [], empty
    assert empty.get("surface") == "repo-audit:phase-1", empty.get("surface")
    print("mcp_server advice-schema selftest: OK (every caller-settable advise() field is advertised "
          "and forwarded)")


def _selftest() -> None:
    import subprocess
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="mcp-selftest-"))
    env = dict(os.environ, ORCH_FEEDBACK_DB=str(tmp / "t.db"),
               HANDOFF_DIR=str(tmp), ORCH_STATE_DIR=str(tmp))
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "record_owner_question",
                    "arguments": {"question": "Ship it?", "default": "yes"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "owner_questions", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "no_such_tool", "arguments": {}}},
    ]
    payload = "".join(json.dumps(r) + "\n" for r in requests)
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve())],
                          input=payload, capture_output=True, text=True,
                          env=env, timeout=60)
    lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    by_id = {m.get("id"): m for m in lines}
    assert by_id[1]["result"]["serverInfo"]["name"] == "orchestrator", by_id[1]
    names = {t["name"] for t in by_id[2]["result"]["tools"]}
    assert {"capacity_status", "fleet_summary", "owner_questions",
            "answer_owner_question", "resume_hint", "route_weights"} <= names, names
    assert '"status": "open"' in by_id[3]["result"]["content"][0]["text"], by_id[3]
    assert "Ship it?" in by_id[4]["result"]["content"][0]["text"], by_id[4]
    assert by_id[5]["result"].get("isError") is True, by_id[5]
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("mcp_server.py selftest: OK (initialize, tools/list, tools/call round-trip, "
          "question record/list through the MCP door, unknown-tool isError)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        _selftest_advice_schema_matches_advise()
        raise SystemExit(0)
    raise SystemExit(serve())
