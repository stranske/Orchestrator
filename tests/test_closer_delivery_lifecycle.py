"""Offline synthetic delivery lifecycles through the production staged/native queues.

Only provider, GitHub, capacity, claim and subprocess transport boundaries are faked.
No live target, eligibility, acceptance validator or queue implementation is replaced.
"""

from __future__ import annotations

import copy
import io
import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from closer_inert_dispatcher_support import InertCaller
from test_closer_disposition import BODY, HEAD, TARGET, fixture

import claims
import closer_dispatcher as adapter
import closer_pipeline
import dispatcher
import verifier_evidence

_SOURCE_LOW_ROUTE = adapter._pipeline_low_route
_SOURCE_EXCEPTION_ROUTE = adapter._exception_route
_SOURCE_CAPACITY_REPORT = adapter._fresh_capacity_report


class OfflineDelivery:
    def __init__(self, tmp_path, monkeypatch, *, native=False):
        self.root = tmp_path
        self.native = native
        self.patch = monkeypatch
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

        if native:
            self._enable_native()

    def _enable_native(self):
        """Opt in only these lifecycle tests; imported pure units keep their defaults."""
        from closer_native_closure_fixture import NativeClosureFixture
        from test_dispatcher_controlled_data_only import world

        self.patch.setenv("HOME", str(self.root / "private-home"))
        self.caller = InertCaller(world.__wrapped__(self.root, self.patch), self.patch)
        self.caller.seed_capacity()
        self.patch.setattr(adapter, "_pipeline_low_route", _SOURCE_LOW_ROUTE)
        self.patch.setattr(adapter, "_exception_route", _SOURCE_EXCEPTION_ROUTE)
        self.patch.setattr(adapter, "_fresh_capacity_report", _SOURCE_CAPACITY_REPORT)
        self.native_closure = NativeClosureFixture(
            self.root / "native-closure",
            self,
            self.caller,
            self.patch,
            source=Path(adapter.__file__).parent,
        )
        original_frontier = self.caller.frontier

        def observed_frontier(snapshots, backend=None):
            from closer_frontier_policy import make_frontier

            observed = original_frontier(snapshots, backend)
            return make_frontier(
                list(snapshots),
                owner=observed["owner"],
                frontier_id="lifecycle-" + adapter.digest(observed["bindings"])[:24],
                bindings=observed["bindings"],
            )

        self.caller.frontier = observed_frontier

    def sync_native_closure(self):
        for path in self.native_closure.files:
            if path not in getattr(self, "synced_closures", set()) and path.is_file():
                observed = json.loads(path.read_text())
                if observed["issue"]["state"] == "closed":
                    self.source_issue = observed["issue"]
                    self.current["sources"][0]["state"] = "closed"
                    self.api_calls.extend(tuple(call) for call in observed["calls"])
                    self.synced_closures = getattr(self, "synced_closures", set()) | {path}
                    self.refresh()

    @staticmethod
    def forbidden(*a, **kw):
        raise AssertionError("live subprocess is forbidden in this offline lifecycle")

    def refresh(self):
        self.current["revision"] = adapter.digest(
            {k: v for k, v in self.current.items() if k != "revision"}
        )

    def snapshot(self, target):
        if self.native:
            self.sync_native_closure()
        assert target == TARGET
        return copy.deepcopy(self.current)

    def api(self, endpoint, *, method="GET", fields=None, **kw):
        self.api_calls.append((endpoint, method, fields))
        if (
            endpoint
            == "repos/owner/repo/contents/.github/workflows/autofix-versions.env?ref=" + HEAD
            and method == "GET"
        ):
            # This acceptance fixture has no mechanical tool-policy source.
            # The actual collector must retain UNMET, never invent a rail.
            return {"message": "Not Found"}
        if endpoint == "repos/owner/repo" and method == "GET":
            return {"default_branch": "main"}
        if endpoint == "repos/owner/repo/actions/workflows/agents-verifier.yml/dispatches":
            assert method == "POST"
            assert self.current["state"] == "MERGED"
            assert fields == {
                "ref": "main",
                "inputs[pr_number]": "12",
                "inputs[mode]": "compare",
                "inputs[verification_request_basis]": self.backend.verification_request_basis(
                    self.current
                ),
            }
            return {"accepted": True}
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
        raise AssertionError((endpoint, method, fields))

    def verification_dispatches(self):
        return [
            call
            for call in self.api_calls
            if len(call) == 3
            and call[0] == "repos/owner/repo/actions/workflows/agents-verifier.yml/dispatches"
            and call[1] == "POST"
        ]

    def offload(self, agent, prompt, **kw):
        from closer_typed_semantic_broker_fixture import emit_assessment

        assert agent == ("claude" if self.native else "gemini")
        self.provider_calls.append(prompt)
        if self.transport_uncertain:
            result = {
                "exit": 1,
                "run_id": "offline:uncertain",
                "outcome_uncertain": True,
                "outcome_status": "uncertain",
                "retry_safe": False,
            }
        else:
            # self.proposal is declared synthetic source-case gold, never an
            # actual provider answer. Emit NEW typed data before caller.response
            # seals it. The host owns all identity/proof metadata after parsing.
            gold = copy.deepcopy(self.proposal["decision"]["criteria"])
            length = getattr(self, "semantic_text_length", 0)
            if length:
                for row in gold:
                    for proof in row["proofs"]:
                        proof["relevance"] = (
                            proof["relevance"] + " Synthetic declared case evidence." * 80
                        )[:length]
            assessment = emit_assessment(
                prompt,
                gold,
                unknown_slots=getattr(self, "semantic_unknown_slots", ()),
                global_explanations=self.proposal.get("substantive_needs", []),
                finding_gold=self.proposal.get("resolutions", ()),
                explanation=(
                    ("Synthetic declared source-case assessment. " * 80)[:length]
                    if length
                    else None
                ),
            )
            if self.invalid_proof:
                assessment["obligations"][0]["citations"][0]["ref"] = 999999
            result = {
                "exit": 0,
                "run_id": f"offline:low:{len(self.provider_calls)}",
                "output": json.dumps(assessment),
                "profile_id": "offline-low",
            }
        if self.native:
            self.last_actual = self.caller.response(result, agent, prompt, **kw)
            return self.last_actual
        return result

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
        if self.native and self.current["state"] != "OPEN":
            assert blocker_fn is None
            return self.run_native()
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

    def run_native(self):
        """Finite collection first; only an actual validated result may select a successor."""
        import closer_disposition
        import closer_evidence

        dispositions = self.root / "actual-native-dispositions.json"
        saved = json.loads(dispositions.read_text()) if dispositions.exists() else {}
        self.backend = adapter.Closer(
            self,
            state_path=self.root / "queue.json",
            apply=True,
            workers=True,
            dispositions=saved,
        )
        args = SimpleNamespace(state=str(self.root / "queue.json"), max_workers=1, shadow=False)
        accepted = []
        original_integrate = closer_disposition.integrate_ordinary

        def observe_integration(*values, **options):
            actual = original_integrate(*values, **options)
            if actual["status"] == "accepted":
                accepted.append(copy.deepcopy(actual["disposition"]))
            return actual

        def collect(snapshot, api, destination, **options):
            packet, _ref, proposal = self.caller.packet(snapshot, destination)
            self.proposal = proposal
            return packet

        with self.patch.context() as local:
            local.setattr(adapter, "GitHub", lambda **_kw: self)
            local.setattr(closer_evidence, "collect_evidence", collect)
            local.setattr(closer_disposition, "integrate_ordinary", observe_integration)
            receipt = self.caller.staged(
                args,
                [TARGET],
                self.backend,
                time.time() + 330,
                coordinator=True,
                snapshot_fn=self.snapshot,
            )
            first = receipt["targets"][TARGET]
            if first.get("deferred_reason") == "finite_frontier_identity_mismatch" and accepted:
                actual = accepted[-1]
                if adapter.acceptance_ok(self.current, actual):
                    # Persist only the unchanged actual consumer result. A new
                    # observed frontier, never predicted ACT, owns closure.
                    dispositions.write_text(json.dumps({TARGET: actual}, sort_keys=True))
                    self.backend.dispositions = {TARGET: actual}
                    before = len(self.caller.results)
                    self.initial_native_receipt = copy.deepcopy(receipt)
                    receipt = self.caller.staged(
                        args,
                        [TARGET],
                        self.backend,
                        time.time() + 330,
                        coordinator=True,
                        snapshot_fn=self.snapshot,
                    )
                    assert len(self.caller.results) == before
                    assert (
                        receipt["finite_frontier"]["frontier_id"]
                        != self.initial_native_receipt["finite_frontier"]["frontier_id"]
                    )
        self.sync_native_closure()
        # Count only the original actor output preserved by the real native
        # queue, never infer a successful actor result from a PATCH observation.
        for path in self.root.rglob("*.json"):
            value = json.loads(path.read_text())
            if not isinstance(value, dict) or value.get("schema") != "closer-queue/v1":
                continue
            for bucket in [value, *(value.get("frontiers") or {}).values()]:
                item = bucket.get("items", {}).get(TARGET, {})
                operation = item.get("operation") or {}
                actor = operation.get("result", {}).get("actor")
                if operation.get("status") == "completed" and actor is not None:
                    seen = getattr(self, "seen_closure_operations", set())
                    if operation["id"] not in seen:
                        self.closure_calls.append(copy.deepcopy(actor))
                        self.seen_closure_operations = seen | {operation["id"]}
        return receipt

    def state(self):
        if self.native:
            files = list(self.root.glob("queue.json.pipeline.json.scoped/targets/*/state.json"))
            assert len(files) == 1
            return json.loads(files[0].read_text())
        return json.loads((self.root / "queue.json.pipeline.json").read_text())


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
    dispatches = delivery.verification_dispatches()
    assert len(dispatches) == 1, result
    assert dispatches[0][2]["inputs[verification_request_basis]"] == (
        delivery.backend.verification_request_basis(delivery.current)
    )
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
