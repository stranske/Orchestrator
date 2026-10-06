#!/usr/bin/env python3
"""credential_redaction.py — a credential an agent prints does not leave its run through what the
Orchestrator writes or returns.

THE INCIDENT (found 2026-10-05). While gh was broken for dispatched agents (fixed by
stranske/Orchestrator#461), agents fetched a GitHub token another way and printed it. A vibe
offload of 2026-09-06 printed the live token: `dispatcher.offload` wrote that stdout to a
world-readable dispatch log and returned it to the calling session, whose driver kept its own copy.
Two experiment arms of 2026-07-09 printed it into their arm logs, which the detached wrapper fills
straight from the agent's stdout. Measured by hash that day (the value never printed), those were
the 4 copies this tool wrote; 58 more sat in the agent CLIs' own transcripts, which it never sees.

THE RULE. One shape list, applied wherever this tool persists or returns an agent's output:

  * `dispatcher.offload` masks the agent's stdout and stderr, the agy log tail and the ambient-env
    hint before it logs or returns any of them, and scrubs the per-run agy log it named;
  * `ledger_reconcile.record_completion`, the step a detached run's wrapper runs after the agent
    exits, scrubs that run's own segment of its log in place (dispatches and experiment arms);
  * the experiment evaluators and the UX-review panel scrub their per-run output file before it is
    parsed, because the parse is what reaches the Brain.

A mask is exactly as long as what it hides: `[REDACTED:<kind>]` padded with `*`, line breaks kept.
That is what makes an in-place edit safe in a log another process may still be appending to: no
byte before or after the masked span moves. Every shape is ASCII, so a mask has the same length in
characters and in bytes, and the text path and the file path agree byte for byte.

WHAT IT CANNOT DO, named rather than implied:

  * the agent CLIs' own stores (cursor chats, agy brain and conversation files, vibe session logs,
    codex rollouts) are written by the CLIs, never by this tool;
  * a detached run's log holds the value from the moment the agent prints it until its completion
    step runs, and keeps it if that step dies first;
  * a credential with no recognisable shape is masked only as the exact value of a file named in
    `ORCH_REDACT_SECRET_FILES` (default: the gh token file) or as the value of an upper-case
    `*_TOKEN` / `*_API_KEY` / `*_SECRET` / `*_PASSWORD` assignment;
  * an encoded value is not decoded (the one exception: an `Authorization: Basic` header).

DETECTION, FYI ONLY. `exposure_report` counts the files under given roots that hold a credential:
the exact value of a known secret file (compared in memory, never printed) or a GitHub token whose
own CRC32 checksum holds, which only GitHub mints. Token-shaped strings that fail the checksum are
test fixtures and are counted apart. It edits and deletes nothing. `switch_review` runs it weekly
over the dispatch-log and agent-runtime directories.

KILL SWITCHES. `ORCH_CREDENTIAL_REDACTION_DISABLED=1` returns every text and file unmasked, and
each caller says so where the output lands. `ORCH_CREDENTIAL_EXPOSURE_SCAN=0` skips the scan, which
then reports `disabled`, never a zero.

    python3 credential_redaction.py --selftest
    python3 credential_redaction.py scan --root NAME=PATH [--root NAME=PATH ...] [--json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import AnyStr

REDACTION_DISABLED_ENV = "ORCH_CREDENTIAL_REDACTION_DISABLED"
EXPOSURE_SCAN_ENV = "ORCH_CREDENTIAL_EXPOSURE_SCAN"
SECRET_FILES_ENV = "ORCH_REDACT_SECRET_FILES"
# The one secret file every local gh call shares (gh_capacity's module docstring names it).
DEFAULT_SECRET_FILES = (Path.home() / ".codex" / "credentials" / "gh_cli_token",)
# A shorter "secret" would mask ordinary words: an empty or truncated file must match nothing.
MIN_SECRET_LEN = 16
# A segment or file larger than this is reported `too_large` and left alone, never half-scrubbed.
MAX_SCRUB_BYTES = 256 * 1024 * 1024
SCAN_CHUNK_BYTES = 8 * 1024 * 1024
# Longer than any shape below can match, so a token straddling two scan chunks is still seen whole.
SCAN_CHUNK_OVERLAP = 512


def _after(chars: str) -> str:
    """What may come right before a credential: no character that could continue it, OR the letter
    of an escape sequence. Codex's `exec --json` stream and the CLIs' transcripts hold command
    output as JSON strings, where a token printed on a new line follows a literal `\\n`, so the
    character before `gh` is `n`. A plain not-after-a-letter guard misses exactly those."""
    return rf"(?:(?<![{chars}])|(?<=\\[nrtbf])|(?<=\\u[0-9A-Fa-f]{{4}}))"


# A JSON-escaped quote or a bare one, around a name or a value.
_QUOTE = r"(?:\\?[\"'])?"
# A line break, raw or JSON-escaped.
_EOL = r"(?:\r?\n|\\r?\\n)"
# GitHub's own formats: classic `gh?_` (36 random characters today; 20 catches a truncated print,
# which matters because the last 6 are a checksum of the 30 before them) and fine-grained. No
# guard AFTER a classic token: one printed straight into more letters is still a token, and the
# few glued characters masked with it cost nothing.
_GITHUB_CLASSIC = _after("A-Za-z0-9") + r"(?P<s>gh[pousr]_[A-Za-z0-9]{20,255})"
_GITHUB_FINE = _after("A-Za-z0-9") + r"(?P<s>github_pat_[A-Za-z0-9_]{22,255})"

# (kind, pattern). Group `s` is what the mask covers; anything matched around it is context and is
# kept, so `Bearer `, `NAME=` and `user:` survive and the reader can see what was there. ASCII-only
# character classes, so a match is the same length in characters and in bytes. A mask starts with
# `[` and is padded with `*`, which no value class below accepts, so masking twice changes nothing.
_SHAPES: tuple[tuple[str, str], ...] = (
    ("github-token", _GITHUB_CLASSIC),
    ("github-token", _GITHUB_FINE),
    # `sk-`: OpenAI and Anthropic keys, including `sk-ant-oat01-` OAuth tokens.
    ("api-key", _after("A-Za-z0-9_-") + r"(?P<s>sk-[A-Za-z0-9_-]{20,255})"),
    ("slack-token", _after("A-Za-z0-9") + r"(?P<s>xox[abposr]-[A-Za-z0-9-]{10,255})"),
    (
        "google-api-key",
        _after("A-Za-z0-9_-") + r"(?P<s>AIza[0-9A-Za-z_-]{35})(?![0-9A-Za-z_-])",
    ),
    ("aws-key-id", _after("A-Za-z0-9") + r"(?P<s>(?:AKIA|ASIA)[0-9A-Z]{16})(?![A-Za-z0-9])"),
    ("langsmith-key", _after("A-Za-z0-9") + r"(?P<s>lsv2_(?:pt|sk)_[A-Za-z0-9_]{20,255})"),
    (
        "jwt",
        _after("A-Za-z0-9_-") + r"(?P<s>eyJ[A-Za-z0-9_-]{8,4096}\.eyJ[A-Za-z0-9_-]{8,4096}"
        r"\.[A-Za-z0-9_-]{8,4096})",
    ),
    ("bearer", _after("A-Za-z0-9") + r"(?i:bearer)[ \t]+(?P<s>[A-Za-z0-9._~+/-]{16,4096}=*)"),
    (
        "basic-auth",
        _after("A-Za-z0-9")
        + r"(?i:authorization:[ \t]*basic)[ \t]+(?P<s>[A-Za-z0-9+/]{16,4096}={0,2})",
    ),
    # `scheme://user:password@host`, the form `git remote -v` prints for a token-in-URL remote.
    (
        "url-password",
        r"(?<=://)[A-Za-z0-9._~%+-]{1,128}:(?P<s>[A-Za-z0-9._~%!$&()+,;=-]{8,256})(?=@)",
    ),
    # An upper-case assignment whose NAME says it holds a secret, the shape `env` and a sourced
    # `.env` file print. The value must hold a digit (real keys do; `os.environ` and other code
    # does not) and must not start with `$` (a reference, not a value).
    (
        "secret-env",
        _after("A-Za-z0-9_") + r"(?:[A-Z][A-Z0-9_]*_)?"
        r"(?:API_KEY|APIKEY|TOKEN|SECRET|SECRET_KEY|PASSWORD|PASSWD|ACCESS_KEY)"
        + _QUOTE
        + r"[ \t]*[=:][ \t]*"
        + _QUOTE
        + r"(?![$])(?=[A-Za-z0-9._~+/=%!&(),;?@^{}|:#-]*[0-9])"
        r"(?P<s>[A-Za-z0-9._~+/=%!&(),;?@^{}|:#-]{8,4096})",
    ),
    # A PEM private key: its base64 body, line breaks kept so the block keeps its shape.
    (
        "private-key",
        r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----"
        + _EOL
        + r"(?P<s>(?:[A-Za-z0-9+/=]{16,}"
        + _EOL
        + r")+)-----END",
    ),
)
_STR_SHAPES = tuple((kind, re.compile(src, re.ASCII)) for kind, src in _SHAPES)
_BYTES_SHAPES = tuple((kind, re.compile(src.encode("ascii"))) for kind, src in _SHAPES)

# A run's own header, exactly as every writer of a run log prints it:
# `=== <UTC timestamp> <what> ... run_id=<id> ===` (dispatcher._spawn, dispatcher.offload,
# exp_abcd, ux_review). STRICT on purpose: a segment ends only at the next REAL header, never at a
# pytest `=====` banner, which `ledger_reconcile._log_segment` (any line starting `===`) stops at.
_RUN_HEADER = re.compile(rb"^=== \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z [^\n]*$", re.MULTILINE)

# What the exposure scan's rg prefilter looks for. Rust regex has no lookaround; Python decides,
# with the redactor's own two GitHub shapes, so the scan cannot count what the mask would miss.
_RG_GITHUB_PREFILTER = r"gh[pousr]_[A-Za-z0-9]{20}|github_pat_[A-Za-z0-9_]{22}"
_GITHUB_DETECT = tuple(re.compile(src.encode("ascii")) for src in (_GITHUB_CLASSIC, _GITHUB_FINE))
_CLASSIC_GITHUB = re.compile(rb"gh[pousr]_([A-Za-z0-9]{30})([A-Za-z0-9]{6})")
_BASE62 = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def disabled() -> bool:
    return os.environ.get(REDACTION_DISABLED_ENV, "").strip() == "1"


def secret_files() -> list[Path]:
    """The files whose exact value is masked wherever it appears.

    `ORCH_REDACT_SECRET_FILES` (os.pathsep-separated) replaces the default, and set to empty it
    names none. The VALUES are read per call and never kept: nothing here caches a secret."""
    raw = os.environ.get(SECRET_FILES_ENV)
    if raw is None:
        return list(DEFAULT_SECRET_FILES)
    return [Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip()]


def _read_secret(path: Path) -> tuple[bytes | None, str | None]:
    """(value, None) or (None, why it cannot be used). The value is never part of the reason."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, "absent"
    except OSError as exc:
        return None, f"unreadable ({type(exc).__name__})"
    value = raw.strip()
    if len(value) < MIN_SECRET_LEN:
        return None, f"shorter than {MIN_SECRET_LEN} characters, so it would mask ordinary text"
    if b"\n" in value or b"\r" in value:
        return None, "holds more than one line"
    return value, None


