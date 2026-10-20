"""Lane C round-5 coverage for Orchestrator #802 redirect_apply tripwires.

CI after rounds 1–4 left only four statements on the five targets — the bodies of
selftest tripwires that fire only when an invariant is already broken:

- `_selftest_prescreen` `must_not_apply` raise (1398)
- `_selftest_prescreen` `brain_tripwire` body (1582–83)
- `_selftest` `must_not_run` raise (1675)

These tests force those broken paths hermetically so the raise lines execute, then
assert the named AssertionError. No production changes.
"""

from __future__ import annotations

import pytest

import feedback
import redirect_apply as ra
import redirect_plan


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """Keep state + learner paths inside the test temp dir; never spawn agents."""
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr(ra.capabilities, "REG", runtime / "capabilities.json")
    # apply_plan writes the retry prompt before it reaches the injected runner tripwire.
    monkeypatch.setattr(redirect_plan, "PROMPT_DIR", tmp_path / "redirect-prompts")
    db = runtime / "feedback" / "orchestrator.db"
    db.parent.mkdir(parents=True)
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(db))
    monkeypatch.setattr(feedback, "DB_PATH", db)
    started: list = []

    def forbid(*a, **k):
        started.append((a, k))
        raise AssertionError(f"subprocess must not start: {a!r}")

    monkeypatch.setattr("subprocess.run", forbid)
    monkeypatch.setattr("subprocess.Popen", forbid)
    monkeypatch.setattr("socket.create_connection", forbid)
    monkeypatch.setattr("socket.socket.connect", forbid)
    yield
    assert started == []


def test_redirect_apply_must_not_run_tripwire_body(monkeypatch) -> None:
    """Line 1675: force flag-off apply_one to would_mutate so must_not_run fires."""
    real_authorize = ra.authorize
    forced = {"n": 0}

    def force_first_dry_mutate(*args, **kwargs):
        out = real_authorize(*args, **kwargs)
        # First dry path in `_selftest`: flag off, allowed, would_mutate False.
        if (
            forced["n"] == 0
            and kwargs.get("flag_on") is False
            and out.get("allowed") is True
            and out.get("would_mutate") is False
        ):
            forced["n"] += 1
            out = dict(out)
            out["would_mutate"] = True
        return out

    monkeypatch.setattr(ra, "authorize", force_first_dry_mutate)
    with pytest.raises(AssertionError, match="apply_plan was reached when it must not be"):
        ra._selftest()


def test_redirect_apply_must_not_apply_tripwire_body(monkeypatch, tmp_path) -> None:
    """Line 1398: force a refused prescreen apply_one to would_mutate so must_not_apply fires."""
    real_authorize = ra.authorize
    forced = {"n": 0}

    def force_refused_mutate(*args, **kwargs):
        out = real_authorize(*args, **kwargs)
        # Prescreen's first apply_one: flag on, running lane → allowed False.
        if (
            forced["n"] == 0
            and kwargs.get("flag_on") is True
            and out.get("allowed") is False
            and out.get("would_mutate") is False
        ):
            forced["n"] += 1
            out = dict(out)
            out["would_mutate"] = True
            out["allowed"] = True
        return out

    monkeypatch.setattr(ra, "authorize", force_refused_mutate)
    with pytest.raises(AssertionError, match="apply_plan was reached when it must not be"):
        ra._selftest_prescreen(tmp_path)


def test_redirect_apply_brain_tripwire_body(monkeypatch, tmp_path) -> None:
    """Lines 1582–83: force status(link_preview=False) to open the Brain via _conn."""
    real_status = ra.status

    def leaky_status(*args, **kwargs):
        if kwargs.get("link_preview") is False:
            # Inside _selftest_prescreen this is patched to brain_tripwire.
            feedback._conn()
        return real_status(*args, **kwargs)

    monkeypatch.setattr(ra, "status", leaky_status)
    with pytest.raises(AssertionError, match="status\\(link_preview=False\\) opened the Brain"):
        ra._selftest_prescreen(tmp_path)
