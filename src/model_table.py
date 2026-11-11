"""The shared agent model table, local side.

ONE table chooses the model and reasoning level for every agent run, hosted (GitHub Actions) and
local (this Orchestrator). Its source is stranske/Workflows `.github/agents/model-table.json`;
`TABLE_JSON` below is a byte-identical copy of that file. It lives inside this module, not in a
config file, so it ships wherever the code ships: the launchd mirror and the flat closer runtime
package copy Python modules only. `adapters.MODEL_TIERS`, the named single-model seats (vibe, muse,
cursor, aider) and the Codex task/offload profiles are DERIVED from it, so nothing in this
repository keeps its own model list.

Change the table in Workflows first, then copy it here with
`python3 src/model_table.py --sync <Workflows>/.github/agents/model-table.json`.
Drift: `tests/test_model_table.py` fails when the copy differs from Workflows `main` or when a
derived value disagrees with the table; Workflows `scripts/check_model_tables.py` prints
`model tables agree: N mismatches`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SCHEMA = "agent-model-table/v1"
BEGIN_MARK = 'TABLE_JSON = r"""'
END_MARK = '"""  # END AGENT MODEL TABLE'
WORKFLOWS_SOURCE = (
    "https://raw.githubusercontent.com/stranske/Workflows/main/.github/agents/model-table.json"
)

# Local tier ladder. `frontier` is reached only through explicit work kinds (hard/hardest),
# never as a router `mode` token, so the router's cheap/mid/full ceiling logic is unchanged.
LOCAL_TIERS = ("cheap", "mid", "full")
# Agents whose local adapter pins a model per tier. The others run one model and say so through
# their named constants in adapters.py; an empty tier map there means "no ladder", never unknown.
TIERED_AGENTS = ("codex", "claude", "gemini")

# Router task types -> kind of work. This is the local half of the shared vocabulary: hosted
# runners reach the same kinds through execution profiles and runner modes.
TASK_WORK_KINDS: dict[str, str] = {
    "implement": "implement",
    "epic": "hard",
    "cross_repo": "hard",
    "runtime_ac": "hard",
    "mechanical": "routine",
    "testgen": "routine",
    "codemod": "routine",
    "polish": "routine",
    "review": "routine",
    "closer": "coordinate",
    "coordinator": "coordinate",
}
# Unknown local task types keep their historical default (Sol, medium effort).
DEFAULT_TASK_WORK_KIND = "coordinate"
# Offload / tier-only modes -> kind of work.
MODE_WORK_KINDS: dict[str, str] = {
    "cheap": "extract",
    "mid": "routine",
    "full": "implement",
    "assess": "coordinate",
}


def load(text: str | None = None) -> dict[str, Any]:
    table = json.loads(TABLE_JSON if text is None else text)
    if table.get("schema") != SCHEMA:
        raise ValueError(f"model table schema {table.get('schema')!r}, need {SCHEMA}")
    return table


def provider(agent: str, table: dict[str, Any] | None = None) -> dict[str, Any]:
    return (table or TABLE)["providers"][agent]


def model_for(agent: str, kind: str, table: dict[str, Any] | None = None) -> str:
    """The pinned LOCAL model for an agent and kind of work."""
    t = table or TABLE
    p = provider(agent, t)
    models = p.get("surface_models", {}).get("local") or p["models"]
    return models[t["work_kinds"][kind]["tier"]]


def effort_for(agent: str, kind: str, table: dict[str, Any] | None = None) -> str | None:
    """The reasoning level for a kind of work, or None where this agent/model takes none."""
    t = table or TABLE
    effort = t["work_kinds"][kind]["effort"]
    spec = provider(agent, t).get("effort") or {}
    allowed = spec.get("models")
    if effort not in (spec.get("levels") or []):
        return None
    if allowed and model_for(agent, kind, t) not in allowed:
        return None
    return effort


def tier_models(agent: str, table: dict[str, Any] | None = None) -> dict[str, str]:
    """`adapters.MODEL_TIERS[agent]`: cheap/mid/full for tiered agents, {} for single-model seats."""
    if agent not in TIERED_AGENTS:
        return {}
    t = table or TABLE
    p = provider(agent, t)
    models = p.get("surface_models", {}).get("local") or p["models"]
    return {tier: models[tier] for tier in LOCAL_TIERS}