def _known_values() -> list[bytes]:
    values = []
    for path in secret_files():
        value, _reason = _read_secret(path)
        if value is not None:
            values.append(value)
    return values


def _mask(span: AnyStr, kind: str) -> AnyStr:
    """`[REDACTED:<kind>]` padded with `*` to exactly the span's length, line breaks kept."""
    text = span.decode("latin-1") if isinstance(span, bytes) else span
    width = sum(1 for ch in text if ch not in "\r\n")
    label = f"[REDACTED:{kind}]"
    if len(label) > width:
        label = "[REDACTED]" if width >= len("[REDACTED]") else ""
    fill = iter(label.ljust(width, "*"))
    masked = "".join(ch if ch in "\r\n" else next(fill) for ch in text)
    if isinstance(span, bytes):
        return masked.encode("latin-1")
    return masked


def _apply_shapes(
    text: AnyStr,
    shapes: tuple[tuple[str, re.Pattern[AnyStr]], ...],
    counts: dict[str, int],
    spans: list[tuple[int, int]],
) -> AnyStr:
    for kind, pattern in shapes:
        pieces: list[AnyStr] = []
        last = 0
        for match in pattern.finditer(text):
            start, end = match.span("s")
            secret = match.group("s")
            masked = _mask(secret, kind)
            if masked == secret:  # already a mask: masking twice changes and counts nothing
                continue
            counts[kind] = counts.get(kind, 0) + 1
            spans.append((start, end))
            pieces += [text[last:start], masked]
            last = end
        if pieces:
            pieces.append(text[last:])
            text = text[:0].join(pieces)
    return text


