"""A credential an agent prints does not leave its run through what the Orchestrator writes
(2026-10-05).

THE FINDING. While gh was broken for dispatched agents (stranske/Orchestrator#461), agents fetched a
GitHub token another way and printed it. `dispatcher.offload` wrote a vibe offload's stdout, token
included, to a world-readable dispatch log and returned it to the caller; two experiment arms
printed it into logs the detached wrapper fills straight from the agent's stdout. Measured by hash
on 2026-10-05, the value never printed: those were the 4 copies this tool wrote, beside 58 in the
agent CLIs' own transcripts, which it never sees.

THE RULE. `credential_redaction` holds one shape list. `offload` masks before it logs or returns;
the completion step masks the run's own log segment in place; the experiment and UX-review panels
mask their output file before parsing it into the Brain. A mask is the same length as what it
hides, so an in-place edit moves no byte another writer may be appending after. `exposure_report`
(FYI, weekly in switch_review) counts the files holding a known secret's exact value or a
GitHub-minted token, and never prints either.

EVERY TOKEN-SHAPED STRING HERE IS BUILT AT RUN TIME from parts, so the source holds no literal that
secret scanning or push protection could match.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import zlib
from pathlib import Path

import pytest
from test_experiment_arm_identity import _v2_meta
from test_offload_failure_evidence import HEAD, _command, _event, isolated_offload

import adapters
import credential_redaction as cr
import dispatcher
import exp_abcd
import feedback
import ledger_reconcile
import rate_incidents
import switch_review
import ux_review

_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def minted(kind: str = "o", seed: str = "Ab3") -> str:
    """A classic GitHub token whose own CRC32/base62 checksum holds, as only GitHub mints them."""
    entropy = (seed * 30)[:30]
    n, digits = zlib.crc32(entropy.encode()), ""
    while n:
        n, r = divmod(n, 62)
        digits = _B62[r] + digits
    return "gh" + kind + "_" + entropy + digits.rjust(6, "0")


# A string with a token's shape that GitHub never minted: what test fixtures across the fleet type.
FIXTURE = "gh" + "p_" + "A" * 36


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No test reads this machine's real secret files, and redaction is on unless a test says so."""
    monkeypatch.setenv(cr.SECRET_FILES_ENV, "")
    monkeypatch.delenv(cr.REDACTION_DISABLED_ENV, raising=False)
    monkeypatch.delenv(cr.EXPOSURE_SCAN_ENV, raising=False)


def _samples() -> list[tuple[str, str, str, str]]:
    """(id, text, the credential inside it, its kind)."""
    key_line = "b3BlbnNzaC1rZXktdjEAAAAA" * 2
    private = (
        "-----BEGIN " + "OPENSSH PRI" + "VATE KEY-----\n" + key_line + "\n" + key_line + "\n"
        "-----END " + "OPENSSH PRI" + "VATE KEY-----"
    )
    raw = [
        ("github-classic", "{}", minted("o"), "github-token"),
        ("github-fine-grained", "{}", "github" + "_pat_" + "11ABCDEFG0" + "x" * 50, "github-token"),
        ("anthropic-oauth", "{}", "s" + "k-ant-oat01-" + "Q" * 40, "api-key"),
        ("slack", "{}", "xo" + "xb-" + "1234567890-" + "abcdefghijklmn", "slack-token"),
        ("google", "{}", "AI" + "za" + "S" * 35, "google-api-key"),
        ("aws-key-id", "{}", "AK" + "IA" + "ABCDEFGHIJKLMNOP", "aws-key-id"),
        ("langsmith", "{}", "lsv" + "2_pt_" + "c" * 32, "langsmith-key"),
        (
            "jwt",
            "{}",
            "ey" + "JhbGciOiJIUzI1" + ".ey" + "JzdWIiOiIxMjM0" + "." + "SflKxwRJSMeKKF2QT4",
            "jwt",
        ),
        ("bearer", "Authorization: Bearer {}", "t0k" * 8, "bearer"),
        ("basic", "Authorization: Basic {}", "eC1hY2Nlc3MtdG9r" + "ZW46cGFzc3dvcmQ=", "basic-auth"),
        (
            "url-password",
            "https://x-access-token:{}@github.com/o/r.git",
            "pa55word" * 2,
            "url-password",
        ),
        ("env-assignment", "MISTRAL_API_KEY={}", "a1b2" * 8, "secret-env"),
        ("private-key", "{}", private, "private-key"),
    ]
    return [
        (i, f"before {text.format(secret)} after", secret, kind) for i, text, secret, kind in raw
    ]


