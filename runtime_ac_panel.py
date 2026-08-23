#!/usr/bin/env python3
"""Multi-judge adjudication for runtime acceptance-criteria gates.

This is the first increment of "multi-judge AC verification": build strict
review prompts from a runtime AC spec + gate evidence, optionally dispatch
reviewer offloads, parse reviewer JSON, and produce a conservative panel
verdict. Evidence-backed fail vetoes force manual adjudication or failure when
corroborated; bare fail labels do not automatically defeat a strong passing
panel. The orchestrator still decides when reviewer capacity is worth spending.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

import feedback
import runtime_ac

VALID_PANEL_VERDICTS = {"PASS", "FAIL", "NEEDS_REVIEW"}
VETO_SEVERITIES = {"high", "critical", "fatal"}
DEFAULT_REVIEWERS = ["vibe", "gemini", "cursor"]
JSON_SCAN_LIMIT = 100_000


def _read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _criterion_index(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {criterion["id"]: criterion for criterion in spec.get("acceptance_criteria") or []}


def _gate_criteria(gate: dict[str, Any]) -> list[dict[str, Any]]:
    criteria = gate.get("criteria")
    return criteria if isinstance(criteria, list) else []


def _active_evidence_contract() -> str:
    types = feedback.active_evidence_types()
    if not types:
        return (
            "Active evidence types: none currently registered. Return "
            '"cited_evidence_types": [].'
        )
    lines = [
        "Active evidence types you may cite if they materially affect your judgment:",
    ]
    for row in types:
        rationale = f" - {row['rationale']}" if row.get("rationale") else ""
        lines.append(f"- {row['name']}{rationale}")
    lines.append(
        'Return exact names in "cited_evidence_types" only when the evidence type '
        "was actually used in your verdict; otherwise return []."
    )
    return "\n".join(lines)


def _shadow_contract_prompt(plan: dict[str, Any] | None) -> str:
    if plan is None:
        return "No candidate-only shadow evidence contract was supplied."
    import capability_compiler

    return capability_compiler.evaluator_prompt_fragment(plan)


def build_review_prompt(
    spec: dict[str, Any],
    gate: dict[str, Any],
    *,
    reviewer: str = "reviewer",
    evidence_contract_plan: dict[str, Any] | None = None,
) -> str:
    errors = runtime_ac.validate_spec(spec)
    if errors:
        raise ValueError("invalid runtime AC spec: " + "; ".join(errors))
    compact_spec = {
        "verification": spec["verification"],
        "acceptance_criteria": [
            {
                "id": criterion["id"],
                "statement": criterion["statement"],
                "evidence_required": criterion["evidence_required"],
                "check_ids": [check["id"] for check in criterion["checks"]],
            }
            for criterion in spec["acceptance_criteria"]
        ],
        "verdict_policy": spec["verdict_policy"],
    }
    compact_gate = {
        "verification_id": gate.get("verification_id"),
        "target": gate.get("target"),
        "verdict": gate.get("verdict"),
        "pass_ratio": gate.get("pass_ratio"),
        "required_check_ids": gate.get("required_check_ids") or [],
        "criteria": _gate_criteria(gate),
        "blocking": gate.get("blocking") or [],
        "needs_review": gate.get("needs_review") or [],
    }
    return (
        "You are a runtime acceptance-criteria judge. Decide whether the supplied evidence actually "
        "satisfies the acceptance criteria. Do not rubber-stamp the automated gate; cite concrete missing "
        "or contradictory evidence. A lone concern should be specific enough for the orchestrator to verify "
        "against ground truth.\n\n"
        f"Reviewer: {reviewer}\n\n"
        "Runtime AC spec:\n"
        f"{json.dumps(compact_spec, indent=2)}\n\n"
        "Runtime AC gate evidence:\n"
        f"{json.dumps(compact_gate, indent=2)}\n\n"
        + _active_evidence_contract()
        + "\n\n"
        + _shadow_contract_prompt(evidence_contract_plan)
        + "\n\n"
        "Return STRICT JSON only with this shape:\n"
        "{\n"
        f'  "reviewer": "{reviewer}",\n'
        '  "verdict": "PASS|FAIL|NEEDS_REVIEW",\n'
        '  "confidence": 0.0,\n'
        '  "rationale": "short reason",\n'
        '  "ac_assessments": [\n'
        '    {"ac_id": "AC1", "status": "PASS|FAIL|NEEDS_REVIEW", "reason": "evidence-based reason"}\n'
        "  ],\n"
        '  "blockers": [\n'
        '    {"ac_id": "AC1", "severity": "medium|high|critical|fatal", "finding": "specific blocker", "evidence_ref": "check id or output"}\n'
        "  ],\n"
        '  "evidence_gaps": ["missing evidence that would improve judgment"],\n'
        '  "cited_evidence_types": ["exact active evidence type name"],\n'
        '  "cited_evidence_contracts": ["exact supplied shadow contract plan ID"]\n'
        "}"
    )


def _clamp_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _normalize_status(value: Any) -> str:
    status = str(value or "NEEDS_REVIEW").strip().upper()
    return status if status in VALID_PANEL_VERDICTS else "NEEDS_REVIEW"


def _list_or_empty(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _normalize_review(review: dict[str, Any], idx: int, known_ac_ids: set[str]) -> dict[str, Any]:
    if not isinstance(review, dict):
        raise ValueError(f"review[{idx}] must be an object")
    reviewer = str(review.get("reviewer") or f"reviewer-{idx + 1}").strip()
    verdict = _normalize_status(review.get("verdict"))
    assessments: list[dict[str, Any]] = []
    for raw in _list_or_empty(review.get("ac_assessments")):
        if not isinstance(raw, dict):
            continue
        ac_id = str(raw.get("ac_id") or "").strip()
        if ac_id and ac_id in known_ac_ids:
            assessments.append(
                {
                    "ac_id": ac_id,
                    "status": _normalize_status(raw.get("status")),
                    "reason": str(raw.get("reason") or "").strip(),
                }
            )
    blockers: list[dict[str, Any]] = []
    for raw in _list_or_empty(review.get("blockers")):
        if not isinstance(raw, dict):
            continue
        finding = str(raw.get("finding") or "").strip()
        if not finding:
            continue
        blockers.append(
            {
                "ac_id": str(raw.get("ac_id") or "").strip() or None,
                "severity": str(raw.get("severity") or "medium").strip().lower(),
                "finding": finding,
                "evidence_ref": str(raw.get("evidence_ref") or "").strip() or None,
            }
        )
    gaps = [
        str(gap).strip() for gap in _list_or_empty(review.get("evidence_gaps")) if str(gap).strip()
    ]
    return {
        "reviewer": reviewer,
        "verdict": verdict,
        "confidence": _clamp_confidence(review.get("confidence")),
        "rationale": str(review.get("rationale") or "").strip(),
        "ac_assessments": assessments,
        "blockers": blockers,
        "evidence_gaps": gaps,
        "cited_evidence_types": feedback.normalize_evidence_type_citations(
            review.get("cited_evidence_types")
        ),
        "cited_evidence_contracts": list(
            dict.fromkeys(
                str(value).strip()
                for value in _list_or_empty(review.get("cited_evidence_contracts"))
                if str(value).strip()
            )
        ),
    }


def _parse_jsonish(text: str) -> Any:
    """Extract one JSON object/array from agent output that may include prose or code fences."""
    raw = (text or "").strip()
    candidates = [raw] if raw else []
    if "```" in raw:
        parts = raw.split("```")
        for idx in range(1, len(parts), 2):
            block = parts[idx].strip()
            if block.lower().startswith("json"):
                block = block[4:].lstrip("\n\r ")
            if block:
                candidates.append(block)
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    scan = raw[:JSON_SCAN_LIMIT]
    for idx, char in enumerate(scan):
        if char not in "{[":
            continue
        try:
            obj, _end = decoder.raw_decode(scan[idx:])
            return obj
        except json.JSONDecodeError:
            continue
    suffix = f" in first {JSON_SCAN_LIMIT} chars" if len(raw) > JSON_SCAN_LIMIT else ""
    raise ValueError(f"no parseable JSON object or array found in reviewer output{suffix}")


def _single_review_from_doc(doc: Any, reviewer: str) -> dict[str, Any]:
    if isinstance(doc, dict) and isinstance(doc.get("reviews"), list):
        candidates = [item for item in doc["reviews"] if isinstance(item, dict)]
        exact = [item for item in candidates if str(item.get("reviewer") or "").strip() == reviewer]
        if exact:
            review = dict(exact[0])
        elif candidates:
            review = dict(candidates[0])
        else:
            raise ValueError("review JSON contained no review objects")
    elif isinstance(doc, list):
        candidates = [item for item in doc if isinstance(item, dict)]
        if not candidates:
            raise ValueError("review JSON array contained no review objects")
        review = dict(candidates[0])
    elif isinstance(doc, dict):
        review = dict(doc)
    else:
        raise ValueError("review JSON must be an object, list, or object with reviews")
    review.setdefault("reviewer", reviewer)
    return review


def parse_review_output(output: str, reviewer: str) -> dict[str, Any]:
    return _single_review_from_doc(_parse_jsonish(output), reviewer)


def _synthetic_review(reviewer: str, reason: str) -> dict[str, Any]:
    clean_reason = str(reason or "reviewer output unavailable").strip()
    return {
        "reviewer": reviewer,
        "verdict": "NEEDS_REVIEW",
        "confidence": 0.0,
        "rationale": clean_reason,
        "ac_assessments": [],
        "blockers": [],
        "evidence_gaps": [clean_reason],
    }


def _parse_reviewers(value: str | None) -> list[str]:
    raw = value or ",".join(DEFAULT_REVIEWERS)
    reviewers = [part.strip() for part in raw.split(",") if part.strip()]
    if not reviewers:
        raise ValueError("at least one reviewer is required")
    return reviewers


def dispatch_reviewers(
    spec: dict[str, Any],
    gate: dict[str, Any],
    reviewers: list[str],
    *,
    cwd: str | Path = ".",
    mode: str | None = None,
    timeout: int = 1800,
    isolate: bool = False,
    offload_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run live reviewer offloads and return raw dispatch metadata plus a review_doc."""
    errors = runtime_ac.validate_spec(spec)
    if errors:
        raise ValueError("invalid runtime AC spec: " + "; ".join(errors))
    if offload_fn is None:
        import dispatcher

        offload_fn = dispatcher.offload
    review_doc: dict[str, list[dict[str, Any]]] = {"reviews": []}
    dispatches: list[dict[str, Any]] = []
    for reviewer in reviewers:
        prompt = build_review_prompt(spec, gate, reviewer=reviewer)
        try:
            result = offload_fn(
                reviewer,
                prompt,
                cwd=str(cwd),
                mode=mode,
                timeout=timeout,
                isolate=isolate,
            )
        except Exception as exc:
            result = {
                "agent": reviewer,
                "exit": 2,
                "output": "",
                "error": f"reviewer offload failed: {exc}",
            }
        meta = {
            "reviewer": reviewer,
            "agent": (result.get("agent", reviewer) if isinstance(result, dict) else reviewer),
            "exit": result.get("exit") if isinstance(result, dict) else None,
            "parsed": False,
            "cwd": result.get("cwd") if isinstance(result, dict) else None,
            "isolated_cwd": (result.get("isolated_cwd") if isinstance(result, dict) else None),
            "stderr_tail": (result.get("stderr_tail") if isinstance(result, dict) else None),
        }
        if not isinstance(result, dict):
            review = _synthetic_review(reviewer, "reviewer offload returned a non-dict result")
            meta["error"] = review["rationale"]
        elif result.get("exit") != 0:
            reason = (
                result.get("error")
                or result.get("stderr_tail")
                or f"reviewer exited {result.get('exit')}"
            )
            review = _synthetic_review(reviewer, reason)
            meta["error"] = reason
        else:
            try:
                review = parse_review_output(result.get("output") or "", reviewer)
                meta["parsed"] = True
            except ValueError as exc:
                review = _synthetic_review(reviewer, str(exc))
                meta["error"] = str(exc)
        review_doc["reviews"].append(review)
        dispatches.append(meta)
    return {
        "reviewers": reviewers,
        "dispatches": dispatches,
        "review_doc": review_doc,
    }


