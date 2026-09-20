import adapters
import execution_profiles


def test_full_tier_routes_sol_and_astra_remains_explicit():
    assert adapters.resolve_model("codex", "full") == "gpt-5.6-sol"
    assert adapters.resolve_model("codex", "mid") == "gpt-5.6-terra"
    assert adapters.resolve_model("codex", "cheap") == "gpt-5.6-luna"
    active = {p["requested_model"] for p in execution_profiles.profiles_for_agent("codex")}
    assert "gpt-6-astra" in active
    assert "gpt-5.6-sol" in active
    assert execution_profiles.default_codex_profile("implement", "full") == "codex-5.6-sol-high"
    assert execution_profiles.get_profile("codex-6-astra-high")["reasoning_effort"] == "high"


def test_existing_profile_database_accepts_new_immutable_profiles(tmp_path, monkeypatch):
    import copy
    import sqlite3

    current = execution_profiles.PROFILE_REGISTRY
    new_ids = {
        "codex-6-astra-medium",
        "codex-5.6-sol-medium",
        "codex-5.6-terra-medium",
        "codex-5.6-luna-low",
    }
    before = copy.deepcopy({pid: p for pid, p in current.items() if pid not in new_ids})
    with sqlite3.connect(tmp_path / "old-profiles.db") as conn:
        monkeypatch.setattr(execution_profiles, "PROFILE_REGISTRY", before)
        execution_profiles.ensure_schema(conn)
        old = dict(conn.execute("SELECT profile_id, definition_json FROM execution_profiles"))
        assert set(old) == set(before)
        monkeypatch.setattr(execution_profiles, "PROFILE_REGISTRY", current)
        execution_profiles.ensure_schema(conn)
        updated = dict(conn.execute("SELECT profile_id, definition_json FROM execution_profiles"))
        assert {pid: updated[pid] for pid in old} == old
        assert set(updated) == set(current)
        assert new_ids <= set(updated)
