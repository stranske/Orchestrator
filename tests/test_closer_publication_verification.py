"""Publication caller contract with mocked source reader/API; no native effects.

The real publication issuer is deliberately absent in the frozen donor. A mocked
reader supplies synthetic DATA to exercise this consumer, never source authority.
"""

import copy
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_closer_queue import _run

import closer_coding_publication as publication
import closer_correction_controller as ctl
import closer_correction_handoff as wire
import closer_correction_native_effects as native_effects
import closer_dispatcher as native
import closer_publication_verification as join


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def reference(schema, name):
    return {"schema": schema, "name": name + ".json", "sha256": "f" * 64}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_STORAGE_STATE_DIR", str(tmp_path / "storage"))
    request = json.loads(
        (Path(__file__).parent / "fixtures/closer_correction/request.json").read_text()
    )
    body, issue_body = (
        "Existing corrective PR request",
        "## Tasks\n- [ ] Preserve original issue and tests",
    )
    # Coding-owner metadata is not the native GitHub assignee identity.
    request["incumbent"]["owner"] = "coding-agent"
    request["incumbent"]["issue"]["body_sha256"] = sha(issue_body)
    request["source_issue"]["body_sha256"] = sha("Original source request")
    finding_text = "Original substantive finding"
    request["finding"]["evidence"]["sha256"] = sha(finding_text)
    request["required_tests"][0]["source_evidence"] = copy.deepcopy(request["finding"]["evidence"])
    request["request_sha256"] = wire.digest(
        {k: v for k, v in request.items() if k != "request_sha256"}
    )
    scope = copy.deepcopy(request["incumbent"]["issue"])
    original = dict(
        repository_id=11,
        epoch="epoch",
        effect_key=ctl.effect_key(request),
        request_sha256=ctl.digest(request),
        consumer_id="codex",
    )
    pr = dict(
        repository="stranske/example",
        repository_id=11,
        pr_number=9,
        pr_node_id="PR_9",
        issue_node_id=scope["node_id"],
        owner_login="instruction-writer",
        owner_id=41,
        writer_login="instruction-writer",
        writer_id=41,
        head_repo_id=11,
        base_repo_id=11,
        head_ref="correction",
        head_oid="d" * 40,
        base_ref="main",
        base_oid="c" * 40,
        body_sha256=sha(body),
        state="open",
        is_draft=False,
        is_merged=False,
        closing_issue_node_ids=[scope["node_id"]],
    )
    result_ref = reference(
        "closer-owned-pr-publication-result-reference/v1", "publication-result-" + "f" * 64
    )
    provenance = {k: "f" * 64 for k in join._PROVENANCE_KEYS if k.endswith("_sha256")}
    for key in join._PROVENANCE_KEYS:
        if key.endswith("_reference"):
            provenance[key] = reference(join._REFERENCE_SCHEMAS[key], key)
    provenance["coding_terminal_reference"]["schema"] = "closer-coding-worker-terminal-reference/v1"
    provenance["publication_result_reference"] = copy.deepcopy(result_ref)
    provenance["scope_issue"] = scope
    for key, prefix in (
        ("publication_origin_reference", "native-publication-origin-"),
        ("io_admission_reference", "publication-io-admission-"),
    ):
        provenance[key]["name"] = prefix + provenance[key]["sha256"] + ".json"
    result = dict(
        schema=join.RESULT_SCHEMA,
        request=request,
        original=original,
        findings=[
            {**copy.deepcopy(request["finding"]), "text": finding_text},
            {
                "id": "other",
                "text": "Other finding remains untouched",
                "evidence": {
                    "ref": "evaluation.json#/concerns/1",
                    "sha256": sha("Other finding remains untouched"),
                },
            },
        ],
        required_tests=copy.deepcopy(request["required_tests"]),
        pr=pr,
        commit=dict(
            repository_id=11, oid=pr["head_oid"], tree_oid="e" * 40, parent_oids=["a" * 40]
        ),
        ref=dict(
            repository_id=11, ref="refs/heads/correction", oid=pr["head_oid"], object_type="commit"
        ),
        provenance=provenance,
        publication_result_reference=result_ref,
        **{k: False for k in publication._AUTHORITY}
    )
    controller = SimpleNamespace(
        config=SimpleNamespace(
            repository="stranske/example", repository_id=11, epoch="epoch", consumers={"codex": {}}
        )
    )
    instruction_body = native_effects._payload(
        request, "instruction_publication", finding_text, raw_findings=result["findings"]
    )["body"]
    owner_instruction = dict(
        repository="stranske/example",
        issue_number="1",
        issue_node_id="I_1",
        comment_id=801,
        comment_node_id="IC_801",
        updated_at="2030-01-01T00:00:00Z",
        body_sha256=sha(instruction_body),
        writer_id=41,
    )
    phase_key = native_effects.phase_key(
        request, "instruction_publication", {"kind": "issue", "number": "1", "node_id": "I_1"}
    )
    intent = dict(
        request=request,
        phase="instruction_publication",
        phase_key=phase_key,
        payload={"body": instruction_body},
    )
    origin = dict(
        schema="local-correction-native-origin/v1",
        phase_key=phase_key,
        intent_sha256=ctl.digest(intent),
        native={"owner_instruction": owner_instruction},
    )
    origin_sha = ctl.digest(origin)
    admission = dict(
        schema="local-correction-admission/v1",
        request=request,
        owner_instruction=owner_instruction,
        seed_selector=dict(
            repository="stranske/example",
            run_id=1,
            run_attempt=1,
            artifact_id=1,
            archive_sha256="a" * 64,
            member_sha256="b" * 64,
        ),
        materializer={"raw_findings": result["findings"]},
        original_incumbent_context={"assignees": [{"id": 42, "login": "owner", "type": "User"}]},
        own_instruction={"phase_key": phase_key, "proof_sha256": origin_sha},
    )
    admission_sha = ctl.digest(admission)
    result["provenance"]["original_admission_sha256"] = admission_sha
    records = {
        "admission-" + admission_sha + ".json": admission,
        "native-intent-" + phase_key + ".json": intent,
        "native-origin-" + origin_sha + ".json": origin,
    }
    custody = SimpleNamespace(read=lambda name: copy.deepcopy(records[name]))
    operation, confirmed = {"id": "inert-live-publication"}, object()
    reads = []

    def reader(*args):
        assert all(
            a is b for a, b in zip(args, (controller, custody, operation, confirmed), strict=True)
        )
        reads.append(args)
        return copy.deepcopy(result)

    monkeypatch.setattr(publication, "read_confirmed_result", reader, raising=False)
    target = "stranske/example#9"

    class Remote:
        deadline = time.time() + 120

        def __init__(self):
            self.posts = []
            self.current = dict(
                target=target,
                pr_target=target,
                kind="pr",
                state="OPEN",
                head=pr["head_oid"],
                head_branch=pr["head_ref"],
                head_repository=pr["repository"],
                base=pr["base_oid"],
                merge=None,
                body=body,
                draft=False,
                sources=[
                    dict(
                        target="stranske/example#8",
                        node_id=scope["node_id"],
                        body=issue_body,
                        state="open",
                        kind="issue",
                        labels=[],
                    )
                ],
                source_inventory={"complete": True},
                revision="initial-revision",
                review_revision="reviews",
                checks="PENDING",
                active_threads=0,
                threads=[],
                labels=[],
                verifier=None,
                verifier_history_complete=True,
            )
            self.pull = dict(
                number=9,
                node_id="PR_9",
                state="open",
                merged=False,
                draft=False,
                body=body,
                user={"id": 41, "login": "instruction-writer"},
                head={"sha": pr["head_oid"], "ref": "correction", "repo": {"id": 11}},
                base={"ref": "main", "repo": {"id": 11}},
            )
            self.issue = dict(
                number=8,
                node_id=scope["node_id"],
                body=issue_body,
                assignees=[{"id": 42, "login": "owner", "type": "User"}],
            )
            self.source = dict(number=1, node_id="I_1", body="Original source request")
            self.comment = dict(
                id=801,
                node_id="IC_801",
                updated_at=owner_instruction["updated_at"],
                body=instruction_body,
                issue_url="https://api.github.com/repos/stranske/example/issues/1",
                user={"id": 41, "login": "instruction-writer"},
            )
            self.ref = dict(
                ref="refs/heads/correction", object={"type": "commit", "sha": pr["head_oid"]}
            )

        def snapshot(self, actual):
            assert actual == target
            return copy.deepcopy(self.current)

        def api(self, endpoint, *, method="GET", fields=None, **_kwargs):
            if method == "POST":
                assert (
                    endpoint
                    == "repos/stranske/example/actions/workflows/agents-verifier.yml/dispatches"
                )
                self.posts.append(copy.deepcopy(fields))
                return None  # Synthetic HTTP acceptance, no run or PASS invented.
            if endpoint == "repos/stranske/example/pulls/9":
                return copy.deepcopy(self.pull)
            if endpoint == "repos/stranske/example/issues/8":
                return copy.deepcopy(self.issue)
            if endpoint == "repos/stranske/example/issues/1":
                return copy.deepcopy(self.source)
            if endpoint == "repos/stranske/example/issues/comments/801":
                return copy.deepcopy(self.comment)
            if endpoint == "repos/stranske/example/git/ref/heads/correction":
                return copy.deepcopy(self.ref)
            if endpoint.startswith(
                "repos/stranske/example/actions/workflows/agents-verifier.yml/runs?"
            ):
                return {"total_count": 0, "workflow_runs": []}
            if endpoint == "repos/stranske/example":
                return {"default_branch": "main"}
            raise AssertionError("unexpected synthetic endpoint: " + endpoint)

    remote = Remote()
    backend = native.Closer(remote, state_path=tmp_path / "queue.json", apply=False)
    return SimpleNamespace(
        controller=controller,
        custody=custody,
        operation=operation,
        confirmed=confirmed,
        result=result,
        records=records,
        owner_instruction=owner_instruction,
        reads=reads,
        remote=remote,
        backend=backend,
        target=target,
    )


