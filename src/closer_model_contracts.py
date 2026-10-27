"""Versioned production Closer prompt and strict JSON syntax contracts.

These functions render supplied data and parse response syntax only: no files,
network, tools, model selection or acceptance authority. Qualification must pin
this module's actual source digest in its independently verified runtime manifest.
Retained-evidence semantic validation remains in closer_disposition and the
existing closer_execution_contracts entrypoints; it is not replaced here.
"""

from __future__ import annotations

import hashlib
import json
import math
import re

CONTRACT_SCHEMA = "closer-model-contracts/v1"
CLASSIFICATION_RENDERING_VERSION = "closer-classification-rendering/v1"
CLASSIFICATION_PROMPT_VERSION = "closer-semantic-assessment-prompt/v2"
MAX_CLASSIFICATION_PROMPT_BYTES = 64 * 1024
STRICT_RESPONSE_VERSION = "closer-strict-response-json/v1"
OPTIONAL_SAMPLE_CHARACTERS = 9000

NONPASS_ACCOUNTING_CONTRACT = (
    "The following accounting path applies ONLY when nonpass_finding records are supplied. "
    "A native PASS or an absent NON_PASS section does not require finding IDs: use the ordinary "
    "original-criterion assessment instead, and do not require empty decision.criteria on that path. "
    "Known NON_PASS accounting is distinct from acceptance. This path requires at least one supplied nonpass_finding. Only when ALL supplied nonpass_finding "
    "exact finding_id values are confidently accounted for and each bounded next_guarded_action is "
    "clear (wait, collect_evidence, post_merge_verify for a bounded evaluable question with retained new material, or repair with a validated bound mechanical plan), return "
    "classification relevant and substantive_needs [] if there is no OTHER semantic, ownership, "
    "security, or routing ambiguity. Put missing acceptance proof in each unresolved_missing_evidence "
    "resolution's reason, preserve every raw NON_PASS verdict, and leave decision.criteria empty. "
    "For post_merge_verify, supply verification_request {question, material:[exact selector]} and retained evidence/proofs on that resolution. "
    "A selector is {kind:'comment',url:<exact GitHub issuecomment URL>} or {kind:'artifact_member',run_id:<integer>,artifact_id:<integer>,path:<exact member>}; "
    "select only specific necessary retained material that makes the question evaluable. Do not require the finding already PASS; the verifier judges it. "
    "Legacy reports without an event digest additionally require the supplied genuine necessary recovery progress, never a timestamp or serialization change. "
    "Accounting never grants PASS or acceptance authority. Resolved duplicate/nonissue claims still "
    "require exact raw evidence/proofs. Generic model writing is unavailable: a repair plan requires an existing allowlisted mechanical operation, exact current finding/head/source owner, retained proof and registered required-test identity; never invent a test or command. Missing tests require collection, substantive implementation requires existing corrective coding owner, and no bounded executor is currently qualified. "
    "Security/high-risk or conflicting-authority findings require existing qualified owner review with classification uncertain and nonempty "
    "substantive_needs, even when their finding IDs are clear. "
    "If any finding identity, meaning, ownership, security implication, or safe routing remains "
    "uncertain, return classification uncertain with nonempty substantive_needs; do not hide "
    "uncertainty to fit the accounting path. "
)


def strict_response_json(document):
    """Reject ambiguous keys and non-finite values throughout a worker response."""

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate response key")
            result[key] = value
        return result

    def finite_float(token):
        value = float(token)
        if not math.isfinite(value):
            raise ValueError("non-finite response number")
        return value

    def reject_constant(_token):
        raise ValueError("non-finite response constant")

    return json.loads(
        document,
        object_pairs_hook=object_pairs,
        parse_float=finite_float,
        parse_constant=reject_constant,
    )


