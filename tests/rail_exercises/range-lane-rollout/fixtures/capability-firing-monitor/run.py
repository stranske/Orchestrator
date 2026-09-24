
import json, sys
from pathlib import Path
import capability_firing_monitor as m
p=Path(__file__).parent; ledger=json.load((p/'ledger.json').open()); now=2000000000
if sys.argv[1]=='break': ledger['fixture-silent']['last_invocation']=now-86400
# Patch the READ-ONLY loader review() calls, for the controlled ledger and history read only;
# never record. Each read is logged: a patch review() bypassed would leave this run judging
# some other ledger, so it passes only if review() read this fixture through the patch, once.
calls=[]; m.capabilities.load_declared=lambda path: calls.append(str(path)) or ledger; m.HISTORY=p/'history.json'
rep=m.review(now=now,path=p/'ledger.json')
ids=[x['capability_id'] for x in rep['overdue']]
ok=ids==['fixture-silent'] and (p/'history.json').exists() is False and calls==[str(p/'ledger.json')]
print(json.dumps({'status':'PASS' if ok else 'FAIL','overdue':rep['overdue'],'history_exists':(p/'history.json').exists(),'loader_calls':calls},indent=2))