SAMPLES = _samples()


@pytest.mark.parametrize("text,secret,kind", [s[1:] for s in SAMPLES], ids=[s[0] for s in SAMPLES])
def test_every_shape_is_masked_at_its_own_length(text, secret, kind):
    masked, counts = cr.redact_with_counts(text)
    assert secret not in masked, masked
    assert counts == {kind: 1}, counts
    assert len(masked) == len(text), "a mask is exactly as long as what it hides"
    assert masked.startswith("before ") and masked.endswith(" after"), masked
    assert masked.count("\n") == text.count("\n"), "line breaks survive a mask"
    assert cr.redact_with_counts(masked) == (masked, {}), "masking twice changes nothing"


def test_the_context_around_a_credential_is_kept():
    for prefix in ("Authorization: Bearer ", "MISTRAL_API_KEY=", "https://x-access-token:"):
        sample = next(s for s in SAMPLES if s[1].startswith("before " + prefix))
        assert cr.redact(sample[1]).startswith("before " + prefix), sample[0]


def test_text_and_bytes_paths_agree_byte_for_byte():
    """One shape list, compiled twice: an offload's text and a log file's bytes mask alike."""
    text = " é ".join(s[1] for s in SAMPLES) + " ü " + FIXTURE
    masked_bytes, counts, _spans = cr.redact_bytes(text.encode("utf-8"))
    assert cr.redact(text).encode("utf-8") == masked_bytes
    assert sum(counts.values()) == len(SAMPLES) + 1


def test_a_token_after_a_json_escaped_newline_is_masked():
    """Found validating against the live trees: in a JSON transcript a token printed on a new line
    follows a literal backslash-n, so the character before it is `n` and a not-after-a-letter
    guard missed it. Codex's `exec --json` stream holds every command's output exactly so."""
    token = minted("o")
    line = json.dumps(
        {"type": "item.completed", "item": {"aggregated_output": f"keychain item:\n{token}\n"}}
    )
    assert (
        re.search(r"(?<![A-Za-z0-9])" + re.escape(token), line) is None
    ), "the fixture must hold the shape the old guard missed"
    masked = cr.redact(line)
    assert token not in masked
    assert json.loads(masked)["item"]["aggregated_output"].startswith("keychain item:\n[REDACTED")


@pytest.mark.parametrize(
    "text",
    [
        "max_tokens=4096 and input_tokens: 123",
        "API_KEY = os.environ['MISTRAL_API_KEY']",
        "GH_TOKEN=$GITHUB_TOKEN gh pr list",
        "task-runner-abcdefghijklmnopqrstuvwxyz",
        "===== 3 passed in 0.12s =====",
        "run_id=offload:codex:1791244800000000000",
        "commit 4f952e3a1b2c3d4e5f60718293a4b5c6d7e8f901",
        "codex resume 019f4789-93cb-7ca3-9ed7-5554d8a9b912",
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}',
        "Bearer tokens expire hourly",
        "https://github.com/stranske/Orchestrator/pull/461",
    ],
)
def test_ordinary_output_is_untouched(text):
    assert cr.redact_with_counts(text) == (text, {})


def test_a_known_secret_with_no_shape_is_masked_by_its_exact_value(monkeypatch, tmp_path):
    value = "Zq7" * 8  # no credential shape: only its exact value can find it
    secret = tmp_path / "secret"
    secret.write_text(value + "\n")
    monkeypatch.setenv(cr.SECRET_FILES_ENV, str(secret))
    masked, counts = cr.redact_with_counts(f"saw {value} twice: {value}")
    assert value not in masked and counts == {"known-secret": 2}, (masked, counts)


@pytest.mark.parametrize("content", ["short\n", "line one is long enough\nline two too\n"])
def test_a_secret_file_that_could_mask_ordinary_text_masks_nothing(monkeypatch, tmp_path, content):
    secret = tmp_path / "secret"
    secret.write_text(content)
    monkeypatch.setenv(cr.SECRET_FILES_ENV, str(secret))
    assert cr.redact_with_counts("short line one is long enough") == (
        "short line one is long enough",
        {},
    )


