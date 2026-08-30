#!/usr/bin/env python3
"""Structured specification provenance shared by recovery and evaluation paths."""

from __future__ import annotations

from typing import Any

MISSING_SPEC_STUB_HEADER = "[SPEC_PROVENANCE: MISSING_SPEC_STUB]"


def is_missing_spec(meta: dict[str, Any] | None = None, spec_text: str | None = None) -> bool:
    """Return true only for an explicitly recovered missing-specification stub.

    Structured metadata is authoritative for newly recovered experiments. The text checks retain
    compatibility with recovery artifacts created before structured provenance existed.
    """

    if isinstance(meta, dict):
        if meta.get("missing_spec") is True:
            return True
        if meta.get("spec_provenance") == "missing_spec_stub":
            return True
        if meta.get("spec_status") == "missing_spec_stub":
            return True

    normalized = str(spec_text or "").lower()
    return (
        MISSING_SPEC_STUB_HEADER.lower() in normalized
        or "[spec unavailable at recovery time" in normalized
    )
