# Source425 follow-up: preserve unknown eligible counts

Merged473 comparison returned CONCERNS/CONCERNS, retained as NON_PASS.
The provider packet truncated the historical floor note and actual mutation
transcripts. Current source also returned candidate_counts.eligible=0 even when
passing_screen=None for missing, unreadable or stale populations. This follow-up
keeps None in the structured count and prints eligible UNKNOWN; a measured empty
population still returns and prints zero.

The existing production screen regression reproduced the defect before the fix:

```text
F                                                                        [100%]
=================================== FAILURES ===================================
_________ test_an_unknown_population_is_none_and_an_empty_one_is_zero __________

stores = {'tmp': PosixPath('/private/var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/pytest-of-teacher/pytest-7576/test_an_unkn...2p00000gp/T/pytest-of-teacher/pytest-7576/test_an_unknown_population_is_0/keepalive-supervisor-stage2-plan.json'), ...}

    def test_an_unknown_population_is_none_and_an_empty_one_is_zero(stores):
        current = {**LANE, "target": "o/r#25", "state": "stalled", "recommended_action": "inspect"}
        judge = Judge("inspect")
    
        missing = _run(stores, judge, plan_path=stores["tmp"] / "absent.json")
        assert missing["passing_screen"] is None and missing["population"]["status"] == "missing"
>       assert missing["candidate_counts"]["eligible"] is None
E       assert 0 is None

tests/test_redirect_apply_prescreen.py:399: AssertionError
=========================== short test summary info ============================
FAILED tests/test_redirect_apply_prescreen.py::test_an_unknown_population_is_none_and_an_empty_one_is_zero
1 failed in 0.49s

```

After restoration on current main3bb39d2:

```text
..........................                                               [100%]
26 passed in 1.38s

```

The test covers missing, malformed, stale and measured-empty plans using private
files. It asserts both the structured population and the real formatter output.
No supervisor, corpus, live claim, shadow/default flag, or outcome is mutated.
Historical floor-note bytes are untouched; prior full source is retained in
merged473, and this follow-up's changed-code packet remains bounded.

Replay: python3 -m pytest tests/test_redirect_apply_prescreen.py
 tests/test_redirect_chain_candidates.py tests/test_redirect_claim_revalidation.py
 -q -o addopts=

Source425 stays open until exact-head checks/reviews/floor and postmerge verifier
are handled. Private current-source evidence does not establish live deployment.

## Current production controls

Replay `python3 scripts/verify_redirect_chain_controls.py`. It mutates only private
module copies and refuses import failures or absent mutation anchors as proof.