def test_the_kill_switch_returns_text_unmasked(monkeypatch):
    token = minted("o")
    monkeypatch.setenv(cr.REDACTION_DISABLED_ENV, "1")
    assert cr.redact_with_counts(token) == (token, {})


def test_githubs_checksum_separates_minted_tokens_from_fixtures():
    token = minted("o")
    assert cr.github_checksum_valid(token)
    assert not cr.github_checksum_valid(FIXTURE)
    flipped = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert not cr.github_checksum_valid(flipped)


# ---- the in-place scrub ------------------------------------------------------------------------


def _header(minute: int, run_id: str) -> str:
    return (
        f"=== 2026-10-06T00:{minute:02d}:00Z dispatch codex/full -> o/r#9 [implement] "
        f"cwd=/w run_id={run_id} ===\n"
    )


def test_the_scrub_never_touches_what_was_written_before_this_run(tmp_path):
    token, other = minted("o"), minted("s", "Zx9")
    earlier = _header(1, "run-1") + f"old {token}\n"
    mine = _header(2, "run-2")
    # pytest prints `=====` banners, and an agent that prints a run log prints whole headers.
    # Neither ends the scrub, which runs to the end of the file.
    body = (
        f"===== test session starts =====\nprinted {token}\n"
        + _header(9, "a-run-the-agent-printed")
        + f"and {other}\n"
    )
    later = _header(3, "run-3") + f"later {token}\n"
    log = tmp_path / "o__r_9.codex.log"
    log.write_text(earlier + mine + body + later)
    inode = log.stat().st_ino
    result = cr.scrub_file(log, run_id="run-2")
    text = log.read_text()
    assert result["status"] == "scrubbed" and result["kinds"] == {"github-token": 3}, result
    assert text.startswith(earlier + mine), "an earlier run's bytes are never edited"
    assert token not in text[len(earlier) :] and other not in text
    assert len(text) == len(earlier + mine + body + later) and log.stat().st_ino == inode


def test_a_header_glued_to_the_previous_runs_last_line_is_still_found(tmp_path):
    """A run whose output ended without a newline leaves the next header mid-line."""
    token = minted("o")
    earlier = _header(1, "run-1") + f"old {token} and no newline at the end"
    log = tmp_path / "run.log"
    log.write_text(earlier + _header(2, "run-2") + f"printed {token}\n")
    assert cr.scrub_file(log, run_id="run-2")["redacted"] == 1
    text = log.read_text()
    assert text.startswith(earlier) and text.count(token) == 1, "only the earlier copy remains"


def test_a_run_that_prints_its_own_header_is_masked_from_the_first(tmp_path):
    """The FIRST header naming the run is its own; a later copy is the run echoing its log."""
    token = minted("o")
    log = tmp_path / "run.log"
    log.write_text(_header(2, "run-2") + f"a {token}\n" + _header(2, "run-2") + f"b {token}\n")
    assert cr.scrub_file(log, run_id="run-2")["redacted"] == 2
    assert token not in log.read_text()


def test_a_writer_still_appending_loses_nothing(tmp_path):
    """The detached wrapper holds the log open for append while its completion step scrubs it."""
    token = minted("o")
    log = tmp_path / "run.log"
    log.write_text(_header(2, "run-2"))
    with log.open("a") as held:
        held.write(f"printed {token}\n")
        held.flush()
        assert cr.scrub_file(log, run_id="run-2")["redacted"] == 1
        held.write("after the scrub\n")
    text = log.read_text()
    assert token not in text and text.endswith(
        "printed [REDACTED:github-token]*****************\nafter the scrub\n"
    ), text


def test_every_scrub_outcome_is_named(tmp_path, monkeypatch):
    token = minted("o")
    assert cr.scrub_file(tmp_path / "absent.log", run_id="x")["status"] == "missing"
    log = tmp_path / "run.log"
    log.write_text(f"no header here {token}\n")
    assert cr.scrub_file(log, run_id="x")["status"] == "no_segment"
    assert token in log.read_text(), "a file holding no header for the run is left alone"
    assert cr.scrub_file(log)["status"] == "scrubbed", "a file written for one run is masked whole"
    log.write_text(_header(1, "x") + token)
    monkeypatch.setenv(cr.REDACTION_DISABLED_ENV, "1")
    before = log.read_bytes()
    assert cr.scrub_file(log, run_id="x")["status"] == "disabled" and log.read_bytes() == before


