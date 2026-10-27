"""Explicit native0953 + semantic-v2 test baseline; historical data stays intact."""

import copy
import hashlib
import json
from pathlib import Path

from closer_semantic_audit_support import reference

MANIFEST = Path(__file__).parent / "fixtures/semantic_api_reference/native_0953_guard_audits.json"
MANIFEST_SHA256 = "e112f9f7152621f13470e8da11950aa9b29e000ee4bcba846ce3c1701ca995b3"


def native_reference():
    raw = MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == MANIFEST_SHA256
    return json.loads(raw)


CURRENT_MANIFEST = (
    Path(__file__).parent / "fixtures/semantic_api_reference/current_core_role_receiver_v1.json"
)
CURRENT_MANIFEST_SHA256 = "32d0a361e2e942fc262ef5847583c53d954d74a2bbdfe6a665c3a819b770035a"


def current_receiver_reference():
    raw = CURRENT_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CURRENT_MANIFEST_SHA256
    return json.loads(raw)


def integrated_reference(base):
    """Preserve historical data, then compose the explicitly reviewed current receiver."""
    original = copy.deepcopy(reference(base))
    current = current_receiver_reference()
    dispatcher = original["files"].get("closer_dispatcher.py")
    if dispatcher is not None:
        dispatcher["top_level_ast"] = current["top_level_ast"]
        dispatcher["unreserved_pipeline_ast_sha256"] = current["unreserved_pipeline_ast_sha256"]
        dispatcher["nested_pipeline_ast"] = copy.deepcopy(current["nested_pipeline_ast"])
        # A separate pinned prospective audit covers ONLY the now unsupported
        # optional-audit job/cache changes; original7488 data stays immutable.
        deferred = audit_deferral_reference()
        dispatcher["nested_pipeline_ast"]["prepare"] = deferred["after_prepare_ast_sha256"]
        dispatcher["unreserved_pipeline_ast_sha256"] = deferred[
            "after_unreserved_pipeline_ast_sha256"
        ]
    return original


AUDIT_DEFERRAL_MANIFEST = (
    Path(__file__).parent / "fixtures/semantic_api_reference/optional_audit_deferral_v1.json"
)
AUDIT_DEFERRAL_SHA256 = "6fa689b6f2677979ef29483bed8d4c7545f0fec97000ad1ce9d5fa1b69d6afc1"


def audit_deferral_reference():
    raw = AUDIT_DEFERRAL_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == AUDIT_DEFERRAL_SHA256
    return json.loads(raw)