def continuation(world):
    return join.from_published(world.controller, world.custody, world.operation, world.confirmed)


def test_exact_reader_objects_and_original_scope_preserved_without_authority(world):
    cap = continuation(world)
    value = join.for_coordinator(cap)
    assert len(world.reads) == 1 and value["target"] == world.target
    assert value["request"] == world.result["request"]
    assert value["findings"] == world.result["findings"]
    assert value["required_tests"] == world.result["required_tests"]
    assert all(value[k] is False for k in publication._AUTHORITY)
    value["findings"].clear()
    assert join.for_coordinator(cap)["findings"] == world.result["findings"]
    with pytest.raises(join.ContinuationHeld, match="source continuation object required"):
        join.queue_callbacks(value, world.backend)


@pytest.mark.parametrize(
    "damage",
    [
        "noinc",
        "tests",
        "finding",
        "wrong_repository",
        "wrong_head",
        "wrong_ref",
        "no_terminal",
        "unknown_terminal",
        "scope_issue",
        "authority",
        "result_reference",
    ],
)
def test_incomplete_or_wrong_result_never_reaches_queue(world, damage):
    value = world.result
    if damage == "noinc":
        value["request"]["incumbent"] = {"kind": "no_incumbent"}
        value["request"]["request_sha256"] = wire.digest(
            {k: v for k, v in value["request"].items() if k != "request_sha256"}
        )
    elif damage == "tests":
        value["required_tests"] = []
    elif damage == "finding":
        value["findings"] = []
    elif damage == "wrong_repository":
        value["pr"]["repository"] = "other/repo"
    elif damage == "wrong_head":
        value["commit"]["oid"] = "0" * 40
    elif damage == "wrong_ref":
        value["ref"]["ref"] = "refs/heads/other"
    elif damage == "no_terminal":
        del value["provenance"]["coding_terminal_reference"]
    elif damage == "unknown_terminal":
        value["provenance"]["coding_terminal_reference"]["schema"] = "unknown-result/v1"
    elif damage == "scope_issue":
        value["provenance"]["scope_issue"]["node_id"] = "I_OTHER"
    elif damage == "authority":
        value["act_authorized"] = True
    else:
        value["publication_result_reference"]["name"] = "other.json"
    with pytest.raises((join.ContinuationHeld, publication.PublicationHeld)):
        continuation(world)
    assert world.remote.posts == []


