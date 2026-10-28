"""Shards must preserve the whole suite and the existing aggregate verification rules."""

import contextlib
import importlib.util
import json
import pathlib
import subprocess
import sys
from collections import Counter

import pytest

import verify


def test_shard_lists_partition_every_collected_test_exactly_once():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=verify.ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    ids = [
        line
        for line in proc.stdout.splitlines()
        if "::" in line and line.split("::", 1)[0].endswith(".py")
    ]
    assert ids, proc.stdout
    plan = verify.shard_plan()
    modules = [module for shard in plan for module in shard]
    collected_modules = {node.split("::", 1)[0] for node in ids}
    assert set(modules) == collected_modules, (
        f"unassigned modules: {sorted(collected_modules - set(modules))}; "
        f"stale modules: {sorted(set(modules) - collected_modules)}"
    )
    assigned = [
        node
        for shard in plan
        for module in shard
        for node in ids
        if node.split("::", 1)[0] == module
    ]
    assert Counter(assigned) == Counter(ids)
    assert len(modules) == len(set(modules))


def test_shard_plan_rejects_duplicate_modules(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "test-shards.json").write_text(
        json.dumps({"shards": [["tests/test_a.py"], ["tests/test_a.py"]]})
    )
    monkeypatch.setattr(verify, "ROOT", tmp_path)

    with pytest.raises(ValueError, match="invalid or duplicate modules"):
        verify.shard_plan()


def test_shard_plan_rejects_parent_path_components(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "test-shards.json").write_text(
        json.dumps({"shards": [["tests/../test_a.py"]]})
    )
    monkeypatch.setattr(verify, "ROOT", tmp_path)

    with pytest.raises(ValueError, match="invalid or duplicate modules"):
        verify.shard_plan()


@pytest.fixture
def summaries(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "shard_plan", lambda: [["a.py"], ["b.py"], ["c.py"]])
    monkeypatch.setattr(verify, "shard_identity", lambda: "plan")
    shape, _ = verify.tree_shape()
    for index, summary in enumerate(
        ["4 passed, 1 skipped in 1s", "3 passed, 1 xfailed in 2s", "2 passed in 3s"], 1
    ):
        (tmp_path / f"shard-{index}.json").write_text(
            json.dumps(
                {
                    "shard": f"{index}/3",
                    "plan": "plan",
                    "shape": shape,
                    "output": f"fixture says 999 passed\n{summary}\n",
                    "returncode": 0,
                    "test_inventory": [
                        [f"shard{index}", f"test_{n}"] for n in range({1: 5, 2: 4, 3: 2}[index])
                    ],
                    "collection_skip_records": [],
                    "inventory_error": None,
                    "verdicts": {f"gate{index}": {"check": {"outcome": "passed", "message": ""}}},
                    "skip_records": (
                        [{"outcome": "skipped", "message": "widget absent"}] if index == 1 else []
                    ),
                }
            )
        )
    return tmp_path


def test_aggregate_fails_when_a_shard_summary_is_missing(summaries):
    (summaries / "shard-2.json").unlink()
    with pytest.raises(ValueError, match="summary for shard 2/3"):
        verify.aggregate_pytest(summaries)