def redact_with_counts(text: str) -> tuple[str, dict[str, int]]:
    """`text` with every credential masked, and how many of each kind were masked."""
    if not text or disabled():
        return text, {}
    counts: dict[str, int] = {}
    spans: list[tuple[int, int]] = []
    text = _apply_shapes(text, _STR_SHAPES, counts, spans)
    for raw in _known_values():
        value = raw.decode("utf-8", "replace")
        found = text.count(value)
        if found:
            text = text.replace(value, _mask(value, "known-secret"))
            counts["known-secret"] = counts.get("known-secret", 0) + found
    return text, counts


def redact(text: str) -> str:
    return redact_with_counts(text)[0]


def redact_bytes(data: bytes) -> tuple[bytes, dict[str, int], list[tuple[int, int]]]:
    """The bytes twin of `redact_with_counts`, plus the masked spans (same length, so the spans of
    every pass share one coordinate system and can be written back in place)."""
    counts: dict[str, int] = {}
    spans: list[tuple[int, int]] = []
    if not data or disabled():
        return data, counts, spans
    data = _apply_shapes(data, _BYTES_SHAPES, counts, spans)
    for value in _known_values():
        at = data.find(value)
        while at != -1:
            data = data[:at] + _mask(value, "known-secret") + data[at + len(value) :]
            counts["known-secret"] = counts.get("known-secret", 0) + 1
            spans.append((at, at + len(value)))
            at = data.find(value, at + len(value))
    return data, counts, spans