def test_absent_or_refused_source_reader_does_not_accept_json_claim(world, monkeypatch):
    monkeypatch.delattr(publication, "read_confirmed_result")
    with pytest.raises(join.ContinuationHeld, match="source reader unavailable"):
        continuation(world)

    def refused(*_args):
        raise publication.PublicationHeld("original result unknown/not terminal")

    monkeypatch.setattr(publication, "read_confirmed_result", refused, raising=False)
    with pytest.raises(publication.PublicationHeld, match="original result unknown"):
        continuation(world)
    assert world.remote.posts == []


@pytest.mark.parametrize(
    "damage",
    [
        "head",
        "pr_node",
        "ref",
        "owner",
        "issue_body",
        "issue_owner",
        "scope_node",
        "cached",
        "missing_body",
        "original_source_body",
        "original_source_node",
        "instruction_body",
        "instruction_writer",
        "instruction_version",
    ],
)
def test_fresh_native_binding_refusals_do_not_trigger_verification_or_act(world, damage):
    callbacks = join.queue_callbacks(continuation(world), world.backend)
    if damage == "head":
        world.remote.current["head"] = "0" * 40
    elif damage == "pr_node":
        world.remote.pull["node_id"] = "PR_OTHER"
    elif damage == "ref":
        world.remote.ref["object"]["sha"] = "0" * 40
    elif damage == "owner":
        world.remote.pull["user"]["id"] = 99
    elif damage == "issue_body":
        world.remote.issue["body"] += "changed"
    elif damage == "issue_owner":
        world.remote.issue["assignees"] = []
    elif damage == "scope_node":
        world.remote.issue["node_id"] = "I_OTHER"
    elif damage == "cached":
        world.remote.current["_local_cached"] = True
    elif damage == "original_source_body":
        world.remote.source["body"] += " changed"
    elif damage == "original_source_node":
        world.remote.source["node_id"] = "I_OTHER"
    elif damage == "instruction_body":
        world.remote.comment["body"] += " changed"
    elif damage == "instruction_writer":
        world.remote.comment["user"]["id"] = 99
    elif damage == "instruction_version":
        world.remote.comment["updated_at"] = "2030-02-01T00:00:00Z"
    else:
        del world.remote.pull["body"]
    row = callbacks.observe(world.target)
    assert row["action"] == "wait" and row["eligible"] is False
    assert world.remote.posts == []


