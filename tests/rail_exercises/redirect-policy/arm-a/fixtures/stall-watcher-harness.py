import json
import os
from pathlib import Path
import claims
import redirect_sweep

root = Path(__file__).resolve().parent.parent.parent
fixture = json.loads((root / "exercises/fixtures/stall-watcher-claim.json").read_text())
for row in fixture.values():
    row["pid"] = os.getpid()
    row["log"] = str(root / row["log"])
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