def segment_span(data: bytes, run_id: str) -> tuple[int, int] | None:
    """Byte range of `run_id`'s own segment: after its header line, up to the next real run header
    or the end. None when the file holds no header for that run. The LAST header naming the run
    wins, as in `ledger_reconcile._log_segment`."""
    needle = re.compile(rb"\brun_id=" + re.escape(run_id.encode("utf-8")) + rb"(?=\s|$)")
    headers = list(_RUN_HEADER.finditer(data))
    own = None
    for index, header in enumerate(headers):
        if needle.search(header.group(0)):
            own = index
    if own is None:
        return None
    start = headers[own].end()
    if data[start : start + 1] == b"\n":
        start += 1
    end = headers[own + 1].start() if own + 1 < len(headers) else len(data)
    return start, end


def scrub_file(path: str | Path, *, run_id: str | None = None) -> dict:
    """Mask credentials IN PLACE in one run's segment of `path`, or in the whole file when
    `run_id` is None (a file written for one run only).

    Same-length masks, written only over the masked spans, through a descriptor opened without
    truncation: a process still appending to the file loses nothing, and no byte outside the
    segment is touched, so what earlier runs wrote stays exactly as it was. Never raises: a
    completion step must survive its own safety layer, so a failure is a `status`, named."""
    result: dict = {"path": str(path), "status": "clean", "redacted": 0, "kinds": {}}
    if disabled():
        result["status"] = "disabled"
        return result
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        result["status"] = "missing"
        return result
    except OSError as exc:
        result.update(status="error", error=type(exc).__name__)
        return result
    try:
        with os.fdopen(fd, "r+b") as fh:
            size = os.fstat(fh.fileno()).st_size
            if size > MAX_SCRUB_BYTES and run_id is None:
                result["status"] = "too_large"
                return result
            # A run's segment is the newest in its log, so it lies in the file's tail; a shared
            # log that has grown past the window is read from there, never whole.
            offset = max(0, size - MAX_SCRUB_BYTES)
            fh.seek(offset)
            data = fh.read(MAX_SCRUB_BYTES)
            span = (0, len(data)) if run_id is None else segment_span(data, run_id)
            if span is None:
                result["status"] = "too_large" if offset else "no_segment"
                return result
            start, end = span
            masked, counts, spans = redact_bytes(data[start:end])
            for lo, hi in sorted(spans):
                fh.seek(offset + start + lo)
                fh.write(masked[lo:hi])
            fh.flush()
    except Exception as exc:  # noqa: BLE001 - never fatal to the caller; named in the result
        result.update(status="error", error=type(exc).__name__)
        return result
    if counts:
        result.update(status="scrubbed", redacted=sum(counts.values()), kinds=counts)
    return result


