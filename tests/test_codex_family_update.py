import json
from datetime import datetime, timezone

import pytest

import codex_family_update as family
import model_table

NOW = datetime(2026, 11, 26, 13, 0, tzinfo=timezone.utc)
OLD = "gpt-5.6-terra"
NEW = "gpt-6.1-terra"


def row(slug, **changes):
    return dict(
        {"slug": slug, "visibility": "list", "supported_reasoning_levels": [{"effort": "medium"}]},
        **changes,
    )


def snapshot(rows, *, desktop=OLD, primary=OLD, mirror=None, when="2026-11-26T12:00:00Z"):
    def table(model):
        value = model_table.load()
        provider = value["providers"]["codex"]
        provider["models"]["mid"] = model
        provider["fallback"] = OLD
        return json.dumps(value)

    return family.report(
        {
            "agents": {
                "default_subagent_model": desktop,
                "default_subagent_reasoning_effort": "medium",
            }
        },
        {"fetched_at": when, "models": rows},
        table(primary),
        NOW,
        table(primary if mirror is None else mirror),
    )


def test_named_successor_promotes_in_source_before_mirror_and_desktop():
    models = [row(OLD, upgrade={"model": NEW, "retirement_at": "2026-12-23T20:00:00Z"}), row(NEW)]
    pending = snapshot(models)
    assert (pending["status"], pending["candidate"], pending["basis"]) == (
        "qualify_candidate",
        NEW,
        "provider migration",
    )
    assert snapshot(models, primary=NEW, mirror=OLD)["status"] == "sync_orchestrator"
    assert snapshot(models, primary=NEW)["status"] == "apply_approved_model"
    current = snapshot(models, desktop=NEW, primary=NEW)
    assert (current["status"], current["candidate"], current["fallback_retirement_at"]) == (
        "current",
        None,
        "2026-12-23T20:00:00Z",
    )


def test_current_model_must_still_support_the_saved_effort():
    low_only = row(NEW, supported_reasoning_levels=[{"effort": "low"}])
    with pytest.raises(family.CatalogIssue, match="configured reasoning effort"):
        snapshot([low_only], desktop=NEW, primary=NEW)


def test_numeric_family_order_recovers_a_retired_incumbent_without_changing_family():
    models = [row("gpt-10.1-terra"), row(NEW), row("gpt-11-terra-preview"), row("gpt-24-sol")]
    result = snapshot(models)
    assert not result["desktop_listed"]
    assert result["candidate"] == "gpt-10.1-terra"
    assert snapshot(models, primary=NEW)["status"] == "apply_approved_model"
    assert snapshot(models, primary="gpt-24-sol")["status"] == "reconcile_role"


@pytest.mark.parametrize(
    "models,message",
    [
        ([row(OLD, upgrade={"model": "gpt-6.2-sol"}), row("gpt-6.2-sol")], "changes family"),
        ([row(OLD, upgrade={"model": NEW}), row(NEW, visibility="hide")], "unavailable"),
        ([row(OLD), row(NEW, supported_reasoning_levels=[{"effort": "low"}])], "effort"),
        ([row(OLD), row(NEW), row(NEW)], "duplicate"),
        ([row("gpt-6.2-sol")], "neither"),
    ],
)
def test_unavailable_or_incompatible_candidates_stop(models, message):
    with pytest.raises(family.CatalogIssue, match=message):
        snapshot(models)


def test_stale_catalog_is_unknown_and_private_config_is_not_printed(tmp_path, capsys):
    with pytest.raises(family.CatalogIssue, match="fresh"):
        snapshot([row(OLD), row(NEW)], when="2026-11-20T01:00:00Z")
    config = tmp_path / "config.toml"
    config.write_text("private-sentinel = [")
    assert (
        family.main(["--config", str(config), "--workflows-table", str(tmp_path / "missing")]) == 2
    )
    assert "private-sentinel" not in capsys.readouterr().out
