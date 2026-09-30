
import json, sys
from pathlib import Path
import switch_review as m
p=Path(__file__).parent; now=2000000000
ledger=json.load((p/'ledger.json').open())
if sys.argv[1]=='break': ledger['range-lane-rollout']['last_invocation']=now-86400
# Patch the READ-ONLY loader review() calls, for the controlled ledger only, and every other
# input the review reads from the machine. Each ledger read is logged: a patch review() bypassed
# would leave this run judging some other ledger, so it stops unless every read was this fixture.
calls=[]; m.capabilities.load_declared=lambda path: calls.append(str(path)) or ledger
m.stale_runners=lambda: []; m.mirror_drift=lambda: {'status':'ok'}
m.fleet_gates=lambda **_: {'suspect':False}; m._exploration_gate=lambda: {'suspect':False}
rep=m.review(now=now,env={'ORCH_RANGE_LANE_ROLLOUT':'1'},path=p/'ledger.json')
if not calls or set(calls)!={str(p/'ledger.json')}: raise SystemExit('review() did not read this fixture through the patch: %r' % calls)
# The requested SUSPECT/value/drainable classification is absent from the real report.
print(json.dumps({'status':'FAIL','source_on_but_idle':rep['on_but_idle'],'loader_calls':calls,'missing_contract_fields':['SUSPECT','gate_value','drainable']},indent=2))
