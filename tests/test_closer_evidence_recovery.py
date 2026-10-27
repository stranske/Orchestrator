"""Offline recovery boundary tests: necessity, retained progress and grouped defects."""

from __future__ import annotations

import copy
import hashlib
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

    def api(self, endpoint, *, method="GET", fields=None):
        assert method == "GET" and fields is None
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


def inert_caller(tmp_path, monkeypatch):
    """Use the shared real broker/receipt fixture, never invent an origin."""
    import test_closer_dispatcher as runtime

    controlled = runtime.controlled_world.__wrapped__(tmp_path, monkeypatch)
    return runtime.InertCaller(controlled, monkeypatch)


def declared_unknown_assessment(prompt, reason="Original criteria still require closer judgment"):
    """New synthetic test spec: every original obligation remains unproved.

    This is emitted before sealing and never parses or repairs a captured answer.
    It preserves the old fixture's explicit uncertainty without inventing proof.
    """
    from closer_typed_semantic_broker_fixture import delivered_inventory, emit_assessment

    view = delivered_inventory(prompt)
    gold = [
        {"source": row["source"], "criterion": row["text"], "result": "UNKNOWN", "proofs": []}
        for row in view["obligations"]
    ]
    return emit_assessment(prompt, gold, global_explanations=[reason])


def recovery_callbacks(tmp_path, monkeypatch, *, recover, initial_missing=True, causes=None):
    import time

    import test_closer_dispatcher as runtime
    import test_closer_disposition as proofs
    import test_closer_pre_model_readiness as readiness_tests

    import closer_pipeline
    import dispatcher as transport

    runtime.no_live_state_or_provider.__wrapped__(monkeypatch, tmp_path)
    snap, packet, *_ = proofs.fixture(tmp_path)
    # Recovery here serves post-verification acceptance, not the deterministic
    # merge path for an OPEN green PR. A bound PASS still needs source judgment.
    snap.update(
        state="MERGED",
        merge="d" * 40,
        active_threads=0,
        draft=False,
        labels=[],
        mergeable="MERGEABLE",
        verifier_history_complete=True,
    )
    snap["verifier"] = {
        "schema": "verifier-corpus-decision/v1",
        "verdict": "PASS",
        "repo": "owner/repo",
        "pr": 12,
        "head_sha": snap["head"],
        "evaluated_sha": snap["merge"],
        "run_id": 34,
        "run_attempt": 1,
        "provider_verdicts": ["PASS"],
        "ci_failed": False,
    }
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
    import closer_semantic_assessment as semantic

    source_host_plan = semantic.host_collection_plan
    host_plans = []

    def observed_host_plan(*args, **kwargs):
        plan = source_host_plan(*args, **kwargs)
        host_plans.append(copy.deepcopy(plan))
        return plan

    monkeypatch.setattr(semantic, "host_collection_plan", observed_host_plan)
    backend = runtime.closer(tmp_path, snap, apply=True, workers=True)
    caller = inert_caller(tmp_path, monkeypatch)
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
                "collection_causes": (
                    causes
                    if causes is not None
                    else [{"kind": "transient_api_failure", "section": "diff_files"}]
                ),
            }
        else:
            response = declared_unknown_assessment(prompt)
        return caller.response({"exit": 0, "output": json.dumps(response)}, _agent, prompt, **_kw)

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
        state=str(tmp_path / "state.json"),
        shadow=False,
        max_workers=1,
        evidence_recovery=True,
    )

    def invoke(stage, target, context):
        observed = {}

        def execute(_path, _targets, **owned):
            observed["result"] = owned[stage](target, context)
            return {"targets": {target: observed["result"]}}

        # The callback executes while the genuine CLI-owned context and scoped
        # lease are alive. Reentry reopens the same private state/recovery store.
        with monkeypatch.context() as local:
            local.setattr(closer_pipeline, "run_pipeline", execute)
            local.setattr(dispatcher, "GitHub", lambda **_kw: backend.gh)
            caller.staged(args, [target], backend, time.time() + 120)
        return observed["result"]

    callbacks.update(
        {
            stage: (lambda target, context, stage=stage: invoke(stage, target, context))
            for stage in ("interpret", "exception", "act")
        }
    )
    callbacks["_inert_caller"] = caller
    callbacks["_host_plans"] = host_plans
    return (
        callbacks,
        {"prepare": prep, "deadline": time.time() + 100, "revision": snap["revision"]},
        calls,
        refreshes,
    )


