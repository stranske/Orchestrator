"""capability:reference-sync-hygiene-test-gate exercise: run_reference_workflow against a TEMP ledger.
pass: the shadow rail compiles, dry-runs, is consumed, and the temp ledger carries match/invocation/output/consumer/success/outcome for it.
break: a consumer that rejects the output must leave NO success event and must surface the failure.
usage: check.py pass|break"""
import json, sys, tempfile, pathlib
sys.path.insert(0, "src")
import capability_compiler as cc  # noqa: E402
mode = sys.argv[1]
with tempfile.TemporaryDirectory() as td:
    ledger = pathlib.Path(td) / "capabilities.json"
    if mode == "pass":
        out = cc.run_reference_workflow(ledger_path=ledger)
        rows = json.load(open(ledger))["capabilities"]
        row = rows["capability:reference-sync-hygiene-test-gate"]
        types = {e.get("type") for e in row.get("event_history") or []}
        need = {"match", "invocation", "output", "consumer", "success", "outcome"}
        assert need <= types, ("missing shadow events", sorted(need - types))
        print("PASS refsync: shadow rail ran; events", sorted(need), "| result keys", sorted(out.keys())[:6])
    else:
        def bad_consumer(result):
            raise ValueError("deliberate: hygiene violation planted by the exercise")
        try:
            out = cc.run_reference_workflow(ledger_path=ledger, consumer=bad_consumer)
            surfaced = "error" in json.dumps(out).lower() or "fail" in json.dumps(out).lower()
        except ValueError as exc:
            out, surfaced = {"raised": str(exc)}, True
        rows = json.load(open(ledger))["capabilities"]
        types = {e.get("type") for e in rows["capability:reference-sync-hygiene-test-gate"].get("event_history") or []}
        assert "success" not in types, ("a rejected output still recorded success", sorted(types))
        assert surfaced, ("the rejection was swallowed", out)
        print("EXPECTED_FAILURE_REFUSED: rejected output recorded no success; surfaced:", json.dumps(out)[:160])