```json
[
  {
    "module": "keepalive_shadow.py",
    "test": "test_an_escalated_pr_is_not_live_and_is_eligible",
    "source_sha256": "bcb992b0fcaa4b12fdc1f035a517a5af2833131f8b365560e624ef70f96cee03",
    "runs": [
      {
        "phase": "baseline",
        "exit": 0,
        "output": ".                                                                        [100%]\n1 passed in 0.32s\n"
      },
      {
        "phase": "broken",
        "exit": 1,
        "output": "F                                                                        [100%]\n=================================== FAILURES ===================================\n_______________ test_an_escalated_pr_is_not_live_and_is_eligible _______________\n\ntmp_path = PosixPath('/private/var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/pytest-of-teacher/pytest-7585/test_an_escalated_pr_is_not_li0')\n\n    def test_an_escalated_pr_is_not_live_and_is_eligible(tmp_path):\n        with _stores(tmp_path):\n            for labels, payload, details in [\n                ([\"needs-human\"], {}, [\"needs-human\"]),\n                ([\" Agent:Needs-Attention \"], {}, [\"agent:needs-attention\"]),\n                (\n                    [\"needs-human\", \"agent:needs-attention\"],\n                    {},\n                    [\"needs-human\", \"agent:needs-attention\"],\n                ),\n                (\n                    [],\n                    {\"attention\": {\"disposition\": \"needs-human\"}},\n                    [\"keepalive-state attention.disposition=needs-human\"],\n                ),\n                (\n                    [],\n                    {\"attention\": {\"disposition\": \"challenge-due\"}},\n                    [\"keepalive-state attention.disposition=challenge-due\"],\n                ),\n            ]:\n                signals = keepalive_shadow.normalize_signals(\n                    \"o/r#1\",\n                    {**payload, \"last_files_changed\": 2, \"rounds_without_task_completion\": 2},\n                    pr_state=\"open\",\n                    labels=[*labels, \"agents:keepalive\"],\n                )\n                assert signals[\"outcome\"] == \"needs_human\"\n                report = keepalive_shadow.synthesize_report(signals)\n>               assert report[\"state\"] == \"escalated\"\nE               AssertionError: assert 'running' == 'escalated'\nE                 \nE                 - escalated\nE                 + running\n\ntests/test_redirect_chain_candidates.py:91: AssertionError\n=========================== short test summary info ============================\nFAILED tests/test_redirect_chain_candidates.py::test_an_escalated_pr_is_not_live_and_is_eligible\n1 failed in 0.24s\n"
      },
      {
        "phase": "restored",
        "exit": 0,
        "output": ".                                                                        [100%]\n1 passed in 0.19s\n"
      }
    ]
  },
  {
    "module": "redirect_apply.py",
    "test": "test_a_sweep_stall_proposal_reaches_the_apply_candidate_list",
    "source_sha256": "2f7fb071d8b308f411e28a9bcb915d44904b43d9ab71c3c40691b6076eb83792",
    "runs": [
      {
        "phase": "baseline",
        "exit": 0,
        "output": ".                                                                        [100%]\n1 passed in 0.19s\n"
      },
      {
        "phase": "broken",
        "exit": 1,
        "output": "F                                                                        [100%]\n=================================== FAILURES ===================================\n_________ test_a_sweep_stall_proposal_reaches_the_apply_candidate_list _________\n\ntmp_path = PosixPath('/private/var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/pytest-of-teacher/pytest-7588/test_a_sweep_stall_proposal_re0')\n\n    def test_a_sweep_stall_proposal_reaches_the_apply_candidate_list(tmp_path):\n        with _stores(tmp_path) as stores:\n            stalled = _report(\"stranske/Orchestrator#999\")\n            supervisor = _report(\"o/r#1\", \"escalated\")\n            plan = {\n                \"generated_at\": stores[\"now\"],\n                \"plans\": [{\"eligible\": True, \"report\": supervisor, \"acceptance_criteria\": \"AC\"}] * 2,\n            }\n            sweep = {\n                \"generated_at\": stores[\"now\"],\n                \"actionable\": [stalled, stalled, _report(\"o/r#1\"), _report(\"o/r#2\", \"running\")],\n            }\n            _write(stores[\"plan\"], plan)\n            _write(stores[\"sweep\"], sweep)\n            kwargs = {\n                \"report_dir\": stores[\"reports\"],\n                \"plan_path\": stores[\"plan\"],\n                \"corpus_path\": stores[\"corpus\"],\n                \"sweep_path\": stores[\"sweep\"],\n                \"now\": stores[\"now\"],\n            }\n            screen = ra._screen(**kwargs)\n>           assert [row[\"target\"] for row in screen[\"rows\"]] == [\"o/r#1\", stalled[\"target\"]]\nE           AssertionError: assert ['o/r#1'] == ['o/r#1', 'st...estrator#999']\nE             \nE             Right contains one more item: 'stranske/Orchestrator#999'\nE             Use -v to get more diff\n\ntests/test_redirect_chain_candidates.py:147: AssertionError\n=========================== short test summary info ============================\nFAILED tests/test_redirect_chain_candidates.py::test_a_sweep_stall_proposal_reaches_the_apply_candidate_list\n1 failed in 0.24s\n"
      },
      {
        "phase": "restored",
        "exit": 0,
        "output": ".                                                                        [100%]\n1 passed in 0.19s\n"
      }
    ]
  }
]

```