def test_open_published_pr_waits_for_ci_and_enters_existing_guarded_merge(world, monkeypatch):
    callbacks = join.queue_callbacks(continuation(world), world.backend)
    assert callbacks.observe(world.target)["action"] == "wait"
    assert world.remote.posts == []
    world.remote.current.update(checks="SUCCESS", revision="ci-success")
    row = callbacks.observe(world.target)
    assert row["action"] == "merge" and row["eligible"] is True
    calls, original = [], native.Closer.execute

    def tracked(self, target, action, snapshot, operation):
        calls.append(action)
        return original(self, target, action, snapshot, operation)

    monkeypatch.setattr(native.Closer, "execute", tracked)
    _run(
        world.backend.state_path, [world.target], callbacks.observe, callbacks.execute, max_passes=1
    )
    assert calls == ["merge"] and world.remote.posts == []
    assert world.backend.apply is False  # Existing actor refuses actual effects.


def test_execute_requires_actual_queue_context_and_rechecks_owner(world):
    callbacks = join.queue_callbacks(continuation(world), world.backend)
    row = callbacks.observe(world.target)
    with pytest.raises(join.ContinuationHeld, match="original guarded queue callback required"):
        callbacks.execute(world.target, "merge", row["evidence"], {"id": "forged"})
    world.remote.current.update(checks="SUCCESS", revision="ready")
    seen = []

    def execute(*args):
        # Change after the queue's final observation, at actual callback entry.
        world.remote.issue["assignees"] = []
        result = callbacks.execute(*args)
        seen.append(result)
        return result

    _run(world.backend.state_path, [world.target], callbacks.observe, execute, max_passes=1)
    assert seen and seen[0]["status"] == "blocked"
    assert world.remote.posts == []


def test_unknown_old_head_is_reconciled_without_new_action(world):
    callbacks = join.queue_callbacks(continuation(world), world.backend)
    result = callbacks.reconcile(world.target, {"binding": {"head": "a" * 40}, "action": "merge"})
    assert result["status"] == "unknown" and world.remote.posts == []


def test_corrective_merged_request_holds_until_canonical_required_scope_is_wired(world):
    world.remote.current.update(state="MERGED", merge="b" * 40, checks="SUCCESS", revision="merged")
    world.remote.pull.update(state="closed", merged=True, merge_commit_sha="b" * 40)
    world.backend.apply = True
    callbacks = join.queue_callbacks(continuation(world), world.backend)
    row = callbacks.observe(world.target)
    assert row["action"] == "post_merge_verify" and row["eligible"] is True
    _run(
        world.backend.state_path, [world.target], callbacks.observe, callbacks.execute, max_passes=1
    )
    assert world.remote.posts == []
    import closer_queue

    operation = closer_queue._read_state(world.backend.state_path)["items"][world.target][
        "operation"
    ]
    assert operation["status"] == "confirmed_not_started"
    assert operation["result"]["verification_no_start"]["post_started"] is False
    assert (
        operation["result"]["reason"] == "canonical corrective evaluator input consumer unavailable"
    )


def test_ordinary_verifier_actor_preserves_its_dispatch_and_ack_wait(world, tmp_path):
    world.remote.current.update(state="MERGED", merge="b" * 40, checks="SUCCESS", revision="merged")
    world.remote.pull.update(state="closed", merged=True, merge_commit_sha="b" * 40)
    # No corrective context: original actor behavior is unchanged. All API effects
    # are the same inert Remote fixture, not GitHub or a verifier/model execution.
    backend = native.Closer(world.remote, state_path=tmp_path / "ordinary.json", apply=True)
    _run(backend.state_path, [world.target], backend.observe, backend.execute, max_passes=1)
    assert len(world.remote.posts) == 1
    fields = world.remote.posts[0]
    assert fields["inputs[mode]"] == "compare" and fields["inputs[pr_number]"] == "9"
    assert "inputs[verification_material]" not in fields
    assert "inputs[verification_predecessor]" not in fields
    assert world.remote.current["verifier"] is None
    assert backend.observe(world.target)["action"] in {"wait", "collect_evidence"}
    assert len(world.remote.posts) == 1


