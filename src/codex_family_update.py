"""Compare the native desktop subscription catalog with the approved routine Codex role."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import model_table

# Named versions only. A new alias, preview, variant or family needs a role decision.
MODEL_ID = re.compile(r"gpt-(\d+(?:\.\d+)*)-([a-z][a-z0-9]*(?:-[a-z0-9]+)*)")


class CatalogIssue(ValueError):
    """A safe operator-facing catalog diagnostic."""


def version_and_family(slug):
    match = MODEL_ID.fullmatch(slug)
    if not match:
        return None
    return tuple(map(int, match[1].split("."))), match[2]


def successor_for(current, models):
    """Prefer the provider migration. Otherwise compare visible versions of the same family."""
    current_id = version_and_family(current)
    if not current_id:
        raise CatalogIssue("configured model has no recognized version and family")
    original = models.get(current, {})
    upgrade = original.get("upgrade") or {}
    if upgrade:
        target = upgrade["model"]
        identity = version_and_family(target)
        if not identity or identity[1] != current_id[1] or identity[0] <= current_id[0]:
            raise CatalogIssue("provider migration changes family or does not advance the version")
        if target not in models or models[target].get("visibility") != "list":
            raise CatalogIssue("provider successor is unavailable on this subscription")
        return target, "provider migration"
    candidates = []
    for slug, row in models.items():
        identity = version_and_family(slug)
        if identity and identity[1] == current_id[1] and identity[0] > current_id[0]:
            if row.get("visibility") == "list":
                candidates.append((identity[0], slug))
    if not candidates:
        if not original:
            raise CatalogIssue("neither the configured model nor a successor is present")
        return None, None
    highest = max(version for version, _ in candidates)
    choices = [slug for version, slug in candidates if version == highest]
    if len(choices) != 1:
        raise CatalogIssue("ambiguous release identifiers for the newest family version")
    return choices[0], "new family version"


def report(config, catalog, source_text, now, mirror_text=None):
    source = model_table.load(source_text)
    mirror_text = model_table.TABLE_JSON if mirror_text is None else mirror_text
    mirror = model_table.load(mirror_text)
    agents = config["agents"]
    current = agents["default_subagent_model"]
    effort = agents["default_subagent_reasoning_effort"]
    fetched = datetime.fromisoformat(catalog["fetched_at"].replace("Z", "+00:00"))
    if not timedelta(0) <= now - fetched <= timedelta(hours=48):
        raise CatalogIssue("subscription catalog needs a fresh read")
    models = {row["slug"]: row for row in catalog["models"]}
    if len(models) != len(catalog["models"]):
        raise CatalogIssue("duplicate catalog model identities")
    selected = model_table.model_for("codex", "routine", source)
    mirror_selected = model_table.model_for("codex", "routine", mirror)
    fallback = model_table.provider("codex", source)["fallback"]
    target, basis = successor_for(current, models)
    summary = {
        "desktop_model": current,
        "desktop_effort": effort,
        "desktop_listed": current in models,
        "workflows_model": selected,
        "orchestrator_model": mirror_selected,
        "fallback_model": fallback,
        "fallback_listed": fallback in models,
        "fallback_retirement_at": ((models.get(fallback) or {}).get("upgrade") or {}).get(
            "retirement_at"
        ),
        "candidate": target,
        "basis": basis,
        "catalog_at": catalog["fetched_at"],
    }
    if source_text != mirror_text:
        status = "sync_orchestrator"
    elif selected != current:
        # A newer offer may have appeared while a previously qualified upgrade was merging.
        approved = version_and_family(selected)
        old = version_and_family(current)
        if approved and old and approved[1] == old[1] and approved[0] > old[0]:
            if models.get(selected, {}).get("visibility") != "list":
                raise CatalogIssue("the approved model is unavailable on this subscription")
            status = "apply_approved_model"
        else:
            status = "reconcile_role"
    elif target:
        status = "qualify_candidate"
    else:
        status = "current"
    check = selected if status in {"current", "apply_approved_model"} else target
    if check and effort not in {
        row["effort"] for row in models[check]["supported_reasoning_levels"]
    }:
        raise CatalogIssue("the model does not support the configured reasoning effort")
    summary["status"] = status
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    codex_dir = Path.home() / ".codex"
    parser.add_argument("--config", type=Path, default=codex_dir / "config.toml")
    parser.add_argument("--catalog", type=Path, default=codex_dir / "models_cache.json")
    parser.add_argument("--workflows-table", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = report(
            tomllib.loads(args.config.read_text()),
            json.loads(args.catalog.read_text()),
            args.workflows_table.read_text(),
            datetime.now(timezone.utc),
        )
    except CatalogIssue as exc:
        print(json.dumps({"status": "unavailable", "detail": str(exc)}))
        return 2
    except (OSError, KeyError, TypeError, ValueError) as exc:
        # A read error must not leak credentials or the contents of the desktop config.
        print(json.dumps({"status": "unavailable", "detail": type(exc).__name__}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
