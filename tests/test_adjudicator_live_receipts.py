"""Live advice receipts stay bounded independently of the certified path inventory."""

import hashlib
import json
import os
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

    def test_redacted_receipt_recovery_without_second_dispatch_including_pending_retry(self):
        """Brain rows restore targets from `runs`; cache loss must not re-pay; retry order holds."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "brain.db"
            report = root / "live.json"
            state_root = root / "state"
            state_root.mkdir()
            sha = "a" * 40

            def collector(entry, *_args, **_kwargs):
                entry["collection_requirements"] = {"source_paths": ["proof.txt"]}
                return roles._issue_collected_case(
                    {"target": entry["target"], "metadata_only": False}, 10000
                )

            @contextmanager
            def checkout(*_args):
                yield root

            calls = []

            def runner(**kw):
                calls.append(kw)
                return {
                    "backend": "stub",
                    "backend_run_id": f"backend-{len(calls)}",
                    "proposal": {"decision": "reject_blocker"},
                    "errors": [],
                }

            def run_once(*, now, limit, dispatch, runner_fn):
                return live.run_live(
                    db=db,
                    path=report,
                    now=now,
                    env={},
                    limit=limit,
                    dispatch=dispatch,
                    evidence_reader=lambda _: live._evidence_for(sha),
                    snapshot_reader=lambda _: {},
                    provider=checkout,
                    runner=runner_fn,
                )

            with patch.object(feedback, "DB_PATH", db):
                with patch.dict(os.environ, {"ORCH_STATE_DIR": str(state_root)}):
                    with closing(feedback._conn()) as conn, conn:
                        conn.execute(
                            "INSERT INTO runs(run_id,ts,target) VALUES ('r-old',10,'owner/repo#99')"
                        )
                        conn.execute(
                            "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
                            "VALUES ('r-old','NON_PASS','PASS',1)"
                        )
                        conn.execute(
                            "INSERT INTO runs(run_id,ts,target) VALUES ('r-new',100,'owner/repo#99')"
                        )
                        conn.execute(
                            "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
                            "VALUES ('r-new','NON_PASS','PASS',1)"
                        )
                    with patch.object(live, "certify", collector):
                        first = run_once(now=200, limit=1, dispatch=True, runner_fn=runner)
                        self.assertEqual((first["attempted"], first["advised"]), (1, 1))
                        self.assertEqual(first["rows"][0]["run_id"], "r-old")
                    with closing(sqlite3.connect(db)) as conn, conn:
                        for (raw,) in conn.execute(
                            "SELECT payload_json FROM completion_events "
                            "WHERE phase='live_shadow_advice'"
                        ):
                            payload = json.loads(raw)
                            payload["live_shadow"]["target"] = ""
                            new_raw = json.dumps(payload)
                            digest = hashlib.sha256(new_raw.encode()).hexdigest()
                            conn.execute(
                                "UPDATE completion_events SET payload_json=?, content_hash=? "
                                "WHERE payload_json=?",
                                (new_raw, digest, raw),
                            )
                    report.unlink()
                    with patch.object(live, "certify", collector):
                        recovered = run_once(now=201, limit=0, dispatch=True, runner_fn=runner)
                    self.assertEqual(recovered["attempted"], 0)
                    self.assertEqual(len(calls), 1)
                    rows_by_id = {row["run_id"]: row for row in recovered["rows"]}
                    self.assertEqual(rows_by_id["r-old"]["target"], "owner/repo#99")

                    dispatch_try = {"n": 0}

                    def flaky_runner(**kw):
                        dispatch_try["n"] += 1
                        if dispatch_try["n"] == 1:
                            raise RuntimeError("backend down")
                        return runner(**kw)

                    with patch.object(live, "certify", collector):
                        third = run_once(now=202, limit=3, dispatch=True, runner_fn=flaky_runner)
                        self.assertEqual(third["attempted"], 1)
                        row = third["rows"][-1]
                        self.assertEqual(row["run_id"], "r-new")
                        self.assertIn("error", row)
                    with patch.object(live, "certify", collector):
                        fourth = run_once(now=203, limit=3, dispatch=True, runner_fn=runner)
                    self.assertEqual(fourth["attempted"], 1)
                    self.assertEqual(fourth["rows"][-1]["run_id"], "r-new")
                    self.assertIn("proposal", fourth["rows"][-1])
                    self.assertEqual(len(calls), 2)

    def test_secret_like_target_is_recovered_and_graded_after_durability(self):
        """`task-management` contains `sk-management`; the receipt redacts it, grading must not."""
        import adjudicator_retro as retro

        target = "owner/task-management#3"
        self.assertTrue(feedback.SECRET_VALUE_RE.search(target))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "brain.db"
            report = root / "live.json"
            with patch.object(feedback, "DB_PATH", db):
                with closing(feedback._conn()) as conn, conn:
                    conn.execute(
                        "INSERT INTO runs(run_id,ts,target) VALUES ('secret-run',100,?)", (target,)
                    )
                    conn.execute(
                        "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
                        "VALUES ('secret-run','NON_PASS','PASS',1)"
                    )
                dispute = {
                    "run_id": "secret-run",
                    "verifier_verdict": "NON_PASS",
                    "adjudicated_verdict": "PASS",
                }
                case_id = retro._case_id(dispute)
                case = roles._issue_collected_case(
                    {"target": target, "metadata_only": False}, 10000
                )
                entry = {
                    **dispute,
                    "case_id": case_id,
                    "target": target,
                    "source": "live",
                    "attempted_at": 101,
                    "dispute_ts": 100,
                    "attempts": 1,
                    "merge_rule_verdict": "PASS",
                    "cost_usd": None,
                    "certification": {"status": "certified", "evaluated_sha": "a" * 40},
                    "decision": "uphold_blocker",
                    "disposition": "uphold_blocker",
                    "proposal": {"decision": "uphold_blocker"},
                }
                live._persist_live_event(entry, db, case)
                with closing(sqlite3.connect(db)) as conn:
                    (raw,) = conn.execute(
                        "SELECT payload_json FROM completion_events "
                        "WHERE phase='live_shadow_advice'"
                    ).fetchone()
                self.assertEqual(json.loads(raw)["live_shadow"]["target"], live.REDACTED_TARGET)

                # Path 1: the runs table still names the target, so recovery uses it.
                first = live.reconcile_live(db=db, path=report)
                self.assertEqual(first["rows"][0]["target"], target)
                # Path 2: the runs join no longer answers; the cached report target must survive.
                with closing(feedback._conn()) as conn, conn:
                    conn.execute("UPDATE runs SET target=NULL WHERE run_id='secret-run'")
                    conn.execute(
                        "UPDATE outcomes SET durability='broke_later', durability_checked_ts=? "
                        "WHERE run_id='secret-run'",
                        (feedback.DURABILITY_DETECTION_SINCE + 10,),
                    )
                graded = live.reconcile_live(db=db, path=report)
                self.assertEqual(graded["rows"][0]["target"], target)
                self.assertEqual(live.live_summary(graded["rows"])["graded"], 1)
                self.assertEqual(retro.live_disputes(db=db, since_ts=0, limit=None), [])