def test_generic_tasks_pass_cannot_close_missing_corrective_test_scope(world, tmp_path):
    world.remote.current.update(
        state="MERGED",
        merge="b" * 40,
        checks="SUCCESS",
        revision="merged",
        verifier={"verdict": "PASS"},
    )
    world.remote.pull.update(state="closed", merged=True, merge_commit_sha="b" * 40)
    snapshot = world.remote.current
    proof = tmp_path / "structural-fixture-proof.txt"
    proof.write_text("Synthetic structural acceptance fixture; not semantic qualification.")
    rows = []
    for source in snapshot["sources"]:
        rows.extend(
            dict(
                source=source["target"],
                criterion=criterion,
                result="PASS",
                evidence=[{"path": str(proof), "sha256": sha(proof.read_text())}],
            )
            for criterion in [source["body"], *native.requirements(source["body"])]
        )
    world.backend.dispositions[world.target] = dict(
        reviewer="inert-structural-control",
        head=snapshot["head"],
        review_revision=snapshot["review_revision"],
        source_digest=native.source_digest(snapshot["sources"]),
        criteria=rows,
    )
    assert world.backend.select(snapshot)[0] == "close_source"
    callbacks = join.queue_callbacks(continuation(world), world.backend)
    assert callbacks.observe(world.target)["action"] == "wait"
    assert world.remote.posts == []


def test_material_selector_keeps_source_parent_incumbent_and_published_target_distinct(world):
    cap = continuation(world)
    value = join.material_selector(cap)
    assert value["kind"] == "corrective_instruction"
    assert value["owner_instruction"] == world.owner_instruction
    assert value["owner_instruction"]["issue_number"] == "1"
    assert world.result["request"]["incumbent"]["owner"] == "coding-agent"
    assert value["incumbent_assignee"] == {"id": 42, "login": "owner"}
    assert world.result["provenance"]["scope_issue"]["number"] == "8"
    assert value["corrective_pr"] == {
        "repository_id": 11,
        "number": "9",
        "node_id": "PR_9",
        "head_sha": "d" * 40,
    }
    assert world.result["request"]["target"]["pr"] == "7"
    assert value["request_sha256"] == world.result["request"]["request_sha256"]
    assert world.result["original"]["request_sha256"] == ctl.digest(world.result["request"])
    assert value["required_tests_sha256"] == wire.digest(world.result["required_tests"])
    assert "act_authorized" not in value


def test_fixed_protected_instruction_body_tamper_refuses_before_selection(world):
    intent = next(
        value for name, value in world.records.items() if name.startswith("native-intent-")
    )
    intent["payload"]["body"] += " edited"
    with pytest.raises(join.ContinuationHeld, match="protected native instruction origin differs"):
        continuation(world)
    assert world.remote.posts == []


