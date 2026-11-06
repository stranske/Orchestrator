import sqlite3

import capability_opportunity as opportunity


def test_classify_trigger_handles_empty_custom_and_task_type_matchers():
    assert opportunity.classify_trigger({}) == {"kind": opportunity.TRIGGER_NONE, "detail": None}
    assert opportunity.classify_trigger({"matcher": {"kind": "custom", "name": "wake"}}) == {
        "kind": opportunity.TRIGGER_INTERNAL,
        "detail": {"kind": "custom", "name": "wake"},
    }
    assert opportunity.classify_trigger({"matcher": {"task_type": "repair"}}) == {
        "kind": opportunity.TRIGGER_TASK_TYPE,
        "detail": ["repair"],
    }
    assert opportunity.classify_trigger({"matcher": {"other": "value"}}) == {
        "kind": opportunity.TRIGGER_INTERNAL,
        "detail": {"other": "value"},
    }


def test_counts_close_owned_connection_and_filter_invalid_role_rows(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE runs (task_type TEXT)")
    conn.executemany(
        "INSERT INTO runs VALUES (?)", [("repair",), ("repair",), ("role:opener",), ("plain",)]
    )
    assert opportunity._task_type_counts(conn) == {
        "repair": 2,
        "role:opener": 1,
        "plain": 1,
    }
    assert opportunity._role_invocation_counts(conn) == {"opener": 1}

    owned = sqlite3.connect(":memory:")
    owned.execute("CREATE TABLE runs (task_type TEXT)")
    owned.execute("INSERT INTO runs VALUES ('repair')")
    monkeypatch.setattr(opportunity.feedback, "_conn", lambda: owned)
    assert opportunity._task_type_counts() == {"repair": 1}
    try:
        owned.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError("owned connection was not closed")


def test_assess_links_override_replay_and_main_dispatches_json(monkeypatch, capsys):
    result = opportunity.assess(
        "cap",
        {"matcher": {"task_type": "repair"}, "outcome_links": ["run-1"]},
        task_counts={"repair": 2},
        role_counts={},
        env={},
    )
    assert result["verdict"] == opportunity.VERDICT_IN_USE

    monkeypatch.setattr(opportunity, "report", lambda: {"ok": True})
    assert opportunity.main(["--json"]) == 0
    assert capsys.readouterr().out == '{\n  "ok": true\n}'

    called = []
    monkeypatch.setattr(opportunity, "_selftest", lambda: called.append(True))
    assert opportunity.main(["--selftest"]) == 0
    assert called == [True]
