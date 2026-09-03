import redirect_apply

plan = {
    "action": "redirect",
    "target": "fixture/repo#9",
    "prompt_text": "fixture prompt",
    "prompt_file": "exercises/fixtures/never-written.md",
    "accepted_role_run_id": "role:redirect:fixture:1",
    "steps": [{"id": "delegate-retry", "commands": [["python3", "dispatcher.py", "delegate", "--influenced-by-role-run-id", "role:redirect:fixture:1"]]}],
}
result = redirect_apply.authorize(
    plan_obj=plan, role_run_id="role:redirect:fixture:1", decision_source="redirect_agent",
    errors=[], pid_alive=True, claim_holder=None, prior_agent="fixture-agent",
    gate={"bootstrap_needed": True, "disagreements_needed": 3}, applied_targets=set(),
    applies_today=0, flag_on=True,
)
assert result["allowed"] is False, result
assert "prior process is still alive — apply never kills a live lane" in result["blocks"], result
print("REFUSED_LIVE_PID")