def describe(counts: Mapping[str, int]) -> str:
    """`3 (github-token x2, bearer x1)`: what a masking pass did, never what it hid."""
    total = sum(counts.values())
    kinds = ", ".join(f"{kind} x{n}" for kind, n in sorted(counts.items()))
    return f"{total} ({kinds})" if kinds else "0"


def github_checksum_valid(token: str | bytes) -> bool:
    """GitHub's own check on a classic token: its last 6 characters are the CRC32 of the 30 before
    them, in base62. Only GitHub mints tokens that pass, so a pass means a real token (live or
    revoked) and a fail means a fixture someone typed."""
    raw = token.encode("ascii", "replace") if isinstance(token, str) else token
    match = _CLASSIC_GITHUB.fullmatch(raw)
    if not match:
        return False
    n = zlib.crc32(match.group(1))
    digits = b""
    while n:
        n, r = divmod(n, 62)
        digits = _BASE62[r : r + 1] + digits
    return digits.rjust(6, b"0") == match.group(2)


def _classify_file(
    path: str, secrets: list[tuple[str, bytes]], budget_end: float | None
) -> dict | None:
    """What one file holds: known secret names, other GitHub-minted tokens (by digest), fixtures.

    Streamed in chunks, each carrying the previous chunk's tail so a value straddling the boundary
    is seen whole. The tail starts after a word boundary: a value that began earlier lies wholly in
    the previous chunk, and a cut-off fragment of it must not read as a fixture. None when the time
    budget ran out before the file was read."""
    holds: set[str] = set()
    minted: set[str] = set()
    synthetic = False
    known = {value for _name, value in secrets}
    with open(path, "rb") as fh:
        tail = b""
        while True:
            if budget_end is not None and time.monotonic() > budget_end:
                return None
            block = fh.read(SCAN_CHUNK_BYTES)
            if not block:
                break
            chunk = tail + block
            for name, value in secrets:
                if value in chunk:
                    holds.add(name)
            for pattern in _GITHUB_DETECT:
                for match in pattern.finditer(chunk):
                    token = match.group("s")
                    # A classic token printed straight into more letters matches long; its own
                    # 40 characters are the token.
                    head = token if token.startswith(b"github_pat_") else token[:40]
                    if token in known or head in known:
                        continue
                    if github_checksum_valid(head):
                        minted.add(hashlib.sha256(head).hexdigest()[:12])
                    else:
                        synthetic = True
            tail = re.sub(rb"^[A-Za-z0-9_-]+", b"", block[-SCAN_CHUNK_OVERLAP:])
    return {"holds": holds, "minted": minted, "synthetic": synthetic}


