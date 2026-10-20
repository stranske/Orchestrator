"""Subprocess entry for test_closer_scoped.py: calls the real run_pipeline.

argv[1] is a JSON spec: state, targets, mode, deadline_in, act_sleep, act_enter_dir,
act_hold_for (target -> file to wait on), crash_in_act, outage_act, log, pin,
keys (target -> resource keys for act), max_io/max_low (pool bounds).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" if (ROOT / "src").is_dir() else ROOT))

from closer_pipeline import run_pipeline  # noqa: E402
from closer_resources import default_resource_keys  # noqa: E402


def main() -> None:
    spec = json.loads(sys.argv[1])
    log = Path(spec["log"]) if spec.get("log") else None

    def note(event: str, target: str) -> None:
        if log:
            with log.open("a") as stream:
                stream.write(json.dumps({"e": event, "t": target, "at": time.time()}) + "\n")

    def prepare(target, ctx):
        note("prepare", target)
        key = "closer-evidence-semantic/v1:" + hashlib.sha256(target.encode()).hexdigest()
        return {"revision": "rev-1", "identity": target, "semantic_cache_key": key}

    def act(target, ctx):
        note("act", target)
        note("act_enter", target)
        if target in spec.get("outage_act", []):
            raise ConnectionError("GitHub outage")
        enter = spec.get("act_enter_dir")
        if enter:
            Path(enter, hashlib.sha256(target.encode()).hexdigest()[:8]).touch()
        hold = spec.get("act_hold_for", {}).get(target)
        while hold and not Path(hold).exists():
            time.sleep(0.02)
        if target in spec.get("crash_in_act", []):
            os._exit(9)
        time.sleep(spec.get("act_sleep", 0))
        note("act_exit", target)
        return {"status": "completed", "target_complete": True}

    kwargs = {}
    if spec.get("mode"):
        kwargs["coordinator_mode"] = spec["mode"]
    if spec.get("pin"):
        kwargs["coordinator_contract_pin"] = spec["pin"]
    # This synthetic backend has no hidden linked aliases: its complete inventory
    # is the own target/repository plus the explicit fixture aliases.
    kwargs["resource_keys"] = lambda target, ctx: [
        *default_resource_keys(target),
        *spec.get("keys", {}).get(target, []),
    ]
    for name, arg in (("max_io", "max_io_workers"), ("max_low", "max_low_workers")):
        if spec.get(name):
            kwargs[arg] = spec[name]
    try:
        receipt = run_pipeline(
            spec["state"],
            spec["targets"],
            prepare=prepare,
            interpret=lambda t, c: {"substantive_needs": [{"kind": "merge"}]},
            act=act,
            exception=lambda t, c: {"reviewed": True},
            capacity_fn=lambda: {"io": 8, "low": 4, "exception": 2},
            deadline=time.time() + spec.get("deadline_in", 30),
            **kwargs,
        )
    except Exception as exc:
        print(json.dumps({"raised": f"{type(exc).__name__}: {exc}"}))
        return
    print(json.dumps(receipt, default=str))


if __name__ == "__main__":
    main()
