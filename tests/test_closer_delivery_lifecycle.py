"""Offline synthetic delivery lifecycles through the production staged/native queues.

Only provider, GitHub, capacity, claim and subprocess transport boundaries are faked.
No live target, eligibility, acceptance validator or queue implementation is replaced.
"""

from __future__ import annotations

import copy
import io
import json
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_closer_disposition import BODY, HEAD, TARGET, fixture

import claims
import closer_dispatcher as adapter
import closer_pipeline
import dispatcher
import verifier_evidence


class OfflineDelivery:
    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path
        self.guard_calls = []
        self.provider_calls = []
        self.api_calls = []
        self.closure_calls = []
        self.transport_uncertain = False
        self.invalid_proof = False
        snap, _, _, _, interpretation = fixture(tmp_path / "seed")
        self.proposal = interpretation["proposal"]
        self.current = dict(
            snap,
            state="OPEN",
            draft=False,
            active_threads=0,
            mergeable="MERGEABLE",
            labels=[],
            verifier=None,
            verifier_history_complete=True,
            head_branch="codex/issue-2-offline",
            head_repository="owner/repo",
        )
        self.refresh()
        self.deadline = time.time() + 600
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(
                "junit.xml", '<testsuite tests="1"><testcase name="Ship the report"/></testsuite>'
            )
        self.archive = stream.getvalue()
        monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))

        # These lifecycle tests simulate routing. Real profile/pool execution is
        # covered separately through test_closer_dispatcher_scoped_integration.
        def semantic_route(route, invoke, *, load, **_kwargs):
            try:
                return invoke(route), route
            finally:
                adapter._release_route_load(route, load)

        monkeypatch.setattr(adapter, "_execute_profile_route", semantic_route)
        monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "brain-state"))
        monkeypatch.setenv("ORCH_STORAGE_STATE_DIR", str(tmp_path / "storage-state"))
        monkeypatch.setattr(claims, "claim", lambda *a, **kw: True)
        monkeypatch.setattr(claims, "release", lambda *a, **kw: True)
        monkeypatch.setattr(claims, "update_metadata", lambda *a, **kw: True)
        monkeypatch.setattr(claims, "holder", lambda *a, **kw: None)
        monkeypatch.setattr(adapter, "GitHub", lambda **kw: self)
        monkeypatch.setattr(
            adapter, "_pipeline_capacity", lambda: {"io": 2, "low": 1, "exception": 0}
        )
        route = {"agent": "gemini", "profile_id": "offline-low", "pools": []}
        monkeypatch.setattr(
            adapter,
            "_pipeline_low_route",
            lambda **kw: (route, "available") if kw.get("with_status") else route,
        )
        monkeypatch.setattr(adapter.gh_capacity, "throttle", lambda *a, **kw: {"action": "allow"})
        monkeypatch.setattr(
            adapter.gh_capacity, "gh_run", lambda *a, **kw: (0, self.archive.decode("latin-1"))
        )
        monkeypatch.setattr(dispatcher, "offload", self.offload)
        monkeypatch.setattr(adapter.subprocess, "Popen", self.forbidden)
        monkeypatch.setattr(adapter.subprocess, "run", self.forbidden)
        self.backend = adapter.Closer(
            self, state_path=tmp_path / "queue.json", apply=True, workers=True, run_fn=self.guard
        )
        # Campaign admission is an external budget/authorization service. Synthetic
        # fixtures have a bounded offline permit; production evidence checks stay real.
        monkeypatch.setattr(
            self.backend, "campaign_admission", lambda *a, **kw: {"allowed": True, "timeout": 60}
        )

    @staticmethod
    def forbidden(*a, **kw):
        raise AssertionError("live subprocess is forbidden in this offline lifecycle")

    def refresh(self):
        self.current["revision"] = adapter.digest(
            {k: v for k, v in self.current.items() if k != "revision"}
        )

    def snapshot(self, target):
        assert target == TARGET
        return copy.deepcopy(self.current)

    def api(self, endpoint, *, method="GET", fields=None, **kw):
        self.api_calls.append((endpoint, method, fields))
        if endpoint == "graphql":
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}}
                        }
                    }
                }
            }
        if "/compare/" in endpoint:
            return {
                "status": "ahead",
                "files": [
                    {
                        "filename": "report.py",
                        "status": "added",
                        "patch": "+def ship_report(): pass",
                        "sha": "c" * 40,
                    }
                ],
            }
        if endpoint.endswith("/runs/7"):
            return {
                "id": 7,
                "run_attempt": 2,
                "head_sha": HEAD,
                "status": "completed",
                "conclusion": "success",
                "pull_requests": [{"number": 12}],
            }
        if endpoint.endswith("/runs/7/artifacts?per_page=100"):
            return {
                "artifacts": [
                    {"id": 9, "name": "tests", "size_in_bytes": len(self.archive), "expired": False}
                ]
            }
        if "/actions/workflows/agents-verifier.yml/runs" in endpoint:
            return {"workflow_runs": [], "total_count": 0}
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": [{"id": 7, "head_sha": HEAD}]}
        if endpoint.endswith("/issues/12/labels") and method == "POST":
            assert fields == {"labels[]": "verify:compare"}
            self.current["labels"] = ["verify:compare"]
            self.current["label_events_complete"] = True
            self.current["label_events"] = [
                {
                    "id": "offline-label-event-1",
                    "label": "verify:compare",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
            self.refresh()
            return []
        raise AssertionError((endpoint, method, fields))

    def offload(self, agent, prompt, **kw):
        assert agent == "gemini"
        self.provider_calls.append(prompt)
        proposal = copy.deepcopy(self.proposal)
        mandatory = json.JSONDecoder().raw_decode(prompt[prompt.index('{"mandatory":') :])[0][
            "mandatory"
        ]
        packet = json.loads(Path(mandatory["packet_ref"]["path"]).read_text())
        raw = packet["sections"]["ci_artifacts"]["content"]["artifacts"][0]["receipts"][0]["raw"]
        for row in proposal["decision"]["criteria"]:
            row["evidence"] = [raw]
        proposal.update(
            drift_review_complete=True,
            drift_mismatches=[],
            drift_summary="Offline raw fixture examined",
        )
        if self.invalid_proof:
            proposal["decision"]["criteria"][0]["proofs"][0]["run_attempt"] = 999
        if self.transport_uncertain:
            return {
                "exit": 1,
                "run_id": "offline:uncertain",
                "outcome_uncertain": True,
                "outcome_status": "uncertain",
                "retry_safe": False,
            }
        return {
            "exit": 0,
            "run_id": f"offline:low:{len(self.provider_calls)}",
            "output": json.dumps(proposal),
            "profile_id": "offline-low",
        }

    def guard(self, command, *, timeout):
        if command[1] == "-c":
            assert command[-1] == TARGET
            import keepalive_supervisor

            result = keepalive_supervisor.close_verified_issues(**self.closure_options)
            self.closure_calls.append(result)
            self.current["sources"][0]["state"] = self.source_issue["state"]
            self.refresh()
            return SimpleNamespace(returncode=0, stdout=json.dumps(result))
        assert command[1].endswith("merge_guard.py")
        assert command[2] == TARGET
        assert command[2:6] == [TARGET, "--expected-head", HEAD, "--confirm-merge"]
        assert set(command[6:]) == {"--skip-immediate-advisory", "--remote-only"}
        assert 0 < timeout <= 600
        self.guard_calls.append(command)
        self.current.update(state="MERGED", merge="d" * 40, merged_at="2026-10-18T00:00:00Z")
        self.refresh()
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"merge_executed": True, "blocked": False})
        )

    def supply_native_pass(self, monkeypatch):
        """Synthetic trusted GitHub verifier response, parsed by the native validator."""
        import backlog
        import capabilities
        import feedback
        import keepalive_supervisor

        monkeypatch.setenv("ORCH_SUPERVISOR_CLOSE", "1")
        monkeypatch.setattr(backlog, "SUPPORTED_REPOS", ["owner/repo"])
        ledger = self.root / "capabilities.json"
        capability = capabilities._blank_capability("live-keepalive-supervisor")
        capability.update(status="wired", capability_version_id="cv:offline-supervisor-close")
        capabilities.save({"live-keepalive-supervisor": capability}, ledger)
        monkeypatch.setattr(capabilities, "REG", ledger)
        monkeypatch.setattr(feedback, "DB_PATH", self.root / "brain.db")
        # Heartbeat is a separate external scheduler; outcome recording stays real.
        monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **kw: None)
        self.brain = feedback.DB_PATH
        decision = {
            "schema": verifier_evidence.MARKER,
            "repo": "owner/repo",
            "pr": 12,
            "head_sha": HEAD,
            "evaluated_sha": self.current["merge"],
            "run_id": "34",
            "run_attempt": "1",
            "provider_verdicts": ["PASS"],
            "verdict": "PASS",
            "ci_failed": False,
        }
        pr = {
            "number": 12,
            "state": "MERGED",
            "headRefOid": HEAD,
            "headRefName": "codex/issue-2-offline",
            "mergeCommit": {"oid": self.current["merge"]},
            "labels": {"nodes": [{"name": "agent:codex"}]},
            "closingIssuesReferences": {
                "nodes": [{"number": 2, "repository": {"nameWithOwner": "owner/repo"}}]
            },
            "comments": {
                "nodes": [
                    {
                        "author": {"login": "github-actions[bot]"},
                        "url": "https://github.com/owner/repo/pull/12#issuecomment-34",
                        "body": "<!-- "
                        + verifier_evidence.MARKER
                        + " "
                        + json.dumps(decision)
                        + " -->",
                    }
                ]
            },
        }
        self.current["verifier"] = verifier_evidence.decision_from_pr("owner/repo", pr)
        assert self.current["verifier"]["verdict"] == "PASS"
        self.refresh()
        self.source_issue = {
            "number": 2,
            "body": BODY,
            "state": "open",
            "labels": [],
            "updated_at": "2026-10-18T00:00:00Z",
        }

        def gh(args):
            self.api_calls.append(tuple(args))
            if args[1].endswith("/comments"):
                if "-f" not in args:
                    return [[]]
                pr["comments"]["nodes"].append(
                    {"body": args[-1].removeprefix("body="), "author": {"login": "offline-owner"}}
                )
                return {}
            assert args[1] == "repos/owner/repo/issues/2"
            if "PATCH" in args:
                self.source_issue["state"] = "closed"
            return copy.deepcopy(self.source_issue)

        self.closure_options = dict(
            targets=[TARGET],
            env={
                keepalive_supervisor.CLOSE_FLAG: "1",
                "ORCH_STATE_DIR": str(self.root / "supervisor"),
            },
            gh=gh,
            pr_read=lambda target: copy.deepcopy(pr),
            ci_green=lambda *a: pytest.fail("native PASS must not buy another CI run"),
            gate=lambda *a: pytest.fail("native PASS must not buy another acceptance run"),
            holds=lambda: set(),
        )

    def run(self, *, blocker_fn=None):
        self.deadline = time.time() + 600
        # A process restart has no injected coordinator decisions or live objects.
        # Only production pipeline/inner ledgers and the external snapshot persist.
        self.backend = adapter.Closer(
            self,
            state_path=self.root / "queue.json",
            apply=True,
            workers=True,
            run_fn=self.backend.run_fn,
            blocker_fn=blocker_fn,
        )
        self.backend.campaign_admission = lambda *a, **kw: {"allowed": True, "timeout": 60}
        args = SimpleNamespace(state=str(self.root / "queue.json"), max_workers=1, shadow=False)
        return adapter._run_staged_pipeline(args, [TARGET], self.backend, time.time() + 330)

    def state(self):
        return json.loads((self.root / "queue.json.pipeline.json").read_text())