@pytest.mark.parametrize(
    "mode",
    ["valid", "unrelated", "absent", "json", "changed_scope", "edited_instruction", "unhinted_v2"],
)
def test_new_root_uses_its_actual_queue_frame_and_source_owned_projection(world, monkeypatch, mode):
    world.remote.current.update(checks="SUCCESS", revision="new-root-current-head")
    context, captures = {
        "prepare": {
            "target": world.target,
            # Explicit non-authorizing corrective selection for this mocked
            # source-reader mechanics test, rather than an ordinary backend.
            "corrective_publication_hint": join.selection_hint(continuation(world)),
        }
    }, []
    if mode == "unhinted_v2":
        from test_closer_publication_existing_git_objects import anchor_policy

        from closer_runtime_binding import RuntimeBinding

        context = {"prepare": {}}
        policy = {**anchor_policy(), "consumer_id": "codex"}
        assert publication.validate_policy(policy) == policy
        world.backend.runtime_binding = RuntimeBinding(
            None,
            {
                "correction_controller_installation": {
                    "config": {"consumers": {"codex": {"publication_v2": policy}}}
                }
            },
        )
        # Configuration selects a read only. This is not an installed binding
        # and cannot grant source/ACT authority; the same DATA reader follows.
    admission = next(v for k, v in world.records.items() if k.startswith("admission-"))
    own = admission["own_instruction"]
    projection = dict(
        schema="closer-admitted-publication-knowledge/v1",
        result=world.result,
        controller_config=dict(
            repository="stranske/example", repository_id=11, epoch="epoch", consumers=["codex"]
        ),
        original_admission=admission,
        instruction_intent=world.records["native-intent-" + own["phase_key"] + ".json"],
        instruction_origin=world.records["native-origin-" + own["proof_sha256"] + ".json"],
    )
    if mode == "changed_scope":
        projection["result"] = copy.deepcopy(world.result)
        projection["result"]["required_tests"] = []
    if mode == "edited_instruction":
        projection = copy.deepcopy(projection)
        projection["instruction_intent"]["payload"]["body"] += "edited"

    class SyntheticView:
        def read(self):
            # Source issuer is mocked DATA here, never actual installation authority.
            return copy.deepcopy(projection)

    def source_reader(backend, target, snapshot, operation, pipeline_context):
        assert backend is world.backend and target == world.target and pipeline_context is context
        assert join.queue._currently_invoking(target, operation)
        if mode == "unhinted_v2":
            captures.append("source_reader_called")
        return None if mode == "unrelated" else (projection if mode == "json" else SyntheticView())

    if mode == "unhinted_v2":
        import closer_coding_publication_io as publication_io

        # Keep the actual publication source-reader entrypoint. Only its
        # source-issued projection boundary supplies synthetic DATA here.
        monkeypatch.setattr(publication_io, "read_for_existing_root", source_reader)
    elif mode != "absent":
        monkeypatch.setattr(publication, "read_for_existing_root", source_reader, raising=False)
    else:
        monkeypatch.delattr(publication, "read_for_existing_root", raising=False)

    def execute(target, action, snapshot, operation):
        if mode in {"valid", "unrelated", "unhinted_v2"}:
            cap = join.attach_existing_root(world.backend, target, snapshot, operation, context)
            assert (cap is None) == (mode == "unrelated")
            if cap is not None:
                selector = join.material_selector(cap)
                assert selector["incumbent_assignee"] == {"id": 42, "login": "owner"}
                assert world.result["pr"]["writer_id"] == 41
            captures.append(mode)
        else:
            with pytest.raises(join.ContinuationHeld):
                join.attach_existing_root(world.backend, target, snapshot, operation, context)
            captures.append("held")
        return {"status": "blocked", "reason": "pure synthetic callback ends without native actor"}

    _run(world.backend.state_path, [world.target], world.backend.observe, execute, max_passes=1)
    assert captures == (
        ["source_reader_called", "unhinted_v2"]
        if mode == "unhinted_v2"
        else [mode if mode in {"valid", "unrelated"} else "held"]
    )
    assert world.remote.posts == []


def test_copied_new_root_operation_cannot_reconstruct_a_continuation(world):
    with pytest.raises(join.ContinuationHeld, match="fresh admitted queue callback"):
        join.attach_existing_root(
            world.backend,
            world.target,
            world.remote.current,
            {"status": "submitted", "id": "copied"},
            {},
        )


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "test_scope",
        "raw_findings",
        "event",
        "source_body",
        "pr_body",
        "execution",
        "material_hash",
    ],
)
def test_actual_canonical_manifest_roundtrip_is_integrity_only(damage):
    base = Path(__file__).parent / "fixtures/closer_corrective_instruction"
    manifest = json.loads((base / "v1-verification-event-manifest.json").read_text())
    payload = json.loads((base / "v1-footer-payload.json").read_text())
    binding = json.loads((base / "v1-required-evidence-row.json").read_text())
    pr_source = next(row for row in manifest["request"]["sources"] if row["kind"] == "pull_request")
    # Protected-record DATA projection for a pure interoperability check. There
    # is deliberately no publication cap, native run, comment or artifact proof.
    record = dict(
        request=payload["original_request"],
        findings=payload["raw_findings"],
        required_tests=payload["original_request"]["required_tests"],
        pr=dict(
            repository="stranske/example",
            repository_id=11,
            pr_number=9,
            pr_node_id="PR_9",
            head_oid="d" * 40,
            writer_id=41,
            writer_login="instruction-writer",
            body_sha256=pr_source["body_sha256"],
        ),
        _instruction=dict(
            payload=payload,
            owner_instruction=binding["owner_instruction"],
            incumbent_assignee={**binding["incumbent_assignee"], "type": "User"},
        ),
    )
    snapshot = {"merge": "e" * 40}
    if damage == "test_scope":
        record["required_tests"][0]["specification"] += " changed"
    elif damage == "raw_findings":
        record["findings"] = record["findings"][:1]
    elif damage == "event":
        manifest["verification_event"]["event_id"] = "0" * 64
    elif damage == "source_body":
        record["request"]["source_issue"]["body_sha256"] = "0" * 64
    elif damage == "pr_body":
        record["pr"]["body_sha256"] = "0" * 64
    elif damage == "execution":
        manifest["execution_binding"]["evaluator_step"] = "Caller says it passed"
    elif damage == "material_hash":
        manifest["material"]["required_evidence"] = []
    if damage is None:
        assert join._validate_manifest(record, manifest, snapshot) == binding
        with pytest.raises(join.ContinuationHeld, match="source continuation"):
            join.require_evaluated_scope(record, snapshot)
    else:
        with pytest.raises(join.ContinuationHeld):
            join._validate_manifest(record, manifest, snapshot)