def test_real_dispatcher_collects_then_rescreens_and_reenters_same_closer(tmp_path, monkeypatch):
    callbacks, context, calls, refreshes = recovery_callbacks(tmp_path, monkeypatch, recover=True)
    result = callbacks["interpret"](context["prepare"]["snapshot"]["target"], context)
    assert len(calls) == 1
    assert "gap-directed second-pass" not in calls[0]
    assert callbacks["_host_plans"][0]["source"] == "existing_readiness_and_admitted_requests"
    assert refreshes == [{"diff_files"}]
    assert result["evidence_recovery"]["outcome"] == "recovered"
    assert result["recovered_prepare"]["revision"] == context["prepare"]["revision"]
    assert not result.get("accepted") and result["authority"] is False
    actual_runs = callbacks["_inert_caller"].results
    assert len(actual_runs) == 1
    for actual in actual_runs:
        reference = actual["provider_result_ref"]
        path = Path(reference["path"])
        assert path.resolve().is_relative_to(tmp_path.resolve())
        raw = path.read_bytes()
        assert reference["sha256"] == hashlib.sha256(raw).hexdigest()
        retained = json.loads(raw)["result"]
        assert retained["run_id"] == actual["run_id"]
        assert retained["data_only_origin"] == actual["data_only_origin"]
        assert actual["data_only_origin"]["process_observation"]["cleanup_terminal"] is True
        assert json.loads(actual["data_only_origin"]["native_json"])["type"] == "result"


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
    assert not calls and refreshes == [{"diff_files"}]
    assert callbacks["_host_plans"][0]["source"] == "existing_readiness_and_admitted_requests"


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
    assert not calls
    waiting = callbacks["interpret"](target, context)
    assert waiting["status"] == "pending" and not calls
    monkeypatch.setattr(recovery, "time", SimpleNamespace(time=lambda: 111))
    result = callbacks["interpret"](target, context)
    assert result["evidence_recovery"]["outcome"] == "recovered"
    # The saved HOST plan resumes at the real reset with no model planning;
    # exactly one subsequent ordinary assessment is physically retained.
    assert len(calls) == 1
    assert callbacks["_host_plans"][0]["source"] == "existing_readiness_and_admitted_requests"
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

    context["prepare"]["recovery_requests"] = recovery.validate_requests(
        {"evidence_requests": [request]}, snap, {}, {"requirements": []}
    )

    def offload(_agent, prompt, **_kwargs):
        model_calls.append(prompt)
        response = declared_unknown_assessment(prompt)
        return callbacks["_inert_caller"].response(
            {
                "exit": 0,
                "output": json.dumps(response),
            },
            _agent,
            prompt,
            **_kwargs,
        )

    monkeypatch.setattr(transport, "offload", offload)
    result = callbacks["interpret"](snap["target"], context)
    if level == "necessary":
        assert len(model_calls) == 1 and len(log_gh.reads) == 3
        assert callbacks["_host_plans"][0]["source"] == "existing_readiness_and_admitted_requests"
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

    context["prepare"]["recovery_requests"] = recovery.validate_requests(
        {"evidence_requests": requests}, snap, {}, {"requirements": []}
    )

    def offload(_agent, prompt, **_kwargs):
        model_calls.append(prompt)
        proposal = declared_unknown_assessment(prompt)
        return callbacks["_inert_caller"].response(
            {
                "exit": 0,
                "output": json.dumps(proposal),
            },
            _agent,
            prompt,
            **_kwargs,
        )

    monkeypatch.setattr(transport, "offload", offload)
    first = callbacks["interpret"](snap["target"], context)
    assert (
        first["status"] == "pending" and first["evidence_recovery"]["outcome"] == "partial_progress"
    )
    assert reads == [88, 89] and not model_calls
    assert callbacks["_host_plans"][0]["source"] == "existing_readiness_and_admitted_requests"
    ready_second = True
    second = callbacks["interpret"](
        snap["target"], {**context, "prepare": first["recovered_prepare"]}
    )
    assert second["evidence_recovery"]["outcome"] == "recovered"
    assert reads == [88, 89, 89] and len(model_calls) == 1
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


