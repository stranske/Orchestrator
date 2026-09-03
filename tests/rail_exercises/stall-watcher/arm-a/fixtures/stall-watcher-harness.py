import json
import os
from pathlib import Path
import claims
import redirect_sweep

# Paths resolve against THIS directory: setup ages the log at $CONTRACT_DIR/fixtures, and the
# harness must read the very file it touched, whatever name the runner gives the directory.
here = Path(__file__).resolve().parent
fixture = json.loads((here / "stall-watcher-claim.json").read_text())
for row in fixture.values():
    row["pid"] = os.getpid()
    row["log"] = str(here / row["log"])
old = claims.active_claims
claims.active_claims = lambda **_kwargs: fixture
try:
    result = redirect_sweep.sweep(stale_seconds=600, now=1_700_000_900.0)
finally:
    claims.active_claims = old
assert result["dry_run_only"] is True and result["mutates_state"] is False, result
assert result["active_claim_count"] == result["watched_count"] == result["actionable_count"] == 1, result
row = result["reports"][0]
assert row["state"] == "stalled", row
assert row["policy_decision"]["action"] == "redirect", row
print(json.dumps({"state": row["state"], "action": row["policy_decision"]["action"], "dry_run_only": result["dry_run_only"]}, sort_keys=True))
