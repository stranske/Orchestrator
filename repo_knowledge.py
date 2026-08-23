#!/usr/bin/env python3
"""repo_knowledge.py - per-repo playbook snippets for Orchestrator delegations.

First increment for durable repo-knowledge / failure-pattern memory:
keep a small JSON registry of repo conventions and recurring gotchas, then inject
the relevant subset into delegated-agent prompts. This is deliberately simple:
structured notes now, richer outcome/RAG feeds later.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import feedback
import provision

ORCH = Path(__file__).resolve().parent
REG = Path(os.environ.get("ORCH_REPO_KNOWLEDGE_PATH", ORCH / "experiments" / "repo_knowledge.json"))
DEFAULT_MAX_CHARS = 3000
AGENTS_EXPORT_START = "<!-- BEGIN orch-playbook -->"
AGENTS_EXPORT_END = "<!-- END orch-playbook -->"
AGENTS_EXPORT_NOTE = "<!-- exported by repo_knowledge.py; owner: Orchestrator; freshness owner: keepalive -->"
AGENTS_EXPORT_MAX_LINES = 30
MANAGED_RULE_HASH_PREFIX = "orch-rule"
FAILURE_DURABILITIES = {"reverted", "reworked", "reopened", "broke_later", "abandoned"}
PLAYBOOK_SECTIONS = {"definition_of_done", "gotchas", "validation"}
MEMORY_SECTION_BASE = {
    "contraindications": 0.30,
    "definition_of_done": 0.28,
    "gotchas": 0.28,
    "validation": 0.24,
    "base_branch": 0.22,
    "summary": 0.12,
    "outcome_notes": 0.08,
}
FUZZY_DUPLICATE_THRESHOLD = 0.72
# A gotcha an auditor cannot see is a gotcha that does not exist. `task_types`/`lanes` are for items
# that only matter WHILE DOING that kind of work; a repo INVARIANT (base branch, formatter, a tool
# that is broken here) must carry no scope, or every review/audit consult is answered with silence.
# `scope_audit()` reports scoped items that look invariant, and the selftest fails if SEED has any.
INVARIANT_SIGNAL_PATTERNS = [
    r"\bdefault branch\b",
    r"\bbase branch\b",
    r"\bblack\b",
    r"\bruff\b",
    r"\bmypy\b",
    r"\bpre-commit\b",
    r"\bline-length\b",
    r"\bpostgres(?:ql)?\b",
    r"\bsqlite\b",
    r"\bunreliable\b",
    r"\bcontraindicated\b",
    r"\bdoes not work\b",
    r"\bdo not assume\b",
]
# Structured, NOT a text section: a contraindication names a capability, so it is typed rather than
# prose. Kept out of PLAYBOOK_SECTIONS deliberately -- approve_suggestion/install_managed_rule write
# free text into those, and free text cannot name a capability the advisor can match on.
CONTRAINDICATION_SECTION = "contraindications"
TOKEN_SYNONYMS = {
    "changes": "change",
    "checks": "check",
    "docs": "doc",
    "documentation": "doc",
    "documents": "doc",
    "files": "file",
    "migrations": "migration",
    "prs": "pr",
    "postgresql": "postgres",
    "renames": "rename",
    "tests": "test",
    "workflows": "workflow",
}
SUGGESTION_STOPWORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "if", "in", "into", "is", "it", "its",
    "of", "on", "or", "that", "the", "this", "to", "when", "with",
}
NOTE_SIGNAL_PATTERNS = [
    r"\bmust\b",
    r"\bshould\b",
    r"\bavoid\b",
    r"\bdo not\b",
    r"\bdon't\b",
    r"\bmissing\b",
    r"\bomitted\b",
    r"\bfailed\b",
    r"\bwrong\b",
    r"\bfragile\b",
    r"\bgotcha\b",
    r"\bblocked\b",
]
DOC_SIGNAL_PATTERNS = [
    r"\bmust\b",
    r"\bshould\b",
    r"\bavoid\b",
    r"\bdo not\b",
    r"\bdon't\b",
    r"\bgotcha\b",
    r"\brequired\b",
    r"\brequires\b",
    r"\balways\b",
    r"\bnever\b",
    r"\bbefore (?:merging|opening|pushing|committing|deploying)\b",
    (
        r"^(?:run|use|prefer|validate|verify|ensure|include|update|keep|document)\b"
        r".{0,90}\b(?:pytest|ruff|black|mypy|lint|format|coverage|ci|migrations?|"
        r"postgres(?:ql)?|sqlite|sync-manifest|workflows?|tests?|testing)\b"
    ),
    (
        r"\b(?:pytest|ruff|black|mypy|lint|coverage|ci|migrations?|postgres(?:ql)?|"
        r"sqlite|sync-manifest|workflows?|tests?|testing)\b"
        r".{0,90}\b(?:required|requires|must|should|before|validate|verify|check|gate)\b"
    ),
]
DOC_EXTENSIONS = {".md", ".rst", ".txt"}
DOC_ROOT_FILES = {
    "README.md",
    "CONTRIBUTING.md",
    "DEVELOPMENT.md",
    "TESTING.md",
    "AGENTS.md",
    "CLAUDE.md",
    "CODEX.md",
}
DOC_DIRS = {"docs", ".github"}
DOC_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", ".next", "__pycache__"}
DOC_SKIP_FILE_PREFIXES = ("BRIEF_",)
DOC_SKIP_FILE_NAMES = {"CODEX_BRIEF.md"}

# v2 (2026-08-23): corrects a factually wrong Trend summary, unscopes seeded invariants, and adds
# the `contraindications` section. Bumping this runs `_migrate()` once against an existing registry.
SEED_SCHEMA_VERSION = 2

# The three corrected Trend lines, named once and consumed twice -- by the SEED below and by the
# v1 -> v2 migration table. A migration that wrote different words than the SEED would leave two
# instances of this tool disagreeing about the same repo, which is the failure the correction is
# for; the selftest asserts every replacement value is actually present in the SEED.
_TREND_SUMMARY = (
    "Quant trend-model app: a Streamlit operator surface, a notebook GUI and the `trend` CLI over "
    "one config contract. `phase-3` IS the default branch -- there is no `main`."
)
_TREND_BASE_GOTCHA = (
    "`phase-3` is the default branch and the base for all work here; there is no `main`. Anything "
    "that assumes `main` is wrong for this repo."
)
_TREND_FORMAT_GOTCHA = (
    "Formatting is Black at line-length 100 (`black --check --line-length 100`, gated by "
    "`pr-00-gate.yml` `format_check: true`); lint is `ruff check` with "
    "`lint.select = [\"E4\",\"E7\",\"E9\",\"F\"]`. Never run `ruff format` -- it is not configured "
    "here and produces pure churn."
)

# v1 -> v2 text corrections, keyed by repo and matched EXACTLY, so an instance that edited one of
# these lines by hand is never clobbered -- only the seeded wording is replaced.
SUPERSEDED_TEXT = {
    "stranske/Trend_Model_Project": {
        "Trend opener work cuts from phase-3, not the default branch.": _TREND_SUMMARY,
        "Use phase-3 as the base for opener work; do not assume main.": _TREND_BASE_GOTCHA,
        "CI convention is ruff check; do not introduce unrelated ruff format churn.":
            _TREND_FORMAT_GOTCHA,
    },
}

SEED = {
    "schema_version": SEED_SCHEMA_VERSION,
    "repos": {
        "stranske/Workflows": {
            "summary": "Shared automation source for the fleet; workflow changes usually need docs and registry surfaces.",
            "definition_of_done": [
                {
                    "text": "Workflow additions or renames must update docs/ci/WORKFLOWS.md, docs/ci/WORKFLOW_SYSTEM.md, and the workflow naming tests.",
                },
                {
                    "text": "Consumer-facing files usually need sync-manifest/template coverage, not just the root copy.",
                },
            ],
            "validation": [
                "Run the narrow workflow/template tests named by the touched surface; include the command/result in the PR body.",
            ],
        },
        "stranske/Trend_Model_Project": {
            "summary": _TREND_SUMMARY,
            # ONE CONSTANT, NOT A MATCHING PAIR OF LITERALS. provision.BASE_BRANCH_OVERRIDES is what
            # actually decides the base an opener branch is cut from; a second "phase-3" literal here
            # would be free to drift away from it silently.
            "base_branch": provision.BASE_BRANCH_OVERRIDES["stranske/Trend_Model_Project"],
            "gotchas": [
                {"text": _TREND_BASE_GOTCHA},
                {"text": _TREND_FORMAT_GOTCHA},
                {
                    "text": "Public demo work tied to presentation-safe/public-LLM modes should honor the stlite/Pyodide/WASM owner direction; do not substitute Streamlit Cloud or remove LangChain unless the issue explicitly changes that decision.",
                },
                # Ingested 2026-08-23 from the "Standing notes for the next round" section of
                # Code/Audits/Trend_Model_Project/README.md, each re-verified against phase-3 before
                # seeding (the section also carried "CI gates ruff check only", which is FALSE --
                # Black is gated too -- so the notes are a candidate queue, not a source of truth).
                {
                    "text": "Two config packages with near-identical names and diverging rules: `src/trend_analysis/config/model.py` (singular -- the strict TrendConfig/PortfolioSettings validator) and `src/trend_analysis/config/models.py` (plural -- the runtime Config). Always check both.",
                },
                {
                    "text": "`config/defaults.yml` ships `signals: {}` -- present but empty -- and `src/trend/spec.py` treats a falsy `signals` as \"no signals\", which discards the whole `vol_adjust` section. Several \"vol adjustment did not run\" symptoms trace to that one empty mapping.",
                },
                {
                    "text": "`validate_config(..., include_model_validation=...)` defaults to False while `src/trend/cli.py` passes True, so validation strictness differs by caller. Always state which setting a validation claim was made under.",
                },
                {
                    "text": "The GUI tests stub the ipywidgets classes (`tests/test_gui_app_extended.py` DummyDropdown accepts any `value` with no option-membership check), so widget-contract behaviour is structurally untestable there. Gate any widget finding against real ipywidgets or it is invisible.",
                },
            ],
            "contraindications": [
                {
                    "capability": "frontend-verifier",
                    "reason": "`frontend_verify.py` snapshots this Streamlit SPA before the websocket render completes, so its evidence is unreliable here.",
                    "instead": "Drive a real browser, or execute the real functions.",
                    "evidence": "Code/Audits/Trend_Model_Project/README.md, \"Standing notes for the next round\" -- recorded in the 2026-08-11 round and re-confirmed in the 2026-08-23 round after the render-timing fix.",
                },
            ],
            "validation": [
                "For public demo changes, include browser-visible evidence for presentation_safe and public_llm_demo modes, including screenshots and network/egress evidence when requested.",
            ],
        },
        "stranske/Counter_Risk": {
            "summary": "Counter_Risk has repo-specific formatting expectations.",
            "gotchas": [
                {
                    "text": "Use Black for formatting checks; do not substitute ruff format unless the repo config explicitly changes.",
                },
            ],
        },
        "stranske/Fine-Art-Archive": {
            "summary": "Fine-Art Archive port/bootstrap work may depend on local workspace artifacts that cloud agents cannot read.",
            "gotchas": [
                {
                    "text": "Do not reintroduce template placeholder scaffolding such as src/my_project; opener work should modify the requested fine_art_archive app/library surface.",
                },
                {
                    "text": "If an issue depends on files from the local Claude Project/Cowork workspace, route it through a local lane or provide the exact files/manifests; cloud agents cannot infer unavailable workspace-only code.",
                },
            ],
            "validation": [
                "For companion-app/acquisition work, verify the requested module or UI surface exists and changed; generic project scaffolding is not acceptable progress.",
            ],
        },
        "stranske/Inv-Man-Intake": {
            "summary": "Investment-manager intake app; docs PRs should stay tightly scoped to the issue and plan acceptance criteria.",
            "gotchas": [
                {
                    "text": "Docs-only epic/plan PRs should not bundle image-feedback or reporting feature files; keep unrelated feature work in separate issues/PRs to avoid targeted reverts.",
                },
            ],
            "validation": [
                "For docs PRs, list changed paths in the PR body and call out any non-doc files; non-doc additions need explicit issue scope.",
            ],
        },
        "stranske/learning-management-system": {
            "summary": "LMS is Postgres-oriented; migrations and tests should not drift to SQLite-only behavior.",
            "gotchas": [
                {
                    "text": "Use PostgreSQL-compatible migrations and SQL; avoid SQLite-only shortcuts or assumptions.",
                },
            ],
            "validation": [
                "When touching persistence, prefer the repo's Postgres-backed test path or explicitly state why only a narrower check was possible.",
            ],
        },
    },
}


def _reseed_section(live: list, seeded: list, *, section: str) -> list:
    """Return `live` with the SEED's copy of each seed-owned item reasserted.

    Seed-owned items are matched by identity -- text for the prose sections, `capability` for
    contraindications -- and an item that matches gets the SEED's SCOPE, which is how a seeded
    invariant loses a `task_types` list it should never have carried. Items the instance added are
    left exactly as they are; nothing is ever deleted.
    """
    def identity(item: object) -> str:
        if section == CONTRAINDICATION_SECTION:
            if not isinstance(item, dict):
                return ""
            return str(item.get("capability") or "").strip().lower()
        return _text(item).lower()

    out = list(live)
    index = {identity(item): pos for pos, item in enumerate(out) if identity(item)}
    for seed_item in seeded:
        key = identity(seed_item)
        if not key:
            continue
        fresh = json.loads(json.dumps(seed_item))
        if key in index:
            out[index[key]] = fresh
        else:
            out.append(fresh)
    return out


def _migrate(reg: dict) -> bool:
    """Bring an existing registry up to SEED_SCHEMA_VERSION. Returns True if anything changed.

    Needed because the SEED is only ever written when the registry file is ABSENT -- so a code-only
    fix to a wrong seeded line leaves every running instance still reading the wrong line. Additive
    and idempotent: it corrects seeded text by exact match, reasserts seeded items and their scope,
    and never removes an instance-added entry.
    """
    if int(reg.get("schema_version") or 1) >= SEED_SCHEMA_VERSION:
        return False
    repos = reg.setdefault("repos", {})
    for repo, seed_entry in SEED["repos"].items():
        entry = repos.setdefault(repo, {})
        replacements = SUPERSEDED_TEXT.get(repo, {})
        if str(entry.get("summary") or "") in replacements:
            entry["summary"] = replacements[entry["summary"]]
        elif not str(entry.get("summary") or "").strip() and seed_entry.get("summary"):
            # A repo entry created by approve_suggestion() starts with summary "". Filling a BLANK
            # is additive; a summary the instance actually wrote is left alone.
            entry["summary"] = seed_entry["summary"]
        for section in (*sorted(PLAYBOOK_SECTIONS), CONTRAINDICATION_SECTION):
            live = list(entry.get(section) or [])
            if section != CONTRAINDICATION_SECTION and replacements:
                for pos, item in enumerate(live):
                    text = _text(item)
                    if text in replacements:
                        if isinstance(item, dict):
                            item = dict(item)
                            item["text"] = replacements[text]
                            live[pos] = item
                        else:
                            live[pos] = replacements[text]
            live = _reseed_section(live, seed_entry.get(section) or [], section=section)
            if live:
                entry[section] = live
        if seed_entry.get("base_branch"):
            # Overwritten, not filled: the seeded value now comes from provision's override table,
            # which is what actually decides the base. A registry that disagreed with it would be
            # telling agents one branch while the dispatcher cut from another.
            entry["base_branch"] = seed_entry["base_branch"]
    reg["schema_version"] = SEED_SCHEMA_VERSION
    return True


def load(path: Path = REG) -> dict:
    if path.exists():
        reg = json.loads(path.read_text())
        if _migrate(reg):
            try:
                save(reg, path)
            except OSError:
                # FAIL TOWARD MOTION. The migration is applied in memory either way; a read-only or
                # full volume must not be able to stop every delegation prompt from being built.
                pass
        return reg
    path.parent.mkdir(parents=True, exist_ok=True)
    save(json.loads(json.dumps(SEED)), path)
    return json.loads(json.dumps(SEED))


def save(reg: dict, path: Path = REG) -> None:
    # Write-then-rename: load() now writes on READ (the migration), and append_context reaches
    # load() on every dispatch, so a torn file here would poison every prompt at once.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(reg, indent=2) + "\n")
    os.replace(tmp, path)


def contraindications_for(repo_or_target: str, *, path: Path | None = None) -> dict[str, dict]:
    """Capabilities this repo's own record says do not work here, keyed by capability id.

    Read by capability_advisor.advise() so a recommended capability and the playbook can disagree
    VISIBLY in one response, instead of the caller having to remember. It annotates; it never
    removes a candidate -- concealing one would deny it the evidence that could clear it.
    """
    # REG resolved at CALL time, not at def time. This one is reached from another module with no
    # path argument, so a `path: Path = REG` default would freeze the registry location at import
    # and silently ignore any later reassignment -- including the one a test makes.
    entry = (load(path or REG).get("repos") or {}).get(repo_for(repo_or_target)) or {}
    out: dict[str, dict] = {}
    for item in entry.get(CONTRAINDICATION_SECTION) or []:
        if not isinstance(item, dict):
            continue
        cap = str(item.get("capability") or "").strip()
        if cap:
            out[cap] = dict(item)
    return out


def repo_for(target_or_repo: str) -> str:
    return provision.parse_target(target_or_repo)[0]


def _repo_for_suggestion_target(target_or_repo: str) -> str:
    """Best-effort repo extraction for snapshot targets that may include experiment suffixes."""
    return repo_for(str(target_or_repo or "").split()[0])


def _applies(item: object, *, task_type: str | None, lane: str | None) -> bool:
    if not isinstance(item, dict):
        return True
    task_types = item.get("task_types")
    lanes = item.get("lanes")
    if task_types and task_type not in task_types:
        return False
    if lanes and lane not in lanes:
        return False
    return True


def _text(item: object) -> str:
    if isinstance(item, dict):
        return str(item.get("text") or "").strip()
    return str(item).strip()


def _matching_lines(items: list, *, task_type: str | None, lane: str | None) -> list[str]:
    lines = []
    for item in items or []:
        if _applies(item, task_type=task_type, lane=lane):
            text = _text(item)
            if text:
                lines.append(text)
    return lines


def _contraindication_lines(entry: dict) -> list[str]:
    """Render the typed contraindications. Never scoped: a tool that is broken here is broken here."""
    lines = []
    for item in entry.get(CONTRAINDICATION_SECTION) or []:
        if not isinstance(item, dict):
            continue
        cap = str(item.get("capability") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not cap or not reason:
            continue
        line = f"{cap}: {reason}"
        instead = str(item.get("instead") or "").strip()
        if instead:
            line += f" Instead: {instead}"
        lines.append(line)
    return lines


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = "\n- [truncated repo playbook; see experiments/repo_knowledge.json]\n"
    return text[: max(0, max_chars - len(marker))].rstrip() + marker


def context_for(target_or_repo: str, *, task_type: str | None = None, lane: str | None = None,
                path: Path = REG, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Return a concise prompt block for the target repo, or empty string if unknown."""
    repo = repo_for(target_or_repo)
    reg = load(path)
    entry = (reg.get("repos") or {}).get(repo)
    if not entry:
        return ""

    lines = [f"REPO PLAYBOOK ({repo}):"]
    summary = str(entry.get("summary") or "").strip()
    if summary:
        lines.append(f"- Summary: {summary}")
    # THE BASE BRANCH IS A REPO INVARIANT, NOT AN OPENER DETAIL. Gating it on lane=="opener" hid the
    # single most load-bearing fact about a repo from every review, audit and closer consult -- the
    # same defect as scoping an invariant gotcha by task_type. (2026-08-23)
    base = str(entry.get("base_branch") or "").strip()
    if base:
        lines.append(f"- Base branch: {base}")

    sections = [
        ("Definition of done", _matching_lines(entry.get("definition_of_done", []), task_type=task_type, lane=lane)),
        ("Known gotchas", _matching_lines(entry.get("gotchas", []), task_type=task_type, lane=lane)),
        ("Validation", _matching_lines(entry.get("validation", []), task_type=task_type, lane=lane)),
    ]
    for title, values in sections:
        if values:
            lines.append(f"- {title}:")
            lines.extend(f"  - {value}" for value in values)
    warned = _contraindication_lines(entry)
    if warned:
        lines.append("- Contraindicated capabilities (this repo's own record says these do not work here):")
        lines.extend(f"  - {value}" for value in warned)

    if len(lines) == 1:
        return ""
    return _truncate("\n".join(lines), max_chars)