def test_source_display_audit_cannot_displace_original_body_or_necessary_extra_on_long_paths(
    tmp_path,
):
    retained = tmp_path / "ci-long-retained-parent" / "scoped-original-request"
    _snap, packet = code_only_packet(retained)
    root = retained / "code-only"
    packet["sections"]["requested_evidence"] = evidence._section(
        "complete", [{"raw": "The specifically requested assertion result."}]
    )
    packet["manifest"]["section_files"]["requested_evidence"] = None
    reseal_section(packet, "requested_evidence", root)
    before = copy.deepcopy(packet)
    full_source = packet["sections"]["source"]
    assert (
        full_source["content"][0]["title_transform"]["schema"] == "closer-source-title-transform/v1"
    )
    context = dispatcher.data_only_evidence_context(
        packet,
        readiness={
            "requirements": [
                {"section": "requested_evidence", "necessity": "necessary", "satisfied": True}
            ]
        },
        max_bytes=1600,
    )
    source = context["sections"]["source"]
    assert source["content_supplied"] is True
    assert context["sections"]["requested_evidence"]["content_supplied"] is True
    for original, projected in zip(full_source["content"], source["content"], strict=True):
        assert projected["body"] == original["body"]
        assert projected["body_sha256"] == original["body_sha256"]
        assert projected["title_sha256"] == original["title_sha256"]
        assert projected["request_revision"] == original["request_revision"]
        assert "title_transform" not in projected
    assert source["content_projection"]["omitted_optional_fields"] == ["title_transform"]
    assert source["content_projection"]["full_content_bytes"] > source["content_bytes"]
    assert (
        source["retained"] == full_source["retained"] and source["sha256"] == full_source["sha256"]
    )
    assert packet == before and evidence.validate_packet(packet)


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


@pytest.mark.parametrize("failure", ["routing", "deadline"])
def test_assessment_failure_cannot_overwrite_successful_recovery(tmp_path, monkeypatch, failure):
    callbacks, context, calls, refreshes = recovery_callbacks(tmp_path, monkeypatch, recover=True)
    target = context["prepare"]["snapshot"]["target"]
    original = dispatcher._pipeline_low_route
    routes = []

    def fail_assessment(**kwargs):
        routes.append(kwargs)
        if len(routes) >= 1:
            if failure == "deadline":
                raise dispatcher.RoundDeadlineExhausted(deadline=0, remaining_seconds=0)
            raise RuntimeError("low provider capacity snapshot is stale or unavailable")
        return original(**kwargs)

    # A selector/read failure is confirmed before allocation or invocation.
    # Throwing from an invoked offload cannot prove the provider never started.
    monkeypatch.setattr(dispatcher, "_pipeline_low_route", fail_assessment)
    with pytest.raises((RuntimeError, dispatcher.RoundDeadlineExhausted)):
        callbacks["interpret"](target, context)
    records = list((tmp_path / "state.json.evidence" / "recovery").glob("*.json"))
    latest = json.loads(records[0].read_text())["latest"]
    assert latest["outcome"] == "recovered"
    assert latest["prepare"]["packet_ref"] != context["prepare"]["packet_ref"]
    monkeypatch.setattr(dispatcher, "_pipeline_low_route", original)
    result = callbacks["interpret"](target, {**context, "prepare": latest["prepare"]})
    assert result["classification"] == "relevant" and result["substantive_needs"]
    assert len(calls) == 1 and refreshes == [{"diff_files"}]


