'use strict';

// Synthetic integration evidence only: these cohorts never count toward live M2.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const source = path.resolve(__dirname, '../src');
const exercise = `
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import adjudicator_live as live
import feedback
import roles
import switch_review

root = Path(sys.argv[2])
agree_count = int(sys.argv[3])
sha = live._toy_repository(root)
now = int(time.time())
feedback._capability_daily_heartbeat = lambda *_a, **_k: None
with feedback._conn() as conn:
    for number in range(1, 21):
        conn.execute("INSERT INTO runs(run_id,ts,target) VALUES (?,?,?)",
                     (f"live-{number}", now, f"fixture/repo#{number}"))
        conn.execute("INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
                     "VALUES (?,'NON_PASS','PASS',1)", (f"live-{number}",))

calls = []
def runner(**kwargs):
    assert isinstance(kwargs["case"], roles.CollectedAdjudicationCase)
    assert kwargs["source"] == "live" and kwargs["dispatch"] is True
    calls.append(kwargs)
    return {"backend": "stub", "backend_run_id": f"stub-{len(calls)}",
            "proposal": {"decision": "reject_blocker", "rationale": "fixture"},
            "advisory_plan": {"decision": "reject_blocker"}, "errors": []}

@contextmanager
def provider(target, head):
    assert target.startswith("fixture/repo#") and head == sha
    yield str(root / "repo")

common = dict(db=feedback.DB_PATH, path=live.report_path(), now=now + 1, env={},
              limit=20, runner=runner, provider=provider,
              evidence_reader=lambda _row: live._evidence_for(sha),
              snapshot_reader=lambda target: {"number": int(target.split("#")[1]),
                  "headRefOid": sha, "body": live.AC_BODY},
              github_reader=lambda _args: {"total_count": 0, "check_runs": []})
first = live.run_live(**common)
assert first["advised"] == 20 and first["summary"]["graded"] == 0, first

def outcomes():
    with feedback._conn() as conn:
        return conn.execute("SELECT * FROM outcomes ORDER BY run_id").fetchall()

def advice_count():
    with feedback._conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM completion_events WHERE producer=? AND phase=?",
                            (live.retro.LIVE_ADVICE_PRODUCER, live.retro.LIVE_ADVICE_PHASE)
                            ).fetchone()[0]

def observe(name):
    summary = live.reconcile_live(db=feedback.DB_PATH)
    line = live.weekly_line()
    destination = root / f"{name}.txt"
    switch_review.write_text_report({"review_days": 7, "raise_count": 0,
        "held_off": [], "on_but_idle": [], "unconditioned": [],
        "mirror_drift": {"status": "ok"},
        "adjudicator_shadow": "adjudicator shadow: fixture cohort",
        "adjudicator_live": line}, destination)
    text = destination.read_text()
    shadow = [row for row in text.splitlines() if row.startswith("adjudicator shadow:")]
    assert shadow == ["adjudicator shadow: fixture cohort; " + line]
    return {"summary": summary, "line": line, "text": text}

with feedback._conn() as conn:
    for number in range(1, 20):
        conn.execute("UPDATE outcomes SET durability=?,durability_checked_ts=? WHERE run_id=?",
                     ("durable" if number <= agree_count else "broke_later",
                      now + 2, f"live-{number}"))
short = observe("short")
with feedback._conn() as conn:
    conn.execute("UPDATE outcomes SET durability='broke_later',durability_checked_ts=? "
                 "WHERE run_id='live-20'", (now + 2,))
complete = observe("complete")

# A new dispute proves that retirement stops dispatch, rather than just deduping the cohort.
with feedback._conn() as conn:
    conn.execute("INSERT INTO runs(run_id,ts,target) VALUES ('new',?,'fixture/repo#21')",
                 (now + 3,))
    conn.execute("INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
                 "VALUES ('new','NON_PASS','PASS',1)")
before = outcomes()
daily = live.run_live(**{**common, "now": now + 4})
assert outcomes() == before, "daily shadow dispatch must not alter any outcome column"

# Corrected truth keeps draining after retirement, without reviving paid dispatch.
with feedback._conn() as conn:
    conn.execute("UPDATE outcomes SET durability='durable',durability_checked_ts=? "
                 "WHERE run_id LIKE 'live-%'", (now + 5,))
before = outcomes()
corrected = observe("corrected")
assert outcomes() == before, "grading and weekly rendering must leave merge inputs unchanged"
print(json.dumps({"short": short, "complete": complete, "corrected": corrected,
                  "attempted": daily["attempted"], "retired": daily.get("retired", False),
                  "calls": len(calls), "advice_events": advice_count()}))
`;

for (const [agree, policy] of [[0, 'retired'], [14, 'retired'], [15, 'qualified']]) {
  test(`live M2 weekly consumer grades ${agree}/20 and applies ${policy}`, (t) => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-live-m2-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const files = ['stdout', 'stderr'].map((name) => path.join(root, name));
    const descriptors = files.map((name) => fs.openSync(name, 'w'));
    let result;
    try {
      result = spawnSync(process.env.PYTHON || 'python3',
        ['-I', '-B', '-c', exercise, source, root, String(agree)], {
          timeout: 60000, stdio: ['ignore', ...descriptors],
          env: { ...process.env, ORCH_STATE_DIR: path.join(root, 'state'),
            ORCH_LOCAL_RUNTIME: path.join(root, 'runtime'),
            ORCH_FEEDBACK_DB: path.join(root, 'brain.db'),
            ORCH_CAPABILITIES_PATH: path.join(root, 'capabilities.json'),
            ORCH_FLEET_ROOT: path.join(root, 'fleet') },
        });
    } finally {
      descriptors.forEach((fd) => fs.closeSync(fd));
    }
    assert.ifError(result.error);
    assert.equal(result.status, 0, fs.readFileSync(files[1], 'utf8'));
    const observed = JSON.parse(fs.readFileSync(files[0], 'utf8'));
    assert.equal(observed.short.summary.graded, 19);
    assert.match(observed.short.line, /qualification insufficient_sample;/);
    assert.equal(observed.complete.summary.graded, 20);
    assert.equal(observed.complete.summary.agree, agree);
    assert.match(observed.complete.line, new RegExp(
      `live: disputes 20, advised 20, uncertified 0, graded 20 \\(agree ${agree}, disagree ${20 - agree}\\)`));
    assert.match(observed.complete.line, new RegExp(
      `agreement ${(agree / 20).toFixed(3)} against 0\\.75; qualification ${policy};`));
    const retired = policy === 'retired';
    assert.equal(observed.retired, retired);
    assert.equal(observed.attempted, retired ? 0 : 1);
    assert.equal(observed.calls, retired ? 20 : 21);
    assert.equal(observed.advice_events, retired ? 20 : 21);
    assert.equal(observed.corrected.summary.graded, 20);
    assert.equal(observed.corrected.summary.agree, 20);
    assert.match(observed.corrected.line, new RegExp(`qualification ${policy};`));
  });
}