def coerce_reviews(doc: Any) -> list[dict[str, Any]]:
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        reviews = doc.get("reviews")
        if isinstance(reviews, list):
            return reviews
    raise ValueError("review JSON must be a list or an object with reviews")


def _substantiated_veto(review: dict[str, Any]) -> bool:
    if review["verdict"] != "FAIL":
        return False
    for blocker in review["blockers"]:
        if blocker["severity"] in VETO_SEVERITIES:
            return True
    return False


def adjudicate_panel(
    spec: dict[str, Any],
    gate: dict[str, Any],
    review_doc: Any,
    *,
    min_reviews: int = 2,
    fail_vetoes: int = 2,
    pass_threshold: float = 0.67,
) -> dict[str, Any]:
    errors = runtime_ac.validate_spec(spec)
    if errors:
        raise ValueError("invalid runtime AC spec: " + "; ".join(errors))
    known_ac_ids = set(_criterion_index(spec))
    reviews = [
        _normalize_review(review, idx, known_ac_ids)
        for idx, review in enumerate(coerce_reviews(review_doc))
    ]
    counts = {
        verdict: sum(1 for review in reviews if review["verdict"] == verdict)
        for verdict in sorted(VALID_PANEL_VERDICTS)
    }
    vetoes = [review for review in reviews if _substantiated_veto(review)]
    evidence_gaps = [
        {"reviewer": review["reviewer"], "gap": gap}
        for review in reviews
        for gap in review["evidence_gaps"]
    ]
    evidence_type_citations = [
        {"reviewer": review["reviewer"], "name": name}
        for review in reviews
        for name in review.get("cited_evidence_types") or []
    ]
    blockers = [
        {"reviewer": review["reviewer"], **blocker}
        for review in reviews
        for blocker in review["blockers"]
    ]

    gate_verdict = str(gate.get("verdict") or "NEEDS_REVIEW").upper()
    verdict = "NEEDS_REVIEW"
    reason = "panel did not reach a pass threshold"
    if gate_verdict == "FAIL":
        verdict = "FAIL"
        reason = "automated runtime AC gate failed"
    elif len(reviews) < min_reviews:
        reason = f"only {len(reviews)} review(s), below required {min_reviews}"
    elif len(vetoes) >= fail_vetoes:
        verdict = "FAIL"
        reason = f"{len(vetoes)} substantiated fail vetoes met threshold {fail_vetoes}"
    elif len(vetoes) > 0:
        reason = "substantiated fail veto below threshold; orchestrator should adjudicate against evidence"
    elif gate_verdict == "NEEDS_REVIEW":
        reason = "automated runtime AC gate still needs review"
    elif counts["PASS"] / max(len(reviews), 1) >= pass_threshold:
        verdict = "PASS"
        reason = f"pass ratio {counts['PASS']}/{len(reviews)} met threshold {pass_threshold:.2f}"
    elif counts["FAIL"] > 0:
        reason = "fail review present, but pass threshold was not met"

    return {
        "verification_id": gate.get("verification_id") or spec["verification"]["id"],
        "target": gate.get("target") or spec["verification"].get("target"),
        "verdict": verdict,
        "verifier_verdict": runtime_ac.VERDICT_BY_GATE[verdict],
        "reason": reason,
        "gate_verdict": gate_verdict,
        "min_reviews": min_reviews,
        "fail_vetoes": fail_vetoes,
        "pass_threshold": pass_threshold,
        "counts": counts,
        "n_reviews": len(reviews),
        "n_vetoes": len(vetoes),
        "blockers": blockers,
        "evidence_gaps": evidence_gaps,
        "evidence_type_citations": evidence_type_citations,
        "reviews": reviews,
    }


