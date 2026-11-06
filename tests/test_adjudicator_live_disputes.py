"""Read-only live dispute selection from a private Brain and checkpoint."""

import hashlib
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
            if phase == "live_shadow_advice":
                fetched = conn.execute(
                    "SELECT run_id,verifier_verdict,adjudicated_verdict FROM outcomes "
                    "WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if fetched:
                    row = {
                        "run_id": fetched[0],
                        "verifier_verdict": fetched[1],
                        "adjudicated_verdict": fetched[2],
                    }
                else:
                    row = {
                        "run_id": run_id,
                        "verifier_verdict": "NON_PASS",
                        "adjudicated_verdict": "PASS",
                    }
                case_id = retro._case_id(row)
                payload = {
                    "live_shadow": {
                        "run_id": run_id,
                        "case_id": case_id,
                        "target": "owner/repo#1",
                    }
                }
            else:
                payload = {}
            raw = json.dumps(payload)
            digest = hashlib.sha256(raw.encode()).hexdigest()
            conn.execute(
                "INSERT INTO completion_events "
                "(event_id,schema_version,run_id,event_type,phase,producer,payload_json,"
                "content_hash,created_ts,updated_ts) VALUES (?,1,?,'role',?,?,?,?,1,1)",
                (
                    f"advice:{run_id}:{phase}:{producer}",
                    run_id,
                    phase,
                    producer,
                    raw,
                    digest,
                ),
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
        self.assertEqual([row["run_id"] for row in rows], ["run-1", "run-2", "run-3"])
        self.assertEqual(
            rows,
            [
                row
                for row in retro.disputes(db=self.db, now=self.now)
                if row["run_id"] in {"run-1", "run-2", "run-3"}
            ],
        )
        self.assertEqual(self.db.read_bytes(), before)

    def test_uses_last_live_report_timestamp_and_explicit_override(self):
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({"generated_at": self.now - 3, "rows": []}))
        before = self.state.read_bytes()
        self.assertEqual([row["run_id"] for row in self.select()], ["run-1", "run-2"])
        # Equality is not newer than the cutoff.
        self.assertEqual([row["run_id"] for row in self.select(since_ts=self.now - 2)], ["run-1"])
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
        self.assertEqual([row["run_id"] for row in self.select()], ["run-3", "run-4", "run-5"])

    def test_reads_legacy_brain_without_creating_event_table(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DROP TABLE completion_events")
        before = self.db.read_bytes()
        self.assertEqual(len(self.select(since_ts=self.now - 6)), 3)
        self.assertEqual(self.db.read_bytes(), before)

    def test_collab_admin_stays_in_retrospective_disputes_and_out_of_live_selection(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO runs(run_id,ts,target) VALUES ('admin',?,'stranske/Collab-Admin#9')",
                (self.now - 1,),
            )
            conn.execute(
                "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict) "
                "VALUES ('admin','NON_PASS','PASS')"
            )
            conn.execute(
                "INSERT INTO runs(run_id,ts,target) VALUES ('other',?,'owner/repo#9')",
                (self.now - 1,),
            )
            conn.execute(
                "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict) "
                "VALUES ('other','NON_PASS','PASS')"
            )
        retrospective = {row["run_id"] for row in retro.disputes(db=self.db, now=self.now)}
        self.assertIn("admin", retrospective)
        self.assertIn("other", retrospective)
        selected = {row["run_id"] for row in self.select(since_ts=0, limit=20)}
        self.assertNotIn("admin", selected)
        self.assertIn("other", selected)

    def test_does_not_hide_a_corrupt_checkpoint(self):
        self.state.parent.mkdir()
        for content in ("{broken", "[]", '{"generated_at": -1}', '{"generated_at": "bad"}'):
            with self.subTest(content=content):
                self.state.write_text(content)
                with self.assertRaises(ValueError):
                    self.select()
                self.assertEqual(len(self.select(since_ts=self.now - 6)), 3)

    def test_rejected_advice_events_do_not_block_target_selection(self):
        """A rejected receipt must not suppress a different dispute on the same target."""
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO runs(run_id,ts,target) VALUES ('same-pr',?,'owner/repo#1')",
                (self.now - 50,),
            )
            conn.execute(
                "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict) "
                "VALUES ('same-pr','NON_PASS','PASS')"
            )
            row = {
                "run_id": "same-pr",
                "verifier_verdict": "NON_PASS",
                "adjudicated_verdict": "PASS",
            }
            case_id = retro._case_id(row)
            raw = json.dumps(
                {
                    "live_shadow": {
                        "run_id": "same-pr",
                        "case_id": case_id,
                        "target": "owner/repo#1",
                    }
                }
            )
            digest = hashlib.sha256(raw.encode()).hexdigest()
            conn.execute(
                "INSERT INTO completion_events "
                "(event_id,schema_version,run_id,event_type,phase,producer,validation_status,"
                "payload_json,content_hash,created_ts,updated_ts) "
                "VALUES (?,1,'rej','role','live_shadow_advice','role-adjudicator','rejected',"
                "?,?,1,1)",
                ("evt:rejected-same-pr", raw, digest),
            )
        rows = self.select(since_ts=self.now - 60, limit=10)
        self.assertIn("same-pr", {row["run_id"] for row in rows})

    def test_case_id_dedup_when_target_string_is_redacted(self):
        """Receipt `case_id` suppresses repeat advice even when `target` is empty in payload."""
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO runs(run_id,ts,target) VALUES ('pair-a',?,'owner/redacted#1')",
                (self.now - 40,),
            )
            conn.execute(
                "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict) "
                "VALUES ('pair-a','NON_PASS','PASS')"
            )
            conn.execute(
                "INSERT INTO runs(run_id,ts,target) VALUES ('pair-b',?,'owner/redacted#1')",
                (self.now - 39,),
            )
            conn.execute(
                "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict) "
                "VALUES ('pair-b','NON_PASS','PASS')"
            )
            row_a = {
                "run_id": "pair-a",
                "verifier_verdict": "NON_PASS",
                "adjudicated_verdict": "PASS",
            }
            case_a = retro._case_id(row_a)
            raw = json.dumps(
                {
                    "live_shadow": {
                        "run_id": "pair-a",
                        "case_id": case_a,
                        "target": "",
                    }
                }
            )
            digest = hashlib.sha256(raw.encode()).hexdigest()
            conn.execute(
                "INSERT INTO completion_events "
                "(event_id,schema_version,run_id,event_type,phase,producer,validation_status,"
                "payload_json,content_hash,created_ts,updated_ts) VALUES (?,1,'x','role',"
                "'live_shadow_advice','role-adjudicator','accepted',?,?,1,1)",
                ("evt:redacted-target", raw, digest),
            )
        rows = self.select(since_ts=self.now - 100, limit=10, oldest_first=True)
        self.assertEqual(rows[0]["run_id"], "pair-b")
        self.assertNotIn("pair-a", {row["run_id"] for row in rows})

    def test_advised_newer_run_does_not_hide_older_unadvised_same_target(self):
        """Receipt binds to dispute `run_id`; a newer advised case does not suppress an older one."""
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO runs(run_id,ts,target) VALUES ('old-run',?,'owner/repo#7')",
                (self.now - 80,),
            )
            conn.execute(
                "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict) "
                "VALUES ('old-run','NON_PASS','PASS')"
            )
            conn.execute(
                "INSERT INTO runs(run_id,ts,target) VALUES ('new-run',?,'owner/repo#7')",
                (self.now - 70,),
            )
            conn.execute(
                "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict) "
                "VALUES ('new-run','NON_PASS','PASS')"
            )
            row_new = {"run_id": "new-run", "verifier_verdict": "NON_PASS", "adjudicated_verdict": "PASS"}
            case_new = retro._case_id(row_new)
            raw = json.dumps(
                {
                    "live_shadow": {
                        "run_id": "new-run",
                        "case_id": case_new,
                        "target": "owner/repo#7",
                    }
                }
            )
            digest = hashlib.sha256(raw.encode()).hexdigest()
            conn.execute(
                "INSERT INTO completion_events "
                "(event_id,schema_version,run_id,event_type,phase,producer,validation_status,"
                "payload_json,content_hash,created_ts,updated_ts) VALUES (?,1,'x','role',"
                "'live_shadow_advice','role-adjudicator','accepted',?,?,1,1)",
                ("evt:advised-newer", raw, digest),
            )
        rows = self.select(since_ts=self.now - 100, limit=10, oldest_first=True)
        self.assertEqual(rows[0]["run_id"], "old-run")
        self.assertNotIn("new-run", {row["run_id"] for row in rows})