def _rg_candidates(
    rg: str, roots: list[str], args: list[str], timeout: float
) -> tuple[set[str], str | None]:
    """Files rg lists for `args`. rg exits 1 for "no match"; 2 means it hit an error, but what it
    listed before the error is still a true match, so it is kept and the error is reported."""
    try:
        proc = subprocess.run(
            [rg, "-uuu", "-l", "--no-messages", *args, "--", *roots],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return set(), f"rg did not finish in {int(timeout)}s"
    except OSError as exc:
        return set(), f"rg could not start ({type(exc).__name__})"
    found = {line for line in proc.stdout.splitlines() if line}
    if proc.returncode not in (0, 1):
        return found, f"rg exited {proc.returncode}"
    return found, None


def exposure_report(
    roots: Mapping[str, str | Path],
    *,
    now: float | None = None,
    window_days: float = 7.0,
    budget_s: float = 300.0,
    list_limit: int = 10,
    rg: str | None = None,
    use_rg: bool = True,
) -> dict:
    """FYI: the files under `roots` that hold a credential. Edits and deletes nothing.

    A file is EXPOSED when it holds the exact value of a known secret file (compared in memory) or
    a GitHub token whose own checksum holds. With rg it measures every file, rg listing the
    candidates and Python deciding; without it only files modified inside the window are read,
    under the same time budget, and `mode` says so. A known secret that cannot be read is
    UNMEASURED, never zero: a gate that prints the same thing for "cannot tell" and "none" lies at
    exactly the moment it matters."""
    now = time.time() if now is None else now
    report: dict = {
        "status": "ok",
        "mode": None,
        "window_days": window_days,
        "roots": {},
        "known_secrets": [],
        "other_github_tokens": {"files": 0, "distinct": 0, "digests": []},
        "fixture_only_files": 0,
        "exposed_files": 0,
        "new_in_window": 0,
        "newest": [],
        "unmeasured": [],
        "clears": (
            "rotating the credential makes every copy dead; deleting or redacting the files is "
            "the owner's call, and nothing here does either. New output this tool writes is "
            "masked at the source (dispatcher.offload, the completion step's segment scrub); "
            "files under agent-runtime are the agent CLIs' own and only cleanup clears them"
        ),
    }
    if os.environ.get(EXPOSURE_SCAN_ENV, "1").strip() == "0":
        report.update(status="disabled", reason=f"{EXPOSURE_SCAN_ENV}=0")
        return report
    secrets: list[tuple[str, bytes]] = []
    patternable: list[tuple[str, Path]] = []
    for path in secret_files():
        value, reason = _read_secret(path)
        if value is None:
            report["known_secrets"].append({"name": path.name, "files": None, "reason": reason})
            report["unmeasured"].append(f"{path.name}: {reason}")
            continue
        secrets.append((path.name, value))
        report["known_secrets"].append({"name": path.name, "files": 0, "reason": None})
        # The file itself is rg's pattern file, so the value never reaches an argv. Only a file of
        # exactly one line qualifies: a blank line in a pattern file matches every file.
        try:
            raw = path.read_bytes()
        except OSError:
            raw = b""
        if raw in (value, value + b"\n"):
            patternable.append((path.name, path))
    present: dict[str, str] = {}
    for name, root in roots.items():
        root_path = Path(root)
        entry: dict = {"path": str(root_path), "status": "ok", "exposed_files": 0}
        if not root_path.exists():
            entry["status"] = "absent"
        elif not root_path.is_dir():
            entry["status"] = "not a directory"
            report["unmeasured"].append(f"{name}: {root_path} is not a directory")
        else:
            present[name] = str(root_path)
        report["roots"][name] = entry
    started = time.monotonic()
    budget_end = started + budget_s
    rg_path = (rg or shutil.which("rg")) if use_rg else None
    candidates: set[str] = set()
    if not present:
        report["mode"] = "nothing to scan"
    elif rg_path:
        report["mode"] = "rg"
        # Never without a root: rg with no path reads stdin and lists nothing.
        found, error = _rg_candidates(
            rg_path, list(present.values()), ["-e", _RG_GITHUB_PREFILTER], budget_s
        )
        candidates |= found
        if error:
            report["unmeasured"].append(f"GitHub-shape prefilter: {error}")
        for name, path in patternable:
            remaining = max(1.0, budget_end - time.monotonic())
            found, error = _rg_candidates(
                rg_path, list(present.values()), ["-F", "-f", str(path)], remaining
            )
            candidates |= found
            if error:
                report["unmeasured"].append(f"{name} exact-value prefilter: {error}")
        for name, _value in secrets:
            if name not in {n for n, _p in patternable}:
                report["unmeasured"].append(
                    f"{name}: not a one-line file, so only files holding a GitHub-shaped string "
                    "were checked for its exact value"
                )
    else:
        report["mode"] = f"python, files modified in the last {window_days:g}d only (no rg)"
        horizon = now - window_days * 86400
        for root in present.values():
            for dirpath, _dirnames, filenames in os.walk(root):
                for filename in filenames:
                    full = os.path.join(dirpath, filename)
                    try:
                        st = os.lstat(full)
                    except OSError:
                        continue
                    if os.path.islink(full) or st.st_mtime < horizon:
                        continue
                    candidates.add(full)
    exposed: list[dict] = []
    digests: set[str] = set()
    other_files = 0
    unscanned = 0
    for candidate in sorted(candidates):
        try:
            st = os.lstat(candidate)
        except OSError:
            continue
        if os.path.islink(candidate):
            continue
        try:
            found_in = _classify_file(candidate, secrets, budget_end)
        except OSError:
            unscanned += 1
            continue
        if found_in is None:
            unscanned += 1
            continue
        root_name = next(
            (n for n, r in present.items() if candidate.startswith(r.rstrip("/") + "/")),
            None,
        )
        holds = sorted(found_in["holds"])
        if found_in["minted"]:
            other_files += 1
            digests |= found_in["minted"]
            holds.append("github-token")
        if not holds:
            if found_in["synthetic"]:
                report["fixture_only_files"] += 1
            continue
        for item in report["known_secrets"]:
            if item["files"] is not None and item["name"] in found_in["holds"]:
                item["files"] += 1
        if root_name is not None:
            report["roots"][root_name]["exposed_files"] += 1
        exposed.append({"path": candidate, "root": root_name, "mtime": st.st_mtime, "holds": holds})
    if unscanned:
        report["unmeasured"].append(
            f"{unscanned} candidate file(s) not read inside the {int(budget_s)}s budget or unreadable"
        )
    report["other_github_tokens"] = {
        "files": other_files,
        "distinct": len(digests),
        "digests": sorted(digests),
    }
    report["exposed_files"] = len(exposed)
    horizon = now - window_days * 86400
    report["new_in_window"] = sum(1 for row in exposed if row["mtime"] >= horizon)
    exposed.sort(key=lambda row: row["mtime"], reverse=True)
    report["newest"] = [
        {**row, "mtime": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(row["mtime"]))}
        for row in exposed[:list_limit]
    ]
    report["elapsed_s"] = round(time.monotonic() - started, 1)
    if report["unmeasured"]:
        report["status"] = "partial"
    return report