def test_aggregate_sums_counts_and_keeps_the_collected_equality(summaries):
    # JUnit's totals deliberately disagree; only the printed summaries may supply counts.
    (summaries / "shard-1.xml").write_text('<testsuite tests="999999" failures="999999"/>')
    result = verify.aggregate_pytest(summaries)
    assert (result["passed"], result["failed"], result["skipped"], result["collected"]) == (
        9,
        0,
        1,
        11,
    )
    assert result["counts"]["xfailed"] == 1
    assert len(result["verdicts"]) == 3
    assert len(result["skip_records"]) == 1
    # Native shard records carry the exact testcase identities even when their
    # printed summary includes a collection-level skip.
    path = summaries / "shard-1.json"
    record = json.loads(path.read_text())
    record.update(
        test_inventory=[["a", f"test_{i}"] for i in range(4)],
        collection_skip_records=[
            {"outcome": "skipped", "message": "collection skipped", "name": "tests.collection"}
        ],
        skip_records=[],
        inventory_error=None,
    )
    path.write_text(json.dumps(record))
    native = verify.aggregate_pytest(summaries)
    assert native["collected"] == 10 and native["skipped"] == 0
    assert len(native["collection_skip_records"]) == 1
    record["inventory_error"] = "malformed XML"
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="testcase inventory"):
        verify.aggregate_pytest(summaries)
    record.pop("test_inventory")
    record["inventory_error"] = None
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="testcase inventory"):
        verify.aggregate_pytest(summaries)
    record["test_inventory"] = []
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="testcase inventory"):
        verify.aggregate_pytest(summaries)
    record["test_inventory"] = [["shard1", f"test_{n}"] for n in range(5)]
    record["collection_skip_records"] = []
    record["inventory_error"] = None
    record["skip_records"] = [{"outcome": "skipped", "message": "widget absent"}]
    path.write_text(json.dumps(record))

    assert not verify._floor_problems({"collected": 11, "passed": 10}, result)
    assert verify._floor_problems({"collected": 10, "passed": 10}, result)
    assert verify._floor_problems({"collected": 12, "passed": 10}, result)


def test_failed_shard_counts_reach_the_existing_verifier(summaries):
    path = summaries / "shard-2.json"
    record = json.loads(path.read_text())
    record.update(output="2 passed, 1 failed, 1 xfailed in 2s", returncode=1)
    path.write_text(json.dumps(record))
    result = verify.aggregate_pytest(summaries)
    assert (result["passed"], result["failed"], result["collected"]) == (8, 1, 11)


@pytest.mark.parametrize(
    "mutation,match",
    [
        ({"output": "nothing ran"}, "missing printed pytest summary for shard 2/3"),
        ({"returncode": 3}, "shard 2/3 failed"),
        ({"plan": "old"}, "shard 2/3: wrong"),
        ({"shape": "wrong"}, "shard 2/3: wrong"),
    ],
)
def test_bad_shard_cannot_read_as_green(summaries, mutation, match):
    path = summaries / "shard-2.json"
    data = json.loads(path.read_text())
    data.update(mutation)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=match):
        verify.aggregate_pytest(summaries)


def test_aggregate_uses_the_normal_verifier_and_ceilings(summaries, monkeypatch):
    @contextlib.contextmanager
    def state():
        yield {"line": "test", "ledger": None}

    monkeypatch.setattr(verify, "private_state", state)
    monkeypatch.setattr(
        verify,
        "declared_rows_at_start",
        lambda _: {"state": "absent", "declared": 0, "missing": None},
    )
    monkeypatch.setattr(
        verify, "load_floor", lambda: {"collected": 11, "passed": 10, "skipped_max": 0}
    )
    monkeypatch.setattr(verify, "selftest_modules", lambda: [])
    monkeypatch.setattr(
        verify, "run_selftests", lambda *a, **kw: {"ok": [], "failed": {}, "skipped": {}}
    )
    monkeypatch.setattr(verify, "run_pytest", lambda **kw: pytest.fail("aggregate reran pytest"))
    monkeypatch.setattr(verify, "run_gates", lambda **kw: {})
    monkeypatch.setattr(verify, "absent_entrypoint_line", lambda _: None)
    code, text = verify.verify(aggregate=summaries)
    assert code == 1
    assert "CEILING exceeded" in text
    assert "11 collected; floor 11" in text


