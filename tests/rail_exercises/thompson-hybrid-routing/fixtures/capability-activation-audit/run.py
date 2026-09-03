
import json, sys
from pathlib import Path
import capability_activation_audit as m
p=Path(__file__).parent; ledger=json.load((p/'ledger.json').open()); m.HERE=p/'tree'; m.sibling_checkouts=lambda: []
cap=dict(ledger['fixture-activation'])
q=None
if sys.argv[1]=='break':
    cap['entrypoint']='present_fixture_module.py:main'
    q=m.HERE/'present_fixture_module.py'; q.write_text('def main(): pass\n')
rep=m.entrypoint_presence(cap)
if q is not None: q.unlink()
ok=rep['state']=='absent_here' and [x['candidates'][0] for x in rep['missing']]==['missing_fixture_module.py'] and 'missing_fixture_module.py is not in this tree' in rep['detail']
print(json.dumps({'status':'PASS' if ok else 'FAIL','entrypoint_presence':rep,'fixture_ledger':str(p/'ledger.json')},indent=2))