def append_context(prompt: str, target_or_repo: str, *, task_type: str | None = None,
                   lane: str | None = None, path: Path = REG,
                   max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Append repo playbook context to a prompt unless no context exists or it is already present."""
    # Credit where the DRIVER actually enters this module. The heartbeat previously sat
    # only in main(), which no driver calls -- dispatcher/tick call this function
    # directly -- so the capability ran constantly and recorded nothing. (2026-08-20)
    _capability_heartbeat()
    # KILL SWITCH. This is the one per-event capability here with a real operational need for one:
    # the playbook is injected into EVERY delegation prompt, so a bad or poisoned rule reaches every
    # agent on the next dispatch. Editing the registry to remove a rule is the correct permanent
    # fix, but it is not a STOP -- you want injection off now and the diagnosis afterwards.
    # Deliberately placed AFTER the heartbeat: the capability was still matched and considered, and
    # recording that it was suppressed is more honest than making a disabled tick look like a tick
    # where no dispatch happened.
    if os.environ.get("ORCH_REPO_PLAYBOOK", "").strip() == "0":
        return prompt
    ctx = context_for(target_or_repo, task_type=task_type, lane=lane, path=path, max_chars=max_chars)
    if not ctx or "REPO PLAYBOOK (" in prompt:
        return prompt
    return f"{prompt.rstrip()}\n\n{ctx}"


def _scope_suffix(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    parts = []
    if item.get("task_types"):
        parts.append("tasks: " + ", ".join(str(x) for x in item["task_types"]))
    if item.get("lanes"):
        parts.append("lanes: " + ", ".join(str(x) for x in item["lanes"]))
    return f" ({'; '.join(parts)})" if parts else ""


def _export_section_lines(title: str, items: list, *, max_items: int = 8) -> list[str]:
    values = []
    for item in items or []:
        text = _text(item)
        if text:
            line = f"- {text}{_scope_suffix(item)}"
            if isinstance(item, dict) and item.get("rule_id") and item.get("content_hash"):
                digest = str(item["content_hash"]).split(":", 1)[-1][:16]
                line += f" <!-- {MANAGED_RULE_HASH_PREFIX}:{item['rule_id']}:{digest} -->"
            values.append(line)
    if not values:
        return []
    lines = ["", f"### {title}", ""]
    lines.extend(values[:max_items])
    if len(values) > max_items:
        lines.append(f"- [{len(values) - max_items} additional approved rule(s) omitted; see Orchestrator repo_knowledge.json]")
    return lines


def _limit_lines(lines: list[str], max_lines: int) -> list[str]:
    if max_lines <= 0 or len(lines) <= max_lines:
        return lines
    if max_lines < 4:
        return lines[:max_lines]
    return lines[: max_lines - 2] + [
        "- [truncated; see Orchestrator repo_knowledge.json]",
        lines[-1],
    ]


def export_agents_md(repo_or_target: str, *, path: Path = REG,
                     max_lines: int = AGENTS_EXPORT_MAX_LINES) -> str:
    """Return a small managed AGENTS.md block from the approved repo playbook registry.

    The block is intentionally export-only: unapproved suggestions never appear here, and existing
    repository AGENTS.md content is preserved by update_agents_md().
    """
    repo = repo_for(repo_or_target)
    entry = (load(path).get("repos") or {}).get(repo)
    if not entry:
        return ""

    lines = [
        AGENTS_EXPORT_START,
        AGENTS_EXPORT_NOTE,
        "",
        f"## Orchestrator Repo Playbook ({repo})",
        "",
    ]
    summary = str(entry.get("summary") or "").strip()
    if summary:
        lines.append(f"- Summary: {summary}")
    base = str(entry.get("base_branch") or "").strip()
    if base:
        lines.append(f"- Base branch: `{base}`")
    for title, key in (
        ("Definition Of Done", "definition_of_done"),
        ("Known Gotchas", "gotchas"),
        ("Validation", "validation"),
    ):
        lines.extend(_export_section_lines(title, entry.get(key, [])))
    warned = _contraindication_lines(entry)
    if warned:
        lines.extend(["", "### Contraindicated Capabilities", ""])
        lines.extend(f"- {value}" for value in warned)
    lines.extend(["", AGENTS_EXPORT_END])
    return "\n".join(_limit_lines(lines, max_lines)).rstrip() + "\n"


def _replace_managed_section(existing: str, managed_block: str) -> tuple[str, bool]:
    if not managed_block:
        return existing, False
    pattern = re.compile(
        rf"{re.escape(AGENTS_EXPORT_START)}.*?{re.escape(AGENTS_EXPORT_END)}\n?",
        re.DOTALL,
    )
    if pattern.search(existing):
        updated = pattern.sub(managed_block, existing, count=1)
    else:
        sep = "\n\n" if existing.strip() else ""
        updated = existing.rstrip() + sep + managed_block
    return updated, updated != existing


def update_agents_md(repo_path: Path | str, *, repo: str | None = None,
                     path: Path = REG, apply: bool = False,
                     max_lines: int = AGENTS_EXPORT_MAX_LINES) -> dict:
    """Preview or write the managed Orchestrator section in a repo's AGENTS.md."""
    root = Path(repo_path).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"repo path not found: {root}")
    repo_name = repo or _repo_from_git_remote(root)
    if not repo_name:
        raise ValueError("--repo is required when the repo path has no GitHub remote")
    block = export_agents_md(repo_name, path=path, max_lines=max_lines)
    if not block:
        return {
            "repo": repo_name,
            "path": str(root / "AGENTS.md"),
            "preview": not apply,
            "changed": False,
            "written": False,
            "reason": "no approved repo playbook entry",
        }
    agents_path = root / "AGENTS.md"
    existing = agents_path.read_text(encoding="utf-8") if agents_path.exists() else "# AGENTS.md\n"
    updated, changed = _replace_managed_section(existing, block)
    if apply and changed:
        agents_path.write_text(updated, encoding="utf-8")
    return {
        "repo": repo_name,
        "path": str(agents_path),
        "preview": not apply,
        "changed": changed,
        "written": bool(apply and changed),
        "line_count": len(block.splitlines()),
    }