def test_only_final_printed_summary_supplies_counts(tmp_path, monkeypatch):
    result = verify.parse_pytest_output(
        "1 passed in 1s\nfixture: 999 failed\n2 passed, 1 skipped in 2s\n"
    )
    assert (result["passed"], result["skipped"], result["failed"]) == (2, 1, 0)
    # Item inventory is independent from the outcome summary: subtest/teardown
    # failures remain failures, and collection skips are diagnostics, not items.
    cases = [
        (
            "3 failed in 1s",
            '<testcase classname="m" name="test_parent"><failure/><failure/><failure/></testcase>',
            1,
            3,
            0,
            0,
        ),
        (
            "1 passed, 1 skipped in 1s",
            '<testcase classname="" name="tests.module"><skipped message="collection skipped"/></testcase><testcase classname="m" name="test_ok"/>',
            1,
            0,
            0,
            1,
        ),
        (
            "1 skipped, 1 xfailed in 1s",
            '<testcase classname="m" name="test_skip"><skipped message="real runtime skip"/></testcase><testcase classname="m" name="test_xfail"><skipped type="pytest.xfail"/></testcase>',
            2,
            0,
            1,
            0,
        ),
        (
            "2 passed in 1s",
            '<testcase classname="a.C" name="test_same"/><testcase classname="b.C" name="test_same"/>',
            2,
            0,
            0,
            0,
        ),
        (
            "1 passed, 1 error in 1s",
            '<testcase classname="m" name="test_teardown"/><testcase classname="m" name="test_teardown"><error/></testcase>',
            1,
            1,
            0,
            0,
        ),
    ]
    xml = tmp_path / "items.xml"
    for summary, body, collected, failed, skipped, collection_skips in cases:
        xml.write_text('<testsuite tests="999999" failures="0">' + body + "</testsuite>")
        parsed = verify.parse_pytest_output(summary, returncode=int(bool(failed)), junit=xml)
        assert (
            parsed["collected"],
            parsed["failed"],
            parsed["skipped"],
            len(parsed["collection_skip_records"]),
        ) == (collected, failed, skipped, collection_skips)
        assert parsed["returncode"] == int(bool(failed)) and parsed["inventory_error"] is None
        assert len(parsed["skip_records"]) == skipped
    for broken in (
        "<garbage/>",
        "<testsuites/>",
        "<testsuite",
        '<testsuite><testcase classname="" name="unknown"><skipped message="not a collection marker"/></testcase></testsuite>',
    ):
        xml.write_text(broken)
        parsed = verify.parse_pytest_output("1 passed in 1s", junit=xml)
        assert parsed["collected"] == 0 and parsed["inventory_error"]
    xml.unlink()
    assert verify.parse_pytest_output("1 passed in 1s", junit=xml)["inventory_error"]
    missing = verify.parse_pytest_output("1 passed in 1s", junit=xml)
    monkeypatch.setattr(verify, "shard_plan", lambda: [["inert.py"]])
    monkeypatch.setattr(verify, "run_pytest", lambda **kwargs: missing)
    monkeypatch.setattr(verify, "private_state", lambda: contextlib.nullcontext({}))
    monkeypatch.setattr(verify, "COVERAGE", False)
    code, _ = verify.run_pytest_shard("1/1", tmp_path / "shard")
    assert code == 1  # Green summary never clears missing expected identity evidence.


@pytest.mark.needs_coverage_module
def test_xdist_shard_coverage_measures_worker_execution(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "src/widget.py").write_text("def answer():\n    return 42\n")
    (tmp_path / "test_widget.py").write_text(
        "from widget import answer\ndef test_answer():\n    assert answer() == 42\n"
    )
    (tmp_path / "config/test-shards.json").write_text(json.dumps({"shards": [["test_widget.py"]]}))
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
        '[tool.coverage.run]\nparallel = true\nsource = ["src"]\n'
    )
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    monkeypatch.setattr(verify, "COVERAGE", True)
    monkeypatch.setenv("COVERAGE_FILE", str(tmp_path / ".coverage.shard-1"))
    monkeypatch.setenv("VERIFY_PYTEST_WORKERS", "2")
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    code, output = verify.run_pytest_shard("1/1", tmp_path / "evidence")
    assert code == 0, output
    report = tmp_path / "worker-coverage.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "json",
            "--data-file=.coverage.shard-1.pytest",
            "-o",
            str(report),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    files = json.loads(report.read_text())["files"]
    widget = next(name for name in files if name.endswith("widget.py"))
    assert 2 in files[widget]["executed_lines"]