def test_positive_low_evidence_to_exact_head_merge_and_native_verifier_wait(tmp_path, monkeypatch):
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    result = delivery.run()
    assert len(delivery.guard_calls) == 1, result
    assert not delivery.provider_calls  # Objective premerge path never calls a model.
    assert delivery.current["state"] == "MERGED"
    assert "verify:compare" in delivery.current["labels"]
    assert closer_pipeline.validate_stop(
        tmp_path / "queue.json.pipeline.json", assignment_targets=[TARGET]
    )["valid"], result

    # A fresh cadence observes the native merge-bound PASS; it must reassess the
    # changed packet, close through the existing actor, and settle real Brain data.
    delivery.supply_native_pass(monkeypatch)
    completed = delivery.run()
    assert completed["targets"][TARGET]["status"] == "completed", completed
    assert len(delivery.guard_calls) == 1
    assert len(delivery.closure_calls) == 1
    assert delivery.closure_calls[0]["closed"] == 1
    assert delivery.closure_calls[0]["errors"] == 0
    operations = list((tmp_path / "supervisor" / "supervisor-close-operations").glob("*.json"))
    assert len(operations) == 1
    operation = json.loads(operations[0].read_text())
    assert operation["phase"] == "settled"
    assert operation["merge_sha"] == "d" * 40
    with sqlite3.connect(delivery.brain) as connection:
        assert connection.execute(
            "select run_id, adjudicated_verdict, durability from outcomes"
        ).fetchall() == [(operation["run_id"], "PASS", "durable")]
    # Closure changes the source state; a fresh packet/assessment may be needed.
    restarted = delivery.run()
    assert restarted["targets"][TARGET]["status"] == "completed", restarted
    assert len(delivery.guard_calls) == len(delivery.closure_calls) == 1
    provider_calls = len(delivery.provider_calls)
    settled = delivery.run()
    assert settled["targets"][TARGET]["status"] == "completed", settled
    # Reassigning a completed target may legitimately re-enter bounded freshness/
    # drift review; it cannot create a second terminal effect or learning outcome.
    assert len(delivery.provider_calls) <= provider_calls + 1
    assert len(delivery.guard_calls) == len(delivery.closure_calls) == 1
    label_posts = [
        call
        for call in delivery.api_calls
        if len(call) == 3 and call[0].endswith("/issues/12/labels") and call[1] == "POST"
    ]
    source_patches = [call for call in delivery.api_calls if "PATCH" in call]
    assert len(label_posts) == len(source_patches) == 1
    assert delivery.current["verifier"]["verdict"] == "PASS"
    assert delivery.current["sources"][0]["state"] == "closed"
    assert json.loads(operations[0].read_text())["phase"] == "settled"
    with sqlite3.connect(delivery.brain) as connection:
        assert connection.execute("select count(*) from outcomes").fetchone()[0] == 1
    assert closer_pipeline.validate_stop(
        tmp_path / "queue.json.pipeline.json", assignment_targets=[TARGET]
    )["valid"]