def single_model(agent: str, table: dict[str, Any] | None = None) -> str:
    """The one model a single-model seat runs (its `full` tier)."""
    return model_for(agent, "implement", table)


def tier_effort(agent: str, mode: str | None, table: dict[str, Any] | None = None) -> str | None:
    """Reasoning level for a tier-only run (no execution profile)."""
    kind = MODE_WORK_KINDS.get(mode or "")
    if kind is None or mode == "assess":
        return None
    return effort_for(agent, kind, table)


def codex_profile_for(
    kind: str, registry: dict[str, dict[str, Any]], retired: dict[str, str]
) -> str:
    """The active, automatically selectable Codex profile for a kind of work.

    Profiles are immutable (model, effort) identities; the table picks which one is current.
    Raises when no registered profile carries the table's pair: the fix is to register one.
    """
    model = model_for("codex", kind)
    effort = effort_for("codex", kind)
    matches = sorted(
        pid
        for pid, p in registry.items()
        if p["agent"] == "codex"
        and p["requested_model"] == model
        and p["reasoning_effort"] == effort
        and p["lifecycle_status"] == "active"
        and pid not in retired
        and not p.get("requires_explicit_selection")
    )
    if len(matches) != 1:
        raise ValueError(
            f"model table: codex {kind} = {model}/{effort} must match exactly one active profile, "
            f"found {matches or 'none'}"
        )
    return matches[0]


def _sync(source: Path) -> int:
    """Replace the embedded copy with `source` byte-for-byte (the paired-PR copy step)."""
    text = source.read_text(encoding="utf-8")
    load(text)
    if '"""' in text or "\\" in text:
        raise SystemExit("model table text must not contain triple quotes or backslashes")
    module = Path(__file__).resolve()
    current = module.read_text(encoding="utf-8")
    start = current.index("\n" + BEGIN_MARK) + 1 + len(BEGIN_MARK)
    end = current.index("\n" + END_MARK, start) + 1
    module.write_text(current[:start] + text + current[end:], encoding="utf-8")
    print(f"model table copied from {source}")
    return 0


