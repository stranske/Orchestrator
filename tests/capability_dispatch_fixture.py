"""Real dispatch planning and Brain writes with only external effects replaced."""

from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import dispatcher
import feedback
import roles


@contextmanager
def local_dispatch(root):
    workspace = root / "worker"
    workspace.mkdir(exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(patch.dict("os.environ", {"ORCH_ROLE_SHADOW": "0"}))
        replacements = [
            (feedback, "DB_PATH", root / "brain.db"),
            (feedback, "_capability_daily_heartbeat", lambda *a, **kw: None),
            (dispatcher, "DISPATCH_LOG_DIR", root / "logs"),
            (dispatcher, "build_prompt", lambda *a, **kw: "baseline worker prompt"),
            (dispatcher, "_agent_preamble", lambda *a: "fixture agent"),
            (dispatcher, "_agent_runtime_prelude", lambda *a: ""),
            (dispatcher, "_auth_prelude", lambda *a: ""),
            (dispatcher.provision, "provision", lambda *a: workspace),
            (dispatcher.provision, "worktree_path", lambda *a: workspace),
            (dispatcher.adapters, "build_command", lambda *a, **kw: ["true"]),
            (dispatcher.adapters, "record_ledger", lambda *a, **kw: None),
            (dispatcher.claims, "active_claims", lambda: {}),
            (dispatcher.claims, "update_metadata", lambda *a, **kw: True),
            (roles.router, "load_capacity", lambda: {"agents": {}}),
        ]
        for obj, name, replacement in replacements:
            stack.enter_context(patch.object(obj, name, replacement))
        process = stack.enter_context(
            patch.object(dispatcher.subprocess, "Popen", return_value=SimpleNamespace(pid=1234))
        )
        stack.enter_context(
            patch.object(
                dispatcher.subprocess, "run", side_effect=AssertionError("external process")
            )
        )
        yield process