@pytest.mark.parametrize("barrier", ["wrong_attempt", "changed_head", "provider_uncertain"])
def test_positive_lifecycle_keeps_observed_evidence_barriers(tmp_path, monkeypatch, barrier):
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    delivery.current.update(state="MERGED", merge="d" * 40, merged_at="2026-10-18T00:00:00Z")
    delivery.supply_native_pass(monkeypatch)
    if barrier == "wrong_attempt":
        delivery.invalid_proof = True
    elif barrier == "provider_uncertain":
        delivery.transport_uncertain = True
    else:
        offload = delivery.offload

        def head_changes_after_assessment(*args, **kwargs):
            result = offload(*args, **kwargs)
            delivery.current["head"] = "e" * 40
            delivery.refresh()
            return result

        monkeypatch.setattr(dispatcher, "offload", head_changes_after_assessment)
    result = delivery.run()
    assert not delivery.guard_calls, result
    assert delivery.current["state"] == "MERGED"
    assert not delivery.closure_calls
    state = delivery.state()
    serialized = json.dumps(state)
    if barrier == "wrong_attempt":
        assert "criterion_proof_unbound_or_unrelated" in serialized
    elif barrier == "provider_uncertain":
        assert "offline:uncertain" in serialized
        assert "provider_outcome_uncertain" in serialized
        assert len(delivery.provider_calls) == 1
        delivery.run()
        assert len(delivery.provider_calls) == 1
        assert not delivery.guard_calls
        assert "offline:uncertain" in json.dumps(delivery.state())
    else:
        assert delivery.current["head"] == "e" * 40
        inner_paths = list(tmp_path.glob("queue.json.pipeline.actions/*.json"))
        assert len(inner_paths) == 1
        inner = json.loads(inner_paths[0].read_text())
        assert inner["items"][TARGET]["evidence"]["head"] == "e" * 40
        assert inner["items"][TARGET]["action"] != "close_source"


