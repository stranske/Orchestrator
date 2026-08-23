#!/usr/bin/env python3
"""improvement_log.py — search and append THIS instance's improvement log, from ANY worktree.

WHY THIS EXISTS. `CLAUDE.md` depends on the improvement log in two places, and until this module
both were unfollowable by exactly the workers they constrain:

  * §0 step 3, part of the MANDATORY dedup-before-develop check — "check the improvement log; items
    carry status notes; many 'ideas' are already DONE";
  * §5 — "append a status note to the relevant improvement-log item".

Both named a bare path, `IMPROVEMENT_BACKLOG.md`, which was gitignored. **A gitignored file does not
exist in a git worktree, and agents work in worktrees** — it was verified absent from every worktree
on this machine. Three agents in one day were structurally unable to do step 3; two said so and fell
back to ledger notes and committed docstrings. The project's stated #1-failure-mode countermeasure —
its accumulated record of what is already DONE — was invisible to every worker that the rule binds.

So the log moved OUT OF THE TREE, to machine-local state, and this module is how it is reached. No
agent needs to know the path. The tree keeps a small COMMITTED pointer of the same name
(`IMPROVEMENT_BACKLOG.md`), so the thirteen places that cite that filename still resolve and a
worktree still hints that the log exists.

WHICH STATE VARIABLE, AND WHY `ORCH_LOCAL_RUNTIME`. `CLAUDE.md` §1 partitions the two: `ORCH_STATE_DIR`
holds the audit cache, the firing monitor and redirect-sweep state — derived artifacts that regenerate
themselves — while `ORCH_LOCAL_RUNTIME` holds the capability LEDGER and the Brain: durable, irreplaceable
instance evidence. The improvement log is the second kind. It cannot be regenerated from anything, and
its §0 role is the prose twin of the ledger's `notes` field (both answer "is this already DONE?"), so it
belongs beside the ledger rather than beside the caches.

A NAMED ABSENCE, NEVER A SILENT ONE. On a fresh clone, a CI runner, or a second instance with its own
`ORCH_LOCAL_RUNTIME`, there is no log. Every command then says what is missing and where it would be,
and exits 2 — never an empty result. A reason-less empty answer is indistinguishable from "no matching
items", which is this repository's founding defect wearing a different hat. The two are also separate
exit codes so a script can tell them apart: 1 is an honest empty, 2 is an absent log.

DEDUP FINDING (CLAUDE.md §0), recorded before a line was written. Grepped by concept, not name:
`git grep IMPROVEMENT_BACKLOG` returns thirteen references and every one is prose or a comment — no
reader, no writer, no accessor of any kind. `improvement_backlog|improvement-backlog|backlog_notes`
over `*.py` returns nothing. The only machinery that touches the filename is
`capability_admission.SKIP_NAMES`, an exclusion rather than an accessor. `backlog.py` is a different
subject entirely (this tool's own fleet work-discovery lane, writing `backlog.json`) and is not
extended here. The concept is genuinely absent, so this is new.

NOT A CAPABILITY, DELIBERATELY. This is documentation access: it has no dispatch path, no outcome and
no learning sink, so the admission gate does not bind on it and it gets no ledger row — the same
reasoning `env_prereq.py` records for itself. Registering a row would also be actively harmful right
now: the ledger is shared per MACHINE while code is branch-isolated per WORKTREE, so a row added here
would turn every sibling worktree's `verify.py` red for a module they cannot see (CLAUDE.md §1).

CLI
    python3 improvement_log.py path
    python3 improvement_log.py search <term> [--limit N] [--context N]
    python3 improvement_log.py append <item-ref> <note>
    python3 improvement_log.py --selftest
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

# The two env knobs, in the order they are consulted. Read through `log_path()` rather than captured
# at import: the selftest and the tests point a subprocess at a synthetic file, and a constant frozen
# at import time would make the module untestable without touching the owner's real 481 KB log.
ENV_DIRECT = "ORCH_IMPROVEMENT_LOG"
ENV_RUNTIME = "ORCH_LOCAL_RUNTIME"
LOG_NAME = "IMPROVEMENT_BACKLOG.md"

# Exit codes, so a caller can distinguish the two kinds of nothing.
EXIT_OK = 0
EXIT_NO_MATCH = 1        # ran, read the whole log, found nothing — an HONEST empty
EXIT_ABSENT = 2          # the log itself is not on this machine — a NAMED absence
EXIT_REFUSED = 3         # the caller's item-ref matched zero or many items; nothing was written

HEADING_RE = re.compile(r"^(#{2,3})\s+(.*?)\s*$")
# `## 🟢 8. GitHub API rate-limit awareness` -> item number 8. The status emoji and any other leading
# punctuation are skipped, because the marker changes over an item's life and the number does not.
ITEM_NUM_RE = re.compile(r"^[^0-9A-Za-z]*(\d+)\s*[.)]")


def log_path() -> Path:
    """Where the improvement log lives, resolved the way the rest of this tree resolves state."""
    direct = os.environ.get(ENV_DIRECT)
    if direct:
        return Path(direct).expanduser()
    runtime = Path(os.environ.get(ENV_RUNTIME, Path.home() / ".codex" / "orchestrator"))
    return runtime.expanduser() / LOG_NAME


def absence_note(path: Path) -> str:
    """What is missing, where it would be, and what to do — never an empty result."""
    return (
        f"PROBLEM: this instance's improvement log is NOT on this machine.\n"
        f"  looked for: {path}\n"
        f"  controlled by: ${ENV_DIRECT} (a full path), else ${ENV_RUNTIME}/{LOG_NAME}\n"
        f"  what it is: the numbered improvement items plus their status log — machine-local\n"
        f"    instance EVIDENCE, deliberately never committed (CLAUDE.md §1, tool vs evidence).\n"
        f"  so: a fresh clone, a CI runner and a second instance all legitimately lack it. This is\n"
        f"    NOT an empty search result. Do NOT create one inside the tree: the tracked\n"
        f"    {LOG_NAME} is a pointer, and committing the evidence is what the split forbids."
    )


# --------------------------------------------------------------------------------------------
# Parsing: sections, so a hit can be reported under the item that owns it.
# --------------------------------------------------------------------------------------------

def parse_sections(text: str) -> list[dict]:
    """Every `##`/`###` heading with its line span and item number, in file order.

    One pass, and the span END is exclusive, so `append` and `search` agree on where an item stops.
    """
    lines = text.splitlines()
    heads: list[dict] = []
    for idx, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if not m:
            continue
        level, title = len(m.group(1)), m.group(2)
        num_m = ITEM_NUM_RE.match(title)
        heads.append({"level": level, "title": title, "line": idx + 1,
                      "item": num_m.group(1) if num_m and level == 2 else None,
                      "end": len(lines)})
    # A section ends where the next heading of the same-or-shallower level begins.
    for i, head in enumerate(heads):
        for nxt in heads[i + 1:]:
            if nxt["level"] <= head["level"]:
                head["end"] = nxt["line"] - 1
                break
    return heads


def _owning(heads: list[dict], line_no: int) -> dict | None:
    """The DEEPEST section containing this line — the `###` if there is one, else the `##`."""
    best = None
    for head in heads:
        if head["line"] <= line_no <= head["end"]:
            if best is None or head["level"] >= best["level"]:
                best = head
    return best


# --------------------------------------------------------------------------------------------
# search — CLAUDE.md §0 step 3, in one command.
# --------------------------------------------------------------------------------------------

def search(term: str, *, path: Path | None = None, limit: int = 40,
           context: int = 0) -> dict:
    """Case-insensitive hits, each reported under the item heading that owns it.

    Returns the DENOMINATOR too (lines and sections searched), because "no matches" only means
    something when the reader can see what was read.
    """
    path = path or log_path()
    if not path.is_file():
        return {"present": False, "path": str(path), "term": term,
                "absent_reason": absence_note(path), "matches": [],
                "lines": 0, "sections": 0}
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    heads = parse_sections(text)
    needle = term.lower()
    matches = []
    for idx, line in enumerate(lines):
        if needle not in line.lower():
            continue
        own = _owning(heads, idx + 1)
        row = {"line": idx + 1, "text": line.strip(),
               "section": own["title"] if own else "(before the first heading)",
               "section_line": own["line"] if own else 0,
               "item": (own or {}).get("item")}
        if context:
            lo, hi = max(0, idx - context), min(len(lines), idx + context + 1)
            row["context"] = [ln.rstrip() for ln in lines[lo:hi]]
        matches.append(row)
    truncated = len(matches) > limit
    return {"present": True, "path": str(path), "term": term, "absent_reason": None,
            "matches": matches[:limit], "total_matches": len(matches),
            "truncated": truncated, "lines": len(lines), "sections": len(heads)}


def render_search(rep: dict) -> str:
    if not rep["present"]:
        return rep["absent_reason"]
    head = (f"improvement log: {rep['path']}\n"
            f"searched {rep['lines']} lines / {rep['sections']} sections for {rep['term']!r}")
    if not rep["matches"]:
        # An honest empty, and it says so in words as well as in the exit code.
        return (f"{head}\nNO MATCHING ITEMS. The log was read in full and nothing mentions "
                f"{rep['term']!r} — this is a real absence of matches, not a missing file.")
    out = [head, f"{rep['total_matches']} match(es)"
                 + (f", showing the first {len(rep['matches'])}" if rep["truncated"] else "")]
    last = None
    for m in rep["matches"]:
        if m["section"] != last:
            out.append("")
            out.append(f"## {m['section']}   (line {m['section_line']}"
                       + (f", item {m['item']}" if m["item"] else "") + ")")
            last = m["section"]
        out.append(f"  {m['line']}: {m['text']}")
        for ctx in m.get("context") or []:
            out.append(f"      | {ctx}")
    return "\n".join(out)


# --------------------------------------------------------------------------------------------
# append — CLAUDE.md §5, in one command.
# --------------------------------------------------------------------------------------------

def find_section(ref: str, heads: list[dict]) -> dict:
    """Resolve an item-ref to exactly ONE section, or refuse and say which candidates it saw.

    A bare number means the numbered item (`8`, `#8` -> `## 🟢 8. GitHub API rate-limit ...`);
    anything else is a case-insensitive substring of the heading. Zero or many matches REFUSE:
    guessing which item a note belongs to would corrupt the record it is meant to improve.
    """
    ref = ref.strip()
    bare = ref.lstrip("#§").strip()
    if bare.isdigit():
        hits = [h for h in heads if h["item"] == str(int(bare))]
        kind = f"item number {int(bare)}"
    else:
        needle = ref.lower()
        hits = [h for h in heads if needle in h["title"].lower()]
        kind = f"heading containing {ref!r}"
    if len(hits) == 1:
        return {"ok": True, "section": hits[0]}
    return {"ok": False, "section": None, "candidates": hits, "kind": kind,
            "reason": ("no section matches" if not hits
                       else f"{len(hits)} sections match — the ref is ambiguous")}


def append_note(ref: str, note: str, *, path: Path | None = None,
                today: str | None = None) -> dict:
    """Append one dated status note at the END of the matched section. Atomic, with one backup."""
    path = path or log_path()
    if not path.is_file():
        return {"ok": False, "absent": True, "path": str(path),
                "reason": absence_note(path)}
    if not note.strip():
        return {"ok": False, "absent": False, "path": str(path),
                "reason": "refusing to append an empty note"}
    text = path.read_text(encoding="utf-8")
    heads = parse_sections(text)
    found = find_section(ref, heads)
    if not found["ok"]:
        return {"ok": False, "absent": False, "path": str(path), "ref": ref,
                "candidates": [{"title": h["title"], "line": h["line"], "item": h["item"]}
                               for h in found["candidates"]],
                "reason": f"{found['reason']} for {found['kind']}"}
    sec = found["section"]
    lines = text.splitlines()
    stamp = today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    entry = f"- **STATUS {stamp}:** {note.strip()}"
    # Insert at the section's end, after trailing blank lines are stepped back over, so the note
    # lands inside the item it belongs to rather than under the next heading.
    at = min(sec["end"], len(lines))
    while at > sec["line"] and not lines[at - 1].strip():
        at -= 1
    new = lines[:at] + ["", entry] + lines[at:]
    body = "\n".join(new) + "\n"
    backup = path.with_suffix(path.suffix + ".prev")
    shutil.copy2(path, backup)                 # ONE rolling backup: this history is unversioned.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".improvement-log-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {"ok": True, "absent": False, "path": str(path), "section": sec["title"],
            "section_line": sec["line"], "inserted_at": at + 2, "entry": entry,
            "backup": str(backup)}


def render_append(rep: dict) -> str:
    if rep.get("absent"):
        return rep["reason"]
    if not rep["ok"]:
        out = [f"REFUSED — nothing was written: {rep['reason']}."]
        cands = rep.get("candidates") or []
        if cands:
            out.append("candidates (give a longer, unambiguous ref):")
            out += [f"  line {c['line']}: {c['title']}" for c in cands[:10]]
        else:
            out.append(f"run `improvement_log.py search <term>` against {rep['path']} to find the "
                       f"item, then use its number or a distinctive phrase from its heading.")
        return "\n".join(out)
    return (f"appended to {rep['path']}\n  section: {rep['section']} (line {rep['section_line']})\n"
            f"  line {rep['inserted_at']}: {rep['entry']}\n  backup: {rep['backup']}")


# --------------------------------------------------------------------------------------------
# selftest — the CALLER's contract, exercised through the CLI on a synthetic log in a tempdir.
# --------------------------------------------------------------------------------------------

SYNTHETIC = """# Synthetic improvement log