def _managed_section(text: str) -> str | None:
    start = text.find(AGENTS_EXPORT_START)
    end = text.find(AGENTS_EXPORT_END)
    if start < 0 or end < 0 or end < start:
        return None
    end += len(AGENTS_EXPORT_END)
    return text[start:end].rstrip() + "\n"


def _path_refs(markdown: str) -> list[str]:
    refs = []
    for value in re.findall(r"`([^`]+)`", markdown):
        candidate = value.strip()
        if (
            "/" in candidate
            or candidate.startswith(".github")
            or Path(candidate).suffix in {".py", ".md", ".yml", ".yaml", ".toml", ".json", ".txt"}
        ):
            refs.append(candidate)
    return refs


def validate_agents_md_export(repo_path: Path | str, *, repo: str | None = None,
                              path: Path = REG,
                              max_lines: int = AGENTS_EXPORT_MAX_LINES) -> dict:
    """Validate the managed AGENTS.md section against the approved registry export."""
    root = Path(repo_path).expanduser().resolve()
    repo_name = repo or _repo_from_git_remote(root)
    if not repo_name:
        raise ValueError("--repo is required when the repo path has no GitHub remote")
    agents_path = root / "AGENTS.md"
    errors: list[str] = []
    warnings: list[str] = []
    registered = bool((load(path).get("repos") or {}).get(repo_name))
    if not agents_path.exists():
        errors.append("AGENTS.md not found")
        current = ""
    else:
        current = agents_path.read_text(encoding="utf-8")
    actual = _managed_section(current)
    expected = export_agents_md(repo_name, path=path, max_lines=max_lines)
    if expected and actual is None:
        errors.append("managed Orchestrator section missing")
    elif expected and actual != expected:
        warnings.append("managed Orchestrator section differs from current registry export")
    elif not expected and actual is not None:
        warnings.append("managed Orchestrator section exists but registry has no approved playbook entry")

    missing_refs: list[str] = []
    for ref in _path_refs(actual or expected or ""):
        # Branch names like `phase-3` are not path refs, but backticked docs/files should resolve.
        if ref.startswith("http://") or ref.startswith("https://"):
            continue
        if not (root / ref).exists():
            warnings.append(f"referenced path not found: {ref}")
            missing_refs.append(ref)
    entry = (load(path).get("repos") or {}).get(repo_name) or {}
    managed_refs = [
        ref
        for section in PLAYBOOK_SECTIONS
        for item in (entry.get(section, []) or [])
        if isinstance(item, dict) and item.get("rule_id")
        for ref in (item.get("current_refs") or [])
    ]
    if managed_refs:
        current_ref_status = validate_current_refs(root, managed_refs)
        for error in current_ref_status["errors"]:
            warnings.append(error)
            detail = error.split(": ", 1)[-1]
            if detail not in missing_refs:
                missing_refs.append(detail)
    if registered and actual is None:
        status = "absent"
    elif actual is not None and expected and actual != expected:
        status = "mismatched"
    elif actual is not None and not expected:
        status = "mismatched"
    elif missing_refs:
        status = "stale"
    elif actual is not None and expected and actual == expected:
        status = "current"
    else:
        status = "absent"
    return {
        "repo": repo_name,
        "path": str(agents_path),
        "ok": not errors,
        "current": status == "current",
        "status": status,
        "registered": registered,
        "missing_refs": missing_refs,
        "errors": errors,
        "warnings": warnings,
    }


def validate_current_refs(repo_path: Path | str, refs: list[dict]) -> dict:
    """Validate exact repo-relative path and optional symbol references."""
    root = Path(repo_path).expanduser().resolve()
    errors: list[str] = []
    normalized: list[dict] = []
    if not root.is_dir():
        return {"valid": False, "status": "stale", "refs": [], "errors": ["repo root not found"]}
    if not isinstance(refs, list) or not refs:
        return {"valid": False, "status": "stale", "refs": [], "errors": ["current refs are required"]}
    for index, item in enumerate(refs):
        if not isinstance(item, dict) or set(item) not in ({"path"}, {"path", "symbol"}):
            errors.append(f"current_refs[{index}] must contain path and optional symbol")
            continue
        rel = str(item.get("path") or "").strip()
        symbol = str(item.get("symbol") or "").strip() or None
        if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
            errors.append(f"unsafe current path: {rel}")
            continue
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            errors.append(f"unsafe current path: {rel}")
            continue
        if not target.is_file():
            errors.append(f"stale current path: {rel}")
            continue
        if symbol:
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                errors.append(f"unreadable current path: {rel}")
                continue
            symbol_pattern = (
                rf"\b{re.escape(symbol)}\b"
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol)
                else re.escape(symbol)
            )
            if not re.search(symbol_pattern, content):
                errors.append(f"stale current symbol: {rel}:{symbol}")
                continue
        normalized.append({"path": rel, **({"symbol": symbol} if symbol else {})})
    return {
        "valid": not errors and len(normalized) == len(refs),
        "status": "current" if not errors and len(normalized) == len(refs) else "stale",
        "refs": normalized,
        "errors": errors,
    }


def managed_rule_duplicate(
    repo: str, text: str, content_hash: str, *, path: Path = REG
) -> dict | None:
    """Return the exact/fuzzy existing rule that owns this meaning, if any."""
    entry = (load(path).get("repos") or {}).get(repo) or {}
    for section in sorted(PLAYBOOK_SECTIONS):
        for item in entry.get(section, []) or []:
            existing_text = _text(item)
            if not existing_text:
                continue
            if isinstance(item, dict) and item.get("content_hash") == content_hash:
                return {"section": section, "reason": "duplicate content hash", "item": item}
            if existing_text.lower() == text.lower() or _token_similarity(existing_text, text) >= FUZZY_DUPLICATE_THRESHOLD:
                return {"section": section, "reason": "duplicate playbook meaning", "item": item}
    return None


def install_managed_rule(
    manifest: dict, *, path: Path = REG, apply: bool = False
) -> dict:
    """Preview/apply one compiler-validated canary rule to the local registry."""
    repo = str(manifest["repo"])
    section = str(manifest["section"])
    if section not in PLAYBOOK_SECTIONS:
        raise ValueError("invalid managed rule section")
    duplicate = managed_rule_duplicate(repo, manifest["text"], manifest["content_hash"], path=path)
    result = {
        "repo": repo,
        "section": section,
        "rule_id": manifest["rule_id"],
        "preview": not apply,
        "applied": False,
        "already_present": bool(duplicate),
    }
    if duplicate or not apply:
        return result
    reg = load(path)
    entry = reg.setdefault("repos", {}).setdefault(
        repo,
        {"summary": "", "definition_of_done": [], "gotchas": [], "validation": []},
    )
    entry.setdefault(section, []).append(
        {
            "text": manifest["text"],
            "rule_id": manifest["rule_id"],
            "content_hash": manifest["content_hash"],
            "task_types": list(manifest["selector"]["task_types"]),
            "lanes": list(manifest["selector"]["lanes"]),
            "current_refs": list(manifest["current_refs"]),
            "state": "canary",
            "expires_at": manifest["lifecycle"]["expires_at"],
            "predecessor": manifest["lifecycle"]["predecessor"],
            "rollback": manifest["lifecycle"]["rollback"],
            "evidence_refs": list(manifest["evidence_refs"]),
        }
    )
    save(reg, path)
    result["applied"] = True
    return result


def remove_managed_rule(rule_id: str, *, repo: str, path: Path = REG, apply: bool = False) -> dict:
    """Preview/apply removal of exactly one managed rule; preserve every other entry."""
    reg = load(path)
    entry = (reg.get("repos") or {}).get(repo) or {}
    found = False
    for section in PLAYBOOK_SECTIONS:
        items = list(entry.get(section, []) or [])
        filtered = [
            item for item in items
            if not (isinstance(item, dict) and item.get("rule_id") == rule_id)
        ]
        if len(filtered) != len(items):
            found = True
            if apply:
                entry[section] = filtered
    if found and apply:
        save(reg, path)
    return {"repo": repo, "rule_id": rule_id, "found": found, "removed": bool(found and apply), "preview": not apply}


def update_capability_bundle(
    bundle_path: Path | str, manifest: dict, *, remove: bool = False, apply: bool = False
) -> dict:
    """Merge one portable playbook rule without replacing unrelated bundle content."""
    target = Path(bundle_path)
    existing = json.loads(target.read_text()) if target.exists() else {"schema_version": 1}
    if not isinstance(existing, dict):
        raise ValueError("capability bundle must be a JSON object")
    updated = json.loads(json.dumps(existing))
    rules = updated.setdefault("orchestrator_repo_playbook_rules", {})
    if not isinstance(rules, dict):
        raise ValueError("capability bundle playbook rules must be an object")
    if remove:
        rules.pop(manifest["rule_id"], None)
    else:
        rules[manifest["rule_id"]] = {
            "repo": manifest["repo"],
            "section": manifest["section"],
            "text": manifest["text"],
            "content_hash": manifest["content_hash"],
            "selector": manifest["selector"],
            "current_refs": manifest["current_refs"],
            "expires_at": manifest["lifecycle"]["expires_at"],
        }
    changed = updated != existing
    if apply and changed:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n")
    return {"path": str(target), "preview": not apply, "changed": changed, "written": bool(apply and changed)}


def _looks_invariant(text: str) -> str:
    """Name the invariant signal in this text, or "" if it reads as work-kind-specific."""
    lowered = (text or "").lower()
    for pattern in INVARIANT_SIGNAL_PATTERNS:
        match = re.search(pattern, lowered)
        if match:
            return match.group(0)
    return ""


