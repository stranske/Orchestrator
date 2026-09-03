
import json, random, sys
from pathlib import Path
import router
p=Path(__file__).parent; fixture=json.load((p/'weights.json').open()); weights=fixture['weights']
if sys.argv[1]=='break': weights={'challenger_a':weights['challenger_b'],'challenger_b':weights['challenger_a']}
scored=[((0,0,0,0,0),{'agent':'winner'},'ok',0),((0,0,0,0,0),{'agent':'challenger_a'},'ok',1),((0,0,0,0,0),{'agent':'challenger_b'},'ok',2)]
rng=random.Random(fixture['seed']); wins={'challenger_a':0,'challenger_b':0}
for _ in range(fixture['draws']): wins[router._thompson_exploration_choice(scored,weights,rng)[1]['agent']]+=1
rate=wins['challenger_a']/fixture['draws']; ok=fixture['lower']<=rate<=fixture['upper']
print(json.dumps({'status':'PASS' if ok else 'FAIL','wins':wins,'rate_a':rate,'tolerance':[fixture['lower'],fixture['upper']],'draws':fixture['draws']},indent=2))