@pytest.mark.needs_coverage_module
@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("verdict", [0, 1])
def test_aggregate_coverage_requires_every_shard_without_changing_verdict(
    tmp_path, monkeypatch, capsys, missing, verdict
):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (tmp_path / "run.py").write_text("answer = 42\n")
    monkeypatch.delenv("COVERAGE_FILE", raising=False)
    for index in (1, 2):
        if missing and index == 2:
            continue
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "coverage",
                "run",
                f"--data-file={evidence / f'.coverage.shard-{index}.pytest'}",
                "run.py",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
    # Stale JSON must not survive an incomplete measurement.
    (tmp_path / "coverage.json").write_text('{"stale": true}')
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    monkeypatch.setattr(verify, "COVERAGE", False)
    monkeypatch.setattr(verify, "shard_plan", lambda: [["a.py"], ["b.py"]])
    monkeypatch.setattr(verify, "verify", lambda **kw: (verdict, "verification verdict\n"))
    monkeypatch.setattr(sys, "argv", ["verify.py", "--aggregate", str(evidence), "--coverage"])
    assert verify.main() == verdict
    output = capsys.readouterr().out
    report = tmp_path / "coverage.json"
    if missing:
        assert "coverage: NOT PUBLISHED, shard 2/2 has no data" in output
        assert "0%" not in output
        assert not report.exists()
    else:
        assert "coverage: combined" in output
        assert "run.py" in json.loads(report.read_text())["files"]


@pytest.fixture
def planner_junit(tmp_path, monkeypatch):
    planner_path = pathlib.Path(__file__).resolve().parents[1] / "tools" / "make_test_shards.py"
    spec = importlib.util.spec_from_file_location("make_test_shards", planner_path)
    assert spec is not None and spec.loader is not None
    make_test_shards = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_test_shards)

    (tmp_path / "tests").mkdir()
    for name in ("a", "b", "c", "untimed"):
        (tmp_path / f"tests/test_{name}.py").touch()
    junit = tmp_path / "timings.xml"
    junit.write_text(
        "<testsuite>"
        '<testcase classname="tests.test_a.TestClass" time="2"/>'
        '<testcase classname="tests.test_a.TestClass" time="4"/>'
        '<testcase classname="tests.test_b" time="2"/>'
        '<testcase classname="tests.test_c" time="10"/>'
        "</testsuite>"
    )
    collected = "\n".join(
        f"tests/test_{name}.py::test_example" for name in ("a", "b", "c", "untimed")
    )
    monkeypatch.setattr(
        make_test_shards.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, collected, ""),
    )
    return make_test_shards, junit, tmp_path


def test_make_plan_aggregates_module_timings_and_defaults_to_median(planner_junit):
    planner, junit, root = planner_junit
    plan = planner.make_plan(junit, root, 2)
    assert plan == planner.make_plan(junit, root, 2)
    # Measured modules: a=6, b=2, c=10; untimed gets median 6. Greedy placement
    # puts c then b in shard 1, and a then untimed in shard 2 (load tie by index).
    assert plan["untimed_modules"] == ["tests/test_untimed.py"]
    assert plan["estimated_test_seconds"] == [12.0, 12.0]
    assert plan["shards"] == [
        ["tests/test_b.py", "tests/test_c.py"],
        ["tests/test_a.py", "tests/test_untimed.py"],
    ]


@pytest.mark.parametrize("duration", ["NaN", "inf", "-inf", "-1"])
def test_make_plan_rejects_invalid_junit_durations(planner_junit, duration):
    planner, junit, root = planner_junit
    junit.write_text(
        f'<testsuite><testcase classname="tests.test_a" time="{duration}"/></testsuite>'
    )
    with pytest.raises(ValueError, match="invalid JUnit duration"):
        planner.make_plan(junit, root, 2)