def scope_audit(path: Path = REG) -> dict:
    """Report scoped playbook items, and which of them look like repo invariants.

    TWO NUMBERS IN ONE PLACE, deliberately. "12 scoped items" reads as normal housekeeping; "12
    scoped, 12 of them invariant" is instantly the defect -- an auditor consult being answered with
    silence because every gotcha was filed under someone else's task_type. Report-only: it never
    edits the registry.
    """
    reg = load(path)
    scoped_total = 0
    invariant: list[dict] = []
    for repo in sorted((reg.get("repos") or {}).keys()):
        entry = reg["repos"][repo] or {}
        for section in sorted(PLAYBOOK_SECTIONS):
            for item in entry.get(section) or []:
                if not isinstance(item, dict):
                    continue
                scope = {key: item[key] for key in ("task_types", "lanes") if item.get(key)}
                if not scope:
                    continue
                scoped_total += 1
                signal = _looks_invariant(_text(item))
                if signal:
                    invariant.append({
                        "repo": repo, "section": section, "text": _text(item),
                        "scope": scope, "invariant_signal": signal,
                        "fix": "drop task_types/lanes: this states a repo invariant, so a "
                               "review/audit consult must see it too",
                    })
    return {
        "scoped_items": scoped_total,
        "invariant_scoped": invariant,
        "invariant_scoped_count": len(invariant),
        "clean": not invariant,
    }


def _known_texts_for_repo(repo: str, path: Path = REG) -> set[str]:
    entry = (load(path).get("repos") or {}).get(repo) or {}
    texts: set[str] = set()
    for key in ("definition_of_done", "gotchas", "validation"):
        for item in entry.get(key, []) or []:
            text = _text(item).lower()
            if text:
                texts.add(text)
    return texts


def _note_has_signal(note: str) -> bool:
    return any(re.search(pattern, note, flags=re.IGNORECASE) for pattern in NOTE_SIGNAL_PATTERNS)


def _doc_has_signal(note: str) -> bool:
    return any(re.search(pattern, note, flags=re.IGNORECASE) for pattern in DOC_SIGNAL_PATTERNS)


def _clean_note(note: str, max_chars: int = 320) -> str:
    text = " ".join(str(note or "").split())
    return text if len(text) <= max_chars else text[: max_chars - 3].rstrip() + "..."


def _classify_candidate(text: str, *, failure_like: bool = False) -> str:
    lowered = text.lower()
    if failure_like or any(s in lowered for s in ("avoid", "do not", "don't", "never", "missing", "failed", "wrong", "gotcha", "pitfall")):
        return "gotchas"
    if any(s in lowered for s in ("must", "required", "always", "before merging", "definition of done", "checklist")):
        return "definition_of_done"
    if any(s in lowered for s in ("run ", "validate", "validation", "test", "pytest", "ruff", "black", "mypy", "lint", "format", "coverage", "ci")):
        return "validation"
    return "gotchas"


def _fingerprint_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.findall(r"[a-z0-9][a-z0-9_/-]*", text.lower()):
        parts = [raw]
        if any(sep in raw for sep in ("_", "/", "-")):
            parts.extend(part for part in re.split(r"[_/-]+", raw) if part)
        for part in parts:
            token = TOKEN_SYNONYMS.get(part, part)
            if len(token) < 2 or token in SUGGESTION_STOPWORDS:
                continue
            tokens.append(token)
    return tokens


