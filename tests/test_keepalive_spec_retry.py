"""A failed linked-issue read must not permanently materialize a bogus shadow spec."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import feedback
import keepalive_outcomes as ingest
import runtime_ac
import runtime_ac_gate as gate


class KeepaliveSpecRetryTests(unittest.TestCase):
    def test_malformed_issue_body_is_retryable(self):
        issue_body = "## Acceptance Criteria\n- Named test: `tests/test_example.py::test_one`.\n"
        pr = {
            "number": 2,
            "state": "OPEN",
            "title": "Delivery",
            "body": "Closes #1",
            "headRefName": "codex/issue-1",
            "headRefOid": "a" * 40,
            "labels": [{"name": "agent:codex"}],
            "author": {"login": "stranske"},
            "createdAt": "2026-10-05T10:00:00Z",
            "updatedAt": "2026-10-05T10:00:00Z",
        }
        responses = (
            {"body": True},
            {"body": 42},
            {"body": [issue_body]},
            {"body": {"text": issue_body}},
            {"body": None},
            {"body": " \n "},
        )
        for response in responses:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with (
                    patch.object(feedback, "DB_PATH", root / "brain.db"),
                    patch.dict(os.environ, {"ORCH_RUN_RUNTIME_AC": "0"}),
                    patch.object(ingest, "_gh_throttle"),
                    patch.object(ingest, "_fetch_prs", return_value=[pr]),
                    patch.object(ingest, "_fetch_dispatch_times", return_value=None),
                    patch.object(
                        ingest, "_run_json", side_effect=[response, {"body": issue_body}]
                    ) as fetch,
                ):
                    kwargs = {"_spec_dir": root / "specs"}
                    path = gate.spec_path("owner/repo#2", spec_dir=kwargs["_spec_dir"])
                    first = ingest.ingest_keepalive_outcomes(["owner/repo"], **kwargs)
                    self.assertEqual(first["runs_recorded"], 1)
                    self.assertEqual(first["runtime_ac_specs_authored"], 0)
                    self.assertEqual(
                        first["runtime_ac_shadow_errors"],
                        ["owner/repo#2: linked issue body unavailable"],
                    )
                    self.assertFalse(path.exists())
                    self.assertEqual(feedback.runtime_ac_gate_events(), [])

                    retry = ingest.ingest_keepalive_outcomes(["owner/repo"], **kwargs)
                    self.assertEqual(retry["runs_recorded"], 0)
                    self.assertEqual(retry["runtime_ac_specs_authored"], 1)
                    self.assertEqual(retry["runtime_ac_shadow_errors"], [])
                    before = path.read_bytes()
                    spec = json.loads(before)
                    self.assertEqual(runtime_ac.validate_spec(spec), [])
                    self.assertEqual(spec["verification"]["source_issue"], "owner/repo#1")
                    self.assertEqual(
                        spec["acceptance_criteria"][0]["checks"][0]["command"],
                        "python3 -m pytest -p no:cov tests/test_example.py::test_one",
                    )
                    repeated = ingest.ingest_keepalive_outcomes(["owner/repo"], **kwargs)
                    self.assertEqual(repeated["runtime_ac_specs_authored"], 0)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(fetch.call_count, 2)
                    events = feedback.runtime_ac_gate_events()
                    self.assertEqual(sum(event["spec_authored"] for event in events), 1)
                    self.assertTrue(all(event["shadow_only"] for event in events))
                    self.assertTrue(all(not event["blocking"] for event in events))
                    self.assertEqual(os.environ["ORCH_RUN_RUNTIME_AC"], "0")
                    with feedback._conn() as conn:
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
                        self.assertEqual(
                            conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0], 0
                        )