@pytest.mark.parametrize("repeat", [False, True])
def test_supported_input_builder_adds_exact_scope_without_fake_predecessor(
    world, monkeypatch, repeat
):
    import closer_corrective_verifier_readback as readback

    cap = continuation(world)
    monkeypatch.setattr(
        readback,
        "require_deployed",
        lambda backend, record, branch: {"synthetic_support_data": True},
    )
    prior = {
        "kind": "comment",
        "url": "https://github.com/stranske/example/issues/8#issuecomment-123",
    }
    prepared = dict(
        mutation_kind="workflow_dispatch",
        fields={"ref": "main"},
        material_selectors=[prior] if repeat else [],
        verification_predecessor="a" * 64 if repeat else None,
    )
    if repeat:
        prepared["fields"]["inputs[verification_predecessor]"] = "a" * 64
    before = copy.deepcopy(prepared)
    snapshot = {**world.remote.current, "state": "MERGED"}
    built = join.bind_verifier_inputs(cap, world.backend, world.target, snapshot, prepared)
    selected = json.loads(built["fields"]["inputs[verification_material]"])
    assert join.material_selector(cap) in selected
    assert built["verification_predecessor"] == before["verification_predecessor"]
    assert ("inputs[verification_predecessor]" in built["fields"]) == repeat
    if repeat:
        assert prior in selected
    assert prepared == before
    # Repeating the builder retains one exact typed scope, not a second row.
    again = join.bind_verifier_inputs(cap, world.backend, world.target, snapshot, built)
    assert again == built
    assert world.remote.posts == []


def test_corrective_readback_preserves_actual_round_capacity_control(world, monkeypatch):
    import closer_corrective_verifier_readback as readback

    cap = continuation(world)

    def deferred(*_args, **_kwargs):
        raise native.RoundCapacityDeferred(
            resource="core", throttle={"action": "defer"}, observed_at=time.time()
        )

    monkeypatch.setattr(readback, "read", deferred)
    with pytest.raises(native.RoundCapacityDeferred):
        join.require_evaluated_scope(cap, world.remote.current, backend=world.backend)
    callbacks = join.queue_callbacks(cap, world.backend)
    monkeypatch.setattr(join, "_fresh", deferred)
    with pytest.raises(native.RoundCapacityDeferred):
        callbacks.observe(world.target)
    assert world.remote.posts == []


@pytest.mark.parametrize("wrong_merge", [False, True])
def test_actual_merged_pr_does_not_require_an_auto_deleted_branch(world, monkeypatch, wrong_merge):
    world.remote.current.update(state="MERGED", merge="b" * 40, checks="SUCCESS", revision="merged")
    world.remote.pull.update(
        state="closed", merged=True, merge_commit_sha=("c" if wrong_merge else "b") * 40
    )
    original = world.remote.api

    def api(endpoint, **kwargs):
        assert "/git/ref/" not in endpoint, "normal deleted merged branch must not be fetched"
        return original(endpoint, **kwargs)

    monkeypatch.setattr(world.remote, "api", api)
    cap = continuation(world)
    record = join._record(cap)
    if wrong_merge:
        with pytest.raises(join.ContinuationHeld, match="PR/ref/owner changed"):
            join._fresh(world.backend, world.target, record)
    else:
        assert join._fresh(world.backend, world.target, record)["evidence"]["merge"] == "b" * 40
    assert world.remote.posts == []


