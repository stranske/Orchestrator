#!/usr/bin/env python3
"""A module selftest must survive a concurrent run of itself.

`verify.py` runs every module's `--selftest`, and several `verify.py` runs from different sessions
overlap on one machine as a matter of course. On 2026-09-24 one went red with
`unknown feature: diff-anonymizer; record it before hardening` while `features.py --selftest` run
alone passed. The selftest kept its registry at a FIXED path under /tmp, so another run's unlink
deleted it between this run's `record_use` and its `mark_hardened`. The red said nothing about the
tree under test, which is the worst thing a verifier can say.

Everything here is deterministic. Nothing sleeps, polls or races: a hook on a module function runs
one COMPLETE second selftest at a chosen call and then lets the first run continue, which is the
incident's interleaving forced rather than hoped for.

1.  **features.py survives a second run at every registry read.** Every read goes through
    `features.load` and every read index is tried, so the property is "survives a concurrent run at
    any read" and the test does not need to know where the vulnerable window is.
2.  **repo_knowledge.py survives a second run before each reader of its selftest scratch** (the
    AGENTS.md export repo, the feedback snapshot, the docs repo, the review-comments file). Its
    selftest read those at fixed /tmp paths while already holding a private directory for its
    registry.
3.  **No module keeps scratch at a fixed `/tmp/__` path.** Three modules had the convention
    independently. research_scheduler.py is covered only here: its window sits inside one
    `load_hypotheses` call, between `exists()` and `read_text()`, so no function boundary can host
    a second run without faking the filesystem.
"""

from __future__ import annotations

import ast

import capabilities
import features
import paths
import repo_knowledge

# Split so that this file can never contain the literal it scans for, wherever it is copied.
SHARED_SCRATCH_PREFIX = "/tmp/" + "__"

REPO_KNOWLEDGE_SCRATCH_READERS = (
    "update_agents_md",
    "suggest_from_snapshot",
    "suggest_from_docs",
    "suggest_from_review_json",
)


def _selftest_with_a_second_run_at(monkeypatch, module, name: str, index: int | None) -> dict:
    """Run `module._selftest()`, first running one complete second selftest at call `index` of
    `module.<name>`.

    `index=None` interleaves nothing: the undisturbed run, which counts the calls. The second run
    passes straight through the hook, so exactly one run is interleaved into the other. Hooking the
    module attribute catches calls from the selftest and from inside the module alike, since both
    resolve the name through the module's globals.
    """
    real = getattr(module, name)
    seen = {"calls": 0, "second_runs": 0}
    inside_second_run = False

    def hooked(*args, **kwargs):
        nonlocal inside_second_run
        if not inside_second_run:
            if seen["calls"] == index:
                inside_second_run = True
                try:
                    module._selftest()
                finally:
                    inside_second_run = False
                seen["second_runs"] += 1
            seen["calls"] += 1
        return real(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module, name, hooked)
        module._selftest()
    return seen


def _short(exc: Exception) -> str:
    # Head and tail: an assertion's repr says most at its start, a missing file's path at its end.
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 200 else f"{text[:120]} ... {text[-60:]}"


def _failures(monkeypatch, module, points) -> dict:
    """Interleave a second run at each `(name, index)` point and collect every failure by point."""
    failed = {}
    for name, index in points:
        try:
            seen = _selftest_with_a_second_run_at(monkeypatch, module, name, index)
        except Exception as exc:
            failed[f"{name}#{index}"] = _short(exc)
            continue
        if seen["second_runs"] != 1:
            # The selftest never reached this call, so nothing was tested at it.
            failed[f"{name}#{index}"] = f"never reached: {seen}"
    return failed


def _empty_lifecycle(create: bool = False) -> dict:
    return {"path": None, "total": 0, "counts_by_status": {}, "active_without_edges": []}


def test_features_selftest_survives_a_concurrent_run_at_every_registry_read(monkeypatch):
    # features.summary joins in this machine's capability ledger: ~0.3 s a call on a populated one,
    # twice a run, and this test runs the selftest twice per registry read. The join shares no path
    # with the registry and the selftest asserts nothing about it, so it is stubbed, not paid for.
    monkeypatch.setattr(capabilities, "summary", _empty_lifecycle)
    undisturbed = _selftest_with_a_second_run_at(monkeypatch, features, "load", None)
    # NON-VACUITY. The interleavings are only as good as the reads they land on, and the incident's
    # window, between a record_use and a mark_hardened, spans at least two of them.
    assert undisturbed["calls"] >= 2, undisturbed

    failed = _failures(monkeypatch, features, [("load", i) for i in range(undisturbed["calls"])])
    assert not failed, (
        "features.py --selftest failed when a second run of it landed at these registry reads, so "
        f"the two runs share a path: {failed}"
    )


def test_repo_knowledge_selftest_survives_a_concurrent_run_before_each_scratch_reader(monkeypatch):
    failed = _failures(
        monkeypatch, repo_knowledge, [(n, 0) for n in REPO_KNOWLEDGE_SCRATCH_READERS]
    )
    assert not failed, (
        "repo_knowledge.py --selftest failed when a second run of it landed before these readers "
        f"of its scratch, so the two runs share a path: {failed}"
    )


def test_no_module_keeps_scratch_at_a_fixed_tmp_path():
    scanned, hits = [], []
    for path in sorted(paths.MODULE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        scanned.append(path.name)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith(SHARED_SCRATCH_PREFIX)
            ):
                hits.append(f"{path.name}:{node.lineno} {node.value!r}")
    # NON-VACUITY: an empty glob would pass this scan having read nothing.
    assert "features.py" in scanned, scanned
    assert not hits, (
        "a fixed /tmp path is shared by every run on the machine, so concurrent verify.py runs "
        "delete each other's scratch mid-run. Use tempfile.TemporaryDirectory(prefix=...) and "
        f"derive every path from it: {hits}"
    )
