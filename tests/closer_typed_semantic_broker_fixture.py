"""Emit NEW synthetic semantic-v2 data before an inert response is sealed.

Inputs are declared test-case gold, never a captured provider answer. Production
parsers/validators are not patched and no metadata is inserted into model JSON.
"""

from __future__ import annotations

import copy
import json

import closer_semantic_assessment as semantic


def delivered_inventory(prompt):
    start = "CURRENT_HOST_INVENTORY_JSON_BEGIN\n"
    end = "\nCURRENT_HOST_INVENTORY_JSON_END"
    if prompt.count(start) != 1 or prompt.count(end) != 1:
        raise ValueError("one actual delivered inventory required")
    return json.loads(prompt.split(start, 1)[1].split(end, 1)[0])


def emit_assessment(
    prompt,
    fixture_rows,
    *,
    unknown_slots=(),
    global_explanations=(),
    explanation=None,
    finding_gold=(),
):
    """Join explicit source/criterion gold to actual slots and visible refs.

    No inference from prose, unknown default, missing excerpt/relevance repair,
    filename guessing, hidden-raw citation or legacy-answer parsing is performed.
    """
    if type(fixture_rows) is not list or any(type(row) is not dict for row in fixture_rows):
        raise ValueError("declared synthetic source rows required, never provider output")
    view = delivered_inventory(prompt)
    remaining = copy.deepcopy(fixture_rows)
    rows = []
    for original in view["obligations"]:
        matches = [
            i
            for i, row in enumerate(remaining)
            if row.get("source") == original["source"] and row.get("criterion") == original["text"]
        ]
        if not matches:
            if original["slot"] not in view["required_obligation_slots"]:
                continue
            raise ValueError("synthetic gold does not cover original occurrence")
        gold = remaining.pop(matches[0])
        statuses = {"PASS": "satisfied", "FAIL": "unsatisfied", "UNKNOWN": "unknown"}
        if gold.get("result") not in statuses:
            raise ValueError("explicit synthetic outcome required")
        status = "unknown" if original["slot"] in unknown_slots else statuses[gold["result"]]
        citations = []
        for proof in gold.get("proofs", []):
            kind = proof.get("kind")
            context_key = "filename" if kind == "codepatch" else "receipt_path"
            supplied = [
                entry
                for entry in view["catalog"]
                if entry["purpose"] == "ordinary"
                and entry["kind"] == kind
                and entry["context"].get(context_key) == proof.get(context_key)
            ]
            if len(supplied) != 1:
                raise ValueError("declared proof must select one actual visible catalog member")
            citations.append(
                {
                    "ref": supplied[0]["ref"],
                    "excerpt": proof.get("excerpt"),
                    "relevance": proof.get("relevance"),
                }
            )
        row = {
            "slot": original["slot"],
            "meaning": "understood",
            "status": status,
            "citations": citations,
            "explanation": explanation
            or "The synthetic case gold explicitly declares this original source obligation and its supplied proof judgment.",
        }
        if status == "unknown":
            row["gap"] = "evidence_inconclusive"
        rows.append(row)
    if remaining:
        raise ValueError("foreign or surplus synthetic gold occurrence")
    import closer_disposition

    declared = copy.deepcopy(list(finding_gold))
    findings = []
    outcomes = {
        "unresolved_missing_evidence": "unknown",
        "resolved_duplicate": "duplicate",
        "resolved_nonissue": "nonissue",
    }
    for original in view["findings"]:
        identity = closer_disposition._finding_id(original["raw"])
        matches = [i for i, gold in enumerate(declared) if gold.get("finding_id") == identity]
        if not matches:
            raise ValueError("explicit synthetic finding semantics required")
        gold = declared.pop(matches[0])
        if gold.get("status") not in outcomes:
            raise ValueError("unsupported synthetic finding outcome")
        if gold.get("status") != "unresolved_missing_evidence":
            raise ValueError("resolved finding fixture must explicitly supply typed citations")
        findings.append(
            {
                "slot": original["slot"],
                "meaning": "understood",
                "status": "unknown",
                "gap": "evidence_absent",
                "citations": [],
                "explanation": gold["reason"],
            }
        )
    if declared:
        raise ValueError("foreign or surplus synthetic finding occurrence")
    return {
        "schema": semantic.SCHEMA,
        "obligations": rows,
        "findings": findings,
        "global_concerns": [
            {"kind": "ambiguity", "explanation": text, "citations": []}
            for text in global_explanations
        ],
    }


def renderer_inventory(source_rows):
    """Declared synthetic renderer input only: no proof catalog or outcomes."""
    import closer_disposition

    expected = closer_disposition._expected({"sources": source_rows})
    if expected is None:
        raise ValueError("explicit original renderer inventory required")
    return {
        "mode": "ordinary_acceptance",
        "host_residual_holds": [],
        "required_obligation_slots": list(range(1, len(expected) + 1)),
        "required_finding_slots": [],
        "obligations": [
            {"slot": n, "source": source, "text": text}
            for n, (source, text) in enumerate(expected, 1)
        ],
        "findings": [],
        "catalog": [],
    }