# ---- dispatcher.offload ------------------------------------------------------------------------


def _offload(monkeypatch, stores, agent, stdout, stderr="", returncode=0):
    monkeypatch.setattr(
        dispatcher.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["agent"], returncode, stdout, stderr),
    )
    return dispatcher.offload(agent, "test", cwd=str(stores))


def test_offload_masks_what_it_logs_and_returns(monkeypatch, tmp_path):
    stores = isolated_offload(tmp_path, monkeypatch)
    rows: list[dict] = []
    monkeypatch.setattr(dispatcher.adapters, "record_ledger", lambda agent, **row: rows.append(row))
    token, bearer = minted("o"), "t0k" * 8
    out = _offload(
        monkeypatch,
        stores,
        "vibe",
        f"Extracted GitHub token from macOS keychain: {token}\nDone.\n",
        f"curl -H 'Authorization: Bearer {bearer}'\n",
    )
    log = Path(out["log"]).read_text()
    assert token not in json.dumps(out) + log and bearer not in json.dumps(out) + log
    assert out["exit"] == 0 and out["credentials_redacted"] == 2, out
    assert "credentials masked before this output was logged or returned: 2 (bearer x1, " in log
    assert [r.get("credentials_redacted") for r in rows if r.get("event") == "complete"] == [2]


def test_a_codex_stream_keeps_its_verdict_once_masked(monkeypatch, tmp_path):
    stores = isolated_offload(tmp_path, monkeypatch)
    token = minted("o")
    stdout = "\n".join(
        [
            *HEAD,
            *_command("security find-generic-password -s gh:github.com -w", f"item\n{token}\n"),
            _event("item.completed", "agent_message", text="Report staged."),
            _event("turn.completed", usage={"input_tokens": 1, "output_tokens": 1}),
        ]
    )
    out = _offload(monkeypatch, stores, "codex", stdout)
    assert out["exit"] == 0 and "error" not in out, out
    assert token not in out["output"] + Path(out["log"]).read_text()
    assert all(json.loads(line) for line in out["output"].splitlines()), "every event still parses"


def test_with_the_kill_switch_offload_says_its_output_is_unmasked(monkeypatch, tmp_path):
    stores = isolated_offload(tmp_path, monkeypatch)
    monkeypatch.setenv(cr.REDACTION_DISABLED_ENV, "1")
    token = minted("o")
    out = _offload(monkeypatch, stores, "vibe", f"token {token}\n")
    assert token in out["output"] and out["credential_redaction"] == "disabled"
    assert "credential redaction DISABLED by" in Path(out["log"]).read_text()


def test_the_per_run_agy_log_is_scrubbed_once_the_run_is_over(monkeypatch, tmp_path):
    """agy writes this file itself, at the path offload chose; offload masks it whole after."""
    stores = isolated_offload(tmp_path, monkeypatch)
    monkeypatch.setattr(
        dispatcher.adapters,
        "build_command",
        lambda agent, *a, **k: ["agent", "--log-file", "shared.log"],
    )
    token = minted("o")

    def fake_run(argv, **_kw):
        named = re.search(r"--log-file (\S+)", argv[-1]).group(1)
        Path(shlex.split(named)[0]).write_text(f"[agy] tool output {token}\n")
        return subprocess.CompletedProcess(["agent"], 0, "Summary: one function, no tests.", "")

    monkeypatch.setattr(dispatcher.subprocess, "run", fake_run)
    out = dispatcher.offload("gemini", "test", cwd=str(stores))
    agy = adapters.agy_log_for(out["log"])
    assert agy is not None and agy.exists() and token not in agy.read_text()
    assert out["credentials_redacted"] == 1, out


def test_the_agy_log_tail_is_masked_whole_before_it_is_cut(tmp_path):
    token = minted("o")
    log = tmp_path / "run.agy.log"
    log.write_text(token + "\n" + "y" * 2389)  # the 2400-character tail starts inside the token
    tail = dispatcher._agent_log_tail_from_argv(["agent", "--log-file", str(log)], tmp_path)
    assert len(tail) == 2400 and token[-10:] not in tail, tail[:40]