## 🟢 4. Durable repo-knowledge memory

Something about caching a pattern.

## ✅ 7. Telemetry integrity

### 7a. Cost-aware scoring

Thompson sampling was wired here and is DONE.

### 7b. Thompson sampling flag

Held OFF behind a switch.

## ⚪ 9. Thompson routing follow-up

Still open.
"""


def _selftest() -> int:
    import json
    import subprocess

    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  ok   {name}")
        else:
            failures.append(f"{name}: {detail}")
            print(f"  FAIL {name} — {detail}")

    def run(argv: list[str], log: str) -> subprocess.CompletedProcess:
        # A SUBPROCESS on purpose: the assertions below are about what a CALLER receives from the
        # CLI — text and exit code — not about what an internal helper returns.
        env = dict(os.environ, **{ENV_DIRECT: log})
        return subprocess.run([sys.executable, str(Path(__file__).resolve()), *argv],
                              capture_output=True, text=True, env=env)

    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / LOG_NAME
        log.write_text(SYNTHETIC, encoding="utf-8")
        missing = Path(td) / "nowhere" / LOG_NAME

        print("improvement_log selftest")

        # 1. search finds a term and reports the ITEM that owns it.
        p = run(["search", "thompson"], str(log))
        check("search exits 0 on a hit", p.returncode == EXIT_OK, f"rc={p.returncode}")
        check("search names the owning section", "7a. Cost-aware scoring" in p.stdout,
              p.stdout[-200:])
        check("search reports the denominator", "sections for 'thompson'" in p.stdout,
              p.stdout[:200])
        check("search finds every occurrence", "3 match(es)" in p.stdout, p.stdout[:200])

        # 2. An HONEST empty: different exit code, and it says the file WAS read.
        p = run(["search", "zzz-nothing-here"], str(log))
        check("no match exits 1", p.returncode == EXIT_NO_MATCH, f"rc={p.returncode}")
        check("no match says so in words", "NO MATCHING ITEMS" in p.stdout, p.stdout[:200])
        check("no match is not reported as absence", "PROBLEM:" not in p.stdout, p.stdout[:200])

        # 3. The NAMED ABSENCE: what is missing, where, and which env vars control it.
        p = run(["search", "thompson"], str(missing))
        check("absent log exits 2", p.returncode == EXIT_ABSENT, f"rc={p.returncode}")
        check("absence names the path", str(missing) in p.stdout, p.stdout[:300])
        check("absence names both env vars",
              ENV_DIRECT in p.stdout and ENV_RUNTIME in p.stdout, p.stdout[:300])
        check("absence is not an empty result", p.stdout.strip() != "", "empty stdout")

        # 4. append lands the note INSIDE the referenced item, dated. The target is item 4, which
        # is deliberately NOT the last section: appending to the last one makes "end of section"
        # and "end of file" the same position, so the placement assertion could not fail. It was
        # written that way first and a break->revert proved it discriminated nothing.
        p = run(["append", "4", "wired and verified"], str(log))
        check("append exits 0", p.returncode == EXIT_OK, f"rc={p.returncode}\n{p.stderr[-300:]}")
        after = log.read_text(encoding="utf-8")
        check("append wrote a dated note", "**STATUS " in after and "wired and verified" in after,
              after[-200:])
        head, _, rest = after.partition("## ✅ 7. Telemetry integrity")
        check("note is INSIDE item 4, above the next heading", "wired and verified" in head,
              f"landed after item 4: {rest[-160:]!r}")
        check("note did not land at end of file", "wired and verified" not in rest, rest[-160:])
        check("append did not touch other items",
              after.count("Thompson sampling was wired here and is DONE.") == 1, "duplicated")
        check("append left one backup", (Path(td) / f"{LOG_NAME}.prev").is_file(), "no .prev")

        # 5. A ref that matches many REFUSES and changes nothing.
        before = log.read_text(encoding="utf-8")
        p = run(["append", "Thompson", "ambiguous"], str(log))
        check("ambiguous ref refuses", p.returncode == EXIT_REFUSED, f"rc={p.returncode}")
        check("refusal lists candidates", "candidates" in p.stdout, p.stdout[:300])
        check("refusal wrote nothing", log.read_text(encoding="utf-8") == before, "file changed")

        # 6. A ref that matches nothing REFUSES too, and points at `search`.
        p = run(["append", "99", "no such item"], str(log))
        check("unknown ref refuses", p.returncode == EXIT_REFUSED, f"rc={p.returncode}")
        check("unknown ref suggests search", "search" in p.stdout, p.stdout[:300])
        check("unknown ref wrote nothing", log.read_text(encoding="utf-8") == before,
              "file changed")

        # 7. append to an absent log names the absence and does NOT create the file.
        p = run(["append", "9", "note"], str(missing))
        check("append to absent log exits 2", p.returncode == EXIT_ABSENT, f"rc={p.returncode}")
        check("append to absent log creates nothing", not missing.exists(), "file created")

        # 8. `path` answers where the log is even when it is missing, for both env knobs.
        p = run(["path", "--json"], str(missing))
        check("path exits 2 when absent", p.returncode == EXIT_ABSENT, f"rc={p.returncode}")
        try:
            doc = json.loads(p.stdout)
        except Exception:                                          # noqa: BLE001
            doc = {}
        check("path reports resolved + present", doc.get("path") == str(missing)
              and doc.get("present") is False, p.stdout[:200])

    print(f"improvement_log selftest: {len(failures)} failure(s)")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Search and append this instance's improvement log (machine-local evidence).")
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("search", help="CLAUDE.md §0 step 3 — is this already DONE?")
    s.add_argument("term")
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--context", type=int, default=0, help="lines of context around each hit")
    s.add_argument("--json", action="store_true")

    a = sub.add_parser("append", help="CLAUDE.md §5 — record a status note on an item")
    a.add_argument("ref", help="item number (e.g. 9) or a distinctive phrase from its heading")
    a.add_argument("note")
    a.add_argument("--json", action="store_true")

    p = sub.add_parser("path", help="where the log resolves to, and whether it is here")
    p.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.cmd:
        ap.print_help()
        return EXIT_OK

    import json

    if args.cmd == "path":
        target = log_path()
        present = target.is_file()
        doc = {"path": str(target), "present": present,
               "env": {ENV_DIRECT: os.environ.get(ENV_DIRECT),
                       ENV_RUNTIME: os.environ.get(ENV_RUNTIME)}}
        if args.json:
            print(json.dumps(doc, indent=2))
        else:
            print(f"improvement log: {target}\npresent: {present}")
            if not present:
                print(absence_note(target))
        return EXIT_OK if present else EXIT_ABSENT

    if args.cmd == "search":
        rep = search(args.term, limit=args.limit, context=args.context)
        print(json.dumps(rep, indent=2) if args.json else render_search(rep))
        if not rep["present"]:
            return EXIT_ABSENT
        return EXIT_OK if rep["matches"] else EXIT_NO_MATCH

    rep = append_note(args.ref, args.note)
    print(json.dumps(rep, indent=2) if args.json else render_append(rep))
    if rep.get("absent"):
        return EXIT_ABSENT
    return EXIT_OK if rep["ok"] else EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
