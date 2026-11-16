"""Regression checks for issue #1379 keepalive-acceptance verification evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path

import paths

EVIDENCE_DIR = paths.REPO_ROOT / "docs" / "verification" / "issue-1379"
GATE_JSON = EVIDENCE_DIR / "verification-gate.json"
NAMED_TEST = EVIDENCE_DIR / "named-acceptance-test.txt"


def test_issue_1379_verification_gate_records_equal_shares():
    record = json.loads(GATE_JSON.read_text(encoding="utf-8"))
    assert record["equal"] is True
    assert record["command"] == "python3 src/feature_goals.py --verify-keepalive-acceptance"
    assert record["measure_share"] == record["rest_share"]
    assert record["rest"]["as_of"] == record["as_of"]
    assert record["rest"]["share"] == record["rest_share"]


def test_issue_1379_named_acceptance_test_documents_passing_command():
    text = NAMED_TEST.read_text(encoding="utf-8")
    assert "Source PR:" in text
    assert "Deterministic gates" in text or "Criterion 1: PASS" in text
    assert re.search(r"Exit code: 0", text)
    assert "keepalive-gate-citation" in text
