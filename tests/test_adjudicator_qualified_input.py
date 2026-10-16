"""Only freshly collected, inventory-qualified inputs leave the metadata floor."""

import copy
import hashlib
import json
import os
import shutil
import subprocess

import pytest

import adjudicator_retro as retro
import roles


@pytest.fixture
def qualified_input(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {key: value for key, value in os.environ.items() if key not in retro._GIT_LOCATION_VARS}

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], env=env, text=True).strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "inherited.py").write_text("first = 'λ'\nsecond = 2\nthird = 3\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "subject.py").write_text("value = 'évaluated'\n")
    (repo / "proof.txt").write_text("Entire proof " + "λ" * 2400)
    git("add", ".")
    git("commit", "-qm", "evaluated")
    sha = git("rev-parse", "HEAD")
    body = "## Tasks\n- Read the complete source.\n## Acceptance Criteria\n- Entire proof is inspected.\n"
    row = {
        "case_id": "one",
        "target": "owner/repo#1",
        "packet": {
            "target": "owner/repo#1",
            "disputed_finding": {
                "body": "Missing evidence " + "X" * 2400,
                "decision": {"evaluated_sha": sha},
            },
            "ground_truth_evidence": {
                "head_sha": sha,
                "merge_sha": sha,
                "diff_summary": [{"path": "subject.py"}, {"path": "proof.txt"}],
                "gate_runs": ["gate-run"],
            },
        },
        "collection_requirements": {
            "source_paths": ["subject.py", "proof.txt"],
            "acceptance": [{"criterion": "proof", "location": "git-path:proof.txt"}],
        },
        "inventory_provenance": {
            "source_criterion_tasks": body,
            "pr_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        },
    }
    qualification = {
        "comparison_base": base,
        "snapshot": {"number": 1, "headRefOid": sha, "body": body},
        "criterion_mapping": [
            {
                "criterion": "Entire proof is inspected.",
                "evidence_locations": ["git-path:proof.txt"],
                "unknowns": [
                    {
                        "evidence_ref": "historical-installation",
                        "owner": "operator",
                        "next_action": "Inspect an authentic installed receipt.",
                    }
                ],
            }
        ],
    }
    return repo, row, qualification, git


def proposal(ref="git-path:proof.txt"):
    return {
        "decision": "reject_blocker",
        "confidence": "high",
        "rationale": "Complete bytes supplied.",
        "evidence_assessment": [
            {
                "claim": "proof unavailable",
                "status": "contradicted",
                "evidence_ref": ref,
                "reason": "Full immutable proof supplied.",
            }
        ],
        "ground_truth_refs": [ref],
        "recommended_next_step": "Inspect the named historical UNKNOWN.",
        "evidence_gaps": ["Historical authenticity remains unverified."],
    }


def test_collector_issued_input_reaches_existing_native_runner_without_truncation(
    qualified_input, monkeypatch
):
    repo, row, qualification, _ = qualified_input
    original = copy.deepcopy(row)
    case = retro.qualify_collected_case(row, repo, qualification)
    captured = {}

    def offload(backend, prompt, **kwargs):
        captured.update(prompt=prompt, **kwargs)
        return {"exit": 0, "run_id": "native-one", "output": json.dumps(proposal())}

    monkeypatch.setattr(roles.dispatcher, "offload", offload)
    records = []
    monkeypatch.setattr(roles.feedback, "record_role_run", lambda **kwargs: records.append(kwargs))
    result = roles.run_adjudicator_agent(case=case, backend="gemini", dispatch=True, cwd=str(repo))
    assert result["decision_source"] == "adjudicator_agent"
    assert result["advisory_plan"]["decision"] == "reject_blocker"
    assert captured["isolate"] is True
    assert "X" * 2400 in captured["prompt"]
    assert "\\u03bb" * 2400 in captured["prompt"]
    assert "Entire proof is inspected." in captured["prompt"]
    assert "historical-installation" in captured["prompt"]
    assert records[0]["source"] == "retrospective"
    assert result["evidence_qualification"]["persisted_admission"] is False
    assert row == original
    # Even self-consistent serialized result fields do not authenticate native execution.
    forged = {
        **result,
        "packet": result["case"],
        "decision": "reject_blocker",
        "disposition": "reject_blocker",
        "shadow_verdict": "PASS",
        "later_truth": "PASS",
    }
    assert retro.summarize([forged])["adjudicated"] == 0


def test_serialized_or_copied_packet_cannot_manufacture_collector_authority(qualified_input):
    repo, row, qualification, _ = qualified_input
    case = retro.qualify_collected_case(row, repo, qualification)
    packet = roles._collected_snapshot(case)
    packet["metadata_only"] = False
    packet["ground_truth_evidence"]["qualification"]["persisted_admission"] = True
    result = roles.run_adjudicator_agent(
        case=json.loads(json.dumps(packet)), backend="gemini", proposal_json=proposal()
    )
    assert result["decision_source"] == "metadata_only_needs_more_evidence"
    assert roles.adjudication_metadata_only(case) is False
    with pytest.raises(TypeError):
        roles.CollectedAdjudicationCase()
    with pytest.raises(ValueError, match="unissued"):
        roles.adjudication_metadata_only(object.__new__(roles.CollectedAdjudicationCase))


def test_collector_capability_digest_is_rechecked_before_use(qualified_input):
    repo, row, qualification, _ = qualified_input
    case = retro.qualify_collected_case(row, repo, qualification)
    case._encoded += b" "
    with pytest.raises(ValueError, match="changed"):
        roles.run_adjudicator_agent(case=case, backend="gemini", proposal_json=proposal())


@pytest.mark.parametrize(
    "defect",
    [
        "missing-path",
        "duplicate-path",
        "wrong-base",
        "wrong-revision",
        "wrong-head",
        "altered-body",
        "altered-body-hash",
        "missing-provenance",
        "missing-mapping",
        "wrong-criterion",
        "foreign-artifact",
        "unowned-unknown",
        "forged-complete",
    ],
)
def test_unqualified_inventory_or_provenance_never_issues_case(qualified_input, defect):
    repo, row, qualification, _ = qualified_input
    if defect == "missing-path":
        row["collection_requirements"]["source_paths"].pop()
    elif defect == "duplicate-path":
        row["collection_requirements"]["source_paths"].append("proof.txt")
    elif defect == "wrong-base":
        qualification["comparison_base"] = "0" * 40
    elif defect == "wrong-revision":
        row["packet"]["ground_truth_evidence"]["merge_sha"] = "0" * 40
    elif defect == "wrong-head":
        qualification["snapshot"]["headRefOid"] = "0" * 40
    elif defect == "altered-body":
        qualification["snapshot"]["body"] += "changed"
    elif defect == "altered-body-hash":
        row["inventory_provenance"]["pr_body_sha256"] = "0" * 64
    elif defect == "missing-provenance":
        row.pop("inventory_provenance")
    elif defect == "missing-mapping":
        qualification["criterion_mapping"] = []
    elif defect == "wrong-criterion":
        qualification["criterion_mapping"][0]["criterion"] = "invented"
    elif defect == "foreign-artifact":
        qualification["criterion_mapping"][0]["evidence_locations"] = ["foreign"]
    elif defect == "unowned-unknown":
        qualification["criterion_mapping"][0]["unknowns"][0].pop("owner")
    elif defect == "forged-complete":
        row["collection_requirements"]["acceptance"][0]["location"] = "git-path:absent"
        row.update(complete=True, metadata_only=False)
    with pytest.raises(ValueError):
        retro.qualify_collected_case(row, repo, qualification)


@pytest.mark.parametrize("operation", ["delete", "rename", "type-change"])
def test_unsupported_git_changes_fail_closed(qualified_input, operation):
    repo, row, qualification, git = qualified_input
    qualification["comparison_base"] = git("rev-parse", "HEAD")
    if operation == "delete":
        (repo / "subject.py").unlink()
    elif operation == "rename":
        (repo / "subject.py").rename(repo / "renamed.py")
    else:
        (repo / "subject.py").unlink()
        (repo / "subject.py").symlink_to("proof.txt")
    git("add", ".")
    git("commit", "-qm", "unsupported")
    sha = git("rev-parse", "HEAD")
    row["packet"]["disputed_finding"]["decision"]["evaluated_sha"] = sha
    row["packet"]["ground_truth_evidence"]["merge_sha"] = sha
    with pytest.raises(ValueError, match="deletion/type"):
        retro.qualify_collected_case(row, repo, qualification)


def test_qualified_proposal_cannot_cite_absent_evidence(qualified_input):
    repo, row, qualification, _ = qualified_input
    case = retro.qualify_collected_case(row, repo, qualification)
    result = roles.run_adjudicator_agent(
        case=case, backend="gemini", proposal_json=proposal("invented")
    )
    assert result["decision_source"] == "baseline_needs_more_evidence"
    assert "absent" in result["errors"][0]
    assert result["backend_run_id"] is None


def test_complete_prompt_budget_includes_unicode_and_context(qualified_input):
    repo, row, qualification, _ = qualified_input
    with pytest.raises(ValueError, match="prompt exceeds"):
        retro.qualify_collected_case(row, repo, qualification, prompt_byte_limit=6000)
    case = retro.qualify_collected_case(row, repo, qualification)
    with pytest.raises(ValueError, match="prompt exceeds"):
        roles.run_adjudicator_agent(case=case, context="C" * 1_048_576, backend="gemini")


def test_modified_sources_include_complete_before_and_after_blobs(qualified_input):
    repo, row, qualification, git = qualified_input
    qualification["comparison_base"] = git("rev-parse", "HEAD")
    (repo / "subject.py").write_text("value = 'changed'\n")
    git("add", ".")
    git("commit", "-qm", "modified")
    sha = git("rev-parse", "HEAD")
    row["packet"]["disputed_finding"]["decision"]["evaluated_sha"] = sha
    row["packet"]["ground_truth_evidence"].update(
        merge_sha=sha, head_sha=sha, diff_summary=[{"path": "subject.py"}]
    )
    row["collection_requirements"]["source_paths"] = ["subject.py"]
    qualification["snapshot"]["headRefOid"] = sha
    case = retro.qualify_collected_case(row, repo, qualification)
    collection = roles._collected_snapshot(case)["ground_truth_evidence"]["collected_evidence"]
    assert collection["sources"][0]["bytes_utf8"] == "value = 'changed'\n"
    assert collection["before_sources"][0]["bytes_utf8"] == "value = 'évaluated'\n"
    assert collection["before_sources"][0]["evaluated_sha"] == qualification["comparison_base"]
    assert collection["sources"][0]["git_mode"] == "100644"


def test_cli_qualified_dry_run_preserves_saved_report_and_refuses_output_reuse(
    qualified_input, tmp_path, monkeypatch
):
    repo, row, qualification, _ = qualified_input
    report, qual, output = (
        tmp_path / name for name in ("saved.json", "qualification.json", "new.json")
    )
    report.write_text(json.dumps({"rows": [row]}))
    qual.write_text(json.dumps(qualification))
    original = report.read_bytes()
    monkeypatch.setattr(roles, "route_role", lambda *args, **kwargs: {"agent": "gemini"})
    monkeypatch.setattr(
        roles.dispatcher, "offload", lambda *args, **kwargs: pytest.fail("dry run dispatched")
    )
    monkeypatch.setattr(
        roles.feedback, "record_role_run", lambda **kwargs: pytest.fail("dry run wrote Brain")
    )
    argv = [
        "adjudicator_retro",
        "--judge-collected-case",
        "one",
        "--report",
        str(report),
        "--qualification",
        str(qual),
        "--repository",
        str(repo),
        "--output",
        str(output),
    ]
    monkeypatch.setattr("sys.argv", argv)
    assert retro.main() == 0
    result = json.loads(output.read_text())
    assert result["decision_source"] == "baseline_needs_more_evidence"
    assert result["backend_run_id"] is None
    assert result["evidence_qualification"]["scope"] == "semantic_judgment_only"
    assert report.read_bytes() == original
    with pytest.raises(SystemExit) as caught:
        retro.main()
    assert caught.value.code == 2
    assert json.loads(output.read_text()) == result


@pytest.mark.parametrize("field", ["qualification", "inventory_provenance", "snapshot"])
@pytest.mark.parametrize("value", [[1], "invalid"])
def test_qualification_rejects_non_object_json_fields(qualified_input, field, value):
    repo, row, qualification, _ = qualified_input
    if field == "qualification":
        qualification = value
    elif field == "inventory_provenance":
        row[field] = value
    else:
        qualification[field] = value
    with pytest.raises(ValueError, match="object"):
        retro.qualify_collected_case(row, repo, qualification)


@pytest.mark.parametrize(
    "saved", [None, [], "invalid", {"rows": None}, {"rows": {}}, {"rows": [None]}]
)
def test_cli_rejects_malformed_report_shapes_without_traceback(
    qualified_input, tmp_path, monkeypatch, capsys, saved
):
    repo, _, qualification, _ = qualified_input
    report, qual, output = (
        tmp_path / name for name in ("saved.json", "qualification.json", "new.json")
    )
    report.write_text(json.dumps(saved))
    qual.write_text(json.dumps(qualification))
    monkeypatch.setattr(
        "sys.argv",
        [
            "adjudicator_retro",
            "--judge-collected-case",
            "one",
            "--report",
            str(report),
            "--qualification",
            str(qual),
            "--repository",
            str(repo),
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as caught:
        retro.main()
    assert caught.value.code == 2
    assert "error:" in capsys.readouterr().err
    assert not output.exists()


def supporting_descriptor(repo, row, git, *, ranges=None):
    sha = row["packet"]["ground_truth_evidence"]["merge_sha"]
    data = (repo / "inherited.py").read_bytes()
    return {
        "path": "inherited.py",
        "evaluated_sha": sha,
        "blob_sha": git("rev-parse", sha + ":inherited.py"),
        "sha256": hashlib.sha256(data).hexdigest(),
        "line_ranges": ranges or [],
    }


@pytest.mark.parametrize("ranges", [[], [[1, 1], [3, 3]]])
def test_supporting_sources_are_recollected_separately_and_reach_role_prompt(
    qualified_input, monkeypatch, ranges
):
    repo, row, qualification, git = qualified_input
    descriptor = supporting_descriptor(repo, row, git, ranges=ranges)
    qualification["supporting_sources"] = [descriptor]
    before = copy.deepcopy(row)
    (repo / "inherited.py").write_text("working tree decoy")
    case = retro.qualify_collected_case(row, repo, qualification)
    packet = roles._collected_snapshot(case)
    ground = packet["ground_truth_evidence"]
    collection = ground["collected_evidence"]
    assert {x["path"] for x in collection["sources"]} == {"subject.py", "proof.txt"}
    support = collection["supporting_sources"][0]
    assert support["sha256"] == descriptor["sha256"]
    assert support["exhaustive"] is (not ranges)
    if ranges:
        assert "bytes_utf8" not in support
        assert [x["bytes_utf8"] for x in support["excerpts"]] == ["first = 'λ'\n", "third = 3\n"]
        for excerpt in support["excerpts"]:
            assert excerpt["sha256"] == hashlib.sha256(excerpt["bytes_utf8"].encode()).hexdigest()
    else:
        assert support["bytes_utf8"] == "first = 'λ'\nsecond = 2\nthird = 3\n"
    assert ground["qualification"]["persisted_admission"] is False
    assert ground["qualification"]["external_exhaustiveness"] == "unverified"
    revision = ground["qualification"]["material_evidence_revision"]
    assert len(revision) == 64
    assert (
        roles._collected_snapshot(retro.qualify_collected_case(row, repo, qualification)) == packet
    )
    seen = {}

    def offload(backend, prompt, **kwargs):
        seen["prompt"] = prompt
        return {
            "exit": 0,
            "run_id": "synthetic-supporting",
            "output": json.dumps(proposal("supporting-git-path:inherited.py")),
        }

    monkeypatch.setattr(roles.dispatcher, "offload", offload)
    monkeypatch.setattr(roles.feedback, "record_role_run", lambda **kwargs: None)
    result = roles.run_adjudicator_agent(case=case, backend="gemini", dispatch=True)
    assert result["decision_source"] == "adjudicator_agent"
    assert "first = '\\u03bb'" in seen["prompt"]
    assert "working tree decoy" not in seen["prompt"]
    assert row == before
    assert (
        retro.summarize([{"packet": packet, "decision": "reject_blocker", "later_truth": "PASS"}])[
            "adjudicated"
        ]
        == 0
    )


@pytest.mark.parametrize(
    "defect",
    [
        "not-list",
        "not-object",
        "forged-hash",
        "forged-blob",
        "stale-revision",
        "diff-collision",
        "duplicate",
        "unsafe-path",
        "injected-text",
        "injected-complete",
        "ranges-not-list",
        "range-zero",
        "range-overlap",
        "range-reversed",
        "range-beyond-file",
        "range-bool",
        "range-float",
        "range-string",
        "range-shape",
    ],
)
def test_supporting_manifest_refuses_forgery_and_invalid_excerpts(qualified_input, defect):
    repo, row, qualification, git = qualified_input
    item = supporting_descriptor(repo, row, git)
    qualification["supporting_sources"] = [item]
    if defect == "not-list":
        qualification["supporting_sources"] = {}
    elif defect == "not-object":
        qualification["supporting_sources"] = ["invalid"]
    elif defect == "forged-hash":
        item["sha256"] = "0" * 64
    elif defect == "forged-blob":
        item["blob_sha"] = "0" * 40
    elif defect == "stale-revision":
        item["evaluated_sha"] = qualification["comparison_base"]
    elif defect == "diff-collision":
        item["path"] = "subject.py"
        item["blob_sha"] = git("rev-parse", item["evaluated_sha"] + ":subject.py")
        item["sha256"] = hashlib.sha256((repo / "subject.py").read_bytes()).hexdigest()
    elif defect == "duplicate":
        qualification["supporting_sources"].append(copy.deepcopy(item))
    elif defect == "unsafe-path":
        item["path"] = "../inherited.py"
    elif defect == "injected-text":
        item["bytes_utf8"] = "forged"
    elif defect == "injected-complete":
        item["complete"] = True
    else:
        item["line_ranges"] = {
            "ranges-not-list": None,
            "range-zero": [[0, 1]],
            "range-overlap": [[1, 2], [2, 3]],
            "range-reversed": [[2, 1]],
            "range-beyond-file": [[1, 4]],
            "range-bool": [[True, 1]],
            "range-float": [[1.0, 1]],
            "range-string": [["1", 1]],
            "range-shape": [[1]],
        }[defect]
    with pytest.raises(ValueError):
        retro.qualify_collected_case(row, repo, qualification)


def test_material_revision_binds_supporting_coverage_and_owned_unknowns(qualified_input):
    repo, row, qualification, git = qualified_input

    def revision():
        return roles._collected_snapshot(retro.qualify_collected_case(row, repo, qualification))[
            "ground_truth_evidence"
        ]["qualification"]["material_evidence_revision"]

    original = revision()
    qualification["supporting_sources"] = [supporting_descriptor(repo, row, git)]
    full = revision()
    qualification["supporting_sources"][0]["line_ranges"] = [[1, 1]]
    excerpt = revision()
    qualification["criterion_mapping"][0]["unknowns"][0][
        "next_action"
    ] = "Recover authentic receipt."
    assert len({original, full, excerpt, revision()}) == 4


def test_supporting_sources_cannot_bypass_complete_prompt_byte_ceiling(qualified_input):
    repo, row, qualification, git = qualified_input
    qualification["supporting_sources"] = [supporting_descriptor(repo, row, git)]
    case = retro.qualify_collected_case(row, repo, qualification)
    packet = roles._collected_snapshot(case)
    prompt = roles.ROLE_REGISTRY["adjudicator"].build_prompt(
        {"case": packet, "complete_collected_case": True}
    )
    with pytest.raises(ValueError, match="prompt exceeds"):
        retro.qualify_collected_case(
            row, repo, qualification, prompt_byte_limit=len(prompt.encode()) - 1
        )


def test_material_revision_is_independent_of_checkout_location(qualified_input, tmp_path):
    repo, row, qualification, git = qualified_input
    qualification["supporting_sources"] = [supporting_descriptor(repo, row, git)]
    other = tmp_path / "other-checkout"
    shutil.copytree(repo, other)
    revisions = [
        roles._collected_snapshot(retro.qualify_collected_case(row, location, qualification))[
            "ground_truth_evidence"
        ]["qualification"]["material_evidence_revision"]
        for location in [repo, other]
    ]
    assert revisions[0] == revisions[1]


def test_material_revision_ignores_supporting_order_and_snapshot_transport(qualified_input):
    repo, row, qualification, git = qualified_input
    one = supporting_descriptor(repo, row, git)
    two = copy.deepcopy(one)
    # Independent paths with identical bytes are still distinct evidence sources.
    (repo / "other.py").write_bytes((repo / "inherited.py").read_bytes())
    git("add", "other.py")
    git("commit", "-qm", "another source")
    sha = git("rev-parse", "HEAD")
    # Both supporting files are inherited at a later evaluated commit that only changes subject.
    qualification["comparison_base"] = sha
    (repo / "subject.py").write_text("new value\n")
    git("add", "subject.py")
    git("commit", "-qm", "evaluated subject")
    sha = git("rev-parse", "HEAD")
    row["packet"]["disputed_finding"]["decision"]["evaluated_sha"] = sha
    row["packet"]["ground_truth_evidence"].update(
        merge_sha=sha, head_sha=sha, diff_summary=[{"path": "subject.py"}]
    )
    row["collection_requirements"]["source_paths"] = ["subject.py"]
    qualification["snapshot"]["headRefOid"] = sha
    one["evaluated_sha"] = two["evaluated_sha"] = sha
    two["path"] = "other.py"
    qualification["supporting_sources"] = [one, two]

    def revision():
        return roles._collected_snapshot(retro.qualify_collected_case(row, repo, qualification))[
            "ground_truth_evidence"
        ]["qualification"]["material_evidence_revision"]

    first = revision()
    qualification["supporting_sources"].reverse()
    assert revision() == first
    qualification["snapshot"]["transport_timestamp"] = "later collection"
    assert revision() == first


@pytest.mark.parametrize("forged_admission", [False, True])
def test_serialized_supporting_case_cannot_grade_with_complete_patch_and_criteria(
    qualified_input, forged_admission
):
    repo, row, qualification, git = qualified_input
    qualification["supporting_sources"] = [supporting_descriptor(repo, row, git)]
    packet = roles._collected_snapshot(retro.qualify_collected_case(row, repo, qualification))
    ground = packet["ground_truth_evidence"]
    ground["diff_patch"] = {"text": "actual-looking patch", "revision": ground["merge_sha"]}
    ground["acceptance_criteria"] = [
        {"text": "Complete criterion.", "revision": ground["head_sha"]}
    ]
    ground["qualification"]["persisted_admission"] = forged_admission
    report_row = {
        "packet": packet,
        "decision": "reject_blocker",
        "disposition": "reject_blocker",
        "shadow_verdict": "PASS",
        "later_truth": "PASS",
        "merge_rule_verdict": "PASS",
    }
    summary = retro.summarize([report_row])
    assert summary["adjudicated"] == summary["graded"] == 0
    retro._apply_evidence_floor(report_row)
    assert report_row["disposition"] == "needs_more_evidence"


@pytest.mark.parametrize("inventory", ["source_paths", "diff_summary"])
def test_material_revision_ignores_unordered_changed_inventory(qualified_input, inventory):
    repo, row, qualification, git = qualified_input
    qualification["supporting_sources"] = [supporting_descriptor(repo, row, git)]

    def revision():
        return roles._collected_snapshot(retro.qualify_collected_case(row, repo, qualification))[
            "ground_truth_evidence"
        ]["qualification"]["material_evidence_revision"]

    first = revision()
    if inventory == "source_paths":
        row["collection_requirements"]["source_paths"].reverse()
    else:
        row["packet"]["ground_truth_evidence"]["diff_summary"].reverse()
    assert revision() == first


def test_supporting_manifest_count_is_bounded_before_git_reads(qualified_input, monkeypatch):
    repo, row, qualification, git = qualified_input
    item = supporting_descriptor(repo, row, git)
    monkeypatch.setattr(
        retro, "_collect_git_blob", lambda *args: pytest.fail("oversized manifest read Git")
    )
    with pytest.raises(ValueError, match="descriptor count limit"):
        retro._collect_supporting_sources(
            [item] * (retro.MAX_SUPPORTING_SOURCES + 1), repo, item["evaluated_sha"], [], 1000
        )