def record_panel_verdict(run_id: str, panel: dict[str, Any]) -> dict[str, Any]:
    feedback.record_outcome(
        run_id,
        verifier_verdict=panel["verifier_verdict"],
        notes=f"runtime_ac_panel: {panel['verifier_verdict']} - {panel['reason']}",
    )
    for gap in panel.get("evidence_gaps") or []:
        feedback.record_evidence_gap(
            run_id,
            f"runtime_ac_panel:{gap.get('reviewer') or 'reviewer'}",
            gap.get("gap") or "",
        )
    cited = feedback.record_evidence_type_citations(
        [row.get("name") for row in panel.get("evidence_type_citations") or []]
    )
    return {
        "run_id": run_id,
        "verifier_verdict": panel["verifier_verdict"],
        "recorded": True,
        "evidence_gaps": len(panel.get("evidence_gaps") or []),
        "evidence_type_citations": len(cited),
    }


def _all_check_results(spec: dict[str, Any], status: str = "PASS") -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for criterion in spec["acceptance_criteria"]:
        for check in criterion["checks"]:
            results.append({"id": check["id"], "status": status})
    return results


def _selftest() -> None:
    import tempfile

    spec = runtime_ac._valid_spec()
    gate_pass = runtime_ac.evaluate_results(spec, {"check_results": _all_check_results(spec)})
    prompt = build_review_prompt(spec, gate_pass, reviewer="gemini")
    assert "STRICT JSON" in prompt and '"reviewer": "gemini"' in prompt and "AC1" in prompt, prompt

    reviews_pass = {
        "reviews": [
            {
                "reviewer": "vibe",
                "verdict": "PASS",
                "confidence": 0.8,
                "ac_assessments": [
                    {"ac_id": "AC1", "status": "PASS", "reason": "evidence present"}
                ],
            },
            {
                "reviewer": "gemini",
                "verdict": "PASS",
                "confidence": 0.9,
                "ac_assessments": [
                    {"ac_id": "AC2", "status": "PASS", "reason": "evidence present"}
                ],
            },
        ]
    }
    panel_pass = adjudicate_panel(spec, gate_pass, reviews_pass)
    assert (
        panel_pass["verdict"] == "PASS" and panel_pass["verifier_verdict"] == "PASS_RUNTIME_AC"
    ), panel_pass

    one_veto = {
        "reviews": [
            {"reviewer": "vibe", "verdict": "PASS", "confidence": 0.8},
            {
                "reviewer": "gemini",
                "verdict": "FAIL",
                "confidence": 0.7,
                "blockers": [
                    {
                        "ac_id": "AC1",
                        "severity": "high",
                        "finding": "runtime output missing",
                    }
                ],
            },
            {"reviewer": "cursor", "verdict": "PASS", "confidence": 0.8},
        ]
    }
    panel_one_veto = adjudicate_panel(spec, gate_pass, one_veto)
    assert (
        panel_one_veto["verdict"] == "NEEDS_REVIEW" and panel_one_veto["n_vetoes"] == 1
    ), panel_one_veto

    unsubstantiated_fail = {
        "reviews": [
            {"reviewer": "vibe", "verdict": "PASS", "confidence": 0.8},
            {
                "reviewer": "gemini",
                "verdict": "FAIL",
                "confidence": 0.7,
                "ac_assessments": {"AC1": "FAIL"},
                "blockers": "runtime output missing",
                "evidence_gaps": {"gap": "need browser trace"},
            },
            {"reviewer": "cursor", "verdict": "PASS", "confidence": 0.8},
            {"reviewer": "codex", "verdict": "PASS", "confidence": 0.8},
        ]
    }
    panel_unsubstantiated = adjudicate_panel(spec, gate_pass, unsubstantiated_fail)
    assert (
        panel_unsubstantiated["verdict"] == "PASS" and panel_unsubstantiated["n_vetoes"] == 0
    ), panel_unsubstantiated
    assert not panel_unsubstantiated["reviews"][1]["blockers"], panel_unsubstantiated

    fenced = parse_review_output(
        "Review result:\n```json\n"
        '{"reviewer":"vibe","verdict":"PASS","confidence":0.7,"rationale":"ok"}'
        "\n```",
        "vibe",
    )
    assert fenced["reviewer"] == "vibe" and fenced["verdict"] == "PASS", fenced

    two_vetoes = {
        "reviews": [
            {
                "reviewer": "vibe",
                "verdict": "FAIL",
                "confidence": 0.8,
                "blockers": [
                    {
                        "ac_id": "AC1",
                        "severity": "high",
                        "finding": "runtime output missing",
                    }
                ],
                "evidence_gaps": ["need browser trace"],
                "cited_evidence_types": ["runtime_output_evidence"],
            },
            {
                "reviewer": "gemini",
                "verdict": "FAIL",
                "confidence": 0.9,
                "blockers": [
                    {
                        "ac_id": "AC2",
                        "severity": "critical",
                        "finding": "dashboard value absent",
                    }
                ],
            },
            {"reviewer": "cursor", "verdict": "PASS", "confidence": 0.6},
        ]
    }
    panel_fail = adjudicate_panel(spec, gate_pass, two_vetoes)
    assert panel_fail["verdict"] == "FAIL" and panel_fail["n_vetoes"] == 2, panel_fail
    assert panel_fail["evidence_gaps"][0]["gap"] == "need browser trace", panel_fail
    assert panel_fail["evidence_type_citations"][0]["name"] == "runtime_output_evidence", panel_fail

    insufficient = adjudicate_panel(
        spec, gate_pass, {"reviews": [{"reviewer": "solo", "verdict": "PASS"}]}
    )
    assert (
        insufficient["verdict"] == "NEEDS_REVIEW" and "below required" in insufficient["reason"]
    ), insufficient
    gate_fail = runtime_ac.evaluate_results(
        spec, {"check_results": _all_check_results(spec, "FAIL")}
    )
    panel_gate_fail = adjudicate_panel(spec, gate_fail, reviews_pass)
    assert (
        panel_gate_fail["verdict"] == "FAIL" and "automated" in panel_gate_fail["reason"]
    ), panel_gate_fail

    def fake_offload(agent: str, prompt: str, **_kwargs: Any) -> dict[str, Any]:
        assert "STRICT JSON" in prompt and f'"reviewer": "{agent}"' in prompt, prompt
        if agent == "vibe":
            return {
                "agent": agent,
                "exit": 0,
                "output": "```json\n"
                '{"reviewer":"vibe","verdict":"PASS","confidence":0.8,'
                '"rationale":"gate evidence satisfies AC"}\n```',
            }
        if agent == "gemini":
            return {
                "agent": agent,
                "exit": 0,
                "output": '{"reviews":[{"reviewer":"gemini","verdict":"PASS",'
                '"confidence":0.9,"rationale":"evidence is sufficient"}]}',
            }
        return {"agent": agent, "exit": 1, "error": "simulated reviewer failure"}

    dispatched = dispatch_reviewers(
        spec,
        gate_pass,
        ["vibe", "gemini", "cursor"],
        offload_fn=fake_offload,
    )
    assert dispatched["dispatches"][0]["parsed"] is True, dispatched
    assert dispatched["dispatches"][2]["parsed"] is False, dispatched
    assert dispatched["review_doc"]["reviews"][2]["verdict"] == "NEEDS_REVIEW", dispatched
    panel_dispatched = adjudicate_panel(
        spec, gate_pass, dispatched["review_doc"], pass_threshold=0.66
    )
    assert panel_dispatched["verdict"] == "PASS", panel_dispatched

    def fake_bad_offload(agent: str, _prompt: str, **_kwargs: Any) -> dict[str, Any]:
        if agent == "cursor":
            return {
                "agent": agent,
                "exit": 0,
                "output": "I agree, but I forgot to return JSON.",
            }
        raise RuntimeError("simulated adapter failure")

    bad_dispatched = dispatch_reviewers(
        spec,
        gate_pass,
        ["cursor", "aider"],
        offload_fn=fake_bad_offload,
    )
    assert bad_dispatched["dispatches"][0]["parsed"] is False, bad_dispatched
    assert (
        "no parseable JSON" in bad_dispatched["review_doc"]["reviews"][0]["rationale"]
    ), bad_dispatched
    assert (
        "simulated adapter failure" in bad_dispatched["review_doc"]["reviews"][1]["rationale"]
    ), bad_dispatched

    with tempfile.TemporaryDirectory(prefix="runtime-ac-panel-") as tmp:
        feedback.DB_PATH = Path(tmp) / "panel.db"
        feedback.record_run("run-panel", "o/r#1", "implement", "codex", mode="remote")
        feedback.record_evidence_type("runtime_output_evidence", "fixture evidence type")
        rec = record_panel_verdict("run-panel", panel_fail)
        assert rec["recorded"] is True and rec["evidence_gaps"] == 1, rec
        assert rec["evidence_type_citations"] == 1, rec
        with feedback._conn() as c:
            vv = c.execute(
                "SELECT verifier_verdict FROM outcomes WHERE run_id='run-panel'"
            ).fetchone()[0]
            gaps = c.execute("SELECT COUNT(*) FROM evidence_gaps").fetchone()[0]
            influence = c.execute(
                "SELECT influence FROM evidence_types WHERE name='runtime_output_evidence'"
            ).fetchone()[0]
        assert vv == "FAIL_RUNTIME_AC" and gaps == 1 and influence == 1, (
            vv,
            gaps,
            influence,
        )

    print(
        "runtime_ac_panel.py selftest: OK (prompt, review parsing, panel adjudication, feedback recording)"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Build and adjudicate multi-judge runtime AC reviews."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prompt", help="runtime AC spec JSON file to turn into a judge prompt")
    group.add_argument(
        "--adjudicate",
        help="runtime AC spec JSON file to adjudicate with --gate and --reviews",
    )
    group.add_argument(
        "--dispatch", help="runtime AC spec JSON file to send through reviewer offloads"
    )
    group.add_argument("--selftest", action="store_true")
    parser.add_argument("--gate", help="runtime AC gate JSON file")
    parser.add_argument("--reviews", help="review JSON file for --adjudicate")
    parser.add_argument("--reviewer", default="reviewer", help="reviewer name to embed in --prompt")
    parser.add_argument(
        "--reviewers",
        default=",".join(DEFAULT_REVIEWERS),
        help="comma-separated reviewer agents for --dispatch (default: vibe,gemini,cursor)",
    )
    parser.add_argument("--cwd", default=".", help="working directory for reviewer offloads")
    parser.add_argument(
        "--mode", help="optional reviewer offload mode to pass to dispatcher.offload"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="per-reviewer offload timeout in seconds",
    )
    parser.add_argument(
        "--isolate",
        "--worktree-isolation",
        action="store_true",
        dest="isolate",
        help="copy cwd to a persistent local offload workspace before each reviewer run",
    )
    parser.add_argument("--min-reviews", type=int, default=2)
    parser.add_argument("--fail-vetoes", type=int, default=2)
    parser.add_argument("--pass-threshold", type=float, default=0.67)
    parser.add_argument(
        "--record-run-id", help="optional feedback.run_id to patch with panel verdict"
    )
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0
    if args.prompt:
        if not args.gate:
            parser.error("--prompt requires --gate")
        spec = runtime_ac.parse_spec_json(Path(args.prompt).read_text(encoding="utf-8"))
        gate = _read_json(args.gate)
        print(build_review_prompt(spec, gate, reviewer=args.reviewer))
        return 0
    if args.dispatch:
        if not args.gate:
            parser.error("--dispatch requires --gate")
        spec = runtime_ac.parse_spec_json(Path(args.dispatch).read_text(encoding="utf-8"))
        gate = _read_json(args.gate)
        dispatched = dispatch_reviewers(
            spec,
            gate,
            _parse_reviewers(args.reviewers),
            cwd=args.cwd,
            mode=args.mode,
            timeout=args.timeout,
            isolate=args.isolate,
        )
        panel = adjudicate_panel(
            spec,
            gate,
            dispatched["review_doc"],
            min_reviews=args.min_reviews,
            fail_vetoes=args.fail_vetoes,
            pass_threshold=args.pass_threshold,
        )
        if args.record_run_id:
            panel["feedback"] = record_panel_verdict(args.record_run_id, panel)
        print(json.dumps({**dispatched, "panel": panel}, indent=2))
        return 0 if panel["verdict"] == "PASS" else 1
    if args.adjudicate:
        if not args.gate or not args.reviews:
            parser.error("--adjudicate requires --gate and --reviews")
        spec = runtime_ac.parse_spec_json(Path(args.adjudicate).read_text(encoding="utf-8"))
        gate = _read_json(args.gate)
        reviews = _read_json(args.reviews)
        panel = adjudicate_panel(
            spec,
            gate,
            reviews,
            min_reviews=args.min_reviews,
            fail_vetoes=args.fail_vetoes,
            pass_threshold=args.pass_threshold,
        )
        if args.record_run_id:
            panel["feedback"] = record_panel_verdict(args.record_run_id, panel)
        print(json.dumps(panel, indent=2))
        return 0 if panel["verdict"] == "PASS" else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
