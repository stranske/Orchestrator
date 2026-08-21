#!/usr/bin/env python3
"""feature_scan.py — the missing caller for the feature registry.

WHY THIS EXISTS. `features.py` implements the RULE OF THREE: log each reusable structure as it
emerges, promote it up a maturity ladder (ad-hoc -> reused -> hardened) so the wheel is not
reinvented. Its registry holds 20 entries, ALL at `hardened` — the ladder is fully climbed for
everything in it, which made the capability look idle. It is not idle; it is BLIND. Nothing ever
called it, so the registry can only describe structures someone remembered to add by hand.

The cost is demonstrable. On 2026-08-19 alone this project produced four new reusable structures —
`issue_readiness`, `capability_activation_audit`, the `durability_sweep` no-delivery guard, and the
durable-issue census — and not one was logged. Each is precisely the "ad-hoc structure that will be
reinvented" the registry exists to prevent.

WHAT COUNTS AS A REUSABLE STRUCTURE. A local module that (a) has a module docstring, and (b) defines
a `_selftest`. That is this project's own definition of a hardened, reusable unit — every one of the
20 registry entries meets it, and it is mechanical rather than a judgement call. Test files and the
registry's own module are excluded.

DELIBERATELY CONSERVATIVE. It records at `ad-hoc` maturity and never promotes: `features.py` advances
ad-hoc -> reused on a second use, and `mark_hardened` stays a deliberate human act so the registry
never claims maturity the code has not earned. Reporting is the default; `--apply` writes.

    python3 feature_scan.py             # what is unlogged
    python3 feature_scan.py --json
    python3 feature_scan.py --apply     # log the unlogged at ad-hoc maturity
    python3 feature_scan.py --selftest
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

import features

HERE = Path(__file__).resolve().parent

# Modules that are not "reusable structures" in the registry's sense.
EXCLUDE_PREFIXES = ("test_",)
EXCLUDE_NAMES = {"features.py", "feature_scan.py", "conftest.py", "setup.py"}


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Credit the feature-reflection capability at the path a driver actually enters."""
    try:
        import capabilities
        capabilities.production_heartbeat("feature-reflection-cli", event_type,
                                          ref="feature_scan.scan")
    except Exception:
        pass


def is_reusable_structure(path: Path) -> tuple[bool, str]:
    """(qualifies, why). A module docstring plus a `_selftest` — this project's own bar."""
    if path.name in EXCLUDE_NAMES or path.name.startswith(EXCLUDE_PREFIXES):
        return False, "excluded (test/registry module)"
    try:
        tree = ast.parse(path.read_text(errors="ignore"))
    except (OSError, SyntaxError) as exc:
        return False, f"unparseable ({type(exc).__name__})"
    has_doc = bool(ast.get_docstring(tree))
    has_selftest = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and n.name == "_selftest" for n in ast.walk(tree))
    if has_doc and has_selftest:
        return True, "module docstring + _selftest"
    missing = []
    if not has_doc:
        missing.append("no docstring")
    if not has_selftest:
        missing.append("no _selftest")
    return False, ", ".join(missing)


def scan(*, path=None, root: Path | None = None) -> dict:
    """Reusable structures present in the tree but absent from the registry."""
    _capability_heartbeat()
    root = root or HERE
    reg = features.load(path) if path else features.load()
    known_modules = {str((v or {}).get("module") or "").strip() for v in reg.values()}
    known_names = {str(k).strip().lower() for k in reg}

    unlogged, qualifying, skipped = [], [], []
    for file in sorted(root.glob("*.py")):
        ok, why = is_reusable_structure(file)
        if not ok:
            skipped.append({"module": file.name, "why": why})
            continue
        qualifying.append(file.name)
        stem = file.stem.replace("_", "-").lower()
        if file.name in known_modules or stem in known_names or file.stem.lower() in known_names:
            continue
        unlogged.append({"name": stem, "module": file.name})
    return {"registry_entries": len(reg), "qualifying_modules": len(qualifying),
            "unlogged": unlogged, "unlogged_count": len(unlogged),
            "skipped_sample": skipped[:5]}