def test_unknown_merge_transport_outcome_is_durable_and_never_repeated(tmp_path, monkeypatch):
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    uncertain_calls = []

    def guard_unknown(command, *, timeout):
        assert "--expected-head" in command
        uncertain_calls.append(command)
        raise TimeoutError("offline transport lost outcome; no remote conclusion available")

    delivery.backend.run_fn = guard_unknown
    first = delivery.run()
    assert len(uncertain_calls) == 1, first
    assert not delivery.guard_calls
    state_before = delivery.state()
    assert "uncertain" in json.dumps(state_before)
    delivery.run()
    assert len(uncertain_calls) == 1
    assert delivery.current["state"] == "OPEN"
    state_after = delivery.state()
    assert "uncertain" in json.dumps(state_after)


def test_pending_current_head_ci_waits_then_resumes_positive_merge(tmp_path, monkeypatch):
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    delivery.current.update(checks="PENDING", pending_check_head=HEAD, pending_check_run_ids=[77])
    delivery.refresh()
    waiting = delivery.run()
    assert waiting["targets"][TARGET]["status"] == "pending", waiting
    assert not delivery.provider_calls
    assert not delivery.guard_calls
    assert closer_pipeline.validate_stop(
        tmp_path / "queue.json.pipeline.json", assignment_targets=[TARGET]
    )["valid"]
    delivery.current.update(checks="SUCCESS", pending_check_run_ids=[])
    delivery.refresh()
    delivery.run()
    assert len(delivery.guard_calls) == 1
    assert delivery.current["state"] == "MERGED"