def parse_low_proposal(output):
    """Parse one complete JSON document, optionally in one JSON code fence."""
    if not isinstance(output, str):
        return {}
    document = output.strip()
    if document.startswith("```"):
        match = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", document, re.DOTALL)
        if match is None:
            return {}
        document = match.group(1)
    try:
        parsed = strict_response_json(document)
    except (ValueError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def render_classification_prompt(
    prompt_mandatory,
    *,
    source_rows,
    diff_rows,
    review_rows,
    artifact_rows,
    run_drift=False,
    data_only=True,
    recovery_enabled=False,
    collection_mode=False,
    readiness=None,
    collection_instructions=None,
    semantic_inventory=None,
):
    """Render supplied trusted inventories only; no file access or semantic answers."""
    if collection_mode:
        raise ClassificationPromptError("semantic_role_is_not_collection_planner")
    if not isinstance(semantic_inventory, dict) or set(semantic_inventory) != {
        "mode",
        "host_residual_holds",
        "required_obligation_slots",
        "required_finding_slots",
        "obligations",
        "findings",
        "catalog",
    }:
        raise ClassificationPromptError("semantic_inventory_unavailable")
    holds = semantic_inventory["host_residual_holds"]
    if (
        type(holds) is not list
        or any(
            type(value) is not str
            or value
            not in {
                "native_provider_FAIL_CONCERNS_mismatch",
                "native_ci_failed_without_same_boundary_failed_receipt",
            }
            for value in holds
        )
        or len(holds) != len(set(holds))
    ):
        raise ClassificationPromptError("semantic_host_residual_inventory_invalid")
    from closer_disposition import _expected

    expected = _expected({"sources": source_rows})
    if expected is None or semantic_inventory["obligations"] != [
        {"slot": n, "source": source, "text": text} for n, (source, text) in enumerate(expected, 1)
    ]:
        raise ClassificationPromptError("semantic_inventory_differs_from_original")
    schema = {
        "root_exact_keys": ["schema", "obligations", "findings", "global_concerns"],
        "schema": "closer-semantic-assessment/v2",
        "row_exact_keys": ["slot", "meaning", "status", "citations", "explanation"],
        "unknown_additional_required_key": "gap",
        "slot": "positive integer from corresponding inventory; not bool; unique; every required slot exactly once, any output order; optional actually assessed original slots permitted",
        "meaning": ["understood", "unknown"],
        "obligation_status": ["satisfied", "unsatisfied", "unknown"],
        "finding_status": ["confirmed", "duplicate", "nonissue", "unknown"],
        "citations": "0..8 unique catalog ref numbers; nonunknown requires1..8; same physical ref once per row",
        "citation_exact_keys": {
            "ref": "positive integer catalog number, not bool",
            "excerpt": "literal raw UTF-8 substring, >=4 stripped characters",
            "relevance": "your semantic explanation, 12..2000 stripped characters",
        },
        "explanation": "1..2000 stripped characters, specific to original obligation/finding",
        "gap_rules": {
            "meaning_unknown": "status unknown plus gap obligation_ambiguous",
            "understood_status_unknown": [
                "evidence_absent",
                "evidence_inconclusive",
                "observed_execution_required",
                "source_conflict",
            ],
            "nonunknown": "meaning understood, omit gap",
        },
        "global_concerns": "0..20 exact objects {kind,explanation,citations}; kind security|authority_conflict|ownership|ambiguity; same bounded explanation/citations grammar",
    }
    tutorial = {
        "target": "tutorial/static#12",
        "head": "a" * 40,
        "base": "b" * 40,
        "source": "tutorial/static#2",
        "body": "## Tasks\n- Strip surrounding whitespace.\n## Acceptance Criteria\n- Return the trimmed string.",
        "filename": "trim.py",
        "blob_sha": "c" * 40,
        "raw_patch": "+def trim(value):\n+    return value.strip()\n",
        "catalog_ref": 1,
    }
    static_example = {
        "schema": "closer-semantic-assessment/v2",
        "obligations": [
            {
                "slot": n,
                "meaning": "understood",
                "status": "satisfied",
                "citations": [
                    {
                        "ref": 1,
                        "excerpt": "+    return value.strip()",
                        "relevance": "The complete supplied implementation returns the input with surrounding whitespace removed, satisfying this original static trimming obligation.",
                    }
                ],
                "explanation": "The complete bound patch directly implements the original static trimming requirement.",
            }
            for n in (1, 2, 3)
        ],
        "findings": [],
        "global_concerns": [],
    }
    example = {
        "schema": "closer-semantic-assessment/v2",
        "obligations": [
            {
                "slot": n,
                "meaning": "understood",
                "status": "unknown",
                "citations": [],
                "explanation": "The original requires observed execution; the supplied tutorial contains no execution receipt.",
                "gap": "observed_execution_required",
            }
            for n in (1, 2)
        ],
        "findings": [],
        "global_concerns": [],
    }
    mandatory = dict(prompt_mandatory)
    mandatory.pop("drift_audit", None)
    prompt = (
        "TOOL-FREE SOURCE-SCOPED SEMANTIC REVIEW. Assess every required original slot, separately. ordinary_acceptance requires every original BODY and Tasks/AC/prose slot. Host-selected known_NONPASS_accounting permits obligations[] and unassessed original obligations; assess every original finding slot. Omitted optional obligations are host-only UNASSESSED, not unknown/unsatisfied/satisfied. Never claim acceptance from accounting mode. "
        "Host slot numbers identify originals; order is irrelevant. Return one JSON object only (optional single json fence). No duplicate keys, nonfinite numbers, aliases, unknown keys or trailing text. "
        "You own semantic sufficiency, unmet obligations, finding meaning, literal excerpts, relevance and genuine gaps. Host owns identities, raw proof binding, supported collection/routing/budgets/actions. "
        "The host_residual_holds inventory is independently derived before your review from exact bound structured native fields. The host preserves these OTHER holds even if you omit them; do not normalize FAIL and CONCERNS or infer that a different successful CI run explains ci_failed. No residual flag, equal values, or absence of a flag proves compatibility. You still own substantive defects, duplicate/finding meaning and genuine semantic contradictions; these host checks do not answer those questions. Do not return or attempt to clear host_residual_holds. "
        "Inventory and catalog identity are not semantic proof. Cite only visible catalog content and explain its connection to each original. Never invent excerpts, evidence, native/CI outcomes or unseen work. "
        "Satisfied is advisory when complete bound code proof decides an originally static obligation; there is no universal executed-test requirement. Observed execution is required when the ORIGINAL obligation explicitly depends on it. "
        "An understood obligation with missing/inconclusive proof stays meaning understood, status unknown, with its typed proof gap. Only genuinely undecidable meaning/scope uses meaning unknown. "
        "Known unmet obligations/confirmed defects remain relevant and not ready; do not call them unrelated, invent a missing-evidence story, mechanical plan or mandatory extra work. "
        "Finding resolutions use the existing admitted ordinary plus native-resolution proof index; code/CI may support a finding when its semantic connection is established. Native-resolution catalog evidence can explain findings but cannot prove ordinary acceptance. Catalog identity alone proves no semantic conclusion; accounting never supplies acceptance authority. "
        "Include security/ownership/authority/ambiguity concerns specifically; independent host holds survive an empty list. Optional speculation is not necessary work. "
        "Do not return classification/readiness, source IDs/hashes, actions, plans, collection requests, budgets or provider choices. No tools or delegation. "
        "Your advice grants no ACT, write, merge, closure, verification or completion authority. Producer drift audit is separately pending, not your completion claim.\n"
        "SEMANTIC_RESPONSE_SCHEMA_JSON_BEGIN\n"
        + json.dumps(schema, ensure_ascii=False, sort_keys=True)
        + "\nSEMANTIC_RESPONSE_SCHEMA_JSON_END\n"
        "Independent invented static tutorial, NOT this case or a current-case oracle. Its host catalog exposes exactly the patch below as ordinary ref1 and its original BODY/Task/AC are slots1/2/3. Host supplies all proof identity metadata; model supplies only the following semantic judgment and literal citation.\n"
        "SEMANTIC_TUTORIAL_INPUT_JSON_BEGIN\n"
        + json.dumps(tutorial, ensure_ascii=False)
        + "\nSEMANTIC_TUTORIAL_INPUT_JSON_END\n"
        "SEMANTIC_TUTORIAL_RESPONSE_JSON_BEGIN\n"
        + json.dumps(static_example, ensure_ascii=False)
        + "\nSEMANTIC_TUTORIAL_RESPONSE_JSON_END\n"
        "Separate invented execution-dependent original BODY '## Tasks\\n- Observe a successful current-head test run.' yields two slots and no execution evidence. Its gap response follows.\n"
        "SEMANTIC_EXECUTION_GAP_RESPONSE_JSON_BEGIN\n"
        + json.dumps(example, ensure_ascii=False)
        + "\nSEMANTIC_EXECUTION_GAP_RESPONSE_JSON_END\n"
        "CURRENT_HOST_INVENTORY_JSON_BEGIN\n"
        + json.dumps(semantic_inventory, ensure_ascii=False, sort_keys=True)
        + "\nCURRENT_HOST_INVENTORY_JSON_END\n"
        "CURRENT_CONTEXT_JSON_BEGIN\n"
        + json.dumps(mandatory, ensure_ascii=False, sort_keys=True)
        + "\nCURRENT_CONTEXT_JSON_END"
    )
    if any(
        isinstance(row, dict) and row.get("visibility") for row in semantic_inventory["catalog"]
    ):
        prompt = (
            "CATALOG VISIBILITY: visibility records declare exact supplied/total bytes, truncation, redaction and omission. "
            "Omitted tails/entries are not supplied or inspected by you; never infer their outcomes. "
            "Citations must quote actually supplied visible content from a citable entry. A retained private raw reference does not make hidden bytes visible. "
            "Redacted entries marked citable=false are context only and cannot supply proof. Complete original obligation/finding coverage is still required by the source-owned mode.\n"
            + prompt
        )
    try:
        size = len(prompt.encode("utf-8"))
    except UnicodeError as exc:
        raise ClassificationPromptError("classification_prompt_invalid_utf8") from exc
    if size > MAX_CLASSIFICATION_PROMPT_BYTES:
        raise ClassificationPromptError("classification_prompt_exceeds_bound", prompt_bytes=size)
    return prompt


class ClassificationPromptError(ValueError):
    """Deterministic collection refusal before model execution or escalation."""

    def __init__(self, reason, *, prompt_bytes=None):
        super().__init__(reason)
        self.reason = reason
        self.prompt_bytes = prompt_bytes


def classification_contract_descriptor(*, source_sha256):
    """Bind the actual source pin supplied by the verified external manifest.

    This pure descriptor does not verify the caller's source hash or qualify a
    provider. Syntax parsing deliberately does not stand in for retained-proof
    semantic validation. Qualification must execute that existing validator.
    """
    if not isinstance(source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("model contract requires an exact externally verified source digest")
    descriptor = {
        "schema": CONTRACT_SCHEMA,
        "role": "bounded_classification",
        "source_module": "closer_model_contracts.py",
        "source_sha256": source_sha256,
        "prompt_version": CLASSIFICATION_PROMPT_VERSION,
        "rendering_version": CLASSIFICATION_RENDERING_VERSION,
        "max_prompt_bytes": MAX_CLASSIFICATION_PROMPT_BYTES,
        "optional_sample_characters": OPTIONAL_SAMPLE_CHARACTERS,
        "response_syntax_version": STRICT_RESPONSE_VERSION,
        "response_syntax": {
            "root": "object",
            "documents": 1,
            "optional_json_fence": True,
            "duplicate_keys": "reject",
            "nonfinite_numbers": "reject",
            "trailing_text": "reject",
            "identity_or_evidence_validation": False,
        },
        "response_fields": {
            "schema": "closer-semantic-assessment/v2",
            "obligations": "exact original host slot set; typed meaning/status/gap plus selected citations",
            "findings": "exact original host finding slot set; typed meaning/status/gap plus selected citations",
            "global_concerns": "bounded semantic concerns; no action authority",
        },
        "semantic_validator": {
            "entrypoint": "closer_semantic_assessment.materialize -> closer_disposition.integrate_ordinary",
            "retained_io_required": True,
            "acceptance_authority": False,
        },
        "nonpass_accounting_sha256": hashlib.sha256(
            NONPASS_ACCOUNTING_CONTRACT.encode()
        ).hexdigest(),
    }
    descriptor["contract_sha256"] = hashlib.sha256(
        json.dumps(descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return descriptor


STRONG_CONTRACT_ENTRYPOINTS = {
    "request_schema": "closer-criterion-request/v2",
    "response_schema": "closer-criterion-response/v1",
    "prompt_builder": "closer_execution_contracts.build_adjudication_prompt",
    "response_validator": "closer_execution_contracts.validate_adjudication_response",
    "merge": "closer_execution_contracts.merge_selected_response",
    "max_prompt_bytes": 64 * 1024,
    "retained_io_required": True,
    "acceptance_authority": False,
}
