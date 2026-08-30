"""Whether the deployed mirror carries the code that is on main.

WHAT HAPPENED, because the test exists for it. On 2026-08-30 `orch-sync-mirror.sh` was run twice.
It reported nothing wrong both times, and it copied faithfully — from a checkout sitting EIGHT
COMMITS behind `origin/main`. The mirror gained nothing. Four merged changes stayed inert,
including the module of a capability whose ledger row had just been registered, and every visible
signal read "synced".

That is a step succeeding at its job while achieving nothing, and reporting success. The check has
to ask two questions because asking one is exactly how it went unnoticed: a mirror/checkout
comparison cannot see it, since after such a sync the two trees agree perfectly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import switch_review


def _tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (root / name).write_text(body, encoding="utf-8")
    return root


def test_a_mirror_matching_the_checkout_is_clean(tmp_path):
    src = _tree(tmp_path / "src", {"a.py": "x = 1\n", "b.py": "y = 2\n"})
    mirror = _tree(tmp_path / "mirror", {"a.py": "x = 1\n", "b.py": "y = 2\n"})
    drift = switch_review.mirror_drift(mirror=mirror, checkout=src)
    assert drift["absent_from_mirror"] == []
    assert drift["differing"] == []


def test_a_module_the_sync_never_carried_is_named(tmp_path):
    """The live case on the day this was written: `coverage_testgen_trigger.py`, merged and absent."""
    src = _tree(tmp_path / "src", {"a.py": "x = 1\n", "new.py": "z = 3\n"})
    mirror = _tree(tmp_path / "mirror", {"a.py": "x = 1\n"})
    drift = switch_review.mirror_drift(mirror=mirror, checkout=src)
    assert drift["absent_from_mirror"] == ["new.py"]
    assert drift["status"] == "drifted"


def test_a_module_that_changed_since_the_sync_is_named(tmp_path):
    src = _tree(tmp_path / "src", {"a.py": "x = 2\n"})
    mirror = _tree(tmp_path / "mirror", {"a.py": "x = 1\n"})
    drift = switch_review.mirror_drift(mirror=mirror, checkout=src)
    assert drift["differing"] == ["a.py"]
    assert drift["status"] == "drifted"


def test_an_absent_mirror_is_unknown_and_never_clean(tmp_path):
    """ "Cannot compare" and "nothing to report" are opposite findings.

    Returning `ok` for a mirror that does not exist would report the most broken deployment
    possible as the healthiest one.
    """
    src = _tree(tmp_path / "src", {"a.py": "x = 1\n"})
    drift = switch_review.mirror_drift(mirror=tmp_path / "no-such-mirror", checkout=src)
    assert drift["status"] == "unknown"
    assert drift["status"] != "ok"
    assert "not the same as clean" in drift["reason"]


def test_an_absent_checkout_is_unknown_too(tmp_path):
    mirror = _tree(tmp_path / "mirror", {"a.py": "x = 1\n"})
    drift = switch_review.mirror_drift(mirror=mirror, checkout=tmp_path / "no-such-src")
    assert drift["status"] == "unknown"


def test_the_report_says_what_to_do_and_does_not_do_it(tmp_path):
    """FYI-only, by construction: an automatic deploy is the circuit breaker the manual sync IS."""
    src = _tree(tmp_path / "src", {"a.py": "x = 2\n", "new.py": "z = 3\n"})
    mirror = _tree(tmp_path / "mirror", {"a.py": "x = 1\n"})
    report = switch_review.format_report(
        {
            "generated_at": 0,
            "review_days": 7,
            "held_off": [],
            "on_but_idle": [],
            "unconditioned": [],
            "stale_runners": [],
            "mirror_drift": switch_review.mirror_drift(mirror=mirror, checkout=src),
            "raise_count": 0,
        }
    )
    assert "new.py" in report
    assert "orch-sync-mirror.sh" in report
    assert "circuit breaker" in report
    # The mirror must be untouched: reporting drift may never repair it.
    assert not (mirror / "new.py").exists()


def test_a_drifted_mirror_is_not_reported_as_nothing_due(tmp_path):
    """The whole point. `Nothing due` beside a mirror missing a merged module is the silence this
    sweep exists to break."""
    src = _tree(tmp_path / "src", {"a.py": "x = 1\n", "new.py": "z = 3\n"})
    mirror = _tree(tmp_path / "mirror", {"a.py": "x = 1\n"})
    report = switch_review.format_report(
        {
            "generated_at": 0,
            "review_days": 7,
            "held_off": [],
            "on_but_idle": [],
            "unconditioned": [],
            "stale_runners": [],
            "mirror_drift": switch_review.mirror_drift(mirror=mirror, checkout=src),
            "raise_count": 0,
        }
    )
    assert "Nothing due" not in report


def test_a_clean_sweep_still_says_nothing_due(tmp_path):
    """The complement: the new check must not make an idle sweep look busy forever."""
    src = _tree(tmp_path / "src", {"a.py": "x = 1\n"})
    mirror = _tree(tmp_path / "mirror", {"a.py": "x = 1\n"})
    drift = switch_review.mirror_drift(mirror=mirror, checkout=src)
    drift["status"] = "ok"  # isolate from this checkout's real upstream state
    report = switch_review.format_report(
        {
            "generated_at": 0,
            "review_days": 7,
            "held_off": [],
            "on_but_idle": [],
            "unconditioned": [],
            "stale_runners": [],
            "mirror_drift": drift,
            "raise_count": 0,
        }
    )
    assert "Nothing due" in report


def test_behind_is_never_silently_zero(tmp_path, monkeypatch):
    """A checkout with no upstream ref locally is UNMEASURED, and the reason says so.

    Reporting 0 would be the same defect one level down: "I could not tell" rendered as "all is
    well", in the very check written because a sync reported success while carrying stale code.
    """
    src = _tree(tmp_path / "src", {"a.py": "x = 1\n"})
    mirror = _tree(tmp_path / "mirror", {"a.py": "x = 1\n"})

    def no_upstream(*args, **kwargs):
        return subprocess.CompletedProcess(args[0] if args else [], 128, "", "no upstream")

    monkeypatch.setattr(switch_review.subprocess, "run", no_upstream)
    drift = switch_review.mirror_drift(mirror=mirror, checkout=src)
    assert drift["checkout_behind"] is None
    assert "UNMEASURED" in drift["reason"]
