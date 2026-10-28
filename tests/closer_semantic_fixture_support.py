"""Portable synthetic data helpers for semantic mechanism tests.

Only physical_product invokes fixed local Git against its new test directory.
No native provider/account call or source validator is replaced. The original fixture algorithms retain reviewed AST after only the explicit
checkout/flat input-root substitution; unused external test fixtures are
deliberately not imported.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
from pathlib import Path
from types import SimpleNamespace

from closer_inert_dispatcher_support import REAL_RUN

import closer_mechanical_catalog as catalog
import closer_mechanical_source as source


def blob(raw):
    return {
        "type": "file",
        "encoding": "base64",
        "content": base64.b64encode(raw).decode(),
        "size": len(raw),
        "sha": hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest(),
    }


def product_source_root():
    """Locate inert files already shipped beside checkout/flat test modules.

    SOURCE_ROOT is not a production reader; it is an unused checkout constant.
    These fixture inputs belong to the existing exec-mirror copy contract.
    """
    modules = Path(source.__file__).resolve(strict=True).parent
    return modules.parent if modules.name == "src" else modules


def physical_product(root):
    root.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Inert Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Inert Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": "2026-10-27T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-10-27T00:00:00Z",
    }

    def git(*args):
        return REAL_RUN(
            ["/usr/bin/git", "-C", str(root), *args],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()

    git("init", "-b", "main")
    for name in (*catalog.SOURCE_FILES, source.WORKFLOW_PATH):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((product_source_root() / name).read_bytes())
    git("add", "--", *catalog.SOURCE_FILES, source.WORKFLOW_PATH)
    git(
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "fixture policy base",
    )
    base = git("rev-parse", "HEAD")
    (root / "report.py").write_text("def ship_report():\n    return True\n")
    git("add", "--", "report.py")
    git(
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "fixture report implementation",
    )
    head = git("rev-parse", "HEAD")
    docs = {
        "repos/owner/repo": {"full_name": "owner/repo", "default_branch": "main"},
        "repos/owner/repo/branches/main": {"name": "main", "commit": {"sha": head}},
    }
    for name in (*catalog.SOURCE_FILES, source.WORKFLOW_PATH):
        raw = (root / name).read_bytes()
        doc = blob(raw)
        assert git("rev-parse", f"{head}:{name}") == doc["sha"]
        docs[f"repos/owner/repo/contents/{name}?ref={head}"] = doc
    assert git("merge-base", "--is-ancestor", base, head) == ""
    policies, unmet = catalog.compile_catalog(root)
    assert len(policies) == 2
    diff = {
        "status": "ahead",
        "files": [
            {
                "filename": "report.py",
                "status": "added",
                "sha": git("rev-parse", f"{head}:report.py"),
                "patch": git("diff", "--no-ext-diff", "--no-color", base, head, "--", "report.py"),
            }
        ],
    }
    return SimpleNamespace(
        root=root, base=base, head=head, docs=docs, diff=diff, policies=policies, unmet=unmet
    )


def closed_function(callback, wanted, seen=None):
    seen = set() if seen is None else seen
    if id(callback) in seen:
        return None
    seen.add(id(callback))
    if callback.__name__ == wanted:
        return callback
    for item in inspect.getclosurevars(callback).nonlocals.values():
        if inspect.isfunction(item):
            found = closed_function(item, wanted, seen)
            if found is not None:
                return found
    return None