@pytest.mark.parametrize("label", ["agents:paused", "agents:auto-pilot-pause"])
@pytest.mark.parametrize("where", ["pr", "source"])
def test_explicit_pause_blocks_positive_evidence_without_provider_or_action(
    tmp_path, monkeypatch, label, where
):
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    if where == "pr":
        delivery.current["labels"] = [label]
    else:
        delivery.current["sources"][0]["labels"] = [label]
    delivery.refresh()
    result = delivery.run()
    assert not delivery.guard_calls
    assert not delivery.provider_calls
    assert delivery.current["sources"][0]["state"] == "open"
    assert label in json.dumps(result)


def test_generic_needs_human_label_preserves_objective_merge_and_verifier_successor(
    tmp_path, monkeypatch
):
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    delivery.current["labels"] = ["needs-human"]
    delivery.refresh()
    result = delivery.run()
    assert len(delivery.guard_calls) == 1, result
    assert not delivery.provider_calls
    assert delivery.current["state"] == "MERGED"
    assert "verify:compare" in delivery.current["labels"]
    assert delivery.current["sources"][0]["state"] == "open"


def test_confirmed_human_owner_blocker_still_prevents_positive_delivery(tmp_path, monkeypatch):
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    delivery.current["labels"] = ["needs-human"]
    delivery.refresh()
    result = delivery.run(
        blocker_fn=lambda: {TARGET: {"reason": "owner confirmed required human input"}}
    )
    assert not delivery.guard_calls
    assert not delivery.provider_calls
    assert delivery.current["sources"][0]["state"] == "open"
    assert "scoped owner blocker " + TARGET in json.dumps(result)