def format_lines(report: Mapping) -> list[str]:
    """The switch-review section. Counts, paths and digests only."""
    lines = ["## Credentials in agent output (FYI only; nothing here edits or deletes a file)", ""]
    if report.get("status") == "disabled":
        return lines + [f"  NOT SCANNED — {report.get('reason')}", ""]
    if report.get("status") == "unmeasured":
        return lines + [f"  NOT MEASURED — {report.get('reason', 'no reason recorded')}", ""]
    lines.append(f"  mode: {report.get('mode')}  ({report.get('elapsed_s', '?')}s)")
    for item in report.get("known_secrets", []):
        if item["files"] is None:
            lines.append(f"  {item['name']} (exact value): UNMEASURED — {item['reason']}")
        else:
            lines.append(f"  {item['name']} (exact value): {item['files']} file(s)")
    other = report.get("other_github_tokens") or {}
    lines.append(
        f"  other GitHub-minted tokens (checksum holds): {other.get('files', 0)} file(s), "
        f"{other.get('distinct', 0)} distinct {other.get('digests') or ''}".rstrip()
    )
    lines.append(
        f"  fixture-shaped strings only (checksum fails): {report.get('fixture_only_files', 0)} file(s)"
    )
    lines.append(
        f"  exposed: {report.get('exposed_files', 0)} file(s), "
        f"{report.get('new_in_window', 0)} modified in the last {report.get('window_days', 7):g}d"
    )
    for name, root in (report.get("roots") or {}).items():
        lines.append(
            f"  {name}: {root['status']}, {root['exposed_files']} exposed  ({root['path']})"
        )
    for row in report.get("newest") or []:
        lines.append(f"    {row['mtime']}  {row['path']}  [{', '.join(row['holds'])}]")
    for item in report.get("unmeasured") or []:
        lines.append(f"  unmeasured: {item}")
    lines += [f"  clears: {report.get('clears')}", ""]
    return lines