# BEGIN AGENT MODEL TABLE: byte-identical copy of stranske/Workflows .github/agents/model-table.json
TABLE_JSON = r"""{
  "schema": "agent-model-table/v1",
  "as_of": "2026-11-10",
  "purpose": "The ONE agent model table for hosted (GitHub Actions) and local (Orchestrator) agent runs: per provider, a pinned model per tier, a reasoning level per kind of work, the surfaces each agent runs on, and one fallback. Hosted runners read this file at run time from .workflows-lib (stranske/Workflows@workflows_ref). Orchestrator carries a byte-identical copy as TABLE_JSON in src/model_table.py; scripts/check_model_tables.py fails when the two disagree.",
  "change_rule": "Edit this file and merge it first, then copy it to stranske/Orchestrator in a paired PR with `python3 src/model_table.py --sync <this file>`. Codex execution_profiles in .github/agents/registry.yml must agree with work_kinds below (the drift check counts every disagreement).",
  "tiers": ["cheap", "mid", "full", "frontier"],
  "work_kinds": {
    "extract": {"tier": "cheap", "effort": "low", "meaning": "structured reads, summaries, classification"},
    "routine": {"tier": "mid", "effort": "medium", "meaning": "bounded edits, autofix, tests, review, codemods"},
    "coordinate": {"tier": "full", "effort": "medium", "meaning": "planning, coordination, closer/lane decisions"},
    "implement": {"tier": "full", "effort": "high", "meaning": "feature/fix implementation (keepalive default)"},
    "hard": {"tier": "frontier", "effort": "medium", "meaning": "epics, cross-repo plans, runtime acceptance evidence"},
    "hardest": {"tier": "frontier", "effort": "high", "meaning": "explicit escalation only"}
  },
  "default_work_kind": "implement",
  "mode_work_kinds": {
    "autofix": "routine",
    "keepalive": "implement"
  },
  "execution_profile_work_kinds": {
    "codex-default": "implement",
    "codex-fast": "routine",
    "codex-extract": "extract",
    "codex-routine": "routine",
    "codex-coordinate": "coordinate",
    "codex-implement": "implement",
    "codex-hard": "hard",
    "codex-hardest": "hardest"
  },
  "providers": {
    "codex": {
      "surfaces": ["hosted", "local"],
      "role": "primary",
      "models": {
        "cheap": "gpt-6-luna",
        "mid": "gpt-5.6-terra",
        "full": "gpt-6.1-sol",
        "frontier": "gpt-6-astra"
      },
      "fallback": "gpt-5.6-terra",
      "effort": {"flag": "-c model_reasoning_effort", "levels": ["low", "medium", "high", "xhigh"]}
    },
    "claude": {
      "surfaces": ["hosted", "local"],
      "role": "primary",
      "models": {
        "cheap": "claude-haiku-4-5",
        "mid": "claude-sonnet-5-5",
        "full": "claude-opus-5-5",
        "frontier": "claude-opus-5-5"
      },
      "fallback": "claude-sonnet-5-5",
      "effort": {
        "flag": "--effort",
        "levels": ["low", "medium", "high", "xhigh", "max"],
        "models": ["claude-sonnet-5-5", "claude-opus-5-5"]
      }
    },
    "gemini": {
      "surfaces": ["hosted", "local"],
      "role": "primary",
      "models": {
        "cheap": "gemini-3.8-flash-low",
        "mid": "gemini-3.8-flash-high",
        "full": "gemini-4-argon-high",
        "frontier": "gemini-4-carbon-high"
      },
      "surface_models": {
        "hosted": {
          "cheap": "gemini-3.8-flash",
          "mid": "gemini-3.8-flash",
          "full": "gemini-4-argon",
          "frontier": "gemini-4-argon"
        }
      },
      "surface_model_evidence": {
        "hosted": "Gemini API ids for the models the fleet runs and evaluates (Gemini 3.8 Flash, Gemini 4 Argon), the same ids config/verifier_shadow.json calls on GEMINI_API_KEY. The API id carries no effort rung; local agy ids do. Corrected 2026-11-09: the first hosted ids (gemini-3.5-flash, gemini-2.5-pro) were copied from the gemini-cli built-in model list, which lags the fleet.",
        "local": "Gemini 4 Carbon (announced 2026-10-28, Google's frontier model above Argon) is the local frontier tier from 2026-11-10: agy 1.5.5 advertises gemini-4-carbon-{low,medium,high} and the Orchestrator's agy answered a live call on each. full stays Argon until Carbon has its own pilot record. Hosted keeps Argon: Carbon is not reported on the public Gemini API."
      },
      "fallback": "gemini-3.8-flash-high",
      "surface_fallbacks": {"hosted": "gemini-3.8-flash"},
      "effort": {"flag": "", "levels": [], "note": "local agy ids carry the effort rung in the model id; hosted gemini-cli has no effort flag"}
    },
    "cursor": {
      "surfaces": ["hosted", "local"],
      "role": "primary",
      "models": {
        "cheap": "composer-2.5",
        "mid": "composer-2.5",
        "full": "composer-2.5",
        "frontier": "composer-2.5"
      },
      "fallback": "composer-2.5",
      "effort": {"flag": "", "levels": [], "note": "owner policy 2026-08-08: Composer only; never `auto` or a frontier model"}
    },
    "muse": {
      "surfaces": ["hosted", "local"],
      "role": "backup",
      "models": {
        "cheap": "muse-wonder-1.0",
        "mid": "muse-wonder-1.0",
        "full": "muse-wonder-1.0",
        "frontier": "muse-wonder-1.0"
      },
      "fallback": "muse-wonder-1.0",
      "effort": {"flag": "--reasoning-effort", "levels": ["low", "medium", "high", "xhigh"]}
    },
    "vibe": {
      "surfaces": ["local"],
      "role": "primary",
      "models": {
        "cheap": "mistral-medium-3.5",
        "mid": "mistral-medium-3.5",
        "full": "mistral-medium-3.5",
        "frontier": "mistral-medium-3.5"
      },
      "fallback": "mistral-medium-3.5",
      "effort": {"flag": "", "levels": [], "note": "single-model lane; the CLI config names the model"}
    }
  }
}
"""  # END AGENT MODEL TABLE

TABLE: dict[str, Any] = load()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--sync":
        raise SystemExit(_sync(Path(sys.argv[2])))
    raise SystemExit("usage: model_table.py --sync <Workflows>/.github/agents/model-table.json")
