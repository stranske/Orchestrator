"""Offline regression checks for the two-profile, three-instance trial layout."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import feedback
import model_profile_trial as trial
import model_profile_trial_bridge as bridge


class ProfileTrialInstancesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sources = [self.root / name for name in ("orchestrator", "workflows")]
        for source in self.sources:
            source.mkdir()
            (source / "sample.py").write_text("VALUE = 1\n")
        self.profiles = ("codex-6-astra-high", "codex-5.6-terra-high")
        self.manifest = trial.build_trial_manifest(
            *self.sources,
            seed=14,
            now=1000,
            capacity_state="ok",
            profile_ids=self.profiles,
            instances_per_profile=3,
            packet={"instruction": "Review the frozen recurring-work specification."},
        )
        self.enterContext(patch.object(feedback, "DB_PATH", self.root / "quarantine-feedback.db"))
        self.enterContext(patch.dict(os.environ, {"ORCH_STATE_DIR": str(self.root / "state")}))

    def results(self):
        attempts = []
        for request in self.manifest["requests"]:
            artifact = self.root / f"identity-{request['launch_ordinal']}.json"
            identity = {
                "profile_id": request["profile_id"],
                "provider_resolved_provider": request["provider"],
                "provider_resolved_model": request["requested_model"],
            }
            artifact.write_text(json.dumps(identity))
            attempts.append(
                {
                    **identity,
                    "run_id": request["run_id"],
                    "operation_role": "worker",
                    "requested_model": request["requested_model"],
                    "selected_model": request["requested_model"],
                    "reported_model": request["requested_model"],
                    "runner_version": "offline-test",
                    "cli_version": "offline-test",
                    "status": "success",
                    "packet_hash": self.manifest["packet_hash"],
                    "acknowledged": True,
                    "tokens_in": request["launch_ordinal"] * 10,
                    "tokens_out": request["launch_ordinal"],
                    "identity_evidence": {
                        "schema": trial.IDENTITY_EVIDENCE_SCHEMA,
                        "version": trial.IDENTITY_EVIDENCE_VERSION,
                        "authority": "openai-response-metadata/v1",
                        "artifact_ref": str(artifact),
                        "artifact_sha256": "sha256:"
                        + hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    },
                }
            )
        return {
            "schema": trial.RESULT_SCHEMA,
            "version": trial.SCHEMA_VERSION,
            "trial_id": self.manifest["trial_id"],
            "packet_hash": self.manifest["packet_hash"],
            "acknowledged": True,
            "attempts": attempts,
            "identity_verified": True,
            "quality_by_profile": dict(zip(self.profiles, (0.8, 0.9))),
        }

    def test_six_instances_ingest_and_report_all_costs_without_collisions(self):
        results = self.results()
        state = trial.finalize_trial(self.manifest, results, ingest_brain=True, now=2000)
        self.assertEqual(state["attempt_count"], 6)
        self.assertEqual(state["shared_pool_debit"], {"codex-subscription": 6.0})
        self.assertTrue(state["brain_ingest_enabled"])
        with feedback._conn() as conn:
            for table in ("runs", "execution_attempts", "outcomes"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 6)
            rows = conn.execute(
                "SELECT profile_id,COUNT(*) FROM execution_attempts GROUP BY profile_id"
            ).fetchall()
        self.assertEqual(dict(rows), dict.fromkeys(self.profiles, 3))
        summary = json.loads(trial._capability_program_summary_path().read_text())
        self.assertEqual((summary["profile_count"], summary["instance_count"]), (2, 6))
        self.assertEqual(summary["quality_by_profile"], results["quality_by_profile"])
        self.assertTrue(summary["identity_verified"])
        for pid in self.profiles:
            attempts = [a for a in results["attempts"] if a["profile_id"] == pid]
            self.assertEqual(
                summary["cost_tokens_by_profile"][pid],
                {key: sum(a[key] for a in attempts) for key in ("tokens_in", "tokens_out")},
            )
            self.assertEqual(
                summary["provider_resolved_identity_by_profile"][pid],
                {"provider": "openai", "model": attempts[0]["provider_resolved_model"]},
            )
        trial.finalize_trial(self.manifest, results, ingest_brain=True, now=2001)
        with feedback._conn() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0], 6
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM profile_trial_ingests").fetchone()[0], 1
            )

    def test_sixth_outcome_failure_rolls_back_every_instance(self):
        original = feedback._record_outcome_in_conn
        calls = 0

        def fail_on_sixth(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 6:
                raise RuntimeError("sixth outcome failed")
            return original(*args, **kwargs)

        with patch.object(feedback, "_record_outcome_in_conn", side_effect=fail_on_sixth):
            with self.assertRaisesRegex(RuntimeError, "sixth outcome"):
                trial.finalize_trial(self.manifest, self.results(), ingest_brain=True)
        with feedback._conn() as conn:
            for table in ("runs", "execution_attempts", "outcomes", "profile_trial_ingests"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_missing_duplicate_and_misjoined_instance_are_rejected(self):
        for mutation in ("missing", "duplicate", "wrong-profile"):
            with self.subTest(mutation=mutation):
                results = self.results()
                if mutation == "missing":
                    results["attempts"].pop()
                elif mutation == "duplicate":
                    results["attempts"][-1] = copy.deepcopy(results["attempts"][0])
                else:
                    attempt = results["attempts"][0]
                    attempt["profile_id"] = next(
                        p for p in self.profiles if p != attempt["profile_id"]
                    )
                with self.assertRaisesRegex(ValueError, "per instance|join mismatch"):
                    trial.finalize_trial(self.manifest, results, ingest_brain=True)
        self.assertFalse(trial._capability_program_summary_path().exists())

    def test_instance_identity_is_bound_to_layout_and_seed(self):
        trial.validate_trial_manifest(self.manifest)
        replay = trial.build_trial_manifest(
            *self.sources,
            seed=14,
            now=1000,
            capacity_state="ok",
            profile_ids=self.profiles,
            instances_per_profile=3,
            packet=self.manifest["frozen_packet"],
        )
        self.assertEqual(self.manifest, replay)
        single = trial.build_trial_manifest(
            *self.sources, seed=14, now=1000, capacity_state="ok", profile_ids=self.profiles
        )
        self.assertNotEqual(self.manifest["trial_id"], single["trial_id"])
        for change in ("run_id", "profile_id"):
            altered = copy.deepcopy(self.manifest)
            altered["requests"][0][change] = altered["requests"][1][change]
            if change == "profile_id" and altered == self.manifest:
                altered["requests"][0][change] = next(
                    p for p in self.profiles if p != altered["requests"][0][change]
                )
            with self.assertRaises(ValueError):
                trial.validate_trial_manifest(altered)

    def test_invalid_layouts_are_rejected(self):
        layouts = (
            (self.profiles, 0),
            (self.profiles, 4),
            (self.profiles, True),
            ((self.profiles[0],) * 2, 3),
            (("unknown", self.profiles[0]), 3),
        )
        for profiles, count in layouts:
            with self.subTest(profiles=profiles, count=count):
                with self.assertRaises(ValueError):
                    trial.build_trial_manifest(
                        *self.sources, profile_ids=profiles, instances_per_profile=count
                    )

    def test_prepare_cli_reuses_frozen_packet_for_six_requests(self):
        packet = self.root / "frozen-packet.json"
        packet.write_text(json.dumps(self.manifest["frozen_packet"]))
        output = self.root / "prepared.json"
        with patch("sys.stdout", new_callable=io.StringIO):
            status = trial.main(
                [
                    "prepare",
                    "--orchestrator-root",
                    str(self.sources[0]),
                    "--workflows-root",
                    str(self.sources[1]),
                    "--output",
                    str(output),
                    "--packet",
                    str(packet),
                    "--profile-id",
                    self.profiles[0],
                    "--profile-id",
                    self.profiles[1],
                    "--instances-per-profile",
                    "3",
                    "--capacity-state",
                    "ok",
                ]
            )
        self.assertEqual(status, 0)
        prepared = json.loads(output.read_text())
        trial.validate_trial_manifest(prepared)
        self.assertEqual(prepared["frozen_packet"], self.manifest["frozen_packet"])
        self.assertEqual(len(prepared["requests"]), 6)

    def test_six_instance_ingest_still_requires_verified_identity(self):
        results = self.results()
        results["identity_verified"] = False
        with self.assertRaisesRegex(ValueError, "identity is unverified"):
            trial.finalize_trial(self.manifest, results, ingest_brain=True)
        self.assertFalse(trial._capability_program_summary_path().exists())

    def test_bridge_reserves_six_instances_and_keeps_artifacts_separate(self):
        reservation = bridge._live_capacity_reservation(
            self.manifest, {"generated_at": 1000, "agents": {"codex": {"state": "ok"}}}
        )
        self.assertEqual(reservation["units"], 6)
        self.assertEqual(set(reservation["profiles"]), set(self.profiles))
        envelope = bridge.build_request_envelope(
            self.manifest,
            artifact_root=self.root / "artifacts",
            transport="remote",
            preflight_result={"ready": True, "capacity_reservation": reservation},
        )
        bridge.validate_envelope(envelope, self.manifest)
        self.assertEqual(len({row["artifact_dir"] for row in envelope["requests"]}), 6)
        self.assertEqual(len({row["request_id"] for row in envelope["requests"]}), 6)
        malformed = copy.deepcopy(envelope)
        malformed["requests"][-1] = malformed["requests"][0]
        malformed.pop("envelope_hash")
        malformed["envelope_hash"] = bridge._hash(malformed)
        with self.assertRaisesRegex(ValueError, "per instance"):
            bridge.validate_envelope(malformed, self.manifest)
        with patch.object(bridge, "collect_remote_attempt", return_value={}) as collect:
            bridge.collect_remote_results(
                self.manifest,
                envelope,
                list(range(1000, 1006)),
                artifact_root=self.root / "artifacts",
            )
            self.assertEqual(collect.call_count, 6)
            with self.assertRaises(ValueError):
                bridge.collect_remote_results(
                    self.manifest, envelope, [1000] * 6, artifact_root=self.root / "artifacts"
                )

    def test_remote_collection_retains_each_instance_artifact_and_digest(self):
        artifact_root = self.root / "artifacts"
        reservation = bridge._live_capacity_reservation(
            self.manifest, {"generated_at": 1000, "agents": {"codex": {"state": "ok"}}}
        )
        source_sha = "3" * 40
        envelope = bridge.build_request_envelope(
            self.manifest,
            artifact_root=artifact_root,
            transport="remote",
            preflight_result={
                "ready": True,
                "capacity_reservation": reservation,
                "workflows_source_sha": source_sha,
            },
        )
        responses = {}
        archives = {}
        run_ids = []
        for request in envelope["requests"]:
            run_id = 1000 + request["launch_ordinal"]
            run_ids.append(run_id)
            artifact_name = (
                f"model-profile-trial-{request['profile_id']}-{run_id}-"
                f"1-{request['launch_ordinal']}"
            )
            artifact = {
                "request_id": request["request_id"],
                "run_id": request["run_id"],
                "profile_id": request["profile_id"],
                "github_repository": bridge.REMOTE_REPOSITORY,
                "github_workflow_ref": bridge.REMOTE_WORKFLOW_REF,
                "github_workflow_sha": source_sha,
                "github_run_id": run_id,
                "github_run_attempt": 1,
                "artifact_name": artifact_name,
            }
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("model-profile-trial-attempt.json", json.dumps(artifact))
            archives[run_id] = buffer.getvalue()
            endpoint = f"repos/{bridge.REMOTE_REPOSITORY}/actions/runs/{run_id}"
            responses[endpoint] = {
                "id": run_id,
                "run_attempt": 1,
                "event": "workflow_dispatch",
                "head_branch": "main",
                "head_sha": source_sha,
                "path": bridge.REMOTE_WORKFLOW_PATH,
                "status": "completed",
                "conclusion": "success",
            }
            responses[endpoint + "/artifacts?per_page=100"] = {
                "artifacts": [
                    {
                        "id": run_id,
                        "name": artifact_name,
                        "digest": "sha256:" + hashlib.sha256(archives[run_id]).hexdigest(),
                        "workflow_run": {"head_sha": source_sha},
                    }
                ]
            }
        with (
            patch.object(bridge, "_gh_json", side_effect=responses.__getitem__),
            patch.object(bridge, "_gh_download_artifact", side_effect=archives.__getitem__),
        ):
            results = bridge.collect_remote_results(
                self.manifest, envelope, run_ids, artifact_root=artifact_root
            )
        attempts = results["attempts"]
        self.assertEqual(len({a["artifact_ref"] for a in attempts}), 6)
        for request, attempt in zip(envelope["requests"], attempts):
            path = Path(attempt["artifact_ref"])
            self.assertEqual(path.parent, Path(request["artifact_dir"]))
            self.assertEqual(json.loads(path.read_bytes())["run_id"], request["run_id"])
            self.assertEqual(
                attempt["artifact_sha256"], "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            )


if __name__ == "__main__":
    unittest.main()
