"""A comment that names a call is not a caller.

THE DEFECT (2026-10-05). `capability_activation_audit._callers_of` matched python drivers as raw
text, so backlog.py's comment explaining why `adversarial.high_stakes_reason()` needs source-issue
labels was credited as a production caller of `adversarial-review`, beside tick.py's real call. The
shell branch had refused comments since a false PASS of the same kind; the python branch had not.
On the day the tick stopped calling the panel, that comment would have been the capability's only
"caller". Measured over the 24 directly-entered capabilities, it was the only verdict the raw-text
match changed.
"""

from __future__ import annotations

import capability_activation_audit as audit

SOURCE = '''
"""Module docstring naming adversarial.review() as if it were called."""
import adversarial


def documented():
    """adversarial.high_stakes_reason(item) is what the merge seat calls."""
    # adversarial.review_at_head(target, head) would also be named here
    return "adversarial.recorded_verdict(target)"


def real(item):
    reason = adversarial.high_stakes_reason(item)
    keep = adversarial.CONCLUSIVE_VERDICTS
    return reason, keep
'''


def test_only_executable_references_count():
    calls, attrs = audit._python_references(SOURCE, "adversarial")
    assert calls == {"high_stakes_reason"}, calls
    assert attrs == {"high_stakes_reason", "CONCLUSIVE_VERDICTS"}, attrs


def test_a_driver_that_only_mentions_a_call_is_not_its_caller(tmp_path, monkeypatch):
    driver = tmp_path / "backlog.py"
    driver.write_text("# `adversarial.high_stakes_reason()` reads source_labels\nimport json\n")
    monkeypatch.setattr(audit, "DRIVER_MODULES", ("backlog.py",))
    monkeypatch.setattr(audit, "_driver_path", lambda name: tmp_path / name)
    assert audit._callers_of("adversarial", {"high_stakes_reason"}) == []
    assert audit._callers_of("adversarial", {"*"}) == []
    driver.write_text("import adversarial\n\nadversarial.high_stakes_reason({})\n")
    assert audit._callers_of("adversarial", {"high_stakes_reason"}) == [
        "backlog.py:high_stakes_reason"
    ]


def test_an_unparseable_driver_falls_back_to_the_text_match(tmp_path, monkeypatch):
    """A broken driver is never silence: it keeps the old answer rather than reporting no caller."""
    (tmp_path / "tick.py").write_text("def broken(:\n    adversarial.review(wt)\n")
    monkeypatch.setattr(audit, "DRIVER_MODULES", ("tick.py",))
    monkeypatch.setattr(audit, "_driver_path", lambda name: tmp_path / name)
    assert audit._python_references("def broken(:", "adversarial") is None
    assert audit._callers_of("adversarial", {"review"}) == ["tick.py:review"]


def test_adversarial_review_is_reached_from_the_merge_seat_in_this_tree():
    """The tree's own drivers, read as code: the panel's production caller is merge_guard, and its
    once-per-head entry reaches the heartbeat `review` records."""
    cap = {
        "capability_id": "adversarial-review",
        "entrypoint": "adversarial.py",
        "matcher": {"kind": "closer_gate", "name": "high_stakes_review"},
    }
    assert audit.entry_class(cap) == audit.ENTRY_DIRECT
    reach = audit.heartbeat_reachable(cap)
    assert reach["status"] == "reachable", reach
    assert reach["via"] and all(v.startswith("merge_guard.py:") for v in reach["via"]), reach
    path = audit.paths.MODULE_DIR / "adversarial.py"
    graph = audit._call_graph(path)
    assert audit._reaches("review_at_head", graph, audit._heartbeat_functions(path)), graph