def _minted_for_test(prefix: str, entropy: str) -> str:
    """A token GitHub's checksum accepts, built at run time so no token-shaped literal is ever in
    the source (push protection and secret scanning read source, not run time)."""
    n = zlib.crc32(entropy.encode("ascii"))
    digits = ""
    while n:
        n, r = divmod(n, 62)
        digits = _BASE62[r : r + 1].decode() + digits
    return prefix + entropy + digits.rjust(6, "0")


def _selftest() -> None:
    saved = {k: os.environ.get(k) for k in (REDACTION_DISABLED_ENV, SECRET_FILES_ENV)}
    try:
        os.environ.pop(REDACTION_DISABLED_ENV, None)
        with tempfile.TemporaryDirectory(prefix="credential-redaction-") as td:
            secret_file = Path(td) / "secret"
            secret = "S3cr3t-" + "v" * 4 + "-value-" + "q" * 9
            secret_file.write_text(secret + "\n")
            os.environ[SECRET_FILES_ENV] = str(secret_file)
            gh = _minted_for_test("gh" + "o_", "Ab3" * 10)
            assert github_checksum_valid(gh), "the test token must satisfy GitHub's checksum"
            fixture = "gh" + "p_" + "A" * 36
            assert not github_checksum_valid(fixture)
            text = f"token {gh} then {fixture}; export X={secret} done"
            masked, counts = redact_with_counts(text)
            assert len(masked) == len(text), (len(masked), len(text))
            assert gh not in masked and fixture not in masked and secret not in masked, masked
            assert counts == {"github-token": 2, "known-secret": 1}, counts
            assert redact_with_counts(masked) == (masked, {}), "masking twice changes nothing"
            log = Path(td) / "run.log"
            earlier = f"=== 2026-01-01T00:00:00Z dispatch a run_id=old ===\nold {gh}\n"
            mine = "=== 2026-01-01T00:01:00Z dispatch a run_id=new ===\n"
            body = f"===== pytest banner =====\nprinted {gh}\n"
            log.write_text(earlier + mine + body)
            result = scrub_file(log, run_id="new")
            after = log.read_text()
            assert result["status"] == "scrubbed" and result["redacted"] == 1, result
            assert after.startswith(earlier), "an earlier run's segment is never edited"
            assert gh not in after[len(earlier) :] and len(after) == len(earlier + mine + body)
            assert scrub_file(log, run_id="absent")["status"] == "no_segment"
            os.environ[REDACTION_DISABLED_ENV] = "1"
            assert redact(gh) == gh, "the kill switch returns text unmasked"
            os.environ.pop(REDACTION_DISABLED_ENV)
            os.environ[SECRET_FILES_ENV] = str(secret_file)
            root = Path(td) / "root"
            root.mkdir()
            (root / "a.log").write_text(f"x {secret} y")
            (root / "b.log").write_text(f"x {gh} y")
            (root / "c.log").write_text(f"x {fixture} y")
            rep = exposure_report({"r": root}, use_rg=False)
            assert rep["known_secrets"] == [{"name": "secret", "files": 1, "reason": None}], rep
            assert rep["other_github_tokens"]["files"] == 1 and rep["fixture_only_files"] == 1, rep
            assert secret not in json.dumps(rep) and gh not in json.dumps(rep)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print(
        "credential_redaction selftest OK: same-length masks, idempotent, segment-only in-place "
        "scrub, kill switch, checksum-classified exposure report"
    )


def _parse_root(spec: str) -> tuple[str, str]:
    name, sep, path = spec.partition("=")
    if not sep or not name or not path:
        raise argparse.ArgumentTypeError(f"--root wants NAME=PATH, got {spec!r}")
    return name, path


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    scan = sub.add_parser("scan", help="FYI exposure report over named roots (read-only)")
    scan.add_argument("--root", action="append", type=_parse_root, default=[])
    scan.add_argument("--window-days", type=float, default=7.0)
    scan.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    if args.cmd == "scan":
        if not args.root:
            ap.error("scan needs at least one --root NAME=PATH")
        rep = exposure_report(dict(args.root), window_days=args.window_days)
        print(json.dumps(rep, indent=2) if args.json else "\n".join(format_lines(rep)))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
