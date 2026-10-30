#!/usr/bin/env python3
"""Render a controller.py whose scheduled `main` runs the pinned closer-campaign tick.

Never edits the installed automation: it reads --controller and writes --out only, for the
coordinator to review and deploy. The rendered controller loads closer_campaign (and the
modules it imports) from the paths and sha256 digests in --manifest, never by a plain
`import` from the controller directory, and falls back to the unchanged demand_router
path if any digest differs or a pinned module is already cached. Cached modules
require a fresh controller process. Refuses unexpected or conflicting hook bindings.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

OLD = "    import demand_router\n    return demand_router.main(argv)\n"
NEW = (
    "    import demand_router\n"
    "    campaign = _load_pinned_closer_campaign()\n"
    "    if campaign is None:\n"
    "        return demand_router.main(argv)\n"
    "    return campaign.controller_entry(argv, demand_router.main)\n"
)
NEW_CLOSURE = (
    "    campaign = _load_pinned_closer_campaign()\n"
    "    if campaign is None:\n"
    "        import demand_router\n"
    "        return demand_router.main(argv)\n"
    "    return campaign.controller_entry(argv, None)\n"
)
MAIN_DEF = "def main(argv: list[str] | None = None) -> int:\n"
MARKER = "# closer-campaign pinned loader"
LOADER = '''{marker}
CLOSER_CAMPAIGN_MANIFEST = {manifest!r}


def _load_pinned_closer_campaign():
    """Load closer_campaign from the manifest's pinned paths; None on any mismatch."""
    import hashlib
    import importlib.util
    import json as _json
    import sys as _sys
    from pathlib import Path as _Path

    try:
        manifest = _json.loads(_Path(CLOSER_CAMPAIGN_MANIFEST).read_text())
        base = _Path(manifest["module_dir"]).resolve()
        modules = manifest["modules"]
        demand = (manifest.get("mechanism_contract") or {{}}).get("controller_demand") or {{}}
        v2_closure = demand.get("schema") == "closer-controller-demand/v2"
        required = ("claims.py", "closer_release.py", "closer_runtime_binding.py",
                    "closer_campaign.py", "router.py", "execution_profiles.py",
                    "closer_distribution_package.py")
        if v2_closure:
            required += ("closer_publisher_closure.py",)
        if not all(name in modules for name in required):
            return None
        for name, digest in modules.items():
            if _Path(name).name != name or not name.endswith(".py"):
                return None
            if hashlib.sha256((base / name).read_bytes()).hexdigest() != digest:
                return None
            # Disk hashes cannot attest code already cached at even the same path.
            # Never reload/delete shared imports: require a fresh controller process.
            if _Path(name).stem in _sys.modules:
                return None
        _sys.dont_write_bytecode = True
        # Always move the pin first, even if it was already later in the path.
        _sys.path[:] = [str(base)] + [item for item in _sys.path if item != str(base)]
        # Lazy imports resolve from this verified closure without eagerly importing
        # unrelated routing/capacity modules into the controller process.
        for name in ("claims", "closer_release", "closer_runtime_binding", "closer_campaign"):
            path = str(base / (name + ".py"))
            if name + ".py" not in modules:
                return None
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            _sys.modules[name] = module
            spec.loader.exec_module(module)
        return _sys.modules["closer_campaign"]
    except Exception:
        return None


'''


def render(source: str, manifest_path: str) -> str:
    wants_closure = False
    try:
        contract = (
            json.loads(Path(manifest_path).read_text())
            .get("mechanism_contract", {})
            .get("controller_demand", {})
        )
        wants_closure = contract.get("schema") == "closer-controller-demand/v2"
    except (OSError, ValueError, AttributeError):
        pass
    replacement = NEW_CLOSURE if wants_closure else NEW
    if MARKER in source:
        expected = str(Path(manifest_path).resolve())
        try:
            tree = ast.parse(source)
            bindings = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Name)
                and node.id == "CLOSER_CAMPAIGN_MANIFEST"
                and isinstance(node.ctx, ast.Store)
            ]
            assignments = [
                node
                for node in tree.body
                if isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "CLOSER_CAMPAIGN_MANIFEST"
            ]
            if len(bindings) != 1 or len(assignments) != 1:
                raise ValueError("ambiguous manifest binding")
            pin = ast.literal_eval(assignments[0].value)
            if not isinstance(pin, str) or pin != expected:
                raise ValueError("manifest pin differs")
            loader = LOADER.format(marker=MARKER, manifest=expected)
            if (
                source.count(MARKER) != 1
                or source.count(loader) != 1
                or source.count(MAIN_DEF) != 1
                or source.count(NEW) + source.count(NEW_CLOSURE) != 1
            ):
                raise ValueError("unexpected pinned loader shape")
        except (SyntaxError, ValueError, TypeError) as exc:
            raise SystemExit(f"controller manifest pin rejected: {exc}") from exc
        return source
    if source.count(OLD) != 1 or source.count(MAIN_DEF) != 1:
        raise SystemExit(
            "controller main() does not have the expected single demand_router delegation"
        )
    loader = LOADER.format(marker=MARKER, manifest=str(Path(manifest_path).resolve()))
    return source.replace(MAIN_DEF, loader + MAIN_DEF).replace(OLD, replacement)


def check_manifest(path: str) -> None:
    """Refuse to render a pin whose modules do not match their recorded digests."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import closer_campaign

    manifest = json.loads(Path(path).read_text())
    problems = closer_campaign.validate_manifest(manifest) + closer_campaign.verify_modules(
        manifest
    )
    if problems:
        raise SystemExit("manifest rejected: " + "; ".join(problems))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--controller", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    if Path(args.out).resolve() == Path(args.controller).resolve():
        p.error("--out must differ from --controller")
    check_manifest(args.manifest)
    Path(args.out).write_text(render(Path(args.controller).read_text(), args.manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
