# INERT_QUALIFICATION_FIXTURE: never resolve or invoke any other program.
import json
import os
import sys
import time
from pathlib import Path

mode = Path(__file__).name
prompt = sys.stdin.buffer.read()
if mode == "timeout":
    time.sleep(5)
if mode == "descendant":
    if os.fork() == 0:
        time.sleep(5)
        os._exit(0)
if mode == "mixedcap":
    sys.stdout.buffer.write(b"x" * 600)
    sys.stderr.buffer.write(b"y" * 600)
    raise SystemExit(0)
if mode == "oversize":
    sys.stdout.buffer.write(b"x" * 10000)
    sys.stderr.buffer.write(b"y" * 10000)
    raise SystemExit(0)
if mode == "nonzero":
    sys.stderr.write("fixture failed")
    raise SystemExit(3)
if mode == "restriction":
    sys.stderr.write("subscription required")
if mode == "secret":
    sys.stderr.write(os.environ["CLAUDE_CODE_OAUTH_TOKEN"] + " sk-ant-SECRET_SYNTHETIC_123456")
if mode == "malformed":
    print("{}\n{}")
    raise SystemExit(0)
p = json.loads(prompt)
final = {k: v for k, v in p["expected"].items() if k != "needs_required"}
if p["role"] == "strong_criterion_adjudication":
    final["reason"] = "Synthetic control only"
elif final["classification"] == "uncertain":
    final["substantive_needs"] = ["Synthetic missing evidence"]
else:
    final["substantive_needs"] = []
if mode == "wrongcriterion":
    final["criterion_id"] = "C2"
model = "claude-sonnet-4-6" if mode == "wrongmodel" else "claude-sonnet-5-5"
usage = {
    "inputTokens": 10,
    "outputTokens": 20,
    "cacheReadInputTokens": 0,
    "cacheCreationInputTokens": 0,
    "costUSD": 0.01,
}
row = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "terminal_reason": "completed",
    "stop_reason": "end_turn",
    "num_turns": 1,
    "result": json.dumps(final),
    "session_id": f"fixture-{os.getpid()}",
    "total_cost_usd": 0.01,
    "usage": {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    },
    "modelUsage": {model: usage},
}
if mode == "resumed":
    row["resumed"] = True
if mode == "multiturn":
    row["num_turns"] = 2
if mode == "toolactivity":
    row["permission_denials"] = [{"tool_name": "shell"}]
print(json.dumps(row))
# Instrumentation on stderr verifies what actually reached this inert child.
print(
    json.dumps(
        {
            "fixture_env": dict(os.environ),
            "fixture_argv": sys.argv,
            "fixture_prompt": prompt.decode(),
            "fixture_cwd": str(Path.cwd()),
        }
    ),
    file=sys.stderr,
)