@pytest.mark.parametrize("failure", ["deadline", "capacity_without_reset"])
def test_read_deferral_without_reset_resumes_saved_plan(tmp_path, monkeypatch, failure):
    callbacks, context, calls, refreshes = recovery_callbacks(tmp_path, monkeypatch, recover=True)
    target = context["prepare"]["snapshot"]["target"]
    original = dispatcher.collect_native_evidence

    def deferred(*args, **kwargs):
        if failure == "deadline":
            raise dispatcher.RoundDeadlineExhausted(deadline=0, remaining_seconds=0)
        raise dispatcher.RoundCapacityDeferred(resource="core", throttle={}, observed_at=100)

    monkeypatch.setattr(dispatcher, "collect_native_evidence", deferred)
    with pytest.raises((dispatcher.RoundDeadlineExhausted, dispatcher.RoundCapacityDeferred)):
        callbacks["interpret"](target, context)
    monkeypatch.setattr(dispatcher, "collect_native_evidence", original)
    result = callbacks["interpret"](target, context)
    assert result["evidence_recovery"]["outcome"] == "recovered"
    assert len(calls) == 1 and refreshes == [{"diff_files"}]


def test_deadline_before_provider_start_is_retryable_but_uncertain_invocation_is_held(
    tmp_path, monkeypatch
):
    callbacks, context, calls, _refreshes = recovery_callbacks(tmp_path, monkeypatch, recover=True)
    target = context["prepare"]["snapshot"]["target"]
    original = dispatcher._execute_profile_route
    monkeypatch.setattr(
        dispatcher,
        "_execute_profile_route",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            dispatcher.RoundDeadlineExhausted(deadline=0, remaining_seconds=0)
        ),
    )
    with pytest.raises(dispatcher.RoundDeadlineExhausted):
        callbacks["interpret"](target, context)
    assert not calls
    monkeypatch.setattr(dispatcher, "_execute_profile_route", original)
    records = list((tmp_path / "state.json.evidence" / "recovery").glob("*.json"))
    latest = json.loads(records[0].read_text())["latest"]
    assert latest["outcome"] == "recovered" and not latest["plan_actual"].get("run_id")
    result = callbacks["interpret"](target, {**context, "prepare": latest["prepare"]})
    assert len(calls) == 1 and result["authority"] is False


