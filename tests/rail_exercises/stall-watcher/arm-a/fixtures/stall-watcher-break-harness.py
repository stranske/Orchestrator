import json
from pathlib import Path
import watch

log = Path(__file__).resolve().parent / "stall-watcher.log"
result = watch.classify_lane(log=log, stale_seconds=600, now=1_700_000_900.0)
assert result["state"] == "stalled", result
assert result["policy_decision"]["action"] == "redirect", result
raise SystemExit("DELIBERATE_BREAK: expected policy action wait, got redirect")