def _token_similarity(left: str, right: str) -> float:
    left_tokens = set(_fingerprint_tokens(left))
    right_tokens = set(_fingerprint_tokens(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _cluster_key(text: str) -> str:
    tokens = _fingerprint_tokens(text)
    return "_".join(tokens[:5]) if tokens else "suggestion"


def _merge_field(target: dict, incoming: dict, *, field: str, list_field: str,
                 joiner: str) -> None:
    value = incoming.get(field)
    if value is None or value == "":
        return
    values = target.get(list_field)
    if not isinstance(values, list):
        values = []
        existing = target.get(field)
        if existing is not None and existing != "":
            values.append(str(existing))
    if str(value) not in values:
        values.append(str(value))
    if values:
        target[list_field] = values
        target[field] = joiner.join(values)


def _merge_suggestion(target: dict, incoming: dict, *, similarity: float) -> None:
    target["occurrence_count"] = int(target.get("occurrence_count") or 1) + 1
    target["max_similarity"] = round(max(float(target.get("max_similarity") or 0), similarity), 3)
    _merge_field(target, incoming, field="evidence", list_field="evidences", joiner="; ")
    _merge_field(target, incoming, field="source", list_field="sources", joiner=", ")
    _merge_field(target, incoming, field="author", list_field="authors", joiner=", ")
    for field, list_field in (
        ("run_id", "run_ids"),
        ("target", "targets"),
        ("task_type", "task_types"),
        ("agent", "agents"),
        ("verdict", "verdicts"),
        ("durability", "durabilities"),
    ):
        _merge_field(target, incoming, field=field, list_field=list_field, joiner="; ")


def _append_suggestion(suggestions: list[dict], seen: set[str], known: set[str], suggestion: dict,
                       *, max_per_repo: int) -> None:
    text = _clean_note(suggestion.get("candidate_text") or "", max_chars=600)
    if len(text) < 20:
        return
    key = text.lower()
    if key in seen:
        return
    if key in known or any(_token_similarity(text, item) >= FUZZY_DUPLICATE_THRESHOLD for item in known):
        return
    for existing in suggestions:
        similarity = _token_similarity(text, existing.get("candidate_text") or "")
        if similarity >= FUZZY_DUPLICATE_THRESHOLD:
            _merge_suggestion(existing, suggestion, similarity=similarity)
            seen.add(key)
            return
    if len(suggestions) >= max_per_repo:
        return
    suggestion["candidate_text"] = text
    suggestion.setdefault("cluster_key", _cluster_key(text))
    suggestion.setdefault("occurrence_count", 1)
    if suggestion.get("evidence") and "evidences" not in suggestion:
        suggestion["evidences"] = [suggestion["evidence"]]
    if suggestion.get("source") and "sources" not in suggestion:
        suggestion["sources"] = [suggestion["source"]]
    if suggestion.get("author") and "authors" not in suggestion:
        suggestion["authors"] = [suggestion["author"]]
    suggestions.append(suggestion)
    seen.add(key)


def _repo_from_git_remote(root: Path) -> str:
    res = subprocess.run(["git", "-C", str(root), "config", "--get", "remote.origin.url"],
                         capture_output=True, text=True, check=False)
    remote = (res.stdout or "").strip()
    if not remote:
        return ""
    match = re.search(r"github\.com[:/](?P<repo>[^/]+/[^/.]+)(?:\.git)?$", remote)
    return match.group("repo") if match else ""


def _clean_doc_line(line: str) -> str:
    text = line.strip()
    if not text or text.startswith(("```", "|")):
        return ""
    text = re.sub(r"^\s*(?:#{1,6}|[-*+>]|\d+[.)])\s*", "", text)
    text = text.replace("**", "").replace("__", "").strip("` *")
    return _clean_note(text, max_chars=600)


_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _doc_blocks(lines: list[str], *, sections: list[str] | None = None) -> list[tuple[int, str]]:
    """Yield (line_no, text) blocks: wrapped markdown bullets folded back into one candidate.

    Both halves exist because pointing the existing line-at-a-time scanner at an out-of-tree
    Code/Audits/<repo>/README.md produced unusable output:

      * a bullet wrapped over three lines became three candidates, two of them starting mid-sentence
        ("Config`). Rules diverge between them.");
      * every directive-like line in the file was a candidate, so round-history prose ("the Dropbox
        checkout was never touched") arrived as durable repo knowledge.

    `sections` restricts scanning to the body of a heading whose text contains one of the given
    phrases, case-insensitively -- e.g. "Standing notes for the next round". Sub-headings nested
    under a matched heading stay in scope; a sibling or shallower heading closes it. Fenced code is
    skipped outright. With `sections=None` the behaviour is the previous whole-file scan.
    """
    wanted = [str(phrase).strip().lower() for phrase in (sections or []) if str(phrase).strip()]
    in_scope = not wanted
    scope_depth: int | None = None
    in_fence = False
    blocks: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0

    def flush() -> None:
        nonlocal buf
        if buf:
            blocks.append((start, " ".join(buf)))
            buf = []

    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if stripped.startswith("```"):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading = _HEADING_RE.match(raw)
        if heading:
            flush()
            depth = len(heading.group(1))
            title = heading.group(2).replace("*", "").replace("`", "").strip().lower()
            if not wanted:
                in_scope = True
            elif any(phrase in title for phrase in wanted):
                in_scope, scope_depth = True, depth
            elif scope_depth is not None and depth > scope_depth:
                pass                                   # a sub-heading inside the matched section
            else:
                in_scope, scope_depth = False, None
            continue
        if not in_scope:
            continue
        if not stripped or stripped.startswith(("|", ">")):
            flush()
            continue
        if _BULLET_RE.match(raw):
            flush()
            start = lineno
            buf = [stripped]
            continue
        if not buf:
            start = lineno
        buf.append(stripped)
    flush()
    return blocks


def _iter_doc_files(root: Path, *, max_files: int = 100, include_root_docs: bool = False) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for name in sorted(DOC_ROOT_FILES):
        path = root / name
        if path.is_file():
            seen.add(path)
            out.append(path)
    if include_root_docs:
        for path in sorted(root.iterdir()):
            if len(out) >= max_files:
                return out
            if path.name in DOC_SKIP_FILE_NAMES or path.name.startswith(DOC_SKIP_FILE_PREFIXES):
                continue
            if path in seen or not path.is_file() or path.suffix.lower() not in DOC_EXTENSIONS:
                continue
            seen.add(path)
            out.append(path)
    for dirname in sorted(DOC_DIRS):
        base = root / dirname
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if len(out) >= max_files:
                return out
            if any(part in DOC_SKIP_DIRS for part in path.relative_to(root).parts):
                continue
            if path.name in DOC_SKIP_FILE_NAMES or path.name.startswith(DOC_SKIP_FILE_PREFIXES):
                continue
            if path.is_file() and path.suffix.lower() in DOC_EXTENSIONS:
                out.append(path)
    return out[:max_files]


def suggest_from_docs(repo_path: Path | str, *, repo: str | None = None, path: Path = REG,
                      max_per_repo: int = 10, include_root_docs: bool = False,
                      sections: list[str] | None = None) -> list[dict]:
    """Suggest playbook candidates by scanning repo docs.

    This is read-only and intentionally conservative: it looks for directive-like
    doc lines and returns a review queue in the same shape used by
    approve_suggestion().

    `repo_path` is any directory -- it does NOT have to be a checkout of `repo`, which is what lets
    an out-of-tree notes directory (an audit folder, say) be mined for the repo it describes; pass
    `--repo` explicitly there, since such a directory has no git remote to infer one from. Pair it
    with `sections=["Standing notes for the next round"]` so round-history prose is not mistaken for
    durable repo knowledge. Naming a section also relaxes the DOC_SIGNAL_PATTERNS prose filter, for
    the reason given at the call site. Output is still a REVIEW QUEUE: the notes are a candidate source, not
    ground truth -- this repo's own audit notes carried a stale "CI gates ruff check only" line that
    was already false -- so verify a candidate against live code before approving it.
    """
    root = Path(repo_path).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"repo docs root not found: {root}")
    repo_name = repo or _repo_from_git_remote(root)
    if not repo_name:
        raise ValueError("--repo is required when the docs root has no GitHub remote")
    known = _known_texts_for_repo(repo_name, path)
    seen: set[str] = set()
    suggestions: list[dict] = []
    for doc in _iter_doc_files(root, include_root_docs=include_root_docs):
        rel = str(doc.relative_to(root))
        try:
            lines = doc.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for lineno, block in _doc_blocks(lines, sections=sections):
            text = _clean_doc_line(block)
            if not text:
                continue
            # WHEN A SECTION IS NAMED, THE SECTION IS THE SIGNAL. DOC_SIGNAL_PATTERNS exists to pick
            # guidance out of unmarked prose, and it is the right filter there -- but it keys on
            # must/should/never/always, and the highest-value note in a real audit README carries
            # none of them ("`frontend_verify.py` is unreliable against this SPA ...; drive with a
            # real browser instead"). Dropping it would leave exactly the gotcha the ingestion was
            # built for on the floor. A caller who names a heading has already asserted the content
            # is durable guidance, and the output is still a review queue, not a write.
            if sections is None and not _doc_has_signal(text):
                continue
            _append_suggestion(suggestions, seen, known, {
                "repo": repo_name,
                "suggested_section": _classify_candidate(text),
                "candidate_text": text,
                "source": "repo-docs",
                "evidence": f"{rel}:{lineno}",
            }, max_per_repo=max_per_repo)
            if len(suggestions) >= max_per_repo:
                return suggestions
    return suggestions


def _extract_comment_records(payload: object) -> list[dict]:
    records: list[dict] = []

    def walk(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        body = value.get("body")
        if isinstance(body, str) and body.strip():
            records.append({
                "body": body,
                "path": value.get("path") or value.get("file") or "",
                "url": value.get("html_url") or value.get("url") or "",
                "author": ((value.get("user") or {}).get("login")
                           if isinstance(value.get("user"), dict) else value.get("author")),
            })
        for child in value.values():
            if isinstance(child, (list, dict)):
                walk(child)

    walk(payload)
    return records


def suggest_from_review_payload(payload: object, *, repo: str, path: Path = REG,
                                source: str = "review-comments",
                                max_per_repo: int = 10) -> list[dict]:
    """Suggest playbook candidates from GitHub/reviewer comment payloads."""
    known = _known_texts_for_repo(repo, path)
    seen: set[str] = set()
    suggestions: list[dict] = []
    for rec in _extract_comment_records(payload):
        text = _clean_note(rec.get("body") or "", max_chars=600)
        if not _note_has_signal(text):
            continue
        evidence_bits = [str(part) for part in (rec.get("path"), rec.get("url")) if part]
        _append_suggestion(suggestions, seen, known, {
            "repo": repo,
            "suggested_section": _classify_candidate(text, failure_like=True),
            "candidate_text": text,
            "source": source,
            "evidence": " ".join(evidence_bits) if evidence_bits else None,
            "author": rec.get("author"),
        }, max_per_repo=max_per_repo)
        if len(suggestions) >= max_per_repo:
            return suggestions
    return suggestions


def suggest_from_review_json(review_json: Path | str, *, repo: str, path: Path = REG,
                             max_per_repo: int = 10) -> list[dict]:
    return suggest_from_review_payload(
        json.loads(Path(review_json).read_text()),
        repo=repo,
        path=path,
        source="review-comments.json",
        max_per_repo=max_per_repo,
    )


def suggest_from_pr_comments(target: str, *, path: Path = REG, max_per_repo: int = 10,
                             runner=subprocess.run) -> list[dict]:
    """Suggest playbook candidates from live GitHub issue, review, and PR comments."""
    repo, number = provision.parse_target(target)
    if number is None:
        raise ValueError("--suggest-from-pr requires an owner/repo#N target")
    payloads: list[object] = []
    errors: list[str] = []
    for endpoint in (
        f"repos/{repo}/issues/{number}/comments",
        f"repos/{repo}/pulls/{number}/comments",
        f"repos/{repo}/pulls/{number}/reviews",
    ):
        try:
            res = runner(["gh", "api", endpoint], capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise RuntimeError("--suggest-from-pr requires the gh CLI on PATH") from exc
        if res.returncode != 0:
            errors.append(f"{endpoint}: {(res.stderr or '').strip() or 'gh api failed'}")
            continue
        if not (res.stdout or "").strip():
            continue
        try:
            payloads.append(json.loads(res.stdout))
        except json.JSONDecodeError:
            continue
    if not payloads and errors:
        raise RuntimeError(
            "--suggest-from-pr could not read GitHub comments; ensure gh is installed and authenticated: "
            + "; ".join(errors[:2])
        )
    return suggest_from_review_payload(
        payloads,
        repo=repo,
        path=path,
        source="github-pr-comments",
        max_per_repo=max_per_repo,
    )


def suggest_from_snapshot(snapshot_path: Path | str, *, path: Path = REG,
                          max_per_repo: int = 5) -> list[dict]:
    """Suggest repo-playbook candidates from retained outcome notes.

    This is a review queue, not an auto-writer. It mines real failures/durability
    reversals for repo-specific rules that a human or orchestrator can promote into
    experiments/repo_knowledge.json after checking the underlying run.
    """
    snapshot = json.loads(Path(snapshot_path).read_text())
    runs = {row.get("run_id"): row for row in snapshot.get("runs", []) if row.get("run_id")}
    repo_order: list[str] = []
    per_repo_suggestions: dict[str, list[dict]] = {}
    per_repo_seen: dict[str, set[str]] = {}
    for outcome in snapshot.get("outcomes", []) or []:
        run = runs.get(outcome.get("run_id"))
        if not run:
            continue
        target = run.get("target") or ""
        try:
            repo = _repo_for_suggestion_target(target)
        except Exception:
            continue
        verdict = str(outcome.get("adjudicated_verdict") or "").upper()
        durability = str(outcome.get("durability") or "").lower()
        notes = _clean_note(outcome.get("notes") or "")
        if not notes:
            continue
        failure_like = verdict == "FAIL" or durability in FAILURE_DURABILITIES
        if not failure_like and not _note_has_signal(notes):
            continue
        known = _known_texts_for_repo(repo, path)
        if repo not in per_repo_suggestions:
            repo_order.append(repo)
            per_repo_suggestions[repo] = []
            per_repo_seen[repo] = set()
        _append_suggestion(per_repo_suggestions[repo], per_repo_seen[repo], known, {
            "repo": repo,
            "run_id": outcome.get("run_id"),
            "target": target,
            "task_type": run.get("task_type"),
            "agent": run.get("agent"),
            "verdict": verdict or None,
            "durability": durability or None,
            "suggested_section": "gotchas" if failure_like else "validation",
            "candidate_text": notes,
            "source": "feedback-snapshot.outcomes.notes",
        }, max_per_repo=max_per_repo)
    suggestions: list[dict] = []
    for repo in repo_order:
        suggestions.extend(per_repo_suggestions[repo])
    return suggestions


def _memory_query_tokens(query: str | None, *, task_type: str | None = None,
                         lane: str | None = None) -> set[str]:
    parts = [query or ""]
    if task_type:
        parts.append(task_type)
    if lane:
        parts.append(lane)
    return set(_fingerprint_tokens(" ".join(parts)))


def _memory_match(tokens: set[str], text: str, *, base: float = 0.0,
                  idf: dict[str, float] | None = None) -> dict:
    if not text:
        return {"score": 0.0, "matched_terms": [], "coverage": 0.0}
    if not tokens:
        return {"score": base, "matched_terms": [], "coverage": 0.0}
    text_tokens = set(_fingerprint_tokens(text))
    if not text_tokens:
        return {"score": 0.0, "matched_terms": [], "coverage": 0.0}
    matched = sorted(tokens & text_tokens)
    overlap = len(matched)
    if overlap == 0:
        return {"score": 0.0, "matched_terms": [], "coverage": 0.0}
    weights = idf or {}
    total_weight = sum(weights.get(token, 1.0) for token in tokens)
    matched_weight = sum(weights.get(token, 1.0) for token in matched)
    coverage = matched_weight / max(1.0, total_weight)
    density = overlap / max(1, len(text_tokens))
    return {
        "score": base + coverage + (0.25 * density),
        "matched_terms": matched,
        "coverage": coverage,
    }


def _score_memory_text(tokens: set[str], text: str, *, base: float = 0.0) -> float:
    return float(_memory_match(tokens, text, base=base)["score"])


def _memory_haystack(record: dict) -> str:
    return " ".join(str(record.get(k) or "") for k in (
        "text", "scope", "section", "target", "task_type", "agent", "verdict", "durability"
    ))


def _memory_idf(records: list[dict]) -> dict[str, float]:
    """Small local IDF map for the current repo-memory candidate set.

    This keeps retrieval deterministic and dependency-free while making repo-specific terms outrank generic
    words such as "test" or "change".
    """
    n_docs = max(1, len(records))
    doc_freq: dict[str, int] = {}
    for record in records:
        for token in set(_fingerprint_tokens(_memory_haystack(record))):
            doc_freq[token] = doc_freq.get(token, 0) + 1
    return {
        token: 1.0 + math.log((1.0 + n_docs) / (1.0 + freq))
        for token, freq in doc_freq.items()
    }


def _memory_base(record: dict) -> float:
    section = str(record.get("section") or "")
    base = MEMORY_SECTION_BASE.get(section, 0.05)
    if record.get("source") == "approved-playbook":
        return max(base, 0.12)
    return base


def _memory_item_visible(item: object, *, task_type: str | None, lane: str | None) -> bool:
    """Search keeps scoped rules visible when the caller omitted that scope.

    Prompt injection stays stricter through _applies(); retrieval is a review surface, so it keeps recall
    high and lets query scoring decide which scoped entries are relevant.
    """
    if not isinstance(item, dict):
        return True
    task_types = item.get("task_types")
    lanes = item.get("lanes")
    if task_type and task_types and task_type not in task_types:
        return False
    if lane and lanes and lane not in lanes:
        return False
    return True


def _playbook_memory_records(repo: str, *, task_type: str | None, lane: str | None,
                             path: Path = REG) -> list[dict]:
    entry = (load(path).get("repos") or {}).get(repo) or {}
    records: list[dict] = []
    summary = str(entry.get("summary") or "").strip()
    if summary:
        records.append({
            "repo": repo,
            "source": "approved-playbook",
            "section": "summary",
            "text": summary,
            "evidence": "repo_knowledge.json:summary",
        })
    base = str(entry.get("base_branch") or "").strip()
    if base:
        records.append({
            "repo": repo,
            "source": "approved-playbook",
            "section": "base_branch",
            "text": f"Base branch for this repo: {base}.",
            "evidence": "repo_knowledge.json:base_branch",
        })
    for item in entry.get(CONTRAINDICATION_SECTION) or []:
        if not isinstance(item, dict) or not item.get("capability"):
            continue
        records.append({
            "repo": repo,
            "source": "approved-playbook",
            "section": CONTRAINDICATION_SECTION,
            "text": f"{item['capability']} is contraindicated here: {item.get('reason') or ''}".strip(),
            "evidence": str(item.get("evidence") or f"repo_knowledge.json:{CONTRAINDICATION_SECTION}"),
        })
    for section in ("definition_of_done", "gotchas", "validation"):
        for item in entry.get(section, []) or []:
            if not _memory_item_visible(item, task_type=task_type, lane=lane):
                continue
            text = _text(item)
            if text:
                scope = _scope_suffix(item)
                records.append({
                    "repo": repo,
                    "source": "approved-playbook",
                    "section": section,
                    "text": text,
                    "scope": scope.strip(" ()"),
                    "evidence": f"repo_knowledge.json:{section}",
                })
    return records


def _run_memory_from_snapshot(snapshot: dict, repo: str) -> list[dict]:
    runs = {row.get("run_id"): row for row in snapshot.get("runs", []) or [] if row.get("run_id")}
    records: list[dict] = []
    for outcome in snapshot.get("outcomes", []) or []:
        run = runs.get(outcome.get("run_id"))
        if not run:
            continue
        target = run.get("target") or ""
        try:
            if _repo_for_suggestion_target(target) != repo:
                continue
        except Exception:
            continue
        notes = _clean_note(outcome.get("notes") or "", max_chars=600)
        if not notes:
            continue
        records.append({
            "repo": repo,
            "source": "feedback-outcome",
            "section": "outcome_notes",
            "text": notes,
            "evidence": outcome.get("run_id"),
            "run_id": outcome.get("run_id"),
            "target": target,
            "task_type": run.get("task_type"),
            "agent": run.get("agent"),
            "verdict": outcome.get("adjudicated_verdict") or outcome.get("verifier_verdict"),
            "durability": outcome.get("durability"),
        })
    return records


def _run_memory_from_feedback(repo: str, *, limit: int = 200) -> list[dict]:
    records: list[dict] = []
    try:
        with feedback._conn() as conn:
            rows = conn.execute(
                "SELECT r.run_id, r.target, r.task_type, r.agent, "
                "o.verifier_verdict, o.adjudicated_verdict, o.durability, o.notes "
                "FROM runs r JOIN outcomes o ON o.run_id=r.run_id "
                "WHERE o.notes IS NOT NULL AND TRIM(o.notes) != '' "
                "ORDER BY COALESCE(o.durability_checked_ts, r.ts) DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except Exception:
        return records
    for row in rows:
        run_id, target, task_type, agent, verifier, adjudicated, durability, notes = row
        try:
            if _repo_for_suggestion_target(target or "") != repo:
                continue
        except Exception:
            continue
        records.append({
            "repo": repo,
            "source": "feedback-outcome",
            "section": "outcome_notes",
            "text": _clean_note(notes, max_chars=600),
            "evidence": run_id,
            "run_id": run_id,
            "target": target,
            "task_type": task_type,
            "agent": agent,
            "verdict": adjudicated or verifier,
            "durability": durability,
        })
    return records


def _memory_scope_match(record: dict, *, task_type: str | None, lane: str | None) -> bool:
    if task_type and record.get("task_type") and record.get("task_type") != task_type:
        return False
    if lane and record.get("lane") and record.get("lane") != lane:
        return False
    return True


def search_repo_memory(repo_or_target: str, *, query: str | None = None,
                       task_type: str | None = None, lane: str | None = None,
                       path: Path = REG, snapshot_path: Path | str | None = None,
                       snapshot: dict | None = None, include_runs: bool = True,
                       max_results: int = 8) -> list[dict]:
    """Search approved playbook rules plus retained run/outcome notes for one repo.

    This is read-only retrieval. It never promotes suggestions or injects unapproved text into prompts.
    """
    repo = repo_for(repo_or_target)
    tokens = _memory_query_tokens(query, task_type=task_type, lane=lane)
    records = _playbook_memory_records(repo, task_type=task_type, lane=lane, path=path)
    if include_runs:
        if snapshot is None and snapshot_path is not None:
            snapshot = json.loads(Path(snapshot_path).read_text())
        records.extend(_run_memory_from_snapshot(snapshot, repo) if snapshot is not None
                       else _run_memory_from_feedback(repo))
    idf = _memory_idf(records)
    scored: list[dict] = []
    for record in records:
        if not _memory_scope_match(record, task_type=task_type, lane=lane):
            continue
        match = _memory_match(tokens, _memory_haystack(record), base=_memory_base(record), idf=idf)
        score = float(match["score"])
        if tokens and score <= 0:
            continue
        enriched = dict(record)
        enriched["score"] = round(score, 4)
        if match["matched_terms"]:
            enriched["matched_terms"] = match["matched_terms"]
            enriched["coverage"] = round(float(match["coverage"]), 4)
        scored.append(enriched)
    scored.sort(key=lambda item: (
        -float(item.get("score") or 0),
        0 if item.get("source") == "approved-playbook" else 1,
        str(item.get("section") or ""),
        str(item.get("text") or ""),
    ))
    return scored[:max(0, max_results)]


def approve_suggestion(suggestion: dict, *, section: str | None = None,
                       path: Path = REG, apply: bool = False) -> dict:
    """Preview or apply one suggested playbook entry.

    Suggestions are intentionally not auto-applied when mined from outcomes. This
    helper is the explicit approval seam: preview by default; with apply=True, add
    the candidate text to the selected repo playbook section if it is not already
    present.
    """
    repo = str(suggestion.get("repo") or "").strip()
    text = _clean_note(suggestion.get("candidate_text") or "", max_chars=600)
    chosen = section or suggestion.get("suggested_section") or "gotchas"
    if not repo:
        raise ValueError("suggestion repo is required")
    if not text:
        raise ValueError("suggestion candidate_text is required")
    if chosen not in PLAYBOOK_SECTIONS:
        raise ValueError(f"section must be one of {sorted(PLAYBOOK_SECTIONS)}")

    reg = load(path)
    repos = reg.setdefault("repos", {})
    entry = repos.setdefault(repo, {"summary": "", "definition_of_done": [], "gotchas": [], "validation": []})
    known = {_text(item).lower() for item in entry.get(chosen, []) or []}
    already = text.lower() in known
    result = {
        "repo": repo,
        "section": chosen,
        "candidate_text": text,
        "source_run_id": suggestion.get("run_id"),
        "preview": not apply,
        "applied": False,
        "already_present": already,
    }
    if apply and not already:
        item = {"text": text}
        if suggestion.get("task_type"):
            item["task_types"] = [suggestion["task_type"]]
        entry.setdefault(chosen, []).append(item)
        save(reg, path)
        result["applied"] = True
    return result


def _load_suggestion_file(path: Path, index: int) -> dict:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError("suggestion file must contain a JSON list")
    if index < 0 or index >= len(data):
        raise IndexError(f"suggestion index {index} out of range")
    return data[index]


def _selftest() -> None:
    p = Path("/tmp/__repo_knowledge_selftest.json")
    p.unlink(missing_ok=True)
    try:
        reg = load(p)
        assert reg["schema_version"] == SEED_SCHEMA_VERSION
        assert repo_for("stranske/Trend_Model_Project#123") == "stranske/Trend_Model_Project"

        # Every migration replacement must be a string the SEED actually seeds, or a migrated
        # instance and a fresh one would hold different words for the same rule.
        for _repo, _pairs in SUPERSEDED_TEXT.items():
            _entry = SEED["repos"][_repo]
            _seeded = {str(_entry.get("summary") or "")}
            for _section in PLAYBOOK_SECTIONS:
                _seeded.update(_text(item) for item in _entry.get(_section) or [])
            for _old, _new in _pairs.items():
                assert _new in _seeded, (_repo, _old)
                assert _old not in _seeded, (_repo, _old, "superseded text is still seeded")

        # ONE CONSTANT: the registry's base branch is provision's, not a second literal beside it.
        for repo, base in provision.BASE_BRANCH_OVERRIDES.items():
            seeded = (SEED["repos"].get(repo) or {}).get("base_branch")
            assert seeded in (None, base), (repo, seeded, base)
        # ...and no seeded base_branch may name a repo provision does not override, which is how the
        # two would drift apart in the other direction.
        for repo, entry in SEED["repos"].items():
            if entry.get("base_branch"):
                assert repo in provision.BASE_BRANCH_OVERRIDES, repo

        ctx = context_for("stranske/Trend_Model_Project#123", task_type="implement", lane="opener", path=p)
        assert "REPO PLAYBOOK (stranske/Trend_Model_Project)" in ctx, ctx
        assert "Base branch: phase-3" in ctx, ctx
        assert "ruff check" in ctx, ctx
        # The summary must not claim phase-3 is something other than the default branch: it IS the
        # default branch, and the old wording sent a reader looking for a `main` that is not there.
        summary = SEED["repos"]["stranske/Trend_Model_Project"]["summary"]
        assert "IS the default branch" in summary, summary
        assert "not the default branch" not in summary, summary

        # A GOTCHA AN AUDITOR CANNOT SEE IS A GOTCHA THAT DOES NOT EXIST. Every seeded invariant must
        # reach a review/audit/unclassified consult, not only the task types that happen to be listed.
        for consult in (None, "review", "audit", "ux"):
            seen = context_for("stranske/Trend_Model_Project#123", task_type=consult, path=p)
            assert "Base branch: phase-3" in seen, (consult, seen)
            assert "ruff check" in seen, (consult, seen)
            assert "frontend_verify.py" in seen, (consult, seen)
            assert "stlite/Pyodide" in seen, (consult, seen)
        review_ctx = context_for("stranske/Trend_Model_Project#123", task_type="review", path=p)
        impl_ctx = context_for("stranske/Trend_Model_Project#123", task_type="implement", path=p)
        assert len(review_ctx) == len(impl_ctx), (len(review_ctx), len(impl_ctx))

        # The contraindication is rendered, and it names both the reason and the alternative.
        assert "Contraindicated capabilities" in review_ctx, review_ctx
        assert "frontend-verifier:" in review_ctx, review_ctx
        assert "Instead:" in review_ctx, review_ctx
        warned = contraindications_for("stranske/Trend_Model_Project#5", path=p)
        assert set(warned) == {"frontend-verifier"}, warned
        assert warned["frontend-verifier"]["evidence"], warned
        assert contraindications_for("stranske/Counter_Risk", path=p) == {}

        # DELIBERATE BREAK -> REVERT: re-scoping a seeded invariant must make the audit go red and
        # must hide it from the auditor again; restoring the scope-free item must clear both.
        broken = load(p)
        broken["repos"]["stranske/Trend_Model_Project"]["gotchas"][1]["task_types"] = ["implement"]
        save(broken, p)
        audit_red = scope_audit(path=p)
        assert not audit_red["clean"] and audit_red["invariant_scoped_count"] == 1, audit_red
        assert audit_red["invariant_scoped"][0]["invariant_signal"] in {"black", "ruff"}, audit_red
        assert "ruff check" not in context_for("stranske/Trend_Model_Project#1", task_type="review", path=p)
        broken["repos"]["stranske/Trend_Model_Project"]["gotchas"][1].pop("task_types")
        save(broken, p)
        assert scope_audit(path=p)["clean"], scope_audit(path=p)
        assert "ruff check" in context_for("stranske/Trend_Model_Project#1", task_type="review", path=p)

        closer_ctx = context_for("stranske/Trend_Model_Project#123", task_type="implement", lane="closer", path=p)
        assert "Base branch: phase-3" in closer_ctx, closer_ctx

        lms_ctx = context_for("stranske/learning-management-system#7", task_type="mechanical", lane="opener", path=p)
        assert "PostgreSQL-compatible" in lms_ctx, lms_ctx
        lms_impl = context_for("stranske/learning-management-system#7", task_type="implement", lane="opener", path=p)
        assert "PostgreSQL-compatible" in lms_impl, lms_impl
        # Scoping still WORKS -- it is only wrong on invariants. An instance-added, work-kind-specific
        # rule must still be filtered, or the fix above would have removed the feature, not the misuse.
        scoped_reg = load(p)
        scoped_reg["repos"]["stranske/learning-management-system"]["validation"] = [
            {"text": "Name the seeded fixture the issue asks for.", "task_types": ["testgen"]},
        ]
        save(scoped_reg, p)
        lms = "stranske/learning-management-system#7"
        assert "seeded fixture" in context_for(lms, task_type="testgen", path=p)
        assert "seeded fixture" not in context_for(lms, task_type="review", path=p)
        assert scope_audit(path=p)["scoped_items"] == 1, scope_audit(path=p)
        assert scope_audit(path=p)["clean"], scope_audit(path=p)
        scoped_reg["repos"]["stranske/learning-management-system"].pop("validation")
        save(scoped_reg, p)

        # MIGRATION: a v1 registry carrying the wrong seeded lines is corrected in place, keeps every
        # instance-added entry, and is idempotent.
        legacy = Path("/tmp/__repo_knowledge_selftest_v1.json")
        legacy.unlink(missing_ok=True)
        try:
            legacy.write_text(json.dumps({"schema_version": 1, "repos": {
                "stranske/Trend_Model_Project": {
                    "summary": "Trend opener work cuts from phase-3, not the default branch.",
                    "base_branch": "phase-3",
                    "gotchas": [
                        {"text": "Use phase-3 as the base for opener work; do not assume main.",
                         "lanes": ["opener"]},
                        {"text": "CI convention is ruff check; do not introduce unrelated ruff format churn.",
                         "task_types": ["mechanical", "implement", "testgen"]},
                        {"text": "An instance rule nobody seeded.", "task_types": ["docs"]},
                    ],
                },
            }}, indent=2) + "\n")
            migrated = load(legacy)
            assert migrated["schema_version"] == SEED_SCHEMA_VERSION, migrated["schema_version"]
            trend = migrated["repos"]["stranske/Trend_Model_Project"]
            assert "IS the default branch" in trend["summary"], trend["summary"]
            texts = [_text(item) for item in trend["gotchas"]]
            assert any("there is no `main`" in t for t in texts), texts
            assert any("black --check --line-length 100" in t for t in texts), texts
            assert "An instance rule nobody seeded." in texts, texts
            kept = [i for i in trend["gotchas"] if _text(i) == "An instance rule nobody seeded."][0]
            assert kept["task_types"] == ["docs"], kept          # instance scope survives untouched
            assert not any(item.get("task_types") or item.get("lanes")
                           for item in trend["gotchas"] if _text(item) != "An instance rule nobody seeded.")
            assert trend[CONTRAINDICATION_SECTION][0]["capability"] == "frontend-verifier", trend
            assert scope_audit(path=legacy)["clean"], scope_audit(path=legacy)
            before = legacy.read_text()
            load(legacy)
            assert legacy.read_text() == before, "migration must be idempotent"
        finally:
            legacy.unlink(missing_ok=True)

        assert context_for("stranske/Unknown#1", path=p) == ""
        appended = append_context("Do the work.", "stranske/Counter_Risk#5", task_type="implement", lane="closer", path=p)
        assert appended.count("REPO PLAYBOOK") == 1 and "Black" in appended, appended
        assert append_context(appended, "stranske/Counter_Risk#5", task_type="implement", lane="closer", path=p) == appended

        reg["repos"]["o/long"] = {"summary": "x" * 200, "gotchas": ["y" * 200]}
        save(reg, p)
        short = context_for("o/long#1", path=p, max_chars=90)
        assert "truncated repo playbook" in short and len(short) <= 120, short

        export = export_agents_md("stranske/Counter_Risk#5", path=p)
        assert AGENTS_EXPORT_START in export and AGENTS_EXPORT_END in export, export
        assert "freshness owner: keepalive" in export, export
        assert "Black" in export, export
        assert len(export.splitlines()) <= AGENTS_EXPORT_MAX_LINES, export
        trend_export = export_agents_md("stranske/Trend_Model_Project", path=p)
        assert "Base branch: `phase-3`" in trend_export, trend_export
        assert "### Contraindicated Capabilities" in trend_export, trend_export
        assert "frontend-verifier:" in trend_export, trend_export
        assert export_agents_md("stranske/Unknown", path=p) == ""
        tiny_export = export_agents_md("o/long", path=p, max_lines=5)
        assert AGENTS_EXPORT_START in tiny_export and AGENTS_EXPORT_END in tiny_export, tiny_export
        assert "truncated" in tiny_export, tiny_export

        export_root = Path("/tmp/__repo_knowledge_export_repo")
        export_root.mkdir(exist_ok=True)
        (export_root / "docs").mkdir(exist_ok=True)
        (export_root / "docs" / "guide.md").write_text("ok\n")
        try:
            reg = load(p)
            reg["repos"]["o/pathy"] = {
                "summary": "Path validation repo.",
                "definition_of_done": [
                    "Update `docs/guide.md` before merging.",
                    "Update `docs/missing.md` before merging.",
                ],
            }
            save(reg, p)
            preview_export = update_agents_md(export_root, repo="o/pathy", path=p)
            assert preview_export["changed"] and preview_export["preview"], preview_export
            assert not (export_root / "AGENTS.md").exists(), preview_export
            applied_export = update_agents_md(export_root, repo="o/pathy", path=p, apply=True)
            assert applied_export["written"], applied_export
            again_export = update_agents_md(export_root, repo="o/pathy", path=p, apply=True)
            assert not again_export["changed"] and not again_export["written"], again_export
            agents_text = (export_root / "AGENTS.md").read_text()
            assert agents_text.count(AGENTS_EXPORT_START) == 1, agents_text
            validation = validate_agents_md_export(export_root, repo="o/pathy", path=p)
            assert validation["ok"] and not validation["current"], validation
            assert validation["status"] == "stale", validation
            assert any("docs/missing.md" in item for item in validation["warnings"]), validation
            refs = validate_current_refs(
                export_root,
                [{"path": "docs/guide.md", "symbol": "ok"}],
            )
            assert refs["valid"] and refs["status"] == "current", refs
            missing_text = re.sub(
                rf"{re.escape(AGENTS_EXPORT_START)}.*?{re.escape(AGENTS_EXPORT_END)}\n?",
                "",
                agents_text,
                count=1,
                flags=re.DOTALL,
            )
            (export_root / "AGENTS.md").write_text(missing_text)
            absent = validate_agents_md_export(export_root, repo="o/pathy", path=p)
            assert absent["status"] == "absent" and not absent["current"], absent
            update_agents_md(export_root, repo="o/pathy", path=p, apply=True)
            agents_text = (export_root / "AGENTS.md").read_text()
            (export_root / "AGENTS.md").write_text(agents_text.replace("Path validation repo.", "stale"))
            stale = validate_agents_md_export(export_root, repo="o/pathy", path=p)
            assert stale["ok"] and not stale["current"] and stale["status"] == "mismatched", stale
            assert any("differs from current registry" in item for item in stale["warnings"]), stale
        finally:
            (export_root / "AGENTS.md").unlink(missing_ok=True)
            (export_root / "docs" / "guide.md").unlink(missing_ok=True)
            (export_root / "docs").rmdir()
            export_root.rmdir()

        snap = Path("/tmp/__repo_knowledge_snapshot.json")
        snap.write_text(json.dumps({
            "runs": [
                {"run_id": "r1", "target": "stranske/Counter_Risk#5",
                 "task_type": "implement", "agent": "vibe"},
                {"run_id": "r2", "target": "stranske/Unknown [exp test]",
                 "task_type": "mechanical", "agent": "cursor"},
            ],
            "outcomes": [
                {"run_id": "r1", "adjudicated_verdict": "FAIL", "durability": "pending",
                 "notes": "Missing Black formatting check caused follow-up churn."},
                {"run_id": "r2", "adjudicated_verdict": "PASS", "durability": "durable",
                 "notes": "should mention narrow validation command in PR body"},
            ],
        }))
        try:
            suggestions = suggest_from_snapshot(snap, path=p)
            assert suggestions and suggestions[0]["repo"] == "stranske/Counter_Risk", suggestions
            assert suggestions[0]["suggested_section"] == "gotchas", suggestions
            assert suggestions[0]["source"] == "feedback-snapshot.outcomes.notes", suggestions
            assert suggestions[1]["repo"] == "stranske/Unknown", suggestions
            preview = approve_suggestion(suggestions[0], path=p)
            assert preview["preview"] and not preview["applied"], preview
            applied = approve_suggestion(suggestions[0], path=p, apply=True)
            assert applied["applied"] and applied["section"] == "gotchas", applied
            dupe = approve_suggestion(suggestions[0], path=p, apply=True)
            assert dupe["already_present"] and not dupe["applied"], dupe
            refreshed = load(p)
            counter_gotchas = refreshed["repos"]["stranske/Counter_Risk"]["gotchas"]
            assert any("Missing Black formatting" in _text(item) for item in counter_gotchas), counter_gotchas
            memory = search_repo_memory(
                "stranske/Counter_Risk#5",
                query="black formatting",
                task_type="implement",
                path=p,
                snapshot_path=snap,
            )
            assert memory and memory[0]["source"] == "approved-playbook", memory
            assert any(item["source"] == "feedback-outcome" and item["run_id"] == "r1" for item in memory), memory
            # The Postgres invariant is unscoped now, so retrieval reaches it at ANY task type.
            scoped_memory = search_repo_memory(
                "stranske/learning-management-system#7",
                query="postgres migration",
                task_type="mechanical",
                path=p,
                snapshot={"runs": [], "outcomes": []},
            )
            assert any("PostgreSQL-compatible" in item["text"] for item in scoped_memory), scoped_memory
            # ...while a genuinely work-kind-specific rule is still filtered by _memory_item_visible.
            filtered_reg = load(p)
            filtered_reg["repos"]["stranske/learning-management-system"]["validation"] = [
                {"text": "Pin the alembic revision id the issue names.", "task_types": ["testgen"]},
            ]
            save(filtered_reg, p)
            assert not any("alembic revision id" in item["text"] for item in search_repo_memory(
                "stranske/learning-management-system#7", query="alembic revision",
                task_type="mechanical", path=p, snapshot={"runs": [], "outcomes": []}))
            assert any("alembic revision id" in item["text"] for item in search_repo_memory(
                "stranske/learning-management-system#7", query="alembic revision",
                task_type="testgen", path=p, snapshot={"runs": [], "outcomes": []}))
            filtered_reg["repos"]["stranske/learning-management-system"].pop("validation")
            save(filtered_reg, p)
            impl_memory = search_repo_memory(
                "stranske/learning-management-system#7",
                query="postgres migration",
                task_type="implement",
                path=p,
                snapshot={"runs": [], "outcomes": []},
            )
            assert any("PostgreSQL-compatible" in item["text"] for item in impl_memory), impl_memory
            workflow_memory = search_repo_memory(
                "stranske/Workflows",
                query="sync manifest consumer workflow docs",
                path=p,
                snapshot={"runs": [], "outcomes": []},
                max_results=4,
            )
            assert workflow_memory[0]["section"] == "definition_of_done", workflow_memory
            assert "sync-manifest/template" in workflow_memory[0]["text"], workflow_memory
            assert {"consumer", "manifest", "sync"} <= set(workflow_memory[0]["matched_terms"]), workflow_memory
        finally:
            snap.unlink(missing_ok=True)

        docs_root = Path("/tmp/__repo_knowledge_docs")
        docs_root.mkdir(exist_ok=True)
        (docs_root / "README.md").write_text("This project uses Postgres for production data.\n")
        (docs_root / "CONTRIBUTING.md").write_text(
            "- Workflow changes must update docs/ci/WORKFLOWS.md before merging.\n"
            "- Run pytest tests/workflows before opening the PR.\n"
            "- Do not use SQLite-only migrations for persistence work.\n"
        )
        (docs_root / "NOTES.md").write_text("- Use PostgreSQL-compatible migrations before merging.\n")
        (docs_root / "docs").mkdir(exist_ok=True)
        (docs_root / "docs" / "testing.md").write_text("Always include the validation command in the PR body.\n")
        try:
            doc_suggestions = suggest_from_docs(docs_root, repo="stranske/Workflows", path=p)
            assert len(doc_suggestions) >= 3, doc_suggestions
            assert doc_suggestions[0]["source"] == "repo-docs", doc_suggestions
            sections = {item["suggested_section"] for item in doc_suggestions}
            assert {"definition_of_done", "validation", "gotchas"} <= sections, doc_suggestions
            assert any(item.get("evidence", "").startswith("CONTRIBUTING.md:") for item in doc_suggestions), doc_suggestions
            assert not any(item.get("evidence", "").startswith("NOTES.md:") for item in doc_suggestions), doc_suggestions
            assert not any("uses Postgres" in item.get("candidate_text", "") for item in doc_suggestions), doc_suggestions
            broad_doc_suggestions = suggest_from_docs(
                docs_root,
                repo="stranske/Workflows",
                path=p,
                include_root_docs=True,
            )
            assert any(item.get("evidence", "").startswith("NOTES.md:") for item in broad_doc_suggestions), (
                broad_doc_suggestions
            )

            # SECTION SCOPING + BULLET FOLDING, the two things that made an out-of-tree audit
            # README unusable as a source: round-history prose arrived as repo knowledge, and a
            # wrapped bullet was split into fragments starting mid-sentence.
            (docs_root / "README.md").write_text(
                "# Repo Audit History\n\n"
                "## Rounds\n\n"
                "- The Dropbox checkout was never touched and nothing must be pushed from it.\n"
                "```\n"
                "you must never read a fenced block as guidance\n"
                "```\n\n"
                "## Standing notes for the next round\n"
                "- `frontend_verify.py` is unreliable against this SPA (snapshots before websocket\n"
                "  render); drive with a real browser instead.\n\n"
                "### A sub-heading inside the section\n"
                "- Always run the narrow gate the issue names.\n\n"
                "## Later section\n"
                "- You must not read this one either.\n"
            )
            scoped_docs = suggest_from_docs(docs_root, repo="stranske/Workflows", path=p,
                                            sections=["Standing notes for the next round"])
            texts = [item["candidate_text"] for item in scoped_docs]
            assert any("drive with a real browser instead." in t for t in texts), texts
            assert any(t.startswith("frontend_verify.py") for t in texts), texts   # folded, not split
            assert any("Always run the narrow gate" in t for t in texts), texts    # sub-heading kept
            assert not any("Dropbox checkout" in t for t in texts), texts          # earlier section
            assert not any("read this one either" in t for t in texts), texts      # later section
            assert not any("fenced block" in t for t in texts), texts              # code fence
            # Unscoped is the previous whole-file behaviour: it sees the round history too.
            assert any("Dropbox checkout" in item["candidate_text"]
                       for item in suggest_from_docs(docs_root, repo="stranske/Workflows", path=p))
        finally:
            for child in (docs_root / "docs").glob("*"):
                child.unlink()
            (docs_root / "docs").rmdir()
            (docs_root / "NOTES.md").unlink()
            (docs_root / "CONTRIBUTING.md").unlink()
            (docs_root / "README.md").unlink()
            docs_root.rmdir()

        review_json = Path("/tmp/__repo_knowledge_review_comments.json")
        review_json.write_text(json.dumps({
            "comments": [
                {
                    "body": "Missing Black formatting check; please run black --check before merging.",
                    "path": "pyproject.toml",
                    "html_url": "https://example.test/review/1",
                    "user": {"login": "reviewer"},
                }
            ]
        }))
        try:
            review_suggestions = suggest_from_review_json(review_json, repo="stranske/Counter_Risk", path=p)
            assert review_suggestions and review_suggestions[0]["source"] == "review-comments.json", review_suggestions
            assert review_suggestions[0]["author"] == "reviewer", review_suggestions
            assert review_suggestions[0]["suggested_section"] == "gotchas", review_suggestions
        finally:
            review_json.unlink(missing_ok=True)

        def fake_gh(command, **_kwargs):
            endpoint = command[-1]
            body = "Should update docs/ci/WORKFLOWS.md when workflow names change."
            payload = [{"body": body, "path": ".github/workflows/ci.yml", "html_url": f"https://example.test/{endpoint}"}]
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

        pr_suggestions = suggest_from_pr_comments("stranske/Workflows#99", path=p, runner=fake_gh)
        assert pr_suggestions and pr_suggestions[0]["source"] == "github-pr-comments", pr_suggestions
        assert pr_suggestions[0]["repo"] == "stranske/Workflows", pr_suggestions

        def fake_gh_failure(command, **_kwargs):
            return subprocess.CompletedProcess(command, 1, "", "auth failed")

        try:
            suggest_from_pr_comments("stranske/Workflows#100", path=p, runner=fake_gh_failure)
            raise AssertionError("expected gh failure to be surfaced")
        except RuntimeError as exc:
            assert "gh is installed and authenticated" in str(exc), exc

        clustered: list[dict] = []
        clustered_seen: set[str] = set()
        known_texts = {"workflow additions or renames must update docs/ci/workflows.md."}
        _append_suggestion(clustered, clustered_seen, known_texts, {
            "repo": "test/repo",
            "candidate_text": "Workflow additions or renames must update docs/ci/WORKFLOWS.md",
            "source": "repo-docs",
            "evidence": "CONTRIBUTING.md:9",
        }, max_per_repo=5)
        assert clustered == [], clustered

        _append_suggestion(clustered, clustered_seen, known_texts, {
            "repo": "test/repo",
            "candidate_text": "Do not introduce unrelated ruff format churn.",
            "source": "review-1",
            "evidence": "pyproject.toml:5",
            "author": "alice",
        }, max_per_repo=5)
        assert len(clustered) == 1, clustered
        assert clustered[0]["cluster_key"] == "do_not_introduce_unrelated_ruff", clustered
        assert clustered[0]["occurrence_count"] == 1, clustered

        _append_suggestion(clustered, clustered_seen, known_texts, {
            "repo": "test/repo",
            "candidate_text": "Do not introduce unrelated ruff format churn in PRs",
            "source": "review-2",
            "evidence": "pyproject.toml:15",
            "author": "bob",
        }, max_per_repo=5)
        assert len(clustered) == 1, clustered
        assert clustered[0]["occurrence_count"] == 2, clustered
        assert clustered[0]["source"] == "review-1, review-2", clustered
        assert clustered[0]["sources"] == ["review-1", "review-2"], clustered
        assert clustered[0]["evidence"] == "pyproject.toml:5; pyproject.toml:15", clustered
        assert clustered[0]["evidences"] == ["pyproject.toml:5", "pyproject.toml:15"], clustered
        assert clustered[0]["author"] == "alice, bob", clustered

        _append_suggestion(clustered, clustered_seen, known_texts, {
            "repo": "test/repo",
            "candidate_text": "Always run pytest before merging changes.",
            "source": "repo-docs",
            "evidence": "TESTING.md:12",
        }, max_per_repo=5)
        assert len(clustered) == 2, clustered
        assert clustered[1]["cluster_key"] == "always_run_pytest_before_merging", clustered
    finally:
        p.unlink(missing_ok=True)
    print("repo_knowledge.py selftest: OK (seed, filters, prompt append, truncation, "
          "snapshot/docs/review suggestions, memory search, clustering, approval)")


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Record that this capability ran, at its own code path.

    Infrastructure and lane capabilities are not always ROUTED to — they are entered directly — so
    each records use where it actually executes. Lazy import (capabilities imports feedback, and
    several of these are imported BY capabilities' dependencies), never raises (recording use must
    not be able to prevent the work), and inert outside an active tick via
    ORCH_CAPABILITY_HEARTBEATS. (2026-08-09)
    """
    try:
        import capabilities
        capabilities.production_heartbeat("repo-playbook", event_type, ref="repo_knowledge.append_context")
    except Exception:
        pass


def main(argv: list[str]) -> int:
    _capability_heartbeat()
    if "--selftest" in argv:
        _selftest()
        return 0
    if "--export-agents-md" in argv:
        idx = argv.index("--export-agents-md")
        repo = argv[idx + 1] if len(argv) > idx + 1 else ""
        if not repo:
            print("--export-agents-md requires owner/repo", file=sys.stderr)
            return 2
        max_lines = int(argv[argv.index("--max-lines") + 1]) if "--max-lines" in argv else AGENTS_EXPORT_MAX_LINES
        if "--apply" in argv and "--repo-path" not in argv:
            print("--apply requires --repo-path <local-repo>", file=sys.stderr)
            return 2
        if "--repo-path" in argv:
            repo_path = Path(argv[argv.index("--repo-path") + 1])
            result = update_agents_md(
                repo_path,
                repo=repo,
                apply="--apply" in argv,
                max_lines=max_lines,
            )
            print(json.dumps(result, indent=2) if "--json" in argv else result)
            return 0
        print(export_agents_md(repo, max_lines=max_lines), end="")
        return 0
    if "--export-all-agents-md" in argv:
        max_lines = int(argv[argv.index("--max-lines") + 1]) if "--max-lines" in argv else AGENTS_EXPORT_MAX_LINES
        reg = load()
        exports = {
            repo: export_agents_md(repo, max_lines=max_lines)
            for repo in sorted((reg.get("repos") or {}).keys())
            if export_agents_md(repo, max_lines=max_lines)
        }
        print(json.dumps(exports, indent=2) if "--json" in argv else "\n".join(exports.values()))
        return 0
    if "--validate-agents-md" in argv:
        idx = argv.index("--validate-agents-md")
        repo_path = Path(argv[idx + 1]) if len(argv) > idx + 1 else Path(".")
        repo = argv[argv.index("--repo") + 1] if "--repo" in argv else None
        max_lines = int(argv[argv.index("--max-lines") + 1]) if "--max-lines" in argv else AGENTS_EXPORT_MAX_LINES
        result = validate_agents_md_export(repo_path, repo=repo, max_lines=max_lines)
        print(json.dumps(result, indent=2) if "--json" in argv else result)
        return 0 if result["ok"] else 1
    if "--suggest-from-snapshot" in argv:
        idx = argv.index("--suggest-from-snapshot")
        snapshot = Path(argv[idx + 1]) if len(argv) > idx + 1 else ORCH / "data" / "feedback-snapshot.json"
        print(json.dumps(suggest_from_snapshot(snapshot), indent=2))
        return 0
    if "--search" in argv:
        idx = argv.index("--search")
        target = argv[idx + 1] if len(argv) > idx + 1 else ""
        if not target:
            print("--search requires owner/repo[#N]", file=sys.stderr)
            return 2
        query = argv[argv.index("--query") + 1] if "--query" in argv else None
        task_type = argv[argv.index("--task-type") + 1] if "--task-type" in argv else None
        lane = argv[argv.index("--lane") + 1] if "--lane" in argv else None
        max_results = int(argv[argv.index("--max") + 1]) if "--max" in argv else 8
        snapshot_path = Path(argv[argv.index("--snapshot-json") + 1]) if "--snapshot-json" in argv else None
        print(json.dumps(search_repo_memory(
            target,
            query=query,
            task_type=task_type,
            lane=lane,
            snapshot_path=snapshot_path,
            include_runs="--no-runs" not in argv,
            max_results=max_results,
        ), indent=2))
        return 0
    if "--audit-scopes" in argv:
        result = scope_audit()
        if "--json" in argv:
            print(json.dumps(result, indent=2))
        else:
            print(f"scoped items: {result['scoped_items']}; "
                  f"of those, invariant (should be unscoped): {result['invariant_scoped_count']}")
            for row in result["invariant_scoped"]:
                print(f"  {row['repo']} [{row['section']}] signal={row['invariant_signal']!r} "
                      f"scope={row['scope']}\n    {row['text'][:140]}")
        return 0 if result["clean"] else 1
    if "--suggest-from-docs" in argv:
        idx = argv.index("--suggest-from-docs")
        repo_path = Path(argv[idx + 1]) if len(argv) > idx + 1 else Path(".")
        repo = argv[argv.index("--repo") + 1] if "--repo" in argv else None
        max_per_repo = int(argv[argv.index("--max") + 1]) if "--max" in argv else 10
        sections = [argv[i + 1] for i, arg in enumerate(argv)
                    if arg == "--section" and i + 1 < len(argv)] or None
        print(json.dumps(suggest_from_docs(
            repo_path,
            repo=repo,
            max_per_repo=max_per_repo,
            include_root_docs="--include-root-docs" in argv,
            sections=sections,
        ), indent=2))
        return 0
    if "--suggest-from-review-json" in argv:
        idx = argv.index("--suggest-from-review-json")
        review_json = Path(argv[idx + 1]) if len(argv) > idx + 1 else None
        if review_json is None:
            print("--suggest-from-review-json requires a JSON file", file=sys.stderr)
            return 2
        if "--repo" not in argv:
            print("--suggest-from-review-json requires --repo owner/repo", file=sys.stderr)
            return 2
        max_per_repo = int(argv[argv.index("--max") + 1]) if "--max" in argv else 10
        print(json.dumps(suggest_from_review_json(
            review_json,
            repo=argv[argv.index("--repo") + 1],
            max_per_repo=max_per_repo,
        ), indent=2))
        return 0
    if "--suggest-from-pr" in argv:
        idx = argv.index("--suggest-from-pr")
        target = argv[idx + 1] if len(argv) > idx + 1 else ""
        if not target:
            print("--suggest-from-pr requires owner/repo#N", file=sys.stderr)
            return 2
        max_per_repo = int(argv[argv.index("--max") + 1]) if "--max" in argv else 10
        print(json.dumps(suggest_from_pr_comments(target, max_per_repo=max_per_repo), indent=2))
        return 0
    if "--approve-suggestion" in argv:
        idx = argv.index("--approve-suggestion")
        suggestion_path = Path(argv[idx + 1]) if len(argv) > idx + 1 else None
        if suggestion_path is None:
            print("--approve-suggestion requires a suggestion JSON file", file=sys.stderr)
            return 2
        suggestion_index = int(argv[argv.index("--index") + 1]) if "--index" in argv else 0
        section = argv[argv.index("--section") + 1] if "--section" in argv else None
        result = approve_suggestion(
            _load_suggestion_file(suggestion_path, suggestion_index),
            section=section,
            apply="--apply" in argv,
        )
        print(json.dumps(result, indent=2))
        return 0
    target = argv[0] if argv else ""
    if not target:
        print("usage: repo_knowledge.py --selftest | --suggest-from-snapshot [snapshot.json] | "
              "--search owner/repo[#N] [--query TEXT] [--task-type T] [--lane L] "
              "[--snapshot-json snapshot.json] [--no-runs] [--max N] | "
              "--export-agents-md owner/repo [--repo-path <local-repo> --apply] [--max-lines N] [--json] | "
              "--export-all-agents-md [--max-lines N] [--json] | "
              "--validate-agents-md <local-repo> [--repo owner/repo] [--max-lines N] [--json] | "
              "--suggest-from-docs <docs-root> [--repo owner/repo] [--max N] [--include-root-docs] "
              "[--section 'Heading text' ...] | "
              "--audit-scopes [--json] | "
              "--suggest-from-review-json comments.json --repo owner/repo [--max N] | "
              "--suggest-from-pr owner/repo#N [--max N] | "
              "--approve-suggestion suggestions.json [--index N] [--section gotchas|validation|definition_of_done] [--apply] | "
              "<owner/repo[#N]> [task_type] [lane]", file=sys.stderr)
        return 2
    task_type = argv[1] if len(argv) > 1 else None
    lane = argv[2] if len(argv) > 2 else None
    print(context_for(target, task_type=task_type, lane=lane))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