def test_bounded_job_log_preserves_failure_tail_and_provenance(tmp_path):
    snap, packet = code_only_packet(tmp_path)
    requests = recovery.validate_requests(
        {"evidence_requests": [log_request(snap)]}, snap, packet, {"requirements": []}
    )
    gh = LogGitHub(snap)
    raw = "runner setup\n" * 5000 + "α failure assertion at final step token=secretvalue\n"
    gh.read_text = lambda _endpoint: raw
    row = recovery.collect_job_logs(gh, snap, requests, tmp_path)[0]
    assert row["status"] == "partial" and row["retained_window"] == "tail"
    assert "failure assertion at final step" in row["raw"] and "secretvalue" not in row["raw"]
    assert len(row["raw"].encode()) <= evidence.MAX_TEXT_BYTES
    assert row["raw_original_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert row["raw_original_bytes"] == len(raw.encode())


@pytest.mark.parametrize("body", [None, 123, "missing"])
def test_search_normalizes_explicit_null_but_rejects_malformed_bodies(tmp_path, body):
    store, _defect = report_store(tmp_path)
    api = IssueAPI()

    def search(endpoint, **kwargs):
        result = api(endpoint, **kwargs)
        if "collector%20in%3Atitle%2Cbody" in endpoint:
            row = {"number": 42, "title": "Unrelated collector"}
            if body != "missing":
                row["body"] = body
            return {"items": [row], "total_count": 1, "incomplete_results": False}
        return result

    result = recovery.flush_defects(store, search, improvement_lookup=no_improvements)
    assert result[0]["status"] == ("published" if body is None else "reporting_deferred")


@pytest.mark.parametrize(
    "prefix,marker,expected",
    [
        ("closer-evidence-root", "closer-evidence-root", 4),
        ("closer-evidence-root", "adversarial-panel", None),
        (None, "adversarial-panel", 4),
    ],
)
def test_followup_marker_namespace_preserves_default_and_isolates_custom(prefix, marker, expected):
    import adversarial

    row = {"number": 4, "title": "Root defect", "body": f"<!-- {marker}:root -->"}

    def run(_argv):
        return SimpleNamespace(returncode=0, stdout=json.dumps([row]))

    kwargs = {} if prefix is None else {"marker_prefix": prefix}
    assert adversarial._find_followup(run, "o/r", "Root defect", "root", **kwargs) == expected


def test_deadline_raised_after_provider_invocation_remains_held(tmp_path, monkeypatch):
    import dispatcher as transport

    callbacks, context, _calls, _refreshes = recovery_callbacks(tmp_path, monkeypatch, recover=True)
    target = context["prepare"]["snapshot"]["target"]
    started = []

    def uncertain(agent, prompt, **kwargs):
        started.append(True)
        callbacks["_inert_caller"].response({"_native_mode": "uncertain"}, agent, prompt, **kwargs)
        raise dispatcher.RoundDeadlineExhausted(deadline=0, remaining_seconds=0)

    monkeypatch.setattr(transport, "offload", uncertain)
    with pytest.raises(dispatcher.RoundDeadlineExhausted):
        callbacks["interpret"](target, context)
    result = callbacks["interpret"](target, context)
    assert result["status"] == "pending" and len(started) == 1


RETAINED_COLLECTION_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "collection-cause-retained-four.json").read_text()
)


@pytest.mark.parametrize("case", RETAINED_COLLECTION_CASES, ids=lambda case: case["target"])
def test_retained_auxiliary_causes_do_not_block_targeted_read_and_reentry(
    tmp_path, monkeypatch, case
):
    callbacks, context, calls, refreshes = recovery_callbacks(
        tmp_path, monkeypatch, recover=True, causes=case["collection_causes"]
    )
    result = callbacks["interpret"](context["prepare"]["snapshot"]["target"], context)
    assert result["evidence_recovery"]["outcome"] == "recovered"
    assert refreshes == [{"diff_files"}]
    assert len(calls) == 1  # source host plan, then one ordinary assessment
    assert "gap-directed second-pass" not in calls[0]
    assert callbacks["_host_plans"][0]["source"] == "existing_readiness_and_admitted_requests"
    assert recovery.causes_from({"collection_causes": case["collection_causes"]})
    assert result["recovered_prepare"]["revision"] == context["prepare"]["revision"]
    assert result["authority"] is False and not result.get("accepted")


@pytest.mark.parametrize("section", sorted(evidence.REQUIRED_SECTIONS | {"delivery_inventory"}))
def test_cause_classification_can_describe_each_producer_section(section):
    proposal = {"collection_causes": [{"section": section, "kind": "unknown"}]}
    assert recovery.causes_from(proposal) == proposal["collection_causes"]


@pytest.mark.parametrize(
    "row",
    [
        None,
        {"section": "invented", "kind": "unknown"},
        {"section": [], "kind": "unknown"},
        {"section": "source", "kind": {}},
    ],
)
def test_malformed_or_unknown_cause_is_a_recoverable_validation_error(row):
    with pytest.raises(ValueError, match="invalid collection cause"):
        recovery.causes_from({"collection_causes": [row]})


def test_cause_description_contract_does_not_grant_auxiliary_retrieval():
    assert "test_coverage_artifacts" not in recovery.SECTIONS
    assert "prior_dispositions" not in recovery.SECTIONS
    for section in evidence.REQUIRED_SECTIONS:
        assert section in recovery.COLLECTION_INSTRUCTIONS