@pytest.mark.parametrize(
    "mode", ["missing", "pending", "accepted", "unknown", "generic_pass", "scoped_pass"]
)
def test_observation_hint_only_plans_missing_scope_and_preserves_existing_fences(
    world, monkeypatch, mode
):
    import closer_corrective_verifier_readback as readback
    import verification_events

    cap = continuation(world)
    snapshot = {**world.remote.current, "state": "MERGED", "merge": "b" * 40}
    observation = dict(
        target=world.target,
        action="close_source",
        eligible=True,
        revision=snapshot["revision"],
        evidence=snapshot,
    )
    if mode == "pending":
        observation.update(action="wait", eligible=False)
    event = copy.deepcopy(world.result["request"]["original_verifier"]["verification_event"])
    event.update(pr="9", head_sha="d" * 40, evaluated_sha="b" * 40)
    event["event_id"] = verification_events.event_id(event)
    terminal = {"verification_event": event}
    if mode == "scoped_pass":
        terminal["corrective_verification_input"] = {
            "schema": "closer-corrective-verification-input/v1",
            "verification_event": event,
            "corrective_instruction": {
                "request_sha256": world.result["request"]["request_sha256"],
                "required_tests_sha256": wire.digest(world.result["required_tests"]),
            },
        }
    reads = []

    def selected(*_args):
        reads.append(True)
        return {}, terminal

    monkeypatch.setattr(readback, "_selected_comment", selected)
    backend = SimpleNamespace(
        observe=lambda _: observation,
        _accepted_verification_request=lambda _: mode == "accepted",
        _unacknowledged_verification_request=lambda _: {} if mode == "unknown" else None,
    )
    context = {"prepare": {}}
    if mode != "missing":
        context["prepare"]["corrective_publication_hint"] = join.selection_hint(cap)
    monkeypatch.setattr(
        publication,
        "discovery_hint",
        lambda *_args: context["prepare"].get("corrective_publication_hint"),
        raising=False,
    )
    value = join.observe_existing_root(backend, world.target, context)
    if mode == "generic_pass":
        assert value["action"] == "post_merge_verify" and value["eligible"] is True
    elif mode in {"accepted", "unknown"}:
        assert value["action"] == "wait" and value["eligible"] is False
    else:
        assert value is observation
    assert bool(reads) == (mode in {"generic_pass", "scoped_pass"})
    assert world.remote.posts == []


def test_additional_scope_uses_only_genuine_same_pr_settled_predecessor(world, monkeypatch):
    import closer_corrective_verifier_readback as readback
    import verification_events

    cap = continuation(world)
    event = copy.deepcopy(world.result["request"]["original_verifier"]["verification_event"])
    event.update(pr="9", head_sha="d" * 40, evaluated_sha="b" * 40)
    event["event_id"] = verification_events.event_id(event)
    terminal = {"verification_event": event}
    snapshot = {**world.remote.current, "state": "MERGED", "merge": "b" * 40, "verifier": terminal}
    monkeypatch.setattr(readback, "require_deployed", lambda *_args: None)
    monkeypatch.setattr(readback, "_selected_comment", lambda *_args: ({}, terminal))
    prepared = dict(
        mutation_kind="workflow_dispatch",
        fields={"ref": "main"},
        material_selectors=[],
        verification_predecessor=None,
    )
    built = join.bind_verifier_inputs(cap, world.backend, world.target, snapshot, prepared)
    assert built["verification_predecessor"] == event["event_id"]
    assert built["fields"]["inputs[verification_predecessor]"] == event["event_id"]
    assert (
        event["event_id"]
        != world.result["request"]["original_verifier"]["verification_event"]["event_id"]
    )
    wrong = copy.deepcopy(prepared)
    wrong["verification_predecessor"] = "0" * 64
    with pytest.raises(join.ContinuationHeld, match="predecessor differs"):
        join.bind_verifier_inputs(cap, world.backend, world.target, snapshot, wrong)
    assert world.remote.posts == []


@pytest.mark.parametrize("mode", ["missing", "mismatch", "absent_reader"])
def test_supplied_planning_hint_cannot_replace_protected_discovery(world, monkeypatch, mode):
    cap = continuation(world)
    hint = join.selection_hint(cap)
    backend = SimpleNamespace(observe=lambda _: {"action": "close_source"})
    context = {"prepare": {"corrective_publication_hint": hint}}
    if mode == "absent_reader":
        monkeypatch.delattr(publication, "discovery_hint", raising=False)
    else:
        discovered = None if mode == "missing" else {**hint, "request_sha256": "0" * 64}
        monkeypatch.setattr(publication, "discovery_hint", lambda *_: discovered, raising=False)
    with pytest.raises(join.ContinuationHeld, match="protected discovery"):
        join.observe_existing_root(backend, world.target, context)
    assert world.remote.posts == []


def test_protected_discovery_without_caller_hint_preserves_existing_pending_hold(
    world, monkeypatch
):
    cap = continuation(world)
    observation = {
        "action": "wait",
        "eligible": False,
        "evidence": {"state": "MERGED", "kind": "pr"},
    }
    backend = SimpleNamespace(observe=lambda _: observation)
    monkeypatch.setattr(
        publication, "discovery_hint", lambda *_: join.selection_hint(cap), raising=False
    )
    assert join.observe_existing_root(backend, world.target, {"prepare": {}}) is observation
    assert world.remote.posts == []
