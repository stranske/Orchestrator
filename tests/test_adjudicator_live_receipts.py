"""Live advice receipts stay bounded independently of the certified path inventory."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import adjudicator_live as live
import feedback
import roles


class LiveReceiptTests(unittest.TestCase):
    def test_large_certified_inventory_preserves_advice_and_cost_without_merge_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "brain.db"
            report = root / "live.json"
            with patch.object(feedback, "DB_PATH", db):
                with closing(feedback._conn()) as conn, conn:
                    conn.execute(
                        "INSERT INTO runs(run_id,ts,target) VALUES ('dispute',100,'owner/repo#1')"
                    )
                    conn.execute(
                        "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
                        "VALUES ('dispute','NON_PASS','PASS',1)"
                    )
                    conn.execute(
                        "INSERT INTO costs(run_id,cost_usd,source) VALUES ('backend',0.125,'ccusage')"
                    )
            # Each basename fits Git/filesystem limits, but JSON escaping the inventory
            # exceeds the event budget even after the normal 32-item list bound.
            stem = "\u8bc1" * 80
            paths = [f"{stem}{number}.py" for number in range(32)]
            self.assertGreater(len(json.dumps(paths)), feedback.MAX_COMPLETION_EVENT_BYTES)

            def collector(entry, *_args, **_kwargs):
                entry["collection_requirements"] = {"source_paths": paths}
                return roles._issue_collected_case(
                    {"target": entry["target"], "metadata_only": False}, 10000
                )

            @contextmanager
            def checkout(*_args):
                yield root

            backend = Mock(
                return_value={
                    "backend": "stub",
                    "backend_run_id": "backend",
                    "proposal": {"decision": "reject_blocker"},
                    "errors": [],
                }
            )

            def merge_inputs():
                with closing(sqlite3.connect(db)) as conn:
                    return {
                        table: conn.execute(f"SELECT * FROM {table}").fetchall()
                        for table in ("runs", "outcomes", "influence_edges")
                    }

            before = merge_inputs()
            options = dict(
                db=db,
                path=report,
                now=101,
                env={},
                evidence_reader=lambda _: live._evidence_for("a" * 40),
                snapshot_reader=lambda _: {},
                provider=checkout,
                runner=backend,
            )
            with patch.object(live, "certify", collector):
                result = live.run_live(**options)
                self.assertEqual(result["advised"], 1)
                self.assertEqual(result["rows"][0]["certification"]["paths"], paths)
                saved = json.loads(report.read_text())
                self.assertEqual(saved["rows"][0]["collection_requirements"]["source_paths"], paths)
                with closing(sqlite3.connect(db)) as conn:
                    events = conn.execute(
                        "SELECT phase,validation_status,payload_json FROM completion_events "
                        "WHERE producer='role-adjudicator'"
                    ).fetchall()
                self.assertEqual(len(events), 1)
                phase, validation, raw = events[0]
                self.assertEqual((phase, validation), ("live_shadow_advice", "accepted"))
                receipt = json.loads(raw)["live_shadow"]
                for key, expected in {
                    "case_id": result["rows"][0]["case_id"],
                    "target": "owner/repo#1",
                    "decision": "reject_blocker",
                    "backend": "stub",
                    "backend_run_id": "backend",
                    "cost_usd": 0.125,
                }.items():
                    self.assertEqual(receipt[key], expected)
                self.assertEqual(
                    receipt["certification"]["digest"], backend.call_args.kwargs["case"]._digest
                )
                # Recovery uses the durable receipt without paying for another role run.
                report.unlink()
                self.assertEqual(live.run_live(**options)["attempted"], 0)
                self.assertEqual(backend.call_count, 1)
            self.assertEqual(merge_inputs(), before)
