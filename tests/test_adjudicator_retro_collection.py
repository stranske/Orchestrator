"""Collection-only retrospective evidence must be immutable and fail closed."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import adjudicator_retro as retro


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, text=True, capture_output=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "src").mkdir()
    (repo / "src" / "subject.py").write_text("answer = 'evaluated'\n")
    (repo / "src" / "binary.bin").write_bytes(b"\xff\x00")
    os.symlink("subject.py", repo / "src" / "subject-link.py")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "evaluated")
    evaluated = git(repo, "rev-parse", "HEAD")
    (repo / "src" / "subject.py").write_text("answer = 'worktree-and-head'\n")
    git(repo, "commit", "-am", "head", "-q")
    return repo, evaluated


def case(evaluated, **extra):
    return {
        "case_id": "case-458",
        "packet": {"disputed_finding": {"decision": {"evaluated_sha": evaluated}}},
        "collection_requirements": {"source_paths": ["src/subject.py"], "acceptance": []},
        **extra,
    }


def test_collection_reads_evaluated_commit_not_worktree_or_head(repository):
    repo, evaluated = repository
    result = retro.collect_case_evidence(case(evaluated), repository=repo)
    source = result["sources"][0]
    assert source["bytes_utf8"] == "answer = 'evaluated'\n"
    assert source["evaluated_sha"] == evaluated
    assert source["blob_sha"] == git(repo, "rev-parse", f"{evaluated}:src/subject.py")
    assert result["complete"] is False
    assert any(gap["kind"] == "missing_acceptance_inventory" for gap in result["gaps"])


def test_source_bytes_length_blob_and_sha256_match(repository):
    repo, evaluated = repository
    result = retro.collect_case_evidence(case(evaluated), repository=repo)
    source = result["sources"][0]
    raw = b"answer = 'evaluated'\n"
    assert source["byte_length"] == len(raw)
    assert source["sha256"] == hashlib.sha256(raw).hexdigest()


def test_collection_reads_literal_glob_metacharacter_filename(repository):
    repo, _evaluated = repository
    path = "src/[literal]*.py"
    raw = b"answer = 'literal path'\n"
    (repo / path).write_bytes(raw)
    git(repo, "add", path)
    git(repo, "commit", "-qm", "add literal glob path")
    evaluated = git(repo, "rev-parse", "HEAD")
    entry = case(
        evaluated,
        collection_requirements={"source_paths": [path], "acceptance": []},
    )
    result = retro.collect_case_evidence(entry, repository=repo)
    source = result["sources"][0]
    assert source["bytes_utf8"] == raw.decode("utf-8")
    assert source["blob_sha"] == git(repo, "rev-parse", f"{evaluated}:{path}")


@pytest.mark.parametrize(
    "paths,limit,kind",
    [
        (["absent.py"], 100, "missing_source_object"),
        (["src/subject.py"], 2, "source_byte_limit_exceeded"),
    ],
)
def test_missing_object_or_oversize_source_stays_incomplete(repository, paths, limit, kind):
    repo, evaluated = repository
    entry = case(evaluated, collection_requirements={"source_paths": paths, "acceptance": []})
    result = retro.collect_case_evidence(entry, repository=repo, byte_limit=limit)
    assert result["complete"] is False
    assert any(gap["kind"] == kind for gap in result["gaps"])


def test_oversize_blob_is_sized_before_blob_read(repository, monkeypatch):
    repo, evaluated = repository
    original = retro._local_git
    calls = []

    def reader(*args):
        calls.append(args)
        if args[1:3] == ("cat-file", "blob"):
            pytest.fail("oversize source must not be read")
        return original(*args)

    monkeypatch.setattr(retro, "_local_git", reader)
    result = retro.collect_case_evidence(case(evaluated), repository=repo, byte_limit=2)
    assert any(gap["kind"] == "source_byte_limit_exceeded" for gap in result["gaps"])
    assert not any(args[1:3] == ("cat-file", "blob") for args in calls)


def test_non_utf8_source_stays_incomplete(repository):
    repo, evaluated = repository
    entry = case(
        evaluated,
        collection_requirements={"source_paths": ["src/binary.bin"], "acceptance": []},
    )
    result = retro.collect_case_evidence(entry, repository=repo)
    assert result["complete"] is False
    assert any(gap["kind"] == "unsupported_source_encoding" for gap in result["gaps"])


@pytest.mark.parametrize(
    "inventory",
    [
        {"source_paths": "src/subject.py", "acceptance": []},
        {"source_paths": ["src/subject.py"], "acceptance": [{"criterion": "missing location"}]},
    ],
)
def test_malformed_inventory_stays_incomplete(repository, inventory):
    repo, evaluated = repository
    result = retro.collect_case_evidence(
        case(evaluated, collection_requirements=inventory), repository=repo
    )
    assert result["complete"] is False
    assert any(gap["kind"] == "invalid_requirements_inventory" for gap in result["gaps"])


def test_symlink_source_stays_incomplete(repository):
    repo, evaluated = repository
    entry = case(
        evaluated,
        collection_requirements={"source_paths": ["src/subject-link.py"], "acceptance": []},
    )
    result = retro.collect_case_evidence(entry, repository=repo)
    assert result["complete"] is False
    assert any(gap["kind"] == "missing_source_object" for gap in result["gaps"])


@pytest.mark.parametrize("path", ["src", "src/", "src/submodule"])
def test_directory_or_submodule_stays_incomplete(repository, path):
    repo, _evaluated = repository
    gitlink = git(repo, "rev-parse", "HEAD")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{gitlink},src/submodule")
    git(repo, "commit", "-qm", "add gitlink")
    evaluated = git(repo, "rev-parse", "HEAD")
    entry = case(
        evaluated,
        collection_requirements={"source_paths": [path], "acceptance": []},
    )
    result = retro.collect_case_evidence(entry, repository=repo)
    assert result["complete"] is False
    assert any(gap["kind"] == "missing_source_object" for gap in result["gaps"])


@pytest.mark.parametrize("path", ["src/*.py", ":(glob)src/*.py"])
def test_source_pathspecs_are_literal_and_stay_incomplete(repository, path):
    repo, evaluated = repository
    entry = case(
        evaluated,
        collection_requirements={"source_paths": [path], "acceptance": []},
    )
    result = retro.collect_case_evidence(entry, repository=repo)
    assert result["complete"] is False
    assert any(gap["kind"] == "missing_source_object" for gap in result["gaps"])


@pytest.mark.parametrize(
    "tree_entry",
    [
        b"100644 blob " + b"a" * 40 + b"\tsrc/other.py\0",
        (
            b"100644 blob "
            + b"a" * 40
            + b"\tsrc/subject.py\0"
            + b"100644 blob "
            + b"b" * 40
            + b"\tsrc/other.py\0"
        ),
        b"100644 blob " + b"a" * 40 + b"\tsrc/subject.py",
    ],
)
def test_mismatched_or_multiple_ls_tree_entries_stay_incomplete(
    repository, monkeypatch, tree_entry
):
    repo, evaluated = repository
    original = retro._local_git
    calls = []

    def reader(repo_path, *args):
        calls.append(args)
        if args[0:2] == ("--literal-pathspecs", "ls-tree"):
            return tree_entry
        return original(repo_path, *args)

    monkeypatch.setattr(retro, "_local_git", reader)
    result = retro.collect_case_evidence(case(evaluated), repository=repo)
    assert result["complete"] is False
    assert any(gap["kind"] == "missing_source_object" for gap in result["gaps"])
    assert not any(args[0] == "cat-file" and args[1] != "-e" for args in calls)


def test_source_present_without_acceptance_inventory_stays_incomplete(repository):
    repo, evaluated = repository
    entry = case(evaluated)
    entry.pop("collection_requirements")
    result = retro.collect_case_evidence(entry, repository=repo)
    assert result["complete"] is False
    assert any(gap["kind"] == "missing_requirements_inventory" for gap in result["gaps"])


def test_missing_required_artifact_stays_incomplete(repository):
    repo, evaluated = repository
    entry = case(
        evaluated,
        collection_requirements={
            "source_paths": ["src/subject.py"],
            "acceptance": [
                {"criterion": "production loader RED/GREEN", "location": "workflow artifact"}
            ],
        },
    )
    result = retro.collect_case_evidence(entry, repository=repo)
    assert result["complete"] is False
    assert any(gap["kind"] == "missing_acceptance_evidence" for gap in result["gaps"])


def test_saved_decision_collects_without_replacing_report_or_brain_or_roles(
    repository, tmp_path, monkeypatch
):
    repo, evaluated = repository
    report = tmp_path / "saved.json"
    original = {"rows": [case(evaluated, decision="uphold_blocker", raw_shadow_verdict="FAIL")]}
    report.write_text(json.dumps(original))
    output = tmp_path / "collection.json"
    monkeypatch.setattr(
        retro.roles, "run_adjudicator_agent", lambda **_kw: pytest.fail("must not dispatch")
    )
    result = retro.collect_saved_case(report, "case-458", output, repo)
    assert result["complete"] is False
    assert json.loads(report.read_text()) == original
    assert json.loads(output.read_text())["case_id"] == "case-458"


@pytest.mark.parametrize("alias_kind", ["direct", "symlink", "hardlink"])
def test_saved_report_output_alias_is_rejected(repository, tmp_path, alias_kind):
    repo, evaluated = repository
    report = tmp_path / "saved.json"
    original = {"rows": [case(evaluated)]}
    report.write_text(json.dumps(original))
    output = report
    if alias_kind == "symlink":
        output = tmp_path / "report-link.json"
        output.symlink_to(report)
    elif alias_kind == "hardlink":
        output = tmp_path / "report-hardlink.json"
        os.link(report, output)
    with pytest.raises(ValueError, match="separate"):
        retro.collect_saved_case(report, "case-458", output, repo)
    assert json.loads(report.read_text()) == original


def test_collection_does_not_call_role_or_feedback_paths(repository, monkeypatch):
    repo, evaluated = repository
    monkeypatch.setattr(
        retro.roles, "run_adjudicator_agent", lambda **_kw: pytest.fail("role dispatch")
    )
    monkeypatch.setattr(retro.feedback, "record_role_run", lambda **_kw: pytest.fail("Brain write"))
    assert retro.collect_case_evidence(case(evaluated), repository=repo)["complete"] is False


def acceptance_case(evaluated, locations):
    return case(
        evaluated,
        collection_requirements={
            "source_paths": ["src/subject.py"],
            "acceptance": [
                {"criterion": "declared transcript", "location": loc} for loc in locations
            ],
        },
    )


def test_git_acceptance_reads_exact_evaluated_bytes_not_head(repository):
    repo, evaluated = repository
    result = retro.collect_case_evidence(
        acceptance_case(evaluated, ["git-path:src/subject.py"]), repository=repo
    )
    artifact = result["acceptance_artifacts"][0]
    assert result["complete"] is True
    assert artifact["bytes_utf8"] == "answer = 'evaluated'\n"
    assert artifact["evaluated_sha"] == evaluated
    assert artifact["blob_sha"] == git(repo, "rev-parse", f"{evaluated}:src/subject.py")
    assert artifact["sha256"] == hashlib.sha256(artifact["bytes_utf8"].encode()).hexdigest()
    assert artifact["byte_length"] == len(artifact["bytes_utf8"].encode())
    assert result["completeness_scope"] == "supplied_inventory_only"
    assert result["inventory_exhaustiveness"] == "unverified"
    assert result["acceptance_semantics"] == "unassessed"


@pytest.mark.parametrize(
    "location",
    [
        "artifact:run/1",
        "git-path:",
        "git-path:/etc/passwd",
        "git-path:../escape",
        "git-path:src/\x00.py",
        "git-path:src/\ud800.py",
        "git-path:absent",
        "git-path:src",
        "git-path:src/",
        "git-path:src/subject-link.py",
        "git-path:src/binary.bin",
        "git-path:src/*.py",
    ],
)
def test_acceptance_unavailable_unsafe_or_nonregular_stays_incomplete(repository, location):
    repo, evaluated = repository
    result = retro.collect_case_evidence(acceptance_case(evaluated, [location]), repository=repo)
    assert result["complete"] is False
    assert result["acceptance_artifacts"][0]["complete"] is False
    assert any(gap["kind"] == "missing_acceptance_evidence" for gap in result["gaps"])


def test_acceptance_limit_checked_before_content_read(repository, monkeypatch):
    repo, evaluated = repository
    original = retro._local_git
    content_reads = []

    def watch(repo, *args):
        if args[:2] == ("cat-file", "blob"):
            content_reads.append(args[-1])
        return original(repo, *args)

    monkeypatch.setattr(retro, "_local_git", watch)
    entry = acceptance_case(evaluated, ["git-path:src/subject.py"])
    entry["collection_requirements"]["source_paths"] = []
    result = retro.collect_case_evidence(entry, repository=repo, byte_limit=1)
    assert result["complete"] is False
    assert content_reads == []
    exact = retro.collect_case_evidence(
        entry, repository=repo, byte_limit=len(b"answer = 'evaluated'\n")
    )
    assert exact["complete"] is True
    assert len(content_reads) == 1


def test_repeated_acceptance_criteria_do_not_hide_a_missing_location(repository):
    repo, evaluated = repository
    result = retro.collect_case_evidence(
        acceptance_case(evaluated, ["git-path:src/subject.py", "git-path:absent"]), repository=repo
    )
    assert [record["complete"] for record in result["acceptance_artifacts"]] == [True, False]
    assert result["complete"] is False
    assert any(
        gap["kind"] == "missing_acceptance_evidence"
        and gap["criterion"]["location"] == "git-path:absent"
        for gap in result["gaps"]
    )


def test_acceptance_cannot_be_satisfied_by_source_or_wrong_inventory_slot(repository):
    repo, evaluated = repository
    result = retro.collect_case_evidence(
        acceptance_case(evaluated, ["git-path:src/subject.py"]), repository=repo
    )
    result["acceptance_artifacts"][0]["inventory_index"] = 1
    assert any(
        gap["kind"] == "missing_acceptance_evidence"
        for gap in retro.validate_collected_evidence(result)
    )
    result["acceptance_artifacts"] = []
    assert any(
        gap["kind"] == "missing_acceptance_evidence"
        for gap in retro.validate_collected_evidence(result)
    )


def test_successful_acceptance_collection_keeps_saved_report_and_roles_unchanged(
    repository, tmp_path, monkeypatch
):
    repo, evaluated = repository
    entry = acceptance_case(evaluated, ["git-path:src/subject.py"])
    report = tmp_path / "report.json"
    original = json.dumps({"rows": [entry]}, indent=2).encode()
    report.write_bytes(original)
    monkeypatch.setattr(retro.roles, "run_adjudicator_agent", lambda **kw: pytest.fail("dispatch"))
    monkeypatch.setattr(retro.feedback, "record_role_run", lambda **kw: pytest.fail("Brain write"))
    assert (
        retro.collect_saved_case(report, "case-458", tmp_path / "output.json", repo)["complete"]
        is True
    )
    assert report.read_bytes() == original


def test_git_reads_disable_lazy_fetch_and_replacement_objects(monkeypatch, tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=b"unchanged")

    monkeypatch.setattr(retro.subprocess, "run", run)
    assert retro._local_git(tmp_path, "cat-file", "-e", "a" * 40) == b"unchanged"
    assert calls[0]["env"]["GIT_NO_LAZY_FETCH"] == "1"
    assert calls[0]["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_inventory_cannot_override_collector_provenance_or_completeness(repository):
    repo, evaluated = repository
    entry = acceptance_case(evaluated, ["git-path:absent"])
    entry["collection_requirements"]["acceptance"][0].update(
        complete=True, evaluated_sha="a" * 40, path="src/subject.py", sha256="forged"
    )
    result = retro.collect_case_evidence(entry, repository=repo)
    assert result["complete"] is False
    artifact = result["acceptance_artifacts"][0]
    assert artifact["complete"] is False
    assert artifact["evaluated_sha"] == evaluated
    assert artifact["path"] == "absent"
    assert "sha256" not in artifact


@pytest.mark.parametrize(
    "fault", ["wrong_name", "multiple", "unterminated", "length", "negative_size", "timeout"]
)
def test_acceptance_git_integrity_failures_remain_incomplete(repository, monkeypatch, fault):
    repo, evaluated = repository
    original = retro._local_git

    def reader(repo_path, *args):
        data = original(repo_path, *args)
        if args[:2] == ("--literal-pathspecs", "ls-tree"):
            if fault == "wrong_name":
                return data.replace(b"subject.py", b"another.py")
            if fault == "multiple":
                return data + data
            if fault == "unterminated":
                return data.rstrip(b"\0")
            if fault == "timeout":
                raise subprocess.TimeoutExpired("git", 30)
        if args[:2] == ("cat-file", "-s") and fault == "negative_size":
            return b"-1"
        if args[:2] == ("cat-file", "blob") and fault == "length":
            return data + b"extra"
        return data

    monkeypatch.setattr(retro, "_local_git", reader)
    result = retro.collect_case_evidence(
        acceptance_case(evaluated, ["git-path:src/subject.py"]), repository=repo
    )
    assert result["complete"] is False
    assert result["acceptance_artifacts"][0]["complete"] is False


def test_acceptance_literal_metacharacter_path_is_not_a_glob(repository):
    repo, _ = repository
    path = "src/[literal]*.log"
    (repo / path).write_text("literal artifact\n")
    git(repo, "add", "--", path)
    git(repo, "commit", "-qm", "artifact")
    evaluated = git(repo, "rev-parse", "HEAD")
    result = retro.collect_case_evidence(
        acceptance_case(evaluated, ["git-path:" + path]), repository=repo
    )
    assert result["complete"] is True
    assert result["acceptance_artifacts"][0]["bytes_utf8"] == "literal artifact\n"


def test_acceptance_ignores_git_replacement_objects(repository):
    repo, evaluated = repository
    git(repo, "replace", evaluated, "HEAD")
    # Ordinary Git reads would now return the newer contents for the old SHA.
    assert "worktree-and-head" in git(repo, "show", f"{evaluated}:src/subject.py")
    result = retro.collect_case_evidence(
        acceptance_case(evaluated, ["git-path:src/subject.py"]), repository=repo
    )
    assert result["complete"] is True
    assert result["acceptance_artifacts"][0]["bytes_utf8"] == "answer = 'evaluated'\n"


@pytest.mark.parametrize(
    "variable",
    [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
    ],
)
def test_git_location_environment_cannot_redirect_collection(monkeypatch, tmp_path, variable):
    """Keep inherited repository locations out of the local Git child."""
    monkeypatch.setenv(variable, "foreign-location")
    monkeypatch.setenv("COLLECTOR_TEST_PRESERVE", "yes")
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=b"ok")

    monkeypatch.setattr(retro.subprocess, "run", run)
    retro._local_git(tmp_path, "rev-parse", "HEAD")
    assert variable not in calls[0]["env"]
    assert calls[0]["env"]["COLLECTOR_TEST_PRESERVE"] == "yes"
    assert calls[0]["env"]["GIT_NO_LAZY_FETCH"] == "1"
    assert calls[0]["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_actual_inherited_git_dir_cannot_change_recorded_repository(
    repository, monkeypatch, tmp_path
):
    """Read the supplied repository even when a hook exports a foreign GIT_DIR."""
    repo, evaluated = repository
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    git(foreign, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    result = retro.collect_case_evidence(
        acceptance_case(evaluated, ["git-path:src/subject.py"]), repository=repo
    )
    assert result["complete"] is True
    assert result["acceptance_artifacts"][0]["bytes_utf8"] == "answer = 'evaluated'\n"
