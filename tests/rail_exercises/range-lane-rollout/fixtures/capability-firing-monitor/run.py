
import json, sys
from pathlib import Path
import capability_firing_monitor as m
p=Path(__file__).parent; ledger=json.load((p/'ledger.json').open()); now=2000000000
if sys.argv[1]=='break': ledger['fixture-silent']['last_invocation']=now-86400
# Patch the loader only for the controlled ledger and history read only; never record.
m.capabilities.load=lambda path: ledger; m.HISTORY=p/'history.json'
rep=m.review(now=now,path=p/'ledger.json')
ids=[x['capability_id'] for x in rep['overdue']]
ok=ids==['fixture-silent'] and (p/'history.json').exists() is False
print(json.dumps({'status':'PASS' if ok else 'FAIL','overdue':rep['overdue'],'history_exists':(p/'history.json').exists()},indent=2))
