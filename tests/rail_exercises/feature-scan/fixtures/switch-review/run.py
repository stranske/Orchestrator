
import json, sys
from pathlib import Path
import switch_review as m
p=Path(__file__).parent; now=2000000000
ledger=json.load((p/'ledger.json').open())
if sys.argv[1]=='break': ledger['range-lane-rollout']['last_invocation']=now-86400
m.capabilities.load=lambda path: ledger; m.stale_runners=lambda: []; m.mirror_drift=lambda: {'status':'ok'}
rep=m.review(now=now,env={'ORCH_RANGE_LANE_ROLLOUT':'1'},path=p/'ledger.json')
# The requested SUSPECT/value/drainable classification is absent from the real report.
print(json.dumps({'status':'FAIL','source_on_but_idle':rep['on_but_idle'],'missing_contract_fields':['SUSPECT','gate_value','drainable']},indent=2))
