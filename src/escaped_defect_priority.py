#!/usr/bin/env python3
"""escaped_defect_priority.py — order test-writing work by where testing actually FAILED.

WHY NOT "MOST UNCOVERED LINES FIRST". That ordering maximises percentage-per-PR, and it points
agents at the largest uncovered files, which are almost always the big glue modules where a
meaningful test is hardest to write. It is the ordering most likely to produce hollow tests, and
hollow tests raise the number while buying nothing. So uncovered mass is the LAST tier here, not
the first.

THREE TIERS, most informative first.

  1. ESCAPED DEFECTS — a file that later needed a bug fix is a file whose tests did not catch
     something. This is the only tier that reports observed failure of the tests themselves rather
     than a property of the code, which is why it leads.
  2. CHURN x TESTABILITY — a file that changes often is where regressions arrive; one with a low
     branch-to-statement ratio is one where a test can pin behaviour rather than smoke it.
  3. UNCOVERED MASS — how much the metric would move. Last, deliberately.

  Every tier is multiplied by (1 - hollow_rate). A module where agents keep producing tests that
  pass against a broken base scores LOW however much uncovered code it has, so the untestable glue
  de-prioritises itself without anyone classifying it by hand. That factor only became measurable
  when testgen_gate grew `no_hollow_nodes`; before it, there was no way to tell the two apart.

TIER 1 IS A GIT PROXY TODAY, AND SAYS SO. The Brain has the better signal in
`outcomes.durability` — `broke_later` means merged, CI green, broke afterwards. Measured
2026-08-26 across 4,665 outcome rows: durable 2842, abandoned 1300, pending 517, reverted 4,
reworked 2, and `broke_later` ZERO. `durability_sweep.py` assigns "reopened, reverted, or durable"
and never `broke_later`, while `pattern_miner.TERMINAL_FAILURE_DURABILITY` consumes it — a
consumer for a label nothing produces. Six escaped-defect rows cannot order a work queue, so tier
1 reads git history instead: it is available in every repo today and needs no instrumentation.
`brain_signal_status()` reports which source is in use, so the day the Brain signal becomes usable
is visible rather than assumed.

CONFIDENCE IS NOT UNIFORM, AND THE DIFFERENCE MATTERS. A `fix(...)` commit touching a file is
decent evidence for ORDERING work and poor evidence for TRAINING a learner: code is changed for
many reasons, and a fix on the same file may repair something the original change never touched.
So this module ranks; it does NOT write durability labels. Anything feeding `outcomes.durability`
must clear a higher bar, and keeping the two apart is deliberate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KILL_SWITCH = "ORCH_ESCAPED_DEFECT_PRIORITY"

# Conventional-commit fix prefixes, plus the bare forms real history actually uses. Anchored at the
# start of the subject so "prefix the fix with..." in prose does not count as a fix commit.
FIX_SUBJECT = re.compile(
    r"^\s*(?:fix|bugfix|hotfix|patch)\b[^:]*:|^\s*(?:fix|fixes|fixed)\s+",
    re.IGNORECASE,
)
REVERT_SUBJECT = re.compile(r"^\s*revert\b", re.IGNORECASE)

# A revert is stronger evidence than an ordinary fix: somebody judged the change wrong outright.
REVERT_WEIGHT = 3.0
FIX_WEIGHT = 1.0

DEFAULT_LOOKBACK_DAYS = 180
DEFAULT_LIMIT = 25


@dataclass
class FileScore:
    """One candidate file, with every tier kept separate so a ranking can be explained."""

    path: str
    escaped: float = 0.0
    churn: int = 0
    uncovered: int = 0
    hollow_rate: float = 0.0
    evidence: list[str] = field(default_factory=list)

    def sort_key(self) -> tuple[float, float, float]:
        """LEXICOGRAPHIC, not a weighted sum — the tiers must not trade against each other.

        The first version multiplied the tiers apart (1e6 / 1e3 / 1) and summed them, which only
        holds while the lower tiers stay small: at 1,000,000 uncovered statements tier 3 exactly
        equals one escaped defect, and the ordering the module exists to guarantee silently
        inverts. A scoring function whose correctness depends on its inputs staying below a magic
        threshold is a defect waiting for a big repository, so the comparison is a tuple: no
        amount of tier 3 can ever reach past tier 2, whatever the magnitudes.

        The hollow discount multiplies EVERY component rather than the total, so a fully hollow
        file collapses to (0, 0, 0) and sorts last regardless of tier, while a partly hollow one
        keeps its tier and is discounted within it.
        """
        keep = 1.0 - self.hollow_rate
        return (self.escaped * keep, self.churn * keep, self.uncovered * keep)

    def as_dict(self) -> dict[str, Any]:
        # No blended "score" is reported. One number formed from three incomparable tiers invites
        # exactly the trade-off the tuple exists to forbid, and a reader can order these rows by
        # eye without it.
        t1, t2, t3 = self.sort_key()
        return {
            "path": self.path,
            "rank_key": [round(t1, 3), round(t2, 3), round(t3, 3)],
            "tier1_escaped_defects": round(self.escaped, 3),
            "tier2_churn": self.churn,
            "tier3_uncovered_statements": self.uncovered,
            "hollow_rate": round(self.hollow_rate, 3),
            "evidence": self.evidence[:5],
        }


def _git(repo: Path, *args: str, timeout: int = 60) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=timeout, check=False
    )
    return proc.stdout if proc.returncode == 0 else ""


def fix_commits(repo: Path, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> list[tuple[str, float]]:
    """Return (file, weight) for every file touched by a fix or revert commit in the window.

    `--no-merges` matters: a squash-merge commit carries the PR title, so counting merges as well
    would double-count the same fix once as the merge and once as the underlying commit.
    """
    raw = _git(
        repo,
        "log",
        f"--since={lookback_days}.days.ago",
        "--no-merges",
        "--name-only",
        "--pretty=format:%x00%s",
    )
    out: list[tuple[str, float]] = []
    weight = 0.0
    for line in raw.split("\n"):
        if line.startswith("\x00"):
            subject = line[1:]
            if REVERT_SUBJECT.search(subject):
                weight = REVERT_WEIGHT
            elif FIX_SUBJECT.search(subject):
                weight = FIX_WEIGHT
            else:
                weight = 0.0
            continue
        path = line.strip()
        if path and weight:
            out.append((path, weight))
    return out


def churn(repo: Path, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict[str, int]:
    """How many non-merge commits touched each file in the window."""
    raw = _git(
        repo,
        "log",
        f"--since={lookback_days}.days.ago",
        "--no-merges",
        "--name-only",
        "--pretty=format:",
    )
    counts: dict[str, int] = {}
    for line in raw.split("\n"):
        path = line.strip()
        if path:
            counts[path] = counts.get(path, 0) + 1
    return counts


def uncovered_by_file(coverage_json: dict[str, Any]) -> dict[str, int]:
    """Missing statements per file, from a coverage.py JSON report.

    Rows with an ABSOLUTE path are dropped: those are tmp-workspace copies a test fixture made,
    and they are not files anybody can open. That contamination put 95 phantom rows into
    stranske/Trend_Model_Project's payload and filled 13 of its 15 worst-file slots.
    """
    out: dict[str, int] = {}
    for path, data in (coverage_json.get("files") or {}).items():
        if Path(path).is_absolute():
            continue
        summary = data.get("summary") or {}
        missing = int(summary.get("missing_lines") or 0)
        if missing:
            out[path] = missing
    return out


def rename_map(repo: Path, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict[str, str]:
    """Map every historical path to the name it goes by now, following chains of renames.

    A reorganised repository otherwise ranks the same module twice. Measured on this repo the day
    the ranking was first used for real: `capability_advisor.py` and `src/capability_advisor.py`
    both appeared, each holding part of one module's evidence, and `dispatcher.py` outranked
    `src/verify.py` while not existing at that path at all. Neither is a file an agent can open.

    Chains are followed to a fixed point because a file moved twice in the window would otherwise
    resolve to an intermediate name that is just as gone as the first one.
    """
    raw = _git(
        repo,
        "log",
        f"--since={lookback_days}.days.ago",
        "--no-merges",
        "--name-status",
        "-M",
        "--pretty=format:",
    )
    direct: dict[str, str] = {}
    for line in raw.split("\n"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) == 3 and parts[0].startswith("R"):
            direct[parts[1]] = parts[2]

    resolved: dict[str, str] = {}
    for start in direct:
        seen = {start}
        current = start
        while current in direct and direct[current] not in seen:
            current = direct[current]
            seen.add(current)
        resolved[start] = current
    return resolved


def rank(
    repo: str | Path,
    coverage_json: dict[str, Any] | None = None,
    *,
    hollow_rates: dict[str, float] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    rows, _ = _rank_rows(
        repo,
        coverage_json,
        hollow_rates=hollow_rates,
        lookback_days=lookback_days,
        limit=limit,
    )
    return rows


def _rank_rows(
    repo: str | Path,
    coverage_json: dict[str, Any] | None = None,
    *,
    hollow_rates: dict[str, float] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[dict[str, Any]], int]:
    """Rank candidate files for test-writing, most informative signal first.

    `hollow_rates` maps path -> observed share of hollow nodes from past testgen attempts. Absent,
    every file scores as if no attempt has been made, which is the correct prior: unmeasured is not
    the same as measured-good, and the first attempt on a file is what produces the measurement.
    """
    repo_path = Path(repo).expanduser().resolve()
    scores: dict[str, FileScore] = {}

    def _row(path: str) -> FileScore:
        if path not in scores:
            scores[path] = FileScore(path=path)
        return scores[path]

    for path, weight in fix_commits(repo_path, lookback_days):
        row = _row(path)
        row.escaped += weight
        label = "revert" if weight == REVERT_WEIGHT else "fix"
        if len(row.evidence) < 5:
            row.evidence.append(f"{label} commit touched this file")

    for path, count in churn(repo_path, lookback_days).items():
        _row(path).churn = count

    for path, missing in uncovered_by_file(coverage_json or {}).items():
        _row(path).uncovered = missing

    for path, rate in (hollow_rates or {}).items():
        if path in scores:
            scores[path].hollow_rate = max(0.0, min(1.0, float(rate)))

    # Fold each historical path onto the name it goes by now, so a module that moved is ONE
    # candidate holding all of its evidence rather than two holding half each.
    renames = rename_map(repo_path, lookback_days)
    if renames:
        merged: dict[str, FileScore] = {}
        for score in scores.values():
            target = renames.get(score.path, score.path)
            if target not in merged:
                merged[target] = FileScore(path=target)
            row = merged[target]
            row.escaped += score.escaped
            row.churn += score.churn
            row.uncovered = max(row.uncovered, score.uncovered)
            row.hollow_rate = max(row.hollow_rate, score.hollow_rate)
            for item in score.evidence:
                if len(row.evidence) < 5 and item not in row.evidence:
                    row.evidence.append(item)
        scores = merged

    # Only Python source is a testgen target. Ranking a lockfile by churn would be true and useless.
    # A path that no longer exists is dropped for the same reason, one step further on: an agent
    # cannot open it, so ranking it spends the queue's first slot on nothing.
    ordered = [
        s
        for s in scores.values()
        if s.path.endswith(".py")
        and not Path(s.path).name.startswith("test_")
        and (repo_path / s.path).exists()
    ]
    # Counted, not just filtered. A window that predates a repository-wide move scores every
    # candidate at a path nobody can open, and "nothing needed tests" is the wrong reading of it.
    vanished = sum(
        1
        for s in scores.values()
        if s.path.endswith(".py")
        and not Path(s.path).name.startswith("test_")
        and not (repo_path / s.path).exists()
    )
    ordered.sort(key=lambda s: (-s.sort_key()[0], -s.sort_key()[1], -s.sort_key()[2], s.path))
    return [s.as_dict() for s in ordered[:limit]], vanished


def rank_status(
    repo: str | Path,
    coverage_json: dict[str, Any] | None = None,
    *,
    hollow_rates: dict[str, float] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """`rank`, plus whether an empty result means "nothing scored" or "nothing was readable".

    `rank` alone cannot say. Both of its git-backed tiers go through `_git`, which returns "" on a
    non-zero exit — so a directory that is not a git repository produces exactly the empty list a
    pristine repository does, and a caller choosing work from it would read "no file needs tests"
    off a failed subprocess. That is this workspace's most repeated defect wearing its test-writing
    costume: one value meaning both "measured zero" and "could not measure", where only the first
    is good news.

    So the git tiers get a probe of their own. `rev-parse --git-dir` is the cheapest question that
    distinguishes them, and it is asked BEFORE ranking rather than inferred from an empty result.
    """
    repo_path = Path(repo).expanduser().resolve()
    _capability_heartbeat()

    if not repo_path.is_dir():
        return [], {"status": "unavailable", "reason": f"no such directory: {repo_path}"}
    probe = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if probe.returncode != 0:
        return [], {
            "status": "unavailable",
            "reason": (
                f"not a git repository: {repo_path} — tiers 1 and 2 both read git history, so an "
                "empty ranking here would report zero signal rather than no reading"
            ),
        }

    rows, vanished = _rank_rows(
        repo_path,
        coverage_json,
        hollow_rates=hollow_rates,
        lookback_days=lookback_days,
        limit=limit,
    )
    if rows:
        return rows, {"status": "ok", "reason": f"{len(rows)} file(s) scored"}

    read = f"git history over {lookback_days} days"
    read += (
        f" and a coverage report naming {len(coverage_json.get('files', {}))} file(s)"
        if coverage_json
        else ", and no coverage report was supplied"
    )
    if vanished:
        return [], {
            "status": "no_signal",
            "reason": (
                f"read {read}; {vanished} Python file(s) scored but NONE still exists at the path "
                "history names them by — the window predates a move, so widen it or rank a "
                "checkout matching the history"
            ),
        }
    return [], {
        "status": "no_signal",
        "reason": f"read {read}; no Python source scored on any tier",
    }


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Record that the ranker ran, at its own code path.

    Filed under `testgen-lane` on purpose. This module is a RAIL the lane consults, not a separate
    capability: it makes no model call, it dispatches nothing, and it does no work a caller could
    be offered instead of the lane. Giving it its own ledger row would add a lifecycle record for
    an implementation detail — and a second inventory of the same capability is how this project
    loses track of features. One capability, two code paths that can prove it ran. Never raises:
    recording use must not be able to prevent the work. (2026-08-29)
    """
    try:
        import capabilities

        capabilities.production_heartbeat(
            "testgen-lane", event_type, ref="escaped_defect_priority.rank_status"
        )
    except Exception:
        pass


