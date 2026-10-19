#!/usr/bin/env python3
"""Render a controller.py whose scheduled `main` runs the pinned closer-campaign tick.

Never edits the installed automation: it reads --controller and writes --out only, for the
coordinator to review and deploy. The rendered controller loads closer_campaign (and the
modules it imports) from the paths and sha256 digests in --manifest, never by a plain
`import` from the controller directory, and falls back to the unchanged demand_router
path if any digest differs. Refuses a controller of unexpected shape.
"""

from __future__ import annotations

import argparse
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
        base = _Path(manifest["module_dir"])
        for name, digest in manifest["modules"].items():
            if hashlib.sha256((base / name).read_bytes()).hexdigest() != digest:
                return None
        for name in ("claims", "closer_release", "closer_runtime_binding", "closer_campaign"):
            path = str(base / (name + ".py"))
            loaded = _sys.modules.get(name)
            if loaded is not None:
                if getattr(loaded, "__file__", None) != path:
                    return None
                continue
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            _sys.modules[name] = module
            spec.loader.exec_module(module)
        return _sys.modules["closer_campaign"]
    except Exception:
        return None


'''


def render(source: str, manifest_path: str) -> str:
    if MARKER in source:
        return source
    if source.count(OLD) != 1 or source.count(MAIN_DEF) != 1:
        raise SystemExit(
            "controller main() does not have the expected single demand_router delegation"
        )
    loader = LOADER.format(marker=MARKER, manifest=str(Path(manifest_path).resolve()))
    return source.replace(MAIN_DEF, loader + MAIN_DEF).replace(OLD, NEW)


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
