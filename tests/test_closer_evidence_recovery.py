"""Offline recovery boundary tests: necessity, retained progress and grouped defects."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_closer_pre_model_readiness import code_only_packet, reseal_section

import closer_dispatcher as dispatcher
import closer_evidence as evidence
import closer_evidence_recovery as recovery


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("no real provider, GitHub or runtime access in recovery tests")

    original = dispatcher.gh_capacity.gh_run
    monkeypatch.setattr(dispatcher.gh_capacity, "gh_run", forbidden)
    return original


def test_evaluation_screen_does_not_require_a_diagnostic_report_as_acceptance(tmp_path):
    snap, packet = code_only_packet(tmp_path)
    snap.update(state="MERGED", verifier=None)
    screen = evidence.action_evidence_readiness(
        snap, packet, "collect_evidence", retained_dir=tmp_path / "code-only"
    )
    assert not screen["ready"]
    assert screen["evaluation_ready"]
    assert any(r["necessity"] == "useful" for r in screen["requirements"])
    assert not dispatcher.acceptance_ok(snap, {})


def test_missing_original_source_is_necessary_even_with_code(tmp_path):
    snap, packet = code_only_packet(tmp_path)
    snap["sources"] = []
    screen = evidence.action_evidence_readiness(
        snap, packet, "collect_evidence", retained_dir=tmp_path / "code-only"
    )
    assert not screen["evaluation_ready"]
    assert recovery.required(screen)


@pytest.mark.parametrize("level", ["useful", "nice_to_have", "possibly_useful"])
def test_optional_model_suggestions_cannot_hold_the_screen(tmp_path, level):
    snap, packet = code_only_packet(tmp_path)
    screen = evidence.action_evidence_readiness(
        snap, packet, "collect_evidence", retained_dir=tmp_path / "code-only"
    )
    requests = recovery.validate_requests(
        {
            "evidence_requests": [
                {
                    "necessity": level,
                    "reason": "Additional coverage might help",
                    "section": "test_coverage_artifacts",
                }
            ]
        },
        snap,
        packet,
        screen,
    )
    assert recovery.apply_requests(screen, requests, packet)["evaluation_ready"]


def log_request(snap):
    return {
        "necessity": "necessary",
        "reason": "The exact failing job output identifies the requested behavior",
        "question": "Which assertion failed?",
        "locator": {"kind": "job_log", "job_id": 88},
        "basis": {"source": snap["sources"][0]["target"], "criterion": snap["sources"][0]["body"]},
    }


@pytest.mark.parametrize(
    "mutation", ["all_artifacts", "no_basis", "no_question", "upgrade_optional"]
)
def test_model_cannot_make_unsupported_global_enrichment_necessary(tmp_path, mutation):
    snap, packet = code_only_packet(tmp_path)
    screen = evidence.action_evidence_readiness(
        snap, packet, "collect_evidence", retained_dir=tmp_path / "code-only"
    )
    request = log_request(snap)
    if mutation == "all_artifacts":
        request["locator"] = {"kind": "all_artifacts"}
    elif mutation == "no_basis":
        request["basis"] = {}
    elif mutation == "no_question":
        request["question"] = ""
    else:
        request["requirement_id"] = next(
            r["id"] for r in screen["requirements"] if r["necessity"] != "necessary"
        )
    with pytest.raises(ValueError):
        recovery.validate_requests({"evidence_requests": [request]}, snap, packet, screen)


def test_targeted_refresh_does_not_fetch_optional_sections_again(tmp_path):
    snap, packet = code_only_packet(tmp_path)
    calls = []

    def api(endpoint, **_kw):
        calls.append(endpoint)
        assert "/compare/" in endpoint
        return {"status": "ahead", "files": []}

    after = evidence.collect_evidence(
        snap,
        api,
        tmp_path / "code-only",
        previous=packet,
        refresh_sections={"diff_files"},
        action="collect_evidence",
    )
    assert evidence.validate_packet(after)
    assert len(calls) == 1
    assert after["sections"]["ci_artifacts"] == packet["sections"]["ci_artifacts"]
    assert after["sections"]["review_conversations"] == packet["sections"]["review_conversations"]


def test_no_progress_is_retained_and_only_new_event_or_progress_reopens(tmp_path):
    snap, packet = code_only_packet(tmp_path)
    snap["revision"] = "revision"
    screen = {"requirements": [{"id": "raw", "section": "ci_artifacts", "necessity": "necessary"}]}
    store = recovery.RecoveryStore(tmp_path)
    first = store.begin(snap, packet, screen)
    assert first
    assert store.blocked(snap, packet, screen)
    store.finish(
        snap,
        first,
        prep={"packet_ref": {"sha256": "retained"}},
        screen=screen,
        requests=[],
        outcome="no_progress",
        causes=[],
    )
    assert store.begin(snap, packet, screen) is None
    packet["sections"]["elapsed"]["sha256"] = "new-clock"
    assert store.begin(snap, packet, screen) is None
    assert store.latest(snap)["prepare"]["packet_ref"]["sha256"] == "retained"
    assert store.begin(snap, packet, {**screen, "retry_trigger": "operator-targeted-retry-1"})
    packet["sections"]["ci_artifacts"]["sha256"] = "rotating-error-metadata"
    packet["sections"]["ci_artifacts"]["reason"] = "A different timeout message"
    assert store.begin(snap, packet, screen) is None
    raw = evidence._retain_bytes(tmp_path, b"new retained raw receipt", "txt")
    packet["sections"]["ci_artifacts"]["content"] = {"received": {"raw_retained": raw}}
    assert store.begin(snap, packet, screen)


class LogGitHub:
    def __init__(self, snap, *, wrong_head=False, wrong_pr=False):
        self.snap, self.reads = snap, []
        self.wrong_head, self.wrong_pr = wrong_head, wrong_pr

    def api(self, endpoint):
        self.reads.append(endpoint)
        if "/jobs/" in endpoint:
            return {"id": 88, "run_id": 7}
        return {
            "id": 7,
            "head_sha": "wrong" if self.wrong_head else self.snap["head"],
            "run_attempt": 2,
            "pull_requests": [
                {"number": 999 if self.wrong_pr else int(self.snap["target"].split("#")[1])}
            ],
        }

    def read_text(self, endpoint):
        self.reads.append(endpoint)
        return "The assertion failed at the preserved behavior check."


@pytest.mark.parametrize("wrong", ["head", "pr"])
def test_job_log_reads_never_cross_delivery_identity(tmp_path, wrong):
    snap, packet = code_only_packet(tmp_path)
    request = recovery.validate_requests(
        {"evidence_requests": [log_request(snap)]}, snap, packet, {"requirements": []}
    )
    gh = LogGitHub(snap, wrong_head=wrong == "head", wrong_pr=wrong == "pr")
    rows = recovery.collect_job_logs(gh, snap, request, tmp_path)
    assert rows[0]["status"] == "unavailable"
    assert not any(p.endswith("/logs") for p in gh.reads)


def test_one_specific_log_can_be_necessary_without_all_artifacts(tmp_path):
    snap, packet = code_only_packet(tmp_path)
    request = recovery.validate_requests(
        {"evidence_requests": [log_request(snap)]}, snap, packet, {"requirements": []}
    )
    rows = recovery.collect_job_logs(LogGitHub(snap), snap, request, tmp_path)
    assert rows[0]["status"] == "complete"
    packet["sections"]["requested_evidence"] = evidence._section("complete", rows)
    screen = recovery.apply_requests({"requirements": []}, request, packet)
    assert screen["evaluation_ready"]
    assert packet["sections"]["ci_artifacts"]["status"] == "missing"
    rows[0]["raw"] = "Forged displayed output"
    assert not recovery.apply_requests({"requirements": []}, request, packet)["evaluation_ready"]


@pytest.mark.parametrize(
    "kind", ["transient_api_failure", "legitimate_absence", "competing_pr_ambiguity", "unknown"]
)
def test_observed_absence_or_transience_does_not_file_a_defect(tmp_path, kind):
    _snap, packet = code_only_packet(tmp_path)
    assert (
        recovery.confirmed_defects(
            packet,
            packet,
            [{"kind": kind, "section": "ci_artifacts"}],
            retained_dir=tmp_path / "code-only",
        )
        == []
    )


class IssueAPI:
    def __init__(self, *, existing_pr=False, uncertain=False, search_failure=False):
        self.issues, self.comments, self.calls = [], [], []
        self.existing_pr, self.uncertain, self.search_failure = (
            existing_pr,
            uncertain,
            search_failure,
        )

    def __call__(self, endpoint, *, method="GET", fields=None, **_kw):
        from urllib.parse import unquote

        self.calls.append((method, endpoint, fields))
        if endpoint.startswith("search/issues"):
            if self.search_failure:
                raise RuntimeError("temporary search failure")
            query = unquote(endpoint)
            rows = self.issues
            if self.existing_pr and "collector in:title,body" in query:
                rows = [
                    {
                        "number": 857,
                        "title": "Share closer collector bindings",
                        "body": "Native and standalone collector bindings: artifact-downloader-binding src/closer_dispatcher.py:collect_native_evidence.",
                        "html_url": "https://github.com/stranske/Orchestrator/pull/857",
                    }
                ]
            return {"items": rows, "total_count": len(rows), "incomplete_results": False}
        if endpoint.endswith("/issues") and method == "POST":
            if self.uncertain:
                raise RuntimeError("response lost after POST")
            issue = {"number": 900, **fields}
            self.issues.append(issue)
            return issue
        if endpoint.endswith("comments?per_page=100"):
            return self.comments
        if endpoint.endswith("/comments") and method == "POST":
            row = {"id": 42 + len(self.comments), **fields}
            self.comments.append(row)
            return row
        if "/issues/comments/" in endpoint and method == "PATCH":
            row = next(r for r in self.comments if r["id"] == int(endpoint.rsplit("/", 1)[1]))
            row.update(fields)
            return row
        raise AssertionError((method, endpoint))


def report_store(tmp_path):
    store = recovery.RecoveryStore(tmp_path)
    defect = {
        "key": "a" * 64,
        "repository": "stranske/Orchestrator",
        "family": "artifact-downloader-binding",
        "component": "src/closer_dispatcher.py:collect_native_evidence",
        "regression": "Retain exact-head raw evidence with both collector entrypoints.",
    }
    for number in (1, 2):
        store.defect(
            defect,
            {
                "target": f"o/r#{number}",
                "head": "a" * 40,
                "before": {"sha256": "before"},
                "after": {"sha256": "after"},
                "requirements": [],
            },
        )
    return store, defect


def no_improvements(*_a, **_kw):
    return {"present": True, "total_matches": 0, "truncated": False}


def test_defect_reporting_batches_cases_and_reuses_one_issue(tmp_path):
    store, defect = report_store(tmp_path)
    api = IssueAPI()
    first = recovery.flush_defects(store, api, improvement_lookup=no_improvements)
    assert first[0]["status"] == "published"
    creates = [c for c in api.calls if c[0] == "POST" and c[1].endswith("/issues")]
    assert len(creates) == 1
    assert "/issues/1" in creates[0][2]["body"] and "/issues/2" in creates[0][2]["body"]
    assert (
        recovery.flush_defects(store, api, improvement_lookup=no_improvements)[0]["status"]
        == "unchanged"
    )
    store.defect(defect, {"target": "o/r#3", "head": "a" * 40})
    recovery.flush_defects(store, api, improvement_lookup=no_improvements)
    store.defect(defect, {"target": "o/r#4", "head": "a" * 40})
    recovery.flush_defects(store, api, improvement_lookup=no_improvements)
    assert len(api.issues) == 1 and len(api.comments) == 1
    assert any(c[0] == "PATCH" for c in api.calls)


def test_existing_upstream_pr_is_reused_without_a_duplicate_issue(tmp_path):
    store, _defect = report_store(tmp_path)
    api = IssueAPI(existing_pr=True)
    result = recovery.flush_defects(store, api, improvement_lookup=no_improvements)
    assert result[0]["status"] == "published"
    assert not api.issues and len(api.comments) == 1
    assert any("/issues/857/comments" in c[1] for c in api.calls)


@pytest.mark.parametrize("failure", ["search", "uncertain_create"])
def test_reporting_failure_does_not_repost_or_change_recovered_case(tmp_path, failure):
    store, _defect = report_store(tmp_path)
    api = IssueAPI(search_failure=failure == "search", uncertain=failure == "uncertain_create")
    assert (
        recovery.flush_defects(store, api, improvement_lookup=no_improvements)[0]["status"]
        == "reporting_deferred"
    )
    recovery.flush_defects(store, api, improvement_lookup=no_improvements)
    assert len([c for c in api.calls if c[0] == "POST"]) == (
        1 if failure == "uncertain_create" else 0
    )
    with store.locked("root-" + "a" * 64) as record:
        assert len(record["cases"]) == 2


def recovery_callbacks(tmp_path, monkeypatch, *, recover, initial_missing=True):
    import time

    import test_closer_dispatcher as runtime
    import test_closer_disposition as proofs
    import test_closer_pre_model_readiness as readiness_tests

    import closer_pipeline
    import dispatcher as transport

    runtime.no_live_state_or_provider.__wrapped__(monkeypatch, tmp_path)
    snap, packet, *_ = proofs.fixture(tmp_path)
    snap.update(
        state="OPEN",
        active_threads=0,
        draft=False,
        labels=[],
        mergeable="MERGEABLE",
        verifier_history_complete=True,
    )
    original_files = copy.deepcopy(packet["sections"]["diff_files"]["content"]["files"])
    if initial_missing:
        for name in ("diff_files", "ci_artifacts"):
            packet["sections"][name] = evidence._section("missing", reason="targeted read failed")
            reseal_section(packet, name, tmp_path / "evidence")
    import shutil

    old_root, root = tmp_path / "evidence", tmp_path / "state.json.evidence"
    shutil.copytree(old_root, root)

    def remap(value):
        if isinstance(value, dict):
            return {k: remap(v) for k, v in value.items()}
        if isinstance(value, list):
            return [remap(v) for v in value]
        if isinstance(value, str) and value.startswith(str(old_root) + "/"):
            return str(root) + value[len(str(old_root)) :]
        return value

    packet = remap(packet)
    for name in packet["sections"]:
        reseal_section(packet, name, root)
    manifest = root / "manifests" / (packet["manifest"]["packet_sha256"] + ".json")
    manifest.write_bytes(evidence._bytes(packet))
    prep = readiness_tests.prepared(tmp_path, snap, packet)
    callbacks, calls, refreshes = {}, [], []
    backend = runtime.closer(tmp_path, snap, apply=True, workers=True)
    backend.campaign_admission = lambda *_a: {"allowed": True, "timeout": 60}
    monkeypatch.setattr(
        closer_pipeline, "run_pipeline", lambda *_a, **kw: callbacks.update(kw) or {"targets": {}}
    )
    monkeypatch.setattr(
        dispatcher,
        "_pipeline_low_route",
        lambda **kw: ({"agent": "gemini", "profile_id": "offline-low", "pools": []}, "available"),
    )
    monkeypatch.setattr(dispatcher, "GitHub", lambda **_kw: backend.gh)

    def offload(_agent, prompt, **_kw):
        calls.append(prompt)
        if "gap-directed second-pass" in prompt:
            screen = evidence.action_evidence_readiness(
                snap, packet, "collect_evidence", retained_dir=root
            )
            response = {
                "target": snap["target"],
                "head": snap["head"],
                "base": snap["base"],
                "classification": "relevant",
                "substantive_needs": [],
                "findings": [],
                "evidence_requests": [
                    {
                        "requirement_id": r["id"],
                        "necessity": "necessary",
                        "reason": "Fetch the exact missing section",
                    }
                    for r in recovery.required(screen)
                ],
                "collection_causes": [{"kind": "transient_api_failure", "section": "diff_files"}],
            }
        else:
            response = {
                "target": snap["target"],
                "head": snap["head"],
                "base": snap["base"],
                "classification": "uncertain",
                "substantive_needs": ["Original criteria still require closer judgment"],
                "findings": [],
                "decision": {"criteria": []},
            }
        return {"exit": 0, "run_id": "offline-" + str(len(calls)), "output": json.dumps(response)}

    monkeypatch.setattr(transport, "offload", offload)

    def collect(s, gh, root, **kwargs):
        refreshes.append(kwargs["refresh_sections"])
        if recover:
            return evidence.collect_evidence(
                s,
                lambda endpoint, **_kw: {"status": "ahead", "files": original_files},
                root,
                previous=kwargs.get("previous", packet),
                refresh_sections=kwargs["refresh_sections"],
                supplemental_sections=kwargs.get("supplemental_sections"),
                action="collect_evidence",
            )
        path = Path(root) / "manifests" / (packet["manifest"]["packet_sha256"] + ".json")
        evidence._atomic_write(path, evidence._bytes(packet))
        return packet

    monkeypatch.setattr(dispatcher, "collect_native_evidence", collect)
    args = SimpleNamespace(
        state=str(tmp_path / "state.json"), shadow=False, max_workers=1, evidence_recovery=True
    )
    dispatcher._run_staged_pipeline(args, [snap["target"]], backend, time.time() + 120)
    return (
        callbacks,
        {"prepare": prep, "deadline": time.time() + 100, "revision": snap["revision"]},
        calls,
        refreshes,
    )


def test_real_dispatcher_collects_then_rescreens_and_reenters_same_closer(tmp_path, monkeypatch):
    callbacks, context, calls, refreshes = recovery_callbacks(tmp_path, monkeypatch, recover=True)
    result = callbacks["interpret"](context["prepare"]["snapshot"]["target"], context)
    assert len(calls) == 2
    assert "gap-directed second-pass" in calls[0] and "gap-directed second-pass" not in calls[1]
    assert refreshes == [{"diff_files"}]
    assert result["evidence_recovery"]["outcome"] == "recovered"
    assert result["recovered_prepare"]["revision"] == context["prepare"]["revision"]
    assert not result.get("accepted") and result["authority"] is False


def test_real_dispatcher_retains_no_progress_and_does_not_buy_another_collection(
    tmp_path, monkeypatch
):
    callbacks, context, calls, refreshes = recovery_callbacks(tmp_path, monkeypatch, recover=False)
    target = context["prepare"]["snapshot"]["target"]
    first = callbacks["interpret"](target, context)
    assert first["status"] == "pending"
    assert first["evidence_recovery"]["outcome"] == "no_progress"
    second = callbacks["interpret"](target, {**context, "prepare": first["recovered_prepare"]})
    assert second["status"] == "pending"
    assert len(calls) == 1 and refreshes == [{"diff_files"}]


def test_authoritative_capacity_reset_resumes_one_saved_plan_without_model_recollection(
    tmp_path, monkeypatch
):
    callbacks, context, calls, refreshes = recovery_callbacks(tmp_path, monkeypatch, recover=True)
    target = context["prepare"]["snapshot"]["target"]
    original = dispatcher.collect_native_evidence
    stopped = False

    def limited(*args, **kwargs):
        nonlocal stopped
        if not stopped:
            stopped = True
            raise dispatcher.RoundCapacityDeferred(
                resource="core", throttle={"reset_in_s": 10}, observed_at=100
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(dispatcher, "collect_native_evidence", limited)
    monkeypatch.setattr(recovery, "time", SimpleNamespace(time=lambda: 100))
    with pytest.raises(dispatcher.RoundCapacityDeferred):
        callbacks["interpret"](target, context)
    assert len(calls) == 1
    waiting = callbacks["interpret"](target, context)
    assert waiting["status"] == "pending" and len(calls) == 1
    monkeypatch.setattr(recovery, "time", SimpleNamespace(time=lambda: 111))
    result = callbacks["interpret"](target, context)
    assert result["evidence_recovery"]["outcome"] == "recovered"
    # Original collection model + normal closer assessment; the saved plan was
    # resumed at the real reset without another collection-model request.
    assert len(calls) == 2
    assert refreshes == [{"diff_files"}]


def test_raw_log_transport_preserves_json_text_without_round_trip(
    tmp_path, monkeypatch, no_external_calls
):

    raw = '{ "event": "failure", "count": 1 }\n'
    response = SimpleNamespace(returncode=0, stdout="HTTP/2.0 200 OK\r\n\r\n" + raw)
    rc, result = no_external_calls(
        ["api", "repos/o/r/actions/jobs/1/logs"], runner=lambda *_a, **_kw: response, raw_body=True
    )
    assert rc == 0 and result == raw


def test_selective_refresh_discards_old_revision_caches(tmp_path):
    snap, packet = code_only_packet(tmp_path)
    snap["revision"] = "changed-source-or-review-revision"
    calls = []

    def api(endpoint, **_kw):
        calls.append(endpoint)
        if "/compare/" in endpoint:
            return {"status": "ahead", "files": []}
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": []}
        raise AssertionError(endpoint)

    evidence.collect_evidence(
        snap, api, tmp_path / "code-only", previous=packet, refresh_sections={"diff_files"}
    )
    assert any("/actions/runs?" in call for call in calls)


@pytest.mark.parametrize("level", ["necessary", "useful", "nice_to_have", "possibly_useful"])
def test_real_closer_routes_only_necessary_requested_extras_to_targeted_reads(
    tmp_path, monkeypatch, level
):
    import dispatcher as transport

    callbacks, context, _calls, refreshes = recovery_callbacks(
        tmp_path, monkeypatch, recover=True, initial_missing=False
    )
    snap = context["prepare"]["snapshot"]
    log_gh = LogGitHub(snap)
    gh = dispatcher.GitHub(deadline=context["deadline"])
    monkeypatch.setattr(gh, "api", log_gh.api)
    monkeypatch.setattr(gh, "read_text", log_gh.read_text, raising=False)
    model_calls = []
    request = {**log_request(snap), "necessity": level}

    def offload(_agent, prompt, **_kwargs):
        model_calls.append(prompt)
        response = {
            "target": snap["target"],
            "head": snap["head"],
            "base": snap["base"],
            "classification": "uncertain",
            "substantive_needs": (
                [request["reason"]]
                if len(model_calls) == 1
                else ["Original acceptance still needs closer judgment"]
            ),
            "findings": [],
            "decision": {"criteria": []},
        }
        if len(model_calls) == 1:
            response["evidence_requests"] = [request]
        return {
            "exit": 0,
            "run_id": "extra-" + str(len(model_calls)),
            "output": json.dumps(response),
        }

    monkeypatch.setattr(transport, "offload", offload)
    result = callbacks["interpret"](snap["target"], context)
    if level == "necessary":
        assert len(model_calls) == 2 and len(log_gh.reads) == 3
        assert refreshes == [set()]
        assert result["evidence_recovery"]["outcome"] == "recovered"
    else:
        assert len(model_calls) == 1 and not log_gh.reads and not refreshes
        assert request["reason"] not in result["substantive_needs"]
    assert result["authority"] is False


def test_large_cohort_is_batched_without_creating_an_issue_per_packet(tmp_path):
    store, defect = report_store(tmp_path)
    # One root cause, many cases. Bodies remain bounded for GitHub transport;
    # all cases remain represented in the same issue and grouped comments.
    for number in range(3, 250):
        store.defect(
            defect,
            {
                "target": f"o/r#{number}",
                "subject_pr": f"o/r#{number}",
                "head": "a" * 40,
                "before": {"sha256": "b" * 64},
                "after": {"sha256": "c" * 64},
                "requirements": [{"id": "d" * 64}, {"id": "e" * 64}],
            },
        )
    api = IssueAPI()
    result = recovery.flush_defects(store, api, improvement_lookup=no_improvements)
    assert result[0]["status"] == "published"
    assert len(api.issues) == 1 and len(api.comments) >= 1
    bodies = [r["body"] for r in [*api.issues, *api.comments]]
    assert all(len(body.encode()) <= 48000 for body in bodies)
    for number in range(3, 250):
        assert any(f"/pull/{number} " in body for body in bodies)
    count = len(api.calls)
    assert (
        recovery.flush_defects(store, api, improvement_lookup=no_improvements)[0]["status"]
        == "unchanged"
    )
    assert len(api.calls) == count


def test_partial_log_progress_is_kept_and_only_unresolved_job_is_retried(tmp_path, monkeypatch):
    import dispatcher as transport

    callbacks, context, _calls, refreshes = recovery_callbacks(
        tmp_path, monkeypatch, recover=True, initial_missing=False
    )
    snap = context["prepare"]["snapshot"]
    ready_second, reads, model_calls = False, [], []
    gh = dispatcher.GitHub(deadline=context["deadline"])

    def api(endpoint, **_kw):
        if "/jobs/" in endpoint:
            return {"id": int(endpoint.rsplit("/", 1)[1]), "run_id": 7}
        return {
            "id": 7,
            "head_sha": snap["head"],
            "run_attempt": 1,
            "pull_requests": [{"number": int(snap["target"].split("#")[1])}],
        }

    def raw(endpoint):
        job = int(endpoint.split("/jobs/")[1].split("/")[0])
        reads.append(job)
        if job == 89 and not ready_second:
            raise TimeoutError("temporary read failure")
        return "Exact retained assertion for job " + str(job)

    monkeypatch.setattr(gh, "api", api)
    monkeypatch.setattr(gh, "read_text", raw, raising=False)
    requests = [
        log_request(snap),
        {**log_request(snap), "locator": {"kind": "job_log", "job_id": 89}},
    ]

    def offload(_agent, prompt, **_kwargs):
        model_calls.append(prompt)
        proposal = {
            "target": snap["target"],
            "head": snap["head"],
            "base": snap["base"],
            "classification": "uncertain",
            "substantive_needs": ["Original criteria still require judgment"],
            "findings": [],
            "decision": {"criteria": []},
        }
        if len(model_calls) == 1:
            proposal["evidence_requests"] = requests
        return {
            "exit": 0,
            "run_id": "partial-" + str(len(model_calls)),
            "output": json.dumps(proposal),
        }

    monkeypatch.setattr(transport, "offload", offload)
    first = callbacks["interpret"](snap["target"], context)
    assert (
        first["status"] == "pending" and first["evidence_recovery"]["outcome"] == "partial_progress"
    )
    assert reads == [88, 89] and len(model_calls) == 1
    ready_second = True
    second = callbacks["interpret"](
        snap["target"], {**context, "prepare": first["recovered_prepare"]}
    )
    assert second["evidence_recovery"]["outcome"] == "recovered"
    assert reads == [88, 89, 89] and len(model_calls) == 2
    assert refreshes == [set(), set()]


@pytest.mark.parametrize("stage", ["exception", "act"])
@pytest.mark.parametrize("fault", ["head", "revision", "snapshot_hash"])
def test_recovered_preparation_cannot_cross_revision_or_use_compact_fallback(
    tmp_path, monkeypatch, stage, fault
):
    import hashlib

    callbacks, context, calls, _refreshes = recovery_callbacks(
        tmp_path, monkeypatch, recover=True, initial_missing=False
    )
    replacement = copy.deepcopy(context["prepare"])
    if fault == "revision":
        replacement["revision"] = "different"
    elif fault == "snapshot_hash":
        replacement["snapshot_ref"]["sha256"] = "0" * 64
    else:
        raw = json.loads(Path(replacement["snapshot_ref"]["path"]).read_bytes())
        raw["head"] = "f" * 40
        data = json.dumps(raw).encode()
        path = tmp_path / "wrong-head-snapshot.json"
        path.write_bytes(data)
        replacement["snapshot_ref"] = {
            "path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    with pytest.raises(ValueError, match="recovered preparation"):
        callbacks[stage](
            context["prepare"]["snapshot"]["target"],
            {
                **context,
                "action_state_path": str(tmp_path / "never-created-queue.json"),
                "interpret": {"recovered_prepare": replacement},
            },
        )
    assert not calls and not (tmp_path / "never-created-queue.json").exists()


@pytest.mark.parametrize("section", ["ci_artifacts", "native_merge_verifier"])
def test_confirmed_downloader_omissions_group_across_packet_types(tmp_path, section):
    import test_closer_disposition as proofs

    _snap, after, *_ = proofs.fixture(tmp_path, native=section == "native_merge_verifier")
    before = copy.deepcopy(after)
    before["sections"][section] = evidence._section(
        "missing",
        {"unavailable": [{"reason": "artifact content unavailable", "expired": False}]},
        reason=(
            "native verifier artifact downloader unavailable"
            if section == "native_merge_verifier"
            else "artifact content unavailable"
        ),
    )
    reseal_section(before, section, tmp_path / "evidence")
    defects = recovery.confirmed_defects(
        before,
        after,
        [{"kind": "collector_contract", "section": section}],
        retained_dir=tmp_path / "evidence",
    )
    assert len(defects) == 1 and defects[0]["family"] == "artifact-downloader-binding"
    assert defects[0]["key"] == recovery.digest(
        [
            "stranske/Orchestrator",
            "closer_dispatcher.collect_native_evidence",
            "artifact-downloader-binding",
        ]
    )


def test_upstream_post_intent_is_durable_before_request_leaves(tmp_path):
    store, _defect = report_store(tmp_path)
    original = IssueAPI()

    def api(endpoint, **kwargs):
        if endpoint.endswith("/issues") and kwargs.get("method") == "POST":
            record = json.loads((store.root / ("root-" + "a" * 64 + ".json")).read_bytes())
            assert record["publication"]["state"] == "create_pending"
        return original(endpoint, **kwargs)

    result = recovery.flush_defects(store, api, improvement_lookup=no_improvements)
    assert result[0]["status"] == "published"


def test_completed_job_read_survives_later_capacity_deferral(tmp_path):
    snap, packet = code_only_packet(tmp_path)
    proposals = [
        log_request(snap),
        {**log_request(snap), "locator": {"kind": "job_log", "job_id": 89}},
    ]
    requests = recovery.validate_requests(
        {"evidence_requests": proposals}, snap, packet, {"requirements": []}
    )
    gh = LogGitHub(snap)
    original = gh.api

    def api(endpoint):
        if endpoint.endswith("/jobs/89"):
            raise dispatcher.RoundCapacityDeferred(
                resource="core", throttle={"reset_in_s": 30}, observed_at=100
            )
        return original(endpoint)

    gh.api = api
    store = recovery.RecoveryStore(tmp_path)
    screen = {"requirements": requests}
    key = store.begin(snap, packet, screen)

    def observe(row):
        store.observe(
            snap,
            key,
            row,
            prep={"packet_ref": {"sha256": "original"}},
            requests=requests,
            plan={"evidence_requests": proposals},
            actual={"run_id": "one-completed-plan"},
        )

    with pytest.raises(dispatcher.RoundCapacityDeferred):
        recovery.collect_job_logs(gh, snap, requests, tmp_path, observe=observe)
    latest = store.latest(snap)
    assert len(latest["observations"]) == 1
    assert "raw" not in next(iter(latest["observations"].values()))
    restored = recovery.restored_observations(latest, tmp_path)
    assert restored["content"][0]["raw"].startswith("The assertion failed")
    packet["sections"]["requested_evidence"] = restored
    remaining = recovery.required(recovery.apply_requests({"requirements": []}, requests, packet))
    assert [r["locator"]["job_id"] for r in remaining] == [89]


def test_native_read_progress_keeps_completed_pages_and_archives_without_copying_zip(tmp_path):
    import io
    import time
    import zipfile

    snap = {"target": "o/r#1", "head": "a" * 40, "revision": "revision"}
    progress = recovery.ReadProgress(tmp_path, snap)
    calls = []

    def api(endpoint, **_kwargs):
        calls.append(endpoint)
        if endpoint.endswith("/runs/2"):
            raise dispatcher.RoundCapacityDeferred(
                resource="core", throttle={"reset_in_s": 30}, observed_at=100
            )
        return {"id": 1, "head_sha": snap["head"], "status": "completed"}

    first = progress.api(api, "repos/o/r/actions/runs/1")
    with pytest.raises(dispatcher.RoundCapacityDeferred):
        progress.api(api, "repos/o/r/actions/runs/2")
    assert progress.api(api, "repos/o/r/actions/runs/1") == first
    assert calls == ["repos/o/r/actions/runs/1", "repos/o/r/actions/runs/2"]
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("report.txt", "retained result")
    archive_calls = []

    def fetch(*_a, **_kw):
        archive_calls.append(True)
        return stream.getvalue()

    assert (
        progress.artifact({"id": 7}, fetch, deadline=time.monotonic() + 30, max_bytes=10000)
        == stream.getvalue()
    )
    recovery.ReadProgress(tmp_path, snap).artifact(
        {"id": 7}, fetch, deadline=time.monotonic() + 30, max_bytes=10000
    )
    evidence._retain_bytes(tmp_path, stream.getvalue(), "zip")
    assert len(archive_calls) == 1 and len(list(tmp_path.rglob("*.zip"))) == 1
    with pytest.raises(ValueError, match="mutation"):
        progress.api(api, "repos/o/r/issues", method="POST", fields={"body": "must not post"})


def test_redacted_log_is_bounded_assessment_input_not_completed_acceptance(tmp_path):
    snap, packet = code_only_packet(tmp_path)
    request = recovery.validate_requests(
        {"evidence_requests": [log_request(snap)]}, snap, packet, {"requirements": []}
    )
    gh = LogGitHub(snap)
    gh.read_text = lambda _endpoint: "The requested assertion failed. token=supersecretvalue"
    rows = recovery.collect_job_logs(gh, snap, request, tmp_path)
    assert rows[0]["status"] == "partial" and rows[0]["redaction_applied"]
    packet["sections"]["requested_evidence"] = evidence._section("partial", rows)
    screen = recovery.apply_requests({"requirements": []}, request, packet)
    assert (
        screen["evaluation_ready"] and screen["requirements"][0]["observation_status"] == "partial"
    )
    assert "supersecretvalue" not in json.dumps(packet)
    assert not dispatcher.acceptance_ok(snap, {})


def test_actual_native_paginator_resumes_completed_page_after_capacity_deferral(tmp_path):
    import time

    from test_closer_native_collector import api_base, connection, snapshot, thread

    snap = snapshot()
    calls, second_allowed = [], False

    def api(endpoint, **kwargs):
        if endpoint != "graphql":
            return api_base(endpoint, **kwargs)
        query = kwargs["fields"]["query"]
        second = 'after: "page-1"' in query
        calls.append(2 if second else 1)
        if second and not second_allowed:
            raise dispatcher.RoundCapacityDeferred(
                resource="graphql", throttle={"reset_in_s": 30}, observed_at=100
            )
        return connection([thread(2)]) if second else connection([thread(1)], True, "page-1")

    gh = SimpleNamespace(api=api)
    with pytest.raises(dispatcher.RoundCapacityDeferred):
        dispatcher.collect_native_evidence(
            snap,
            gh,
            tmp_path,
            deadline=time.monotonic() + 30,
            read_progress=recovery.ReadProgress(tmp_path, snap),
        )
    second_allowed = True
    packet = dispatcher.collect_native_evidence(
        snap,
        gh,
        tmp_path,
        deadline=time.monotonic() + 30,
        read_progress=recovery.ReadProgress(tmp_path, snap),
    )
    assert evidence.validate_packet(packet)
    assert packet["sections"]["review_conversations"]["status"] == "complete"
    assert len(packet["sections"]["review_conversations"]["content"]) == 2
    assert calls == [1, 2, 2]


def test_necessary_extra_is_delivered_before_optional_context_can_consume_window(tmp_path):
    _snap, packet = code_only_packet(tmp_path)
    root = tmp_path / "code-only"
    packet["sections"]["requested_evidence"] = evidence._section(
        "complete", [{"raw": "The specifically requested assertion result."}]
    )
    # Register the extension in the existing packet manifest, then seal it.
    packet["manifest"]["section_files"]["requested_evidence"] = None
    reseal_section(packet, "requested_evidence", root)
    assert evidence.validate_packet(packet)
    context = dispatcher.data_only_evidence_context(
        packet,
        readiness={
            "requirements": [
                {"section": "requested_evidence", "necessity": "necessary", "satisfied": True}
            ]
        },
        max_bytes=1600,
    )
    assert context["sections"]["requested_evidence"]["content_supplied"] is True
    assert list(context["sections"]).index("requested_evidence") < list(context["sections"]).index(
        "ci_artifacts"
    )


@pytest.mark.parametrize(
    "body",
    [
        "Repair native and standalone collector bindings for CSV delimiter detection.",
        "collector bindings artifact-downloader-binding src/unrelated.py:collect_native_evidence",
        "collector bindings review-pagination-binding src/closer_dispatcher.py:collect_native_evidence",
    ],
)
def test_related_collector_wording_cannot_confirm_an_unrelated_root(tmp_path, body):
    store, _defect = report_store(tmp_path)
    api = IssueAPI(existing_pr=True)

    def unrelated(endpoint, **kwargs):
        answer = api(endpoint, **kwargs)
        if endpoint.startswith("search/issues"):
            for row in answer["items"]:
                row["body"] = body
        return answer

    result = recovery.flush_defects(store, unrelated, improvement_lookup=no_improvements)
    assert result[0]["status"] == "candidate_needs_root_confirmation"
    assert not api.issues and not api.comments
    assert not any(call[0] in {"POST", "PATCH"} for call in api.calls)