def brain_signal_status(db_path: str | Path | None = None) -> dict[str, Any]:
    """Report whether the Brain's escaped-defect signal is usable yet, and never guess.

    Tier 1 reads git today because `outcomes.durability` carries almost no terminal-failure rows.
    This makes that a measured statement rather than an assumption, so the day it becomes usable is
    visible. An unreadable Brain reports `unknown` -- NOT zero, because "no data" and "cannot read"
    are different answers and only one of them means the signal is absent.
    """
    import sqlite3

    if db_path is None:
        try:
            import feedback

            db_path = getattr(feedback, "DB", None) or getattr(feedback, "DB_PATH", None)
        except Exception:
            db_path = None
    if not db_path or not Path(str(db_path)).exists():
        return {"source": "git", "brain": "unknown", "reason": "feedback store not readable here"}
    try:
        conn = sqlite3.connect(str(db_path))
        rows = dict(conn.execute("SELECT durability, COUNT(*) FROM outcomes GROUP BY durability"))
    except Exception as exc:  # noqa: BLE001 - a broken read must not take the ranking down
        return {"source": "git", "brain": "unknown", "reason": f"query failed: {exc}"}
    terminal = sum(
        int(rows.get(k) or 0) for k in ("broke_later", "reopened", "reverted", "reworked")
    )
    return {
        "source": "git" if terminal < 30 else "brain",
        "brain": "sparse" if terminal < 30 else "usable",
        "terminal_failure_rows": terminal,
        "broke_later_rows": int(rows.get("broke_later") or 0),
        "reason": (
            "fewer than 30 terminal-failure outcomes: too sparse to order a queue, so tier 1 "
            "falls back to git history"
            if terminal < 30
            else "enough terminal-failure outcomes to rank on directly"
        ),
    }


