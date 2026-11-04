"""HEAD-only sensitivity pin for the #599 consumer_missing reopen.

The deliberate-break gate overlays this file onto base. Pre-reopen main already
drains consumers and lacks CONSUMER_DRAIN_MARKER, so the named test must live in
a file that does not remove assertions from an existing suite module — otherwise
the gate reports test-assertion-tamper on the prose→consumer_missing updates in
test_capability_admission.py.
"""

from __future__ import annotations

import capabilities
import capability_admission as admission


def test_consumer_drain_marker_and_missing_class():
    """Marker present on HEAD; reviewed absence is consumer_missing, not prose."""
    assert getattr(capabilities, "CONSUMER_DRAIN_MARKER", None) == "599-reopen-2026-11-04"
    declaration = capabilities.KNOWN_DECLARATIONS["docs-drift-fix-agent"]
    assert declaration.get("consumer_missing") is True
    cls, ok, detail = admission.consumer_class(
        {**declaration, "capability_id": "docs-drift-fix-agent"},
        {},
    )
    assert cls == "consumer_missing" and not ok, (cls, detail)
    assert "consumer_missing" in detail
