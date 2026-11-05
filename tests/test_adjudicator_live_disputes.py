"""Read-only live dispute selection from a private Brain and checkpoint."""

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

import adjudicator_retro as retro
import feedback


class LiveDisputesTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.db = self.root / "brain.db"
        self.stack.enter_context(patch.object(feedback, "DB_PATH", self.db))
        self.stack.enter_context(patch.dict(os.environ, {"ORCH_STATE_DIR": str(self.root)}))
        self.now = 2_000_000_000
        self.state = self.root / "capability-program" / "adjudicator-live.json"
        with closing(feedback._conn()) as conn, conn:
            for number in range(1, 7):
                conn.execute(
                    "INSERT INTO runs(run_id,ts,target) VALUES (?,?,?)",
                    (f"run-{number}", self.now - number, f"owner/repo#{number}"),
                )
                conn.execute(
                    "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict) "
                    "VALUES (?,'NON_PASS','PASS')",
                    (f"run-{number}",),
                )

    def select(self, **kwargs):
        return retro.live_disputes(db=self.db, now=self.now, **kwargs)

    def advice_event(self, run_id, *, phase="live_shadow_advice", producer="role-adjudicator"):
        # Seed the event independently of the writer, whose live phase registration
        # is a separate task. Selection must never initialize or migrate the Brain.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO completion_events "
                "(event_id,schema_version,run_id,event_type,phase,producer,payload_json,"
                "content_hash,created_ts,updated_ts) VALUES (?,1,?,'role',?,?,?,'test',1,1)",
                (f"advice:{run_id}:{phase}:{producer}", run_id, phase, producer, "{}"),
            )

    def test_filters_since_and_advised_targets_before_limiting(self):
        # Advice belongs to an earlier run on the same PR, not to the new dispute.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO runs(run_id,ts,target) VALUES ('earlier',?,'owner/repo#1')",
                (self.now - 20,),
            )
        self.advice_event("earlier")
        self.advice_event("run-2", phase="execution")
        self.advice_event("run-3", producer="other")
        before = self.db.read_bytes()
        rows = self.select(since_ts=self.now - 6)
        self.assertEqual([row["run_id"] for row in rows], ["run-2", "run-3", "run-4"])
        self.assertEqual(
            rows,
            [
                row
                for row in retro.disputes(db=self.db, now=self.now)
                if row["run_id"] in {"run-2", "run-3", "run-4"}
            ],
        )
        self.assertEqual(self.db.read_bytes(), before)

    def test_uses_last_live_report_timestamp_and_explicit_override(self):
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({"generated_at": self.now - 3, "rows": []}))
        before = self.state.read_bytes()
        self.assertEqual([row["run_id"] for row in self.select()], ["run-1", "run-2"])
        # Equality is not newer than the cutoff.
        self.assertEqual(
            [row["run_id"] for row in self.select(since_ts=self.now - 2)], ["run-1"]
        )
        self.assertEqual(self.state.read_bytes(), before)

    def test_first_run_and_explicit_limit(self):
        self.assertEqual(len(self.select()), 3)
        self.assertEqual(len(self.select(limit=20)), 6)
        self.assertEqual(self.select(limit=0), [])
        self.assertEqual(self.select(limit=-1), [])
        self.assertFalse(self.state.exists())

    def test_selects_only_actual_disagreements(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE outcomes SET verifier_verdict='PASS' WHERE run_id='run-1'")
            conn.execute("UPDATE outcomes SET adjudicated_verdict=NULL WHERE run_id='run-2'")
        self.assertEqual(
            [row["run_id"] for row in self.select()], ["run-3", "run-4", "run-5"]
        )

    def test_reads_legacy_brain_without_creating_event_table(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DROP TABLE completion_events")
        before = self.db.read_bytes()
        self.assertEqual(len(self.select(since_ts=self.now - 6)), 3)
        self.assertEqual(self.db.read_bytes(), before)

    def test_does_not_hide_a_corrupt_checkpoint(self):
        self.state.parent.mkdir()
        for content in ("{broken", "[]", '{"generated_at": -1}', '{"generated_at": "bad"}'):
            with self.subTest(content=content):
                self.state.write_text(content)
                with self.assertRaises(ValueError):
                    self.select()
                self.assertEqual(len(self.select(since_ts=self.now - 6)), 3)