def enabled() -> bool:
    """The kill switch. Unset or 0 means the caller keeps its previous ordering."""
    return os.environ.get(KILL_SWITCH, "") == "1"


def _selftest() -> None:
    import tempfile

    # --- tier separation: one escaped defect outranks a large uncovered file -----------------
    defect = FileScore(path="a.py", escaped=1.0, churn=0, uncovered=0)
    # DELIBERATELY ABSURD magnitudes: the weighted-sum version passed at 5,000 and inverted at
    # 1,000,000, so the selftest uses the number that actually breaks a scaled sum.
    bulk = FileScore(path="b.py", escaped=0.0, churn=0, uncovered=10**9)
    assert defect.sort_key() > bulk.sort_key(), (defect.sort_key(), bulk.sort_key())
    churny = FileScore(path="c.py", escaped=0.0, churn=50, uncovered=0)
    assert churny.sort_key() > bulk.sort_key(), "churn must outrank raw uncovered mass"
    assert defect.sort_key() > churny.sort_key(), "an escaped defect must outrank churn"

    # --- the hollow multiplier sinks a file however much uncovered code it has ---------------
    hollow = FileScore(path="d.py", escaped=1.0, uncovered=0, hollow_rate=1.0)
    assert hollow.sort_key() == (0.0, 0.0, 0.0), hollow.sort_key()
    half = FileScore(path="e.py", escaped=1.0, hollow_rate=0.5)
    assert abs(half.sort_key()[0] - defect.sort_key()[0] / 2) < 1e-6

    # --- subject matching: prose about fixing is not a fix commit ---------------------------
    assert FIX_SUBJECT.search("fix(coverage): omit tmp rows")
    assert FIX_SUBJECT.search("fix: null deref")
    assert FIX_SUBJECT.search("Fixes crash on empty input")
    assert not FIX_SUBJECT.search("feat: prefix the fix with a scope")
    assert not FIX_SUBJECT.search("refactor: tidy the fixer")
    assert REVERT_SUBJECT.search('Revert "feat: thing"')

    # --- absolute paths are contamination, not candidates ------------------------------------
    cov = {
        "files": {
            "src/real.py": {"summary": {"missing_lines": 10}},
            "/tmp/pytest-of-runner/ws/src/real.py": {"summary": {"missing_lines": 900}},
            "src/clean.py": {"summary": {"missing_lines": 0}},
        }
    }
    unc = uncovered_by_file(cov)
    assert unc == {"src/real.py": 10}, unc

    # --- end to end against a REAL git repo, so the log parsing is exercised, not mocked ------
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "buggy.py").write_text("x = 1\n")
        (repo / "calm.py").write_text("y = 2\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "feat: initial"], cwd=repo, check=True)
        (repo / "buggy.py").write_text("x = 2\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "fix(core): off-by-one"], cwd=repo, check=True)

        ranked = rank(repo, {"files": {"calm.py": {"summary": {"missing_lines": 9999}}}})
        assert ranked, ranked
        assert ranked[0]["path"] == "buggy.py", ranked
        assert ranked[0]["tier1_escaped_defects"] == 1.0, ranked[0]
        # calm.py has 9,999 uncovered statements and still loses to one fix commit.
        calm = [r for r in ranked if r["path"] == "calm.py"]
        assert calm and calm[0]["rank_key"] < ranked[0]["rank_key"], ranked

        # A hollow history sinks the top candidate below the bulk file.
        sunk = rank(
            repo,
            {"files": {"calm.py": {"summary": {"missing_lines": 9999}}}},
            hollow_rates={"buggy.py": 1.0},
        )
        assert sunk[0]["path"] == "calm.py", sunk

    # --- an unreadable coverage report must not read as "everything is covered" -------------
    # Break -> revert 2026-08-26: restoring the silent `if path.exists()` fall-through makes the
    # CLI report a column of zeros for tier 3 with no indication that nothing was read.
    import contextlib
    import io

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "m.py").write_text("z = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "fix: something"], cwd=repo, check=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["--repo", str(repo), "--coverage-json", str(repo / "nope.json")])
        out = buf.getvalue()
        assert "UNAVAILABLE" in out, out
        assert "does not exist" in out, out

    # --- the Brain status never guesses ------------------------------------------------------
    absent = brain_signal_status("/no/such/store.db")
    assert absent["source"] == "git" and absent["brain"] == "unknown", absent

    # --- rank_status separates "nothing scored" from "nothing readable" ----------------------
    # Both produce the SAME empty list from `rank`, because both git tiers go through a helper
    # that returns "" on a non-zero exit. Only the probe tells them apart, and a caller choosing
    # test-writing work from the wrong one reads a failed subprocess as "no file needs tests".
    with tempfile.TemporaryDirectory() as td:
        pristine = Path(td)
        subprocess.run(["git", "init", "-q", "."], cwd=pristine, check=True)
        rows, drained = rank_status(pristine)
        assert rows == [] and drained["status"] == "no_signal", drained
        assert "no coverage report" in drained["reason"], drained

    _, unreadable = rank_status("/no/such/directory/at/all")
    assert unreadable["status"] == "unavailable", unreadable
    assert drained["status"] != unreadable["status"], "one sentinel must not mean both"

    print(
        "escaped_defect_priority.py selftest: OK (tier ordering, hollow multiplier, fix-subject "
        "matching, contamination drop, live git ranking, an unreadable Brain reported as "
        "unknown rather than zero, and an empty ranking distinguished from an unreadable one)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--repo", default=".", help="repository to rank")
    parser.add_argument("--coverage-json", type=Path, default=None, help="coverage.py JSON report")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--json", action="store_true")
    ns = parser.parse_args(argv)
    if ns.selftest:
        _selftest()
        return 0

    # A MISSING COVERAGE FILE IS NOT AN EMPTY ONE. The first version of this silently fell
    # through to {} when the path did not exist, so tier 3 rendered as a column of zeros that
    # looked like "everything is covered" rather than "nothing was read" -- the same
    # could-not-measure-as-measured-zero defect this module's own tier 1 note describes. Caught
    # when a scratch path was cleaned up between runs and the ranking cheerfully carried on.
    cov: dict[str, Any] = {}
    tier3 = "off (no --coverage-json given)"
    if ns.coverage_json:
        if not ns.coverage_json.exists():
            tier3 = f"UNAVAILABLE — {ns.coverage_json} does not exist"
        else:
            try:
                cov = json.loads(ns.coverage_json.read_text(encoding="utf-8"))
                n = len(uncovered_by_file(cov))
                tier3 = (
                    f"{n} file(s) with missing statements"
                    if n
                    else f"UNAVAILABLE — {ns.coverage_json} parsed but names no in-repo file "
                    "with missing statements (wrong report, or wrong path root?)"
                )
            except (OSError, json.JSONDecodeError) as exc:
                tier3 = f"UNAVAILABLE — {ns.coverage_json} unreadable: {exc}"
    ranked = rank(ns.repo, cov, lookback_days=ns.lookback_days, limit=ns.limit)
    status = brain_signal_status()
    if ns.json:
        print(
            json.dumps(
                {"tier1_source": status, "tier3_coverage": tier3, "ranked": ranked}, indent=2
            )
        )
        return 0
    print(f"tier 1 source: {status['source']} ({status.get('reason')})")
    print(f"tier 3 coverage: {tier3}")
    print(f"{'':<3}{'FILE':<52}{'DEFECT':>7}{'CHURN':>7}{'UNCOV':>7}")
    for i, row in enumerate(ranked, 1):
        print(
            f"{i:<3}{row['path'][:50]:<52}{row['tier1_escaped_defects']:>7}"
            f"{row['tier2_churn']:>7}{row['tier3_uncovered_statements']:>7}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
