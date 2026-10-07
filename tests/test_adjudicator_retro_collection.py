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
