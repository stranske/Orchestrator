"""Offline regressions for source-only model-admission gaps."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_closer_dispatcher as runtime_fixture
import test_closer_disposition as proof_fixture

import closer_dispatcher as adapter
import closer_evidence as evidence
import closer_pipeline


@pytest.fixture(autouse=True)
def private_boundaries(tmp_path, monkeypatch):
    runtime_fixture.no_live_state_or_provider.__wrapped__(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCH_CAPABILITY_LEDGER_PATH", str(tmp_path / "capabilities.json"))
    monkeypatch.setenv("ORCH_BRAIN_ROOT", str(tmp_path / "brain"))


def source_only(tmp_path, *, lookup_failure=True, intake=False, ambiguous=False):
    # Actual-shaped, synthetic identities; never fabricate live-target qualification.
    body = (
        "## Tasks\n- [ ] Select primary by metadata\n## Acceptance Criteria\n"
        '- [ ] pytest tests/cli/test_ingest_entrypoint.py -k "primary_only or permutation" passes'
        if intake
        else "## Tasks\n- [ ] Extract tracked variables from segment text\n"
        "## Acceptance Criteria\n- [ ] Changed-fee regression fails when extraction is hardcoded; "
        "restore and pass"
    )
    snap = {
        "target": "offline/example#111",
        "kind": "issue",
        "state": "OPEN",
        "sources": [{"target": "offline/example#111", "body": body, "state": "open"}],
        "body": body,
        "revision": "source-revision",
    }

    if ambiguous:
        snap["delivery_links"] = {
            "status": "ambiguous",
            "source": "closedByPullRequestsReferences",
            "complete": True,
            "candidates": ["offline/example#128", "offline/example#142"],
        }

    def api(*_args, **_kwargs):
        pytest.fail("missing head must not invent a CI or diff lookup")

    def reviews(*_args, **_kwargs):
        if lookup_failure:
            raise RuntimeError("synthetic PR lookup on issue returned no PR")
        return {"threads": [], "complete": True, "has_next_page": False}

    packet = evidence.collect_evidence(
        snap, api, tmp_path / "evidence", review_threads_fetch=reviews
    )
    return snap, packet


def prepared(tmp_path, snap, packet, *, action="collect_evidence"):
    path = (
        (
            tmp_path / "state.json.evidence"
            if (tmp_path / "state.json.evidence").exists()
            else tmp_path / "evidence"
        )
        / "manifests"
        / (packet["manifest"]["packet_sha256"] + ".json")
    )
    snapshot_path = tmp_path / "snapshot.json"
    raw = json.dumps(snap, sort_keys=True).encode()
    snapshot_path.write_bytes(raw)
    return {
        "snapshot": copy.deepcopy(snap),
        "snapshot_ref": {"path": str(snapshot_path), "sha256": hashlib.sha256(raw).hexdigest()},
        "packet_ref": {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
        "producer_sha256": packet["producer_sha256"],
        "revision": snap["revision"],
        "semantic_cache_key": packet["semantic_cache_key"],
        "evidence_summary": {
            "sections": {
                k: {"status": v["status"], "retained": v["retained"]}
                for k, v in packet["sections"].items()
            }
        },
        "proposal": {"action": action, "eligible": True},
    }


@pytest.mark.parametrize("intake", [False, True], ids=["extractor", "intake"])
@pytest.mark.parametrize("lookup_failure", [False, True], ids=["no-review-debt", "lookup-failed"])
def test_source_only_is_integrity_valid_but_not_action_ready(tmp_path, intake, lookup_failure):
    snap, packet = source_only(tmp_path, intake=intake, lookup_failure=lookup_failure)
    assert evidence.validate_packet(packet)
    assert packet["sections"]["source"]["status"] == "complete"
    assert packet["sections"]["source"]["truncated"] is False
    assert packet["head"] is None and packet["subject_pr"] is None
    result = evidence.action_evidence_readiness(snap, packet, "collect_evidence")
    assert result["schema"] == "closer-action-evidence-readiness/v1"
    assert result["ready"] is False and result["gaps"]


@pytest.mark.parametrize("stage", ["interpret", "exception"])
@pytest.mark.parametrize("intake", [False, True], ids=["extractor", "intake"])
def test_missing_delivery_never_requests_low_or_strong_provider(
    tmp_path, monkeypatch, stage, intake, action="collect_evidence", transport=None, ambiguous=False
):
    snap, packet = source_only(tmp_path, intake=intake, ambiguous=ambiguous)
    prep = prepared(tmp_path, snap, packet, action=action)
    if transport == "missing":
        prep["packet_ref"]["path"] = str(tmp_path / "missing-packet.json")
    elif transport == "mismatched-sha":
        prep["packet_ref"]["sha256"] = "0" * 64
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append("provider-or-route")
        pytest.fail("deterministic evidence gap must not allocate a provider")

    monkeypatch.setattr(adapter, "_pipeline_low_route", forbidden)
    monkeypatch.setattr(adapter, "_exception_route", forbidden)
    monkeypatch.setitem(sys.modules, "dispatcher", SimpleNamespace(offload=forbidden))
    backend = runtime_fixture.closer(tmp_path, snap)
    backend.campaign_admission = forbidden
    seen = {}

    def run_pipeline(_path, _targets, **callbacks):
        context = {
            "prepare": prep,
            "interpret": {"classification": "uncertain", "substantive_needs": ["missing proof"]},
            "deadline": time.time() + 60,
            "work_key": "offline-pre-model-admission",
        }
        seen.update(callbacks[stage](snap["target"], context))
        return {"targets": {snap["target"]: seen}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", run_pipeline)
    args = SimpleNamespace(state=str(tmp_path / "state.json"), shadow=False, max_workers=1)
    adapter._run_staged_pipeline(args, [snap["target"]], backend, time.time() + 60)
    assert calls == []
    if action != "collect_evidence":
        assert seen["authority"] is False and seen["substantive_needs"] == []
        assert seen["classification"].startswith("deterministic_")
        return
    assert seen["status"] == "pending" and seen["pending_kind"] == "external_event"
    assert seen["classification"] == "evidence_pending"
    assert seen["authority"] is False and seen["substantive_needs"] == []
    assert seen["pending_evidence"]["target"] == snap["target"]
    assert seen["pending_evidence"]["revision"] == snap["revision"]
    assert seen["pending_evidence"]["packet_ref"] == prep["packet_ref"]
    if transport:
        assert "packet:invalid_or_unavailable" in seen["pending_evidence"]["readiness"]["gaps"]
    if ambiguous:
        summary = seen["pending_evidence"]["readiness"]["delivery_links"]
        for key in ("status", "source", "complete", "candidates"):
            assert summary[key] == snap["delivery_links"][key]
        assert summary["candidate_count"] == 2 and summary["candidates_complete"] is True
        assert summary["summary_only"] is True
        ref = summary["inventory_ref"]
        raw = Path(ref["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == ref["sha256"]
        assert json.loads(raw)["content"]["candidates"] == snap["delivery_links"]["candidates"]
        assert "all" in seen["next_trigger"].lower()


@pytest.mark.parametrize("native", [False, True], ids=["ordinary", "bound-nonpass"])
def test_real_collected_bound_proofs_remain_admissible(tmp_path, native):
    snap, packet, *_ = proof_fixture.fixture(tmp_path, native=native)
    snap.update(state="MERGED" if native else "OPEN", active_threads=0, labels=[])
    action = "disposition" if native else "collect_evidence"
    result = evidence.action_evidence_readiness(
        snap, packet, action, retained_dir=tmp_path / "evidence"
    )
    assert result["ready"] is True, result
    assert result["gaps"] == []
    # Partial topology may retain sufficient typed per-file evidence.
    assert packet["sections"]["test_coverage_artifacts"]["status"] == "partial"


@pytest.mark.parametrize("mutation", ["wrong-head", "wrong-base", "wrong-source", "invalid-packet"])
def test_present_bytes_wrong_binding_cannot_unlock_assessment(tmp_path, mutation):
    snap, packet, *_ = proof_fixture.fixture(tmp_path)
    if mutation == "wrong-head":
        snap["head"] = "f" * 40
    elif mutation == "wrong-base":
        snap["base"] = "f" * 40
    elif mutation == "wrong-source":
        snap["sources"][0]["body"] += "\n- Additional original criterion"
    else:
        packet["sections"]["checks"]["content"]["head"] = "f" * 40
    result = evidence.action_evidence_readiness(
        snap, packet, "collect_evidence", retained_dir=tmp_path / "evidence"
    )
    assert result["ready"] is False and result["gaps"]


@pytest.mark.parametrize("stage", ["interpret", "exception"])
def test_evidence_pending_persists_and_restarts_without_action(tmp_path, monkeypatch, stage):
    snap, packet = source_only(tmp_path)
    prep = prepared(tmp_path, snap, packet)
    original_run = closer_pipeline.run_pipeline
    captured = {}

    def capture(_path, _targets, **callbacks):
        captured.update(callbacks)
        return {"targets": {}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", capture)

    def forbidden(*_a, **_kw):
        pytest.fail("pending proof cannot run a provider or action")

    monkeypatch.setattr(adapter, "_pipeline_low_route", forbidden)
    monkeypatch.setattr(adapter, "_exception_route", forbidden)
    backend = runtime_fixture.closer(tmp_path, snap)
    backend.campaign_admission = forbidden
    args = SimpleNamespace(state=str(tmp_path / "state.json"), shadow=False, max_workers=1)
    adapter._run_staged_pipeline(args, [snap["target"]], backend, time.time() + 60)
    callbacks = {
        "prepare": lambda *_a: prep,
        "interpret": (
            captured["interpret"]
            if stage == "interpret"
            else lambda *_a: {"substantive_needs": ["old semantic gap"]}
        ),
        "exception": captured["exception"],
        "act": forbidden,
    }
    path = tmp_path / "pending-pipeline.json"
    for _ in range(2):
        receipt = original_run(
            path,
            [snap["target"]],
            **callbacks,
            capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
            deadline=time.time() + 3,
        )
        row = receipt["targets"][snap["target"]]
        assert row["status"] == "pending", row
        assert row["stage"] == stage
        assert closer_pipeline.validate_stop(path, [snap["target"]])["valid"]
        saved = closer_pipeline._load(path)
        last = max(saved["runs"].values(), key=lambda run: run["created"])
        assert last["targets"][snap["target"]]["status"] == "pending"


def code_only_packet(tmp_path):
    snap, full, *_ = proof_fixture.fixture(tmp_path / "full")
    snap.update(state="OPEN", active_threads=0, labels=[])
    files = full["sections"]["diff_files"]["content"]["files"]

    def api(endpoint, **_kwargs):
        if "/compare/" in endpoint:
            return {"status": "ahead", "files": files}
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": []}
        raise AssertionError(endpoint)

    packet = evidence.collect_evidence(
        snap,
        api,
        tmp_path / "code-only",
        review_threads_fetch=lambda *_a, **_k: {
            "threads": [
                {
                    "id": "offline-thread",
                    "comments": {
                        "nodes": [
                            {
                                "id": "offline-comment",
                                "body": "Please explain the report behavior",
                                "author": {"login": "offline-reviewer"},
                            }
                        ]
                    },
                }
            ],
            "complete": True,
            "has_next_page": False,
        },
    )
    return snap, packet


@pytest.mark.parametrize("action", ["collect_evidence", "review"])
def test_optional_ci_absence_does_not_hold_code_or_review_assessment(tmp_path, action):
    snap, packet = code_only_packet(tmp_path)
    assert packet["sections"]["ci_artifacts"]["status"] == "missing"
    result = evidence.action_evidence_readiness(
        snap, packet, action, retained_dir=tmp_path / "code-only"
    )
    assert result["ready"] is True, result


def test_issue_collector_never_performs_pr_review_lookup(tmp_path):
    snap, _packet = source_only(tmp_path)
    packet = evidence.collect_evidence(
        snap,
        lambda *_a, **_k: pytest.fail("no head has no CI or diff query"),
        tmp_path / "second",
        review_threads_fetch=lambda *_a, **_k: pytest.fail("issue is not a PR number"),
    )
    assert packet["sections"]["review_conversations"]["status"] != "complete"
    assert evidence.validate_packet(packet)


@pytest.mark.parametrize("numbers", [[], [128, 142]], ids=["absent", "ambiguous-two-deliveries"])
def test_source_snapshot_preserves_complete_delivery_inventory(tmp_path, monkeypatch, numbers):
    gh = adapter.GitHub(deadline=time.time() + 30)

    def api(endpoint, **_kwargs):
        if endpoint == "repos/offline/example/issues/111":
            return {"state": "open", "body": "## Tasks\n- [ ] Preserve every original criterion"}
        assert endpoint == "graphql"
        return {
            "data": {
                "repository": {
                    "issue": {
                        "closedByPullRequestsReferences": {
                            "nodes": [
                                {"number": n, "repository": {"nameWithOwner": "offline/example"}}
                                for n in numbers
                            ],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            }
        }

    monkeypatch.setattr(gh, "api", api)
    snap = gh.snapshot("offline/example#111")
    assert snap["delivery_links"]["status"] == ("ambiguous" if numbers else "missing")
    assert snap["delivery_links"]["complete"] is True
    assert snap["delivery_links"]["candidates"] == [f"offline/example#{n}" for n in numbers]
    assert snap["sources"][0]["body"] == "## Tasks\n- [ ] Preserve every original criterion"
    packet = evidence.collect_evidence(
        snap,
        lambda *_a, **_kw: pytest.fail("unresolved delivery must not query invented head"),
        tmp_path / "evidence",
    )
    assert evidence.validate_packet(packet)
    result = evidence.action_evidence_readiness(snap, packet, "collect_evidence")
    assert result["ready"] is False
    assert "delivery_pr:" + snap["delivery_links"]["status"] in result["gaps"]


@pytest.mark.parametrize("native", [False, True], ids=["ordinary", "bound-nonpass"])
def test_ready_collector_packet_reaches_one_actual_advisory_boundary(tmp_path, monkeypatch, native):
    snap, packet, _ref, _prepared, response = proof_fixture.fixture(tmp_path, native=native)
    snap.update(state="MERGED" if native else "OPEN", active_threads=0, labels=[])
    # Provision these synthetic files at the adapter's actual configured retention root.
    old_root, new_root = tmp_path / "evidence", tmp_path / "state.json.evidence"
    shutil.copytree(old_root, new_root)

    def remap(value):
        if isinstance(value, dict):
            return {key: remap(item) for key, item in value.items()}
        if isinstance(value, list):
            return [remap(item) for item in value]
        if isinstance(value, str) and value.startswith(str(old_root) + "/"):
            return str(new_root) + value[len(str(old_root)) :]
        return value

    packet = remap(packet)
    for section in packet["sections"].values():
        section["sha256"] = evidence._section_hash(section)
        section["retained"] = evidence._retain_bytes(
            new_root, evidence._bytes({k: v for k, v in section.items() if k != "retained"}), "json"
        )
    packet["section_digests"] = {k: v["sha256"] for k, v in packet["sections"].items()}
    packet["semantic_cache_key"] = evidence._semantic_cache_key(packet)
    packet["manifest"] = {
        "section_files": {k: v["retained"] for k, v in packet["sections"].items()}
    }
    packet["manifest"]["packet_sha256"] = evidence._packet_hash(packet)
    manifest_path = new_root / "manifests" / (packet["manifest"]["packet_sha256"] + ".json")
    manifest_path.write_bytes(evidence._bytes(packet))
    assert evidence.validate_packet(packet)
    prep = prepared(tmp_path, snap, packet, action="disposition" if native else "collect_evidence")
    prep["packet_ref"] = {
        "path": str(manifest_path),
        "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    from test_closer_evidence_recovery import inert_caller

    import dispatcher as transport

    caller = inert_caller(tmp_path, monkeypatch)
    calls = []

    def offload(*args, **kwargs):
        calls.append((args, kwargs))
        return caller.response(
            {
                "exit": 0,
                "output": json.dumps(response["proposal"]),
            },
            args[0],
            args[1],
            **kwargs,
        )

    monkeypatch.setattr(transport, "offload", offload)

    backend = runtime_fixture.closer(tmp_path, snap)
    seen = {}

    def run_pipeline(_path, _targets, **callbacks):
        seen.update(
            callbacks["interpret"](snap["target"], {"prepare": prep, "deadline": time.time() + 60})
        )
        return {"targets": {snap["target"]: seen}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", run_pipeline)
    args = SimpleNamespace(state=str(tmp_path / "state.json"), shadow=False, max_workers=1)
    monkeypatch.setattr(adapter, "GitHub", lambda **_kwargs: backend.gh)
    caller.staged(args, [snap["target"]], backend, time.time() + 60)
    assert len(calls) == 1
    assert seen["actual"]["exit"] == 0
    assert seen["classification"] != "evidence_pending"
    assert backend.gh.calls == []


@pytest.mark.parametrize("section_name", ["source", "review_conversations"])
def test_valid_retained_body_ref_must_match_displayed_body(tmp_path, section_name):
    snap, packet = code_only_packet(tmp_path)
    section = packet["sections"][section_name]
    row = section["content"][0]
    if section_name == "review_conversations":
        row = row["comments"][0]
    row["body_retained"] = evidence._retain_bytes(
        tmp_path / "code-only", b"Different but correctly hashed raw body", "txt"
    )
    row["body_retained"]["truncated"] = False
    section["sha256"] = evidence._section_hash(section)
    section["retained"] = evidence._retain_bytes(
        tmp_path / "code-only",
        evidence._bytes({k: v for k, v in section.items() if k != "retained"}),
        "json",
    )
    packet["section_digests"][section_name] = section["sha256"]
    packet["semantic_cache_key"] = evidence._semantic_cache_key(packet)
    packet["manifest"]["section_files"][section_name] = section["retained"]
    packet["manifest"]["packet_sha256"] = evidence._packet_hash(packet)
    assert evidence.validate_packet(packet)
    result = evidence.action_evidence_readiness(
        snap,
        packet,
        "review" if section_name == "review_conversations" else "collect_evidence",
        retained_dir=tmp_path / "code-only",
    )
    assert result["ready"] is False and result["gaps"]


@pytest.mark.parametrize("action", ["wait", "complete", "post_merge_verify"])
def test_old_substantive_context_cannot_promote_deterministic_action_to_strong(
    tmp_path, monkeypatch, action
):
    test_missing_delivery_never_requests_low_or_strong_provider(
        tmp_path, monkeypatch, "exception", False, action=action
    )


@pytest.mark.parametrize("stage", ["interpret", "exception"])
@pytest.mark.parametrize("transport", ["missing", "mismatched-sha"])
def test_packet_transport_failure_cannot_fall_through_to_provider(
    tmp_path, monkeypatch, stage, transport
):
    test_missing_delivery_never_requests_low_or_strong_provider(
        tmp_path, monkeypatch, stage, False, transport=transport
    )


@pytest.mark.parametrize("stage", ["interpret", "exception"])
def test_ambiguous_delivery_retains_both_candidates_without_any_provider(
    tmp_path, monkeypatch, stage
):
    test_missing_delivery_never_requests_low_or_strong_provider(
        tmp_path, monkeypatch, stage, False, ambiguous=True
    )


def test_bound_native_nonpass_does_not_require_ordinary_ci_or_diff(tmp_path):
    snap, packet, *_ = proof_fixture.fixture(tmp_path, native=True)
    snap.update(state="MERGED", active_threads=0, labels=[])
    for name in ("ci_artifacts", "diff_files", "test_coverage_artifacts"):
        section = evidence._section("missing", reason="ordinary receipt not supplied")
        section["retained"] = evidence._retain_bytes(
            tmp_path / "evidence", evidence._bytes(section), "json"
        )
        packet["sections"][name] = section
        packet["section_digests"][name] = section["sha256"]
        packet["manifest"]["section_files"][name] = section["retained"]
    packet["semantic_cache_key"] = evidence._semantic_cache_key(packet)
    packet["manifest"]["packet_sha256"] = evidence._packet_hash(packet)
    assert evidence.validate_packet(packet)
    result = evidence.action_evidence_readiness(
        snap, packet, "disposition", retained_dir=tmp_path / "evidence"
    )
    assert result["ready"] is True and result["mode"] == "nonpass_triage", result


class CapturedShapeGitHub:
    """Public response shapes normalized to offline identities, never live GitHub."""

    source = "offline/example#111"
    head = "e" * 40
    base = "f" * 40
    merge = "d" * 40
    earlier_head = "a" * 40
    earlier_merge = "b" * 40
    body = "## Tasks\n- [ ] Ship text-derived records\n## Acceptance Criteria\n- [ ] Preserve original evidence"

    def __init__(self, variant="linear"):
        self.variant = variant
        self.calls = []
        self.deadline = time.time() + 60
        self.github = adapter.GitHub(deadline=self.deadline)
        self.github.api = self.api

    def candidate(self, number):
        later = number == 142
        return {
            "number": number,
            "state": "OPEN" if self.variant == "open" and not later else "MERGED",
            "headRefOid": self.head if later else self.earlier_head,
            "baseRefOid": self.base if later else "c" * 40,
            "mergeCommit": {"oid": self.merge if later else self.earlier_merge},
            # Deliberately reverse chronology: ancestry, not newest/number, must decide.
            "mergedAt": "2026-01-01T00:00:00Z" if later else "2026-01-02T00:00:00Z",
            "repository": {
                "nameWithOwner": (
                    "offline/foreign"
                    if self.variant == "foreign" and not later
                    else "offline/example"
                )
            },
        }

    def pr(self, number):
        data = self.candidate(number)
        data.update(
            {
                "body": "Closes #111",
                "isDraft": False,
                "headRefName": "offline-feature",
                "headRepository": {"nameWithOwner": "offline/example"},
                "updatedAt": "2026-01-03T00:00:00Z",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "labels": {
                    "nodes": [{"name": "verify:compare"}],
                    "pageInfo": {"hasNextPage": False},
                },
                "timelineItems": {"nodes": [], "pageInfo": {"hasPreviousPage": False}},
                "closingIssuesReferences": {
                    "nodes": [
                        {
                            "number": 111,
                            "body": self.body,
                            "state": "OPEN",
                            "repository": {"nameWithOwner": "offline/example"},
                            "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                        }
                    ],
                    "pageInfo": {"hasNextPage": False},
                },
                "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "oid": data["headRefOid"],
                                "statusCheckRollup": {"state": "SUCCESS"},
                            }
                        }
                    ]
                },
                "comments": {"nodes": [], "pageInfo": {"hasPreviousPage": False}},
            }
        )
        if self.variant in {"ready-linear", "missing-raw"}:
            marker = {
                "schema": "verifier-corpus-decision/v1",
                "repo": "offline/example",
                "pr": number,
                "head_sha": data["headRefOid"],
                "evaluated_sha": data["mergeCommit"]["oid"],
                "run_id": 77,
                "run_attempt": 1,
                "provider_verdicts": ["PASS"],
                "ci_failed": False,
                "verdict": "PASS",
            }
            data["comments"]["nodes"] = [
                {
                    "author": {"login": "github-actions[bot]"},
                    "url": f"https://github.com/offline/example/pull/{number}#issuecomment-1",
                    "body": "<!-- verifier-corpus-decision/v1 " + json.dumps(marker) + " -->",
                }
            ]
        if self.variant == "revalidation-drift":
            data["headRefOid"] = "9" * 40
        return data

    def api(self, endpoint, **kwargs):
        self.calls.append(endpoint)
        if endpoint == "repos/offline/example/issues/111":
            return {"number": 111, "body": self.body, "state": "open", "labels": []}
        if endpoint in {"repos/offline/example/issues/128", "repos/offline/example/issues/142"}:
            return {"pull_request": {}}
        if endpoint == "graphql":
            query = kwargs["fields"]["query"]
            if "closedByPullRequestsReferences" in query:
                if self.variant == "lookup-failed":
                    raise RuntimeError("synthetic inventory retrieval failed")
                numbers = [] if self.variant == "zero" else [142, 128]
                return {
                    "data": {
                        "repository": {
                            "issue": {
                                "closedByPullRequestsReferences": {
                                    "nodes": [self.candidate(n) for n in numbers],
                                    "pageInfo": {"hasNextPage": False},
                                }
                            }
                        }
                    }
                }
            number = 142 if "pullRequest(number:142)" in query else 128
            return {"data": {"repository": {"pullRequest": self.pr(number)}}}
        if "/compare/" in endpoint:
            comparison = endpoint.rsplit("/", 1)[1]
            if comparison == self.base + "..." + self.head:
                if self.variant == "missing-raw":
                    return {"status": "ahead", "files": []}
                return {
                    "status": "ahead",
                    "files": [
                        {
                            "filename": "report.py",
                            "status": "modified",
                            "sha": "1" * 40,
                            "patch": "+def records_from_text(): return read_segments()",
                        }
                    ],
                }
            if self.variant == "compare-failed":
                raise RuntimeError("synthetic ancestry lookup failed")
            earlier, later = comparison.split("...")
            good = (
                earlier == self.earlier_merge and later == self.head and self.variant != "nonlinear"
            )
            return {
                "status": "ahead" if good else "diverged",
                "ahead_by": 18,
                "behind_by": 0 if good else 2,
                "base_commit": {
                    "sha": "0" * 40 if self.variant == "wrong-base-binding" else earlier
                },
                "merge_base_commit": {"sha": earlier if good else "0" * 40},
            }
        if (
            "/actions/runs?" in endpoint
            or "/actions/workflows/agents-verifier.yml/runs?" in endpoint
        ):
            return {"workflow_runs": []}
        raise AssertionError(endpoint)


@pytest.mark.parametrize(
    "variant",
    [
        "linear",
        "ready-linear",
        "nonlinear",
        "zero",
        "open",
        "foreign",
        "lookup-failed",
        "compare-failed",
        "wrong-base-binding",
        "revalidation-drift",
        "missing-raw",
    ],
)
def test_delivery_producer_to_consumer_preserves_criteria_and_uses_only_ready_evidence(
    tmp_path, monkeypatch, variant
):
    observed = CapturedShapeGitHub(variant)
    monkeypatch.setattr(adapter, "GitHub", lambda **_kwargs: observed.github)
    backend = adapter.Closer(observed.github, state_path=tmp_path / "state.json")
    from test_closer_evidence_recovery import inert_caller

    import dispatcher as transport

    caller = inert_caller(tmp_path, monkeypatch)
    calls = []

    def offload(*args, **kwargs):
        calls.append((args, kwargs))
        return caller.response(
            {
                "exit": 0,
                "output": json.dumps(
                    {
                        "target": observed.source,
                        "head": observed.head,
                        "base": observed.base,
                        "classification": "uncertain",
                        "substantive_needs": ["Offline model stub grants no acceptance"],
                        "findings": [],
                        "decision": {"criteria": []},
                    }
                ),
            },
            args[0],
            args[1],
            **kwargs,
        )

    monkeypatch.setattr(transport, "offload", offload)

    monkeypatch.setattr(
        adapter,
        "_exception_route",
        lambda **_kw: pytest.fail("no strong call for deterministic collection gap"),
    )
    seen = {}

    def run_pipeline(_path, _targets, **callbacks):
        prep = callbacks["prepare"](observed.source, {"deadline": time.time() + 60})
        packet = json.loads(Path(prep["packet_ref"]["path"]).read_text())
        full_snap = json.loads(Path(prep["snapshot_ref"]["path"]).read_text())
        assert full_snap["sources"][0]["body"] == observed.body
        assert packet["sections"]["source"]["content"][0]["body"] == observed.body
        assert evidence.validate_packet(packet)
        context = {"prepare": prep, "deadline": time.time() + 60}
        result = callbacks["interpret"](observed.source, context)
        seen.update(result=result, packet=packet, snapshot=full_snap)
        for key in ("schema", "status", "action", "evaluation_ready", "acceptance_authority"):
            assert prep["collection_result"][key] == packet["collection_result"][key]
        assert prep["evidence_summary"]["collection_result"] == prep["collection_result"]
        assert (
            prep["collection_result"]["readiness"]["gaps"]
            == packet["collection_result"]["readiness"]["gaps"]
        )
        assert packet["collection_result"]["acceptance_authority"] is False
        if variant != "ready-linear":
            strong = callbacks["exception"](observed.source, {**context, "interpret": result})
            if variant == "linear":
                # A resolved merged delivery without a verdict first requests
                # its native verification; it does not purchase an assessment.
                assert strong["classification"] == "deterministic_post_merge_verify"
                assert strong["substantive_needs"] == []
                assert strong["authority"] is False
            else:
                assert strong["status"] == "pending" and strong["authority"] is False
        return {"targets": {observed.source: result}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", run_pipeline)
    args = SimpleNamespace(state=str(tmp_path / "state.json"), shadow=False, max_workers=1)
    caller.staged(
        args, [observed.source], backend, time.time() + 60, deterministic=variant != "ready-linear"
    )
    if variant in {"linear", "ready-linear"}:
        assert seen["snapshot"]["pr_target"] == "offline/example#142"
        assert seen["snapshot"]["head"] == observed.head
        assert seen["snapshot"]["base"] == observed.base
        links = seen["snapshot"]["delivery_links"]
        assert links["selected"] == "offline/example#142"
        assert links["method"] == "unique_merged_chain"
        assert set(links["candidates"]) == {"offline/example#128", "offline/example#142"}
        assert len(links["candidate_metadata"]) == 2
        assert any(
            p["earlier_merge"] == observed.earlier_merge and p["contains_merge"]
            for p in links["ancestry_proofs"]
        )
    if variant not in {"linear", "ready-linear", "missing-raw"}:
        assert seen["snapshot"]["kind"] == "issue"
        assert seen["snapshot"]["delivery_links"]["status"] != "resolved"
    if variant == "ready-linear":
        assert len(calls) == 1, seen["result"]
        assert seen["packet"]["collection_result"]["status"] == "ready"
        assert seen["result"]["actual"]["exit"] == 0
        prompt = calls[0][0][1]
        assert "Ship text-derived records" in prompt and "Preserve original evidence" in prompt
    else:
        assert calls == []
        assert seen["packet"]["collection_result"]["status"] == (
            "deterministic" if variant == "linear" else "pending"
        )
        if variant == "linear":
            assert seen["result"]["classification"] == "deterministic_post_merge_verify"
            assert seen["result"]["substantive_needs"] == []
            assert seen["result"]["authority"] is False
        else:
            assert seen["result"]["status"] == "pending" and seen["result"]["authority"] is False


@pytest.mark.parametrize(
    "variant",
    ["too-many", "incomplete", "missing-head", "deadline", "two-terminals", "boolean-behind"],
)
def test_delivery_resolution_bounds_and_immutable_compare_proofs_fail_closed(variant):
    captured = CapturedShapeGitHub()
    nodes = [captured.candidate(128), captured.candidate(142)]
    if variant == "too-many":
        nodes = [{**captured.candidate(142), "number": n} for n in range(9)]
    if variant == "missing-head":
        nodes[0]["headRefOid"] = None
    calls = []

    def compare(endpoint):
        calls.append(endpoint)
        if variant == "deadline":
            raise TimeoutError("original collection deadline ended")
        earlier = endpoint.rsplit("/", 1)[1].split("...")[0]
        return {
            "status": "ahead",
            "behind_by": False if variant == "boolean-behind" else 0,
            "base_commit": {"sha": earlier},
            "merge_base_commit": {"sha": earlier},
        }

    result = adapter.resolve_delivery_candidates(
        "offline/example", nodes, complete=variant != "incomplete", compare=compare
    )
    assert result["status"] in {"failed", "ambiguous"}
    assert "selected" not in result
    assert result["acceptance_authority"] is False
    assert len(result["candidates"]) == len(nodes)
    if variant in {"too-many", "incomplete", "missing-head"}:
        assert calls == []


@pytest.mark.parametrize("control", ["deadline", "capacity"])
@pytest.mark.parametrize(
    "site", ["resolver", "issue-inventory", "chosen-revalidation", "native-diagnostic"]
)
def test_typed_round_controls_propagate_without_further_collection(
    tmp_path, monkeypatch, control, site
):
    at = time.time()
    error = (
        adapter.RoundDeadlineExhausted(deadline=at, remaining_seconds=0)
        if control == "deadline"
        else adapter.RoundCapacityDeferred(
            resource="graphql", throttle={"action": "defer", "reset_in_s": 120}, observed_at=at
        )
    )
    calls = []

    def stop(endpoint, **_kwargs):
        calls.append(endpoint)
        raise error

    with pytest.raises(type(error)) as caught:
        if site == "resolver":
            captured = CapturedShapeGitHub()
            adapter.resolve_delivery_candidates(
                "offline/example",
                [captured.candidate(128), captured.candidate(142)],
                complete=True,
                compare=stop,
            )
        elif site in {"issue-inventory", "chosen-revalidation"}:
            captured = CapturedShapeGitHub()
            original = captured.api

            def api(endpoint, **kwargs):
                calls.append(endpoint)
                if (
                    site == "issue-inventory"
                    and endpoint == "graphql"
                    or site == "chosen-revalidation"
                    and endpoint == "repos/offline/example/issues/142"
                ):
                    raise error
                return original(endpoint, **kwargs)

            monkeypatch.setattr(captured.github, "api", api)
            captured.github.snapshot(captured.source)
        else:
            snapshot = {
                "target": "offline/example#142",
                "kind": "pr",
                "state": "MERGED",
                "head": "e" * 40,
                "base": "f" * 40,
                "merge": "d" * 40,
                "labels": ["verify:compare"],
                "sources": [
                    {"target": "offline/example#111", "body": "## Tasks\n- [ ] Preserve criteria"}
                ],
            }
            evidence.collect_evidence(
                snapshot,
                stop,
                tmp_path / "evidence",
                action="collect_evidence",
                artifact_fetch=lambda *_a, **_kw: pytest.fail("no archive fetch after typed stop"),
            )
    assert caught.value is error
    assert calls
    if site == "issue-inventory":
        assert calls == ["repos/offline/example/issues/111", "graphql"]
    elif site == "chosen-revalidation":
        assert calls[-1] == "repos/offline/example/issues/142"
        assert calls.count("repos/offline/example/issues/142") == 1
    else:
        assert len(calls) == 1
        if site == "native-diagnostic":
            assert "/actions/workflows/agents-verifier.yml/runs?head_sha=" in calls[0]
    assert not list(tmp_path.rglob("manifests/*.json"))


@pytest.mark.parametrize("candidate_count", [8, 100])
def test_maximum_delivery_inventory_stays_retained_while_callbacks_fit_cap_and_restart(
    tmp_path, monkeypatch, candidate_count
):
    repo = "owner-with-valid-long-name-123456789012/" + "repository-" + "x" * 89
    observed = CapturedShapeGitHub()
    source = repo + "#111"
    numbers = list(range(200, 200 + candidate_count))
    expected_proofs = 56 if candidate_count == 8 else 0
    external_calls = []

    def api(endpoint, **kwargs):
        external_calls.append(endpoint)
        if endpoint == f"repos/{repo}/issues/111":
            return {"number": 111, "state": "open", "body": observed.body, "labels": []}
        if endpoint == "graphql":
            assert "closedByPullRequestsReferences" in kwargs["fields"]["query"]
            return {
                "data": {
                    "repository": {
                        "issue": {
                            "closedByPullRequestsReferences": {
                                "nodes": [
                                    {
                                        "number": n,
                                        "state": "MERGED",
                                        "headRefOid": f"{n:040x}",
                                        "baseRefOid": f"{n + 1000:040x}",
                                        "mergeCommit": {"oid": f"{n + 2000:040x}"},
                                        "mergedAt": "2026-01-01T00:00:00Z",
                                        "repository": {"nameWithOwner": repo},
                                    }
                                    for n in numbers
                                ],
                                "pageInfo": {"hasNextPage": False},
                            }
                        }
                    }
                }
            }
        assert "/compare/" in endpoint
        earlier = endpoint.rsplit("/", 1)[1].split("...")[0]
        return {
            "status": "diverged",
            "behind_by": 1,
            "ahead_by": 2,
            "base_commit": {"sha": earlier},
            "merge_base_commit": {"sha": "0" * 40},
        }

    github = adapter.GitHub(deadline=time.time() + 60)
    monkeypatch.setattr(github, "api", api)
    monkeypatch.setattr(adapter, "GitHub", lambda **_kwargs: github)

    def forbidden(*_args, **_kwargs):
        pytest.fail("ambiguous maximum inventory must never allocate a provider")

    monkeypatch.setattr(adapter, "_pipeline_low_route", forbidden)
    monkeypatch.setattr(adapter, "_exception_route", forbidden)
    monkeypatch.setitem(sys.modules, "dispatcher", SimpleNamespace(offload=forbidden))
    prior = {}
    pending = []
    for restarted in [False, True]:
        backend = runtime_fixture.closer(tmp_path, {})
        backend.campaign_admission = forbidden

        def run_pipeline(_path, _targets, **callbacks):
            prep = (
                json.loads((tmp_path / "retained-prepare.json").read_text())
                if restarted
                else callbacks["prepare"](source, {"deadline": time.time() + 60})
            )
            closer_pipeline._bounded(prep, limit=32768)
            (tmp_path / "retained-prepare.json").write_text(json.dumps(prep))
            packet_raw = Path(prep["packet_ref"]["path"]).read_bytes()
            assert hashlib.sha256(packet_raw).hexdigest() == prep["packet_ref"]["sha256"]
            packet = json.loads(packet_raw)
            inventory = packet["sections"]["delivery_inventory"]
            assert len(inventory["content"]["candidates"]) == candidate_count
            assert len(inventory["content"]["ancestry_proofs"]) == expected_proofs
            raw = Path(inventory["retained"]["path"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == inventory["retained"]["sha256"]
            assert len(json.loads(raw)["content"]["ancestry_proofs"]) == expected_proofs
            if restarted:
                assert inventory["retained"] == prior["inventory_ref"]
            prior["inventory_ref"] = inventory["retained"]
            for stage in ["interpret", "exception"]:
                value = callbacks[stage](
                    source,
                    {
                        "prepare": prep,
                        "deadline": time.time() + 60,
                        "interpret": {"substantive_needs": ["old semantic gap"]},
                    },
                )
                closer_pipeline._bounded(value, limit=32768)
                assert value["status"] == "pending" and value["authority"] is False
                assert value["pending_evidence"]["packet_ref"] == prep["packet_ref"]
                summary = value["pending_evidence"]["readiness"]["delivery_links"]
                assert summary["candidate_count"] == candidate_count
                assert summary["candidates_complete"] is (candidate_count <= 8)
                assert len(summary["candidates"]) == min(8, candidate_count)
                assert summary["inventory_ref"] == inventory["retained"]
                assert "ancestry_proofs" not in summary
                pending.append(value)
            return {"targets": {source: pending[-1]}}

        monkeypatch.setattr(closer_pipeline, "run_pipeline", run_pipeline)
        args = SimpleNamespace(state=str(tmp_path / "state.json"), shadow=False, max_workers=1)
        adapter._run_staged_pipeline(args, [source], backend, time.time() + 60)
    assert len([call for call in external_calls if "/compare/" in call]) == expected_proofs
    assert len(pending) == 4


@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "null-verdict",
        "schema",
        "repo",
        "pr",
        "head",
        "merge",
        "run",
        "attempt",
        "providers",
        "ci-failed-type",
        "valid",
    ],
)
def test_merged_collect_requires_exact_trusted_marker_even_with_valid_codeproof(
    tmp_path, monkeypatch, mutation
):
    snap, packet, *_ = proof_fixture.fixture(tmp_path)
    snap.update(state="MERGED", merge="d" * 40, active_threads=0, labels=["verify:compare"])
    marker = {
        "schema": "verifier-corpus-decision/v1",
        "repo": "owner/repo",
        "pr": 12,
        "head_sha": snap["head"],
        "evaluated_sha": snap["merge"],
        "run_id": 77,
        "run_attempt": 1,
        "provider_verdicts": ["PASS"],
        "ci_failed": False,
        "verdict": "PASS",
    }
    replacements = {
        "null-verdict": ("verdict", None),
        "schema": ("schema", "unknown"),
        "repo": ("repo", "owner/foreign"),
        "pr": ("pr", 99),
        "head": ("head_sha", "9" * 40),
        "merge": ("evaluated_sha", "9" * 40),
        "run": ("run_id", "unknown"),
        "attempt": ("run_attempt", 0),
        "providers": ("provider_verdicts", ["CONCERNS"]),
        "ci-failed-type": ("ci_failed", 0),
    }
    if mutation == "empty":
        marker = {}
    elif mutation in replacements:
        key, value = replacements[mutation]
        marker[key] = value
    snap["verifier"] = marker
    verdict = evidence.action_evidence_readiness(
        snap, packet, "collect_evidence", retained_dir=tmp_path / "evidence"
    )
    if mutation == "valid":
        assert verdict["ready"] is True, verdict
        return
    assert verdict["ready"] is False and verdict["mode"] == "native_diagnostic", verdict
    prep = prepared(tmp_path, snap, packet)

    def forbidden(*_args, **_kwargs):
        pytest.fail("unbound marker must not admit any low or strong assessment")

    backend = runtime_fixture.closer(tmp_path, snap)
    backend.campaign_admission = forbidden
    monkeypatch.setattr(adapter, "_pipeline_low_route", forbidden)
    monkeypatch.setattr(adapter, "_exception_route", forbidden)
    monkeypatch.setitem(sys.modules, "dispatcher", SimpleNamespace(offload=forbidden))

    def run_pipeline(_path, _targets, **callbacks):
        context = {
            "prepare": prep,
            "deadline": time.time() + 60,
            "interpret": {"substantive_needs": ["old gap"]},
        }
        for stage in ["interpret", "exception"]:
            result = callbacks[stage](snap["target"], context)
            assert result["status"] == "pending" and result["authority"] is False
        return {"targets": {}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", run_pipeline)
    args = SimpleNamespace(state=str(tmp_path / "state.json"), shadow=False, max_workers=1)
    adapter._run_staged_pipeline(args, [snap["target"]], backend, time.time() + 60)


def transformed_packet(
    tmp_path, *, source_body=None, review_body="Please validate password: at least 8 chars"
):
    snapshot, original = code_only_packet(tmp_path)
    if source_body is not None:
        snapshot["sources"][0]["body"] = source_body
    snapshot["revision"] = adapter.digest(snapshot)

    def api(endpoint, **_kwargs):
        if "/compare/" in endpoint:
            return {
                "status": "ahead",
                "files": original["sections"]["diff_files"]["content"]["files"],
            }
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": []}
        raise AssertionError(endpoint)

    packet = evidence.collect_evidence(
        snapshot,
        api,
        tmp_path / "evidence",
        action="review",
        review_threads_fetch=lambda *_a, **_kw: {
            "threads": [
                {
                    "id": "offline-review",
                    "isResolved": False,
                    "isOutdated": False,
                    "comments": {
                        "nodes": [
                            {
                                "id": "offline-comment",
                                "body": review_body,
                                "author": {"login": "offline-reviewer"},
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "hasPreviousPage": False},
                    },
                }
            ],
            "complete": True,
            "has_next_page": False,
        },
    )
    return snapshot, packet


def reseal_section(packet, name, root):
    section = packet["sections"][name]
    section["sha256"] = evidence._section_hash(section)
    section["retained"] = evidence._retain_bytes(
        root, evidence._bytes({k: v for k, v in section.items() if k != "retained"}), "json"
    )
    packet["section_digests"][name] = section["sha256"]
    packet["semantic_cache_key"] = evidence._semantic_cache_key(packet)
    packet["manifest"]["section_files"][name] = section["retained"]
    packet["manifest"]["packet_sha256"] = evidence._packet_hash(packet)


@pytest.mark.parametrize("transform", ["redacted", "oversized"])
def test_original_source_transform_requires_specific_senior_dependency_before_models(
    tmp_path, monkeypatch, transform
):
    original_body = (
        "## Tasks\n- [ ] Validate password: at least 8 chars"
        if transform == "redacted"
        else "## Tasks\n- [ ] Preserve complete criteria\n" + "x" * (evidence.MAX_TEXT_BYTES + 1)
    )
    snapshot, packet = transformed_packet(tmp_path, source_body=original_body)
    source = packet["sections"]["source"]
    assert source["status"] == ("partial" if transform == "redacted" else "truncated")
    assert snapshot["sources"][0]["body"] == original_body
    assert evidence.validate_packet(packet)
    readiness = evidence.action_evidence_readiness(
        snapshot, packet, "collect_evidence", retained_dir=tmp_path / "evidence"
    )
    assert readiness["ready"] is False and readiness["mode"] == "source_transform"
    assert packet["collection_result"]["status"] == "pending"
    assert packet["collection_result"]["evaluation_ready"] is False
    dependency = readiness["dependency"]
    assert packet["collection_result"]["readiness"]["dependency"] == dependency
    assert dependency["schema"] == "closer-source-transform-dependency/v1"
    assert dependency["kind"] == "senior_source_transform"
    assert dependency["source_status"] == source["status"]
    assert dependency["reason"] == source["reason"]
    assert dependency["source_ref"] == source["retained"]
    assert dependency["source_sha256"] == source["sha256"]
    assert dependency["original_source_sha256"] == packet["bindings"]["source"]
    assert dependency["target"] == snapshot["target"]
    assert dependency["revision"] == snapshot["revision"]
    assert dependency["next_trigger"] == (
        "senior source resolution supplies a complete criteria-preserving safely representable "
        "original source bound to this source identity"
    )
    prep = prepared(tmp_path, snapshot, packet)

    def forbidden(*_args, **_kwargs):
        pytest.fail("stable source transform needs senior resolution, not another model attempt")

    backend = runtime_fixture.closer(tmp_path, snapshot)
    backend.campaign_admission = forbidden
    monkeypatch.setattr(adapter, "_pipeline_low_route", forbidden)
    monkeypatch.setattr(adapter, "_exception_route", forbidden)
    monkeypatch.setitem(sys.modules, "dispatcher", SimpleNamespace(offload=forbidden))

    def run_pipeline(_path, _targets, **callbacks):
        for stage in ["interpret", "exception"]:
            result = callbacks[stage](
                snapshot["target"],
                {
                    "prepare": prep,
                    "deadline": time.time() + 60,
                    "interpret": {"substantive_needs": ["old incomplete evidence"]},
                },
            )
            assert result["status"] == "pending" and result["authority"] is False
            assert result["classification"] == "source_transform_pending"
            assert result["reason"] == "source_transform_requires_senior_resolution"
            assert result["next_trigger"] == dependency["next_trigger"]
            assert result["pending_evidence"]["readiness"]["dependency"] == dependency
        return {"targets": {}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", run_pipeline)
    args = SimpleNamespace(state=str(tmp_path / "state.json"), shadow=False, max_workers=1)
    adapter._run_staged_pipeline(args, [snapshot["target"]], backend, time.time() + 60)


@pytest.mark.parametrize(
    "mutation",
    [
        "valid",
        "incomplete-pagination",
        "incomplete-comment-pages",
        "next-comment-page",
        "truncated",
        "bad-ref",
        "displayed-body-mismatch",
        "not-redaction-only",
    ],
)
def test_only_complete_retained_redaction_review_partial_is_assessable_without_ci(
    tmp_path, mutation
):
    snapshot, packet = transformed_packet(tmp_path)
    section = packet["sections"]["review_conversations"]
    assert section["status"] == "partial" and section["reason"] == "credential-like values redacted"
    comment = section["content"][0]["comments"][0]
    assert comment["redaction_applied"] is True and "[REDACTED]" in comment["body"]
    assert comment["body_sha256"] != comment["body_retained"]["sha256"]
    assert packet["sections"]["ci_artifacts"]["status"] == "missing"
    if mutation == "incomplete-pagination":
        section["provenance"]["pagination_complete"] = False
    elif mutation == "incomplete-comment-pages":
        section["content"][0]["comments_complete"] = False
    elif mutation == "next-comment-page":
        section["content"][0]["comments_has_next_page"] = True
    elif mutation == "truncated":
        comment["truncated"] = True
        comment["body_retained"]["truncated"] = True
    elif mutation == "bad-ref":
        comment["body_retained"]["sha256"] = "0" * 64
    elif mutation == "displayed-body-mismatch":
        comment["body"] += " altered after retention"
    elif mutation == "not-redaction-only":
        comment["redaction_applied"] = False
    if mutation != "valid":
        reseal_section(packet, "review_conversations", tmp_path / "evidence")
    assert evidence.validate_packet(packet)
    verdict = evidence.action_evidence_readiness(
        snapshot, packet, "review", retained_dir=tmp_path / "evidence"
    )
    assert verdict["ready"] is (mutation == "valid"), verdict
    if mutation != "valid":
        assert verdict["gaps"]


@pytest.mark.parametrize("mutation", ["displayed_title", "raw_title_ref"])
def test_captured_title_is_bound_to_source_and_retained_bytes(tmp_path, mutation):
    snap, packet = code_only_packet(tmp_path)
    root = tmp_path / "code-only"
    snap["sources"][0]["title"] = "Preserve original behavior"
    packet["bindings"]["source"] = evidence._hash(snap["sources"])
    packet["sections"]["source"] = evidence._source_section(snap["sources"], root)
    reseal_section(packet, "source", root)
    assert evidence.validate_packet(packet)
    assert evidence.action_evidence_readiness(snap, packet, "collect_evidence", retained_dir=root)[
        "ready"
    ]
    row = packet["sections"]["source"]["content"][0]
    if mutation == "displayed_title":
        row["title"] = "Different requirements"
    else:
        row["title_retained"] = {
            **evidence._retain_bytes(root, b"Different retained title", "txt"),
            "truncated": False,
        }
    reseal_section(packet, "source", root)
    assert evidence.validate_packet(packet)
    result = evidence.action_evidence_readiness(snap, packet, "collect_evidence", retained_dir=root)
    assert not result["ready"]
    assert "source:original_bodies_unavailable" in result["gaps"]
