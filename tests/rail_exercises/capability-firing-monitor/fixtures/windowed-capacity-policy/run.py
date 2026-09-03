
import json, sys
from pathlib import Path
import capacity as m
p=Path(__file__).parent; fx=json.load((p/'fixture.json').open()); m.LEDGER=p/'ledger.ndjson'; m._auth_health=lambda a: None; m._unresolvable_tier=lambda a: None
if sys.argv[1]=='break': fx['ccusage']['projection']['totalTokens']=50
m._shed=lambda a: a=='fixture_429'
spent=m.compute('fixture_spent',{'model':'ccusage','block_token_limit':100},fx['ccusage'])[0]
fresh=m.compute('fixture_fresh',{'model':'ccusage','block_token_limit':100},fx['fresh_ccusage'])[0]
flag=m.compute('fixture_429',{'model':'ccusage','block_token_limit':100},fx['ccusage'])[0]
count=m.compute('fixture_count',{'model':'count','window':'24h','limit':2},fx['ccusage'])[0]
ok=(spent,fresh,flag,count)==('shed','ok','shed','shed')
print(json.dumps({'status':'PASS' if ok else 'FAIL','states':{'spent':spent,'fresh':fresh,'flag':flag,'ledger_count':count}},indent=2))