def test_a_token_printed_straight_into_more_letters_is_still_found(tmp_path):
    token = minted("o")
    glued = f"value={token}" + "y" * 300
    assert token not in cr.redact(glued) and len(cr.redact(glued)) == len(glued)
    (tmp_path / "glued.log").write_text(glued)
    rep = cr.exposure_report({"r": tmp_path}, use_rg=False)
    assert rep["other_github_tokens"]["files"] == 1, "its own 40 characters carry the checksum"


def test_the_ambient_env_hint_masks_a_proxy_password(monkeypatch):
    password = "pw" * 6
    monkeypatch.setenv("HTTPS_PROXY", f"http://user:{password}@proxy.local:8080")
    lines = dispatcher._suspicious_net_env()
    assert any(line.startswith("HTTPS_PROXY=http://user:") for line in lines), lines
    assert not any(password in line for line in lines), lines


# ---- the completion step -----------------------------------------------------------------------


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A private Brain, capacity ledger, incident authority and dispatch log dir."""
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity-ledger.ndjson")
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "dispatch-logs")
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "rate-limit-incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "rate-limit-incidents.ndjson.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", tmp_path / "capacity-shed")
    return tmp_path


def test_the_real_wrapper_masks_this_runs_segment_and_no_other(monkeypatch, world):
    """`_spawn` exactly as the dispatcher runs it; only the claims release is a stand-in, so the
    completion step that masks the segment is the real `ledger_reconcile.py complete`."""
    stub = world / "claims.py"
    stub.write_text("import sys\nsys.exit(0)\n")
    monkeypatch.setattr(dispatcher, "CLAIMS_PY", stub)
    token = minted("o")
    logs = dispatcher.DISPATCH_LOG_DIR
    logs.mkdir(parents=True)
    log = logs / "o__r_9.codex.log"
    earlier = _header(1, "an-earlier-run") + f"old {token}\n"
    log.write_text(earlier)
    seen: dict = {}

    class FakeProcess:
        pid = 4242

    def fake_popen(argv, **_kw):
        seen["argv"] = argv
        return FakeProcess()

    cwd = world / "wt"
    cwd.mkdir()
    d = {
        "run_id": "o__r_9-codex-1",
        "agent": "codex",
        "mode": "full",
        "target": "o/r#9",
        "lane": "opener",
        "task_type": "implement",
        "model": "gpt-5.6-codex",
        "cwd": str(cwd),
        "wrapped": f"printf 'gh says %s\\n' {token}",
    }
    with monkeypatch.context() as scoped:
        scoped.setattr(dispatcher.subprocess, "Popen", fake_popen)
        dispatcher._spawn(d)
    env = {
        **os.environ,
        "HANDOFF_DIR": str(world),
        "ORCH_STATE_DIR": str(world / "state"),
        "ORCH_LOCAL_RUNTIME": str(world / "runtime"),
        "ORCH_FEEDBACK_DB": str(world / "t.db"),
        "ORCH_CAPABILITIES_PATH": str(world / "runtime" / "capabilities.json"),
        "ORCH_PUSH_RECORD_DISABLED": "1",
        cr.SECRET_FILES_ENV: "",
    }
    with log.open("a") as fh:
        done = subprocess.run(
            ["bash", "-c", seen["argv"][-1]],
            cwd=cwd,
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
    text = log.read_text()
    assert done.returncode == 0, text
    assert text.startswith(earlier), "an earlier run's bytes are never edited"
    assert token not in text[len(earlier) :], text
    assert "gh says [REDACTED:github-token]" in text
    assert "credentials masked in this run's log segment: 1 (github-token x1)" in text
    rows = [json.loads(line) for line in adapters.LEDGER.read_text().splitlines()]
    complete = [r for r in rows if r.get("event") == "complete" and r["run_id"] == d["run_id"]]
    assert [r.get("credentials_redacted") for r in complete] == [1], complete


def test_a_run_started_before_the_cutoff_is_never_scrubbed(monkeypatch, world):
    """Logs that held a token when the leak was found are the owner's to clean, not this step's,
    including when `complete` is re-run by hand for an old run."""
    monkeypatch.setattr(ledger_reconcile.adapters, "record_ledger", lambda *a, **k: None)
    monkeypatch.setattr(ledger_reconcile.feedback, "record_completion_event", lambda *a, **k: None)
    token = minted("o")
    log = world / "run.log"
    log.write_text(_header(2, "run-2") + f"printed {token}\n")
    before = log.read_bytes()
    for started in (ledger_reconcile.CREDENTIAL_SCRUB_SINCE - 1, None):
        ledger_reconcile.record_completion("run-2", "codex", log_file=str(log), started_ts=started)
        assert log.read_bytes() == before, started
    ledger_reconcile.record_completion(
        "run-2", "codex", log_file=str(log), started_ts=ledger_reconcile.CREDENTIAL_SCRUB_SINCE
    )
    assert token not in log.read_text()


def test_an_incident_excerpt_never_carries_a_github_token(world):
    """The six patterns the excerpt had lacked a GitHub token; the shared list runs after them."""
    token = minted("o")
    rate_incidents.record_incident(
        agent="codex",
        surface="dispatch",
        category="rate_limit",
        run_id="run-x",
        evidence=f"429 Too Many Requests while reading {token}",
        shed=False,
    )
    row = json.loads(rate_incidents.INCIDENT_FILE.read_text())
    assert (
        token not in row["evidence_excerpt"]
        and "[REDACTED:github-token]" in row["evidence_excerpt"]
    )


# ---- the evaluator panels ----------------------------------------------------------------------


def _fake_popen(payload: str):
    class FakePopen:
        def __init__(self, *_args, stdout=None, **_kwargs):
            stdout.write(payload)
            stdout.flush()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            raise AssertionError("a finished evaluator must not be killed")

    return FakePopen


def _brain_text(db: Path, prefix: str) -> str:
    import sqlite3

    with sqlite3.connect(db) as c:
        tables = [
            r[0]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?", (prefix + "%",)
            )
        ]
        return " ".join(
            str(value)
            for table in tables
            for row in c.execute(f"SELECT * FROM {table}")
            for value in row
        )


def test_an_experiment_evaluators_output_is_masked_before_it_reaches_the_brain(
    tmp_path, monkeypatch
):
    token = minted("o")
    exp_id = "mask-eval"
    meta = _v2_meta(
        exp_id,
        [
            {"arm_id": "sol", "agents": ["codex"], "profile_id": "sol"},
            {"arm_id": "terra", "agents": ["codex"], "profile_id": "terra"},
        ],
    )
    edir = tmp_path / exp_id
    edir.mkdir()
    (edir / "meta.json").write_text(json.dumps(meta))
    spec = edir / "spec.md"
    spec.write_text("frozen")
    for member in meta["members"]:
        (edir / exp_abcd.exp_diff_path(member["agent"], member["member_id"])).write_text(
            f"diff --git a/{member['member_id']} b/{member['member_id']}\n"
        )
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path)
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setenv("ORCH_OBJECTIVE_ANCHOR", "0")
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")
    monkeypatch.setattr(exp_abcd.adapters, "_ADVERTISED_MEMO", {})
    monkeypatch.setattr(exp_abcd, "_record_execution_start", lambda *a, **kw: 1)
    monkeypatch.setattr(exp_abcd, "_record_execution_complete", lambda *a, **kw: None)
    verdict = {"scores": {"A": 8.0, "B": 6.0}, "notes": {"A": f"the diff prints {token}"}}
    payload = f"ran gh auth token: {token}\n" + json.dumps(verdict)
    monkeypatch.setattr(exp_abcd.subprocess, "Popen", _fake_popen(payload))
    exp_abcd.evaluate("o/r", str(spec), exp_id, ["claude", "codex", "cursor", "vibe"], timeout=1)
    outs = sorted(edir.glob("eval-out-*.txt"))
    assert outs and all(token not in p.read_text() for p in outs)
    stored = _brain_text(tmp_path / "feedback.db", "evaluations")
    assert "[REDACTED:github-token]" in stored and token not in stored


def test_a_ux_review_panels_output_is_masked_before_it_reaches_the_brain(tmp_path, monkeypatch):
    token = minted("o")
    monkeypatch.setattr(ux_review, "REVIEW_DIR", tmp_path)
    monkeypatch.setattr(ux_review, "register_panel_subject", lambda *a, **k: None)
    monkeypatch.setattr(ux_review, "build_rubric_prompt", lambda bundle: "RUBRIC")
    monkeypatch.setattr(ux_review, "build_adversarial_prompt", lambda bundle: "ADVERSARY")
    monkeypatch.setattr(ux_review, "_eval_command", lambda agent, promptfile: "true")
    recorded: list = []
    monkeypatch.setattr(ux_review.feedback, "record_run", lambda *a, **k: None)
    monkeypatch.setattr(
        ux_review.feedback, "record_evaluation", lambda *a, **k: recorded.append((a, k))
    )
    monkeypatch.setattr(ux_review.feedback, "record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(ux_review.feedback, "record_evidence_gap", lambda *a, **k: None)
    finding = {
        "screen": "settings",
        "element": "token field",
        "severity": 3,
        "failure_mode": "secret_shown",
        "expected": "masked",
        "actual": f"shows {token}",
    }
    panel = {"scores": {d: 6 for d in ux_review.DIMENSIONS}, "overall": 6, "findings": [finding]}
    monkeypatch.setattr(
        ux_review.subprocess, "Popen", _fake_popen(f"echo {token}\n" + json.dumps(panel))
    )
    ux_review.review(
        {"review_id": "ux:mask:1", "app": "demo"},
        evaluators=["claude", "codex", "cursor", "gemini"],
        timeout=1,
    )
    outs = sorted(tmp_path.rglob("*-out-*.txt"))
    assert len(outs) == 5 and all(token not in p.read_text() for p in outs)
    assert recorded and token not in json.dumps(recorded, default=str)


# ---- the exposure report -----------------------------------------------------------------------


def _tree(root: Path, secret_value: str) -> dict[str, str]:
    files = {
        "dispatch/offload.vibe.1.log": f"Extracted GitHub token: {secret_value}\n",
        "runtime/cursor/store.db": "\x00\x01" + json.dumps({"out": f"line\n{minted('s', 'Kw2')}"}),
        "runtime/codex/fixture.log": f"test token {FIXTURE}\n",
        "runtime/clean.log": "nothing here\n",
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return files


@pytest.mark.parametrize("use_rg", [False, True], ids=["python", "rg-when-installed"])
def test_the_report_counts_by_hash_and_checksum_and_prints_no_value(monkeypatch, tmp_path, use_rg):
    """With rg on PATH the candidates come from rg, without it from the window walk; every file
    here is fresh, so both modes must count the same. Nothing is edited."""
    value = minted("o")
    secret = tmp_path / "gh_cli_token"
    secret.write_text(value + "\n")
    monkeypatch.setenv(cr.SECRET_FILES_ENV, str(secret))
    _tree(tmp_path, value)
    roots = {"dispatch-logs": tmp_path / "dispatch", "agent-runtime": tmp_path / "runtime"}
    before = {p: (p.read_bytes(), p.stat().st_mtime) for p in tmp_path.rglob("*") if p.is_file()}
    rep = cr.exposure_report(roots, use_rg=use_rg)
    assert rep["status"] == "ok", rep
    assert rep["known_secrets"] == [{"name": "gh_cli_token", "files": 1, "reason": None}], rep
    assert rep["other_github_tokens"]["files"] == 1 and rep["other_github_tokens"]["distinct"] == 1
    assert (
        rep["fixture_only_files"] == 1 and rep["exposed_files"] == 2 and rep["new_in_window"] == 2
    )
    assert {n: r["exposed_files"] for n, r in rep["roots"].items()} == {
        "dispatch-logs": 1,
        "agent-runtime": 1,
    }
    printed = json.dumps(rep) + "\n".join(cr.format_lines(rep))
    assert value not in printed and minted("s", "Kw2") not in printed
    assert before == {p: (p.read_bytes(), p.stat().st_mtime) for p in before}, "nothing is edited"


def test_a_drained_tree_prints_a_measured_zero(monkeypatch, tmp_path):
    """The drained state is reachable and reads as good news, apart from `UNMEASURED` below."""
    secret = tmp_path / "gh_cli_token"
    secret.write_text(minted("o") + "\n")
    monkeypatch.setenv(cr.SECRET_FILES_ENV, str(secret))
    (tmp_path / "clean").mkdir()
    (tmp_path / "clean" / "run.log").write_text("nothing here\n")
    rep = cr.exposure_report({"r": tmp_path / "clean"}, use_rg=False)
    assert rep["status"] == "ok" and rep["exposed_files"] == 0, rep
    lines = "\n".join(cr.format_lines(rep))
    assert "gh_cli_token (exact value): 0 file(s)" in lines and "UNMEASURED" not in lines


def test_an_unreadable_secret_is_unmeasured_never_zero(monkeypatch, tmp_path):
    monkeypatch.setenv(cr.SECRET_FILES_ENV, str(tmp_path / "missing"))
    rep = cr.exposure_report({"r": tmp_path}, use_rg=False)
    assert rep["status"] == "partial" and rep["known_secrets"][0]["files"] is None, rep
    assert "missing (exact value): UNMEASURED — absent" in "\n".join(cr.format_lines(rep))


def _recording_rg(tmp_path: Path, *, exit_code: int, listing: str = "") -> tuple[str, Path]:
    calls = tmp_path / "rg-calls"
    script = tmp_path / "fake-rg"
    script.write_text(
        f"#!{sys.executable}\nimport sys\nopen({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"sys.stdout.write({listing!r})\nsys.exit({exit_code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script), calls


def test_with_no_root_to_scan_rg_is_never_run(tmp_path):
    """rg given no path reads stdin, and under a harness that lists nothing."""
    rg, calls = _recording_rg(tmp_path, exit_code=0)
    rep = cr.exposure_report({"gone": tmp_path / "absent"}, rg=rg)
    assert rep["mode"] == "nothing to scan" and rep["roots"]["gone"]["status"] == "absent"
    assert not calls.exists()


def test_an_rg_error_is_reported_and_what_it_listed_is_kept(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    held = root / "held.log"
    held.write_text(f"x {minted('s', 'Kw2')}\n")
    rg, calls = _recording_rg(tmp_path, exit_code=2, listing=f"{held}\n")
    rep = cr.exposure_report({"root": root}, rg=rg)
    assert rep["status"] == "partial" and "GitHub-shape prefilter: rg exited 2" in rep["unmeasured"]
    assert rep["other_github_tokens"]["files"] == 1 and calls.exists()
    assert " -- " in calls.read_text(), "the roots follow `--`"


def test_the_scan_kill_switch_reports_disabled_never_zero(monkeypatch, tmp_path):
    monkeypatch.setenv(cr.EXPOSURE_SCAN_ENV, "0")
    rep = cr.exposure_report({"r": tmp_path})
    assert rep["status"] == "disabled" and "NOT SCANNED" in "\n".join(cr.format_lines(rep))


def test_switch_review_scans_the_dispatchers_own_directories(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "dispatch")
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "runtime")
    rep = switch_review.credential_exposure()
    assert {n: r["path"] for n, r in rep["roots"].items()} == {
        "dispatch-logs": str(tmp_path / "dispatch"),
        "agent-runtime": str(tmp_path / "runtime"),
    }


def test_the_weekly_caller_carries_the_section_into_its_report(monkeypatch, capsys):
    sentinel = {"status": "disabled", "reason": "sentinel"}
    captured: dict = {}
    monkeypatch.setenv("ORCH_VALUE_CHAIN_MONITOR", "0")
    # main() also records the weekly adversarial shape measurement, a ledger write that only a
    # live tick's heartbeat flag arms; unset, it writes nothing.
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(switch_review, "credential_exposure", lambda **_k: sentinel)

    def fake_review(**kwargs):
        captured.update(kwargs)
        return {"credential_exposure": kwargs["credential_exposure_report"]}

    monkeypatch.setattr(switch_review, "review", fake_review)
    assert switch_review.main(["--json", "--env", "process"]) == 0
    assert captured["credential_exposure_report"] is sentinel
    assert json.loads(capsys.readouterr().out)["credential_exposure"] == sentinel


def test_a_long_clean_transcript_is_masked_in_linear_time():
    """No shape backtracks across a long run of ordinary characters."""
    text = ("x" * 200 + "\n") * 5000 + "a" * 100000
    started = time.monotonic()
    assert cr.redact_with_counts(text) == (text, {})
    assert time.monotonic() - started < 5