def apply_scan(rep: dict, *, dry_run: bool = True, path=None) -> dict:
    """Log unlogged structures at AD-HOC maturity. Never promotes."""
    recorded = []
    for row in rep["unlogged"]:
        if dry_run:
            recorded.append(row["name"])
            continue
        try:
            kwargs = {"module": row["module"], "maturity": "ad-hoc"}
            if path:
                features.record_use(row["name"], "feature_scan", path=path, **kwargs)
            else:
                features.record_use(row["name"], "feature_scan", **kwargs)
            recorded.append(row["name"])
        except Exception as exc:                       # noqa: BLE001
            recorded.append(f"{row['name']}: ERROR {str(exc)[:60]}")
    return {"recorded": recorded, "dry_run": dry_run}


def format_report(rep: dict) -> str:
    lines = ["# Feature scan — reusable structures the registry has never seen", "",
             f"  registry entries:    {rep['registry_entries']}",
             f"  qualifying modules:  {rep['qualifying_modules']}",
             f"  UNLOGGED:            {rep['unlogged_count']}", ""]
    if not rep["unlogged"]:
        lines += ["  Every reusable structure in the tree is logged. This is the healthy state.", ""]
    else:
        lines += ["  These will be reinvented unless logged (RULE OF THREE):", ""]
        for row in rep["unlogged"]:
            lines.append(f"    {row['name']:<34} {row['module']}")
        lines.append("")
    return "\n".join(lines)


def _selftest() -> None:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="feat-scan-") as td:
        root = Path(td)
        reg_path = root / "features.json"
        # A qualifying module: docstring + _selftest.
        (root / "good_thing.py").write_text('"""A reusable thing."""\ndef _selftest():\n    pass\n')
        # Non-qualifying: no selftest, and no docstring.
        (root / "no_selftest.py").write_text('"""Has docs only."""\ndef go():\n    pass\n')
        (root / "no_docs.py").write_text('def _selftest():\n    pass\n')
        # Excluded by name.
        (root / "test_thing.py").write_text('"""t."""\ndef _selftest():\n    pass\n')
        (root / "features.py").write_text('"""registry itself."""\ndef _selftest():\n    pass\n')

        rep = scan(path=reg_path, root=root)
        names = {r["name"] for r in rep["unlogged"]}
        assert names == {"good-thing"}, names
        assert rep["qualifying_modules"] == 1, rep
        # The exclusions must be REPORTED, not silently dropped.
        skipped = {s["module"] for s in rep["skipped_sample"]}
        assert {"no_selftest.py", "no_docs.py", "test_thing.py", "features.py"} <= skipped, skipped

        # Dry run writes nothing.
        assert apply_scan(rep, dry_run=True, path=reg_path)["dry_run"] is True
        before = features.load(reg_path)
        assert "good-thing" not in before, before

        # Apply records it at AD-HOC, never hardened — the registry must not claim maturity the
        # code has not earned (that is `mark_hardened`, a deliberate human act).
        out = apply_scan(rep, dry_run=False, path=reg_path)
        assert out["recorded"] == ["good-thing"], out
        after = features.load(reg_path)
        assert "good-thing" in after, after
        assert (after["good-thing"] or {}).get("maturity") == "ad-hoc", after["good-thing"]

        # IDEMPOTENT: a second scan sees it as logged, so re-running is a no-op.
        rep2 = scan(path=reg_path, root=root)
        assert rep2["unlogged_count"] == 0, rep2
        assert "healthy state" in format_report(rep2)

    print("feature_scan.py selftest: OK (docstring+_selftest bar, exclusions reported, records at "
          "ad-hoc never hardened, idempotent)")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--apply", action="store_true", help="log unlogged structures at ad-hoc")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    rep = scan()
    if args.apply:
        rep["apply"] = apply_scan(rep, dry_run=False)
    print(json.dumps(rep, indent=2) if args.json else format_report(rep), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
