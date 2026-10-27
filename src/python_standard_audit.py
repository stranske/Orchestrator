#!/usr/bin/env python3
"""Read-only launchd Python audit. Never execute a scheduled job or its entry script."""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import shlex
import subprocess
import tokenize
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from io import StringIO
from pathlib import Path
from xml.parsers.expat import ExpatError

DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
PREFIXES = ("com.stranske.", "com.openai.codex.", "com.codex.")
BAD = {"system-3.9", "anaconda-3.12"}
PYTHON = re.compile(r"(?:.*/)?python(?:3(?:\.\d+)?)?$")
STANDARD = re.compile(r"py-standard\.sh|standard-python|STANDARD_PYTHON")
HEREDOC = re.compile(r"(?<!<)<<(-?)(?!<)")


@dataclass(frozen=True)
class Resolution:
    executable: str
    version: str


Resolver = Callable[[str, str, bool, Path], Resolution]


def expand(value: str, home: Path, path: str) -> str:
    """Expand only the two known variables; never evaluate shell input."""
    known = {"HOME": str(home), "PATH": path}
    value = re.sub(
        r"\$(?:\{(HOME|PATH)\}|(HOME|PATH)(?!\w))",
        lambda match: known[match[1] or match[2]],
        value,
    )
    if value.startswith("~/"):
        value = str(home / value[2:])
    return value


def resolve_python(command: str, path: str, login: bool, home: Path) -> Resolution:
    """Resolve in the job's environment, then ask that interpreter for its version.

    Only command lookup, the shared standard selector, and interpreter --version run here.
    Plist command strings and entry scripts are data, never executable audit input.
    """
    env = {"HOME": str(home), "PATH": path}

    def run(argv: list[str]) -> str:
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise ValueError(f"{argv[0]} exited {result.returncode}")
        return (result.stdout or result.stderr).strip()

    executable = expand(command, home, path)
    if "/" not in executable:
        if not PYTHON.fullmatch(executable):
            raise ValueError(f"unsupported bare interpreter {executable}")
        executable = run(["/bin/bash", "-lc" if login else "-c", f"command -v {executable}"])
    if not Path(executable).is_absolute() or "\n" in executable:
        raise ValueError("command lookup did not return one absolute executable")
    shims = {home / ".codex/python-bin/python3", home / ".codex/bin/python3"}
    if Path(executable) in shims:
        if "standard-python" not in Path(executable).read_text(encoding="utf-8"):
            raise ValueError(f"unrecognized shared shim {executable}")
        executable = run([str(home / ".codex/bin/standard-python")])
        if not Path(executable).is_absolute() or "\n" in executable:
            raise ValueError("standard-python did not return one absolute interpreter")
    version = run([executable, "--version"])
    if not re.fullmatch(r"Python \d+\.\d+(?:\.\d+)?\S*", version):
        raise ValueError(f"no Python version reported by {executable}")
    return Resolution(executable, version)


def classify(resolved: Resolution) -> str:
    version = re.search(r"Python (\d+\.\d+)(?:\D|$)", resolved.version)
    if not version:
        raise ValueError("interpreter version unavailable")
    minor = version[1]
    if minor in ("3.14", "3.13"):
        return "standard"
    if minor == "3.9":
        return "system-3.9"
    if minor == "3.12" and "/anaconda3/" in resolved.executable:
        return "anaconda-3.12"
    return "other"


def code_lines(text: str) -> Iterator[list[str]]:
    """Tokenize active lines, retaining shebangs but excluding comments and heredoc bodies."""
    pending: list[tuple[str, bool]] = []
    continued = ""
    for index, line in enumerate(text.replace("\\\n", "").splitlines()):
        if pending:
            delimiter, tabs = pending[0]
            if (line.lstrip("\t") if tabs else line) == delimiter:
                pending.pop(0)
            continue
        if index == 0 and line.startswith("#!"):
            yield shlex.split(line[2:])
            continue
        # shlex drops comments without treating a quoted # as a comment.
        continued += line
        try:
            tokens = shlex.split(continued, comments=True)
        except ValueError as exc:
            if "No closing quotation" not in str(exc):
                raise
            continued += "\n"
            continue
        active = continued
        continued = ""
        if not tokens:
            continue
        pending.extend(heredoc_delimiters(active))
        yield tokens
    if continued or pending:
        raise ValueError("a complete entry script (unterminated quotation/heredoc)")


def heredoc_word(line: str, start: int) -> tuple[str, int]:
    """Read one redirection word with shell quote removal, without evaluating it."""
    while start < len(line) and line[start].isspace():
        start += 1
    index, quote = start, ""
    while index < len(line):
        char = line[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char.isspace() or char in ";&|()<>":
            break
        index += 1
    words = shlex.split(line[start:index])
    if len(words) != 1:
        raise ValueError("a complete heredoc delimiter")
    return words[0], index


def heredoc_delimiters(line: str) -> list[tuple[str, bool]]:
    """Find shell redirections outside quotes, comments and arithmetic shifts."""
    found: list[tuple[str, bool]] = []
    quote, index, arithmetic = "", 0, 0
    while index < len(line):
        char = line[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (
            index == 0 or line[index - 1].isspace() or line[index - 1] in ";&|()<>"
        ):
            break
        elif line.startswith("((", index):
            arithmetic += 1
            index += 2
            continue
        elif arithmetic and line.startswith("))", index):
            arithmetic -= 1
            index += 2
            continue
        elif not arithmetic and (match := HEREDOC.match(line, index)):
            delimiter, index = heredoc_word(line, match.end())
            found.append((delimiter, bool(match[1])))
            continue
        index += 1
    return found


def references(text: str, path: str, home: Path, *, shell: bool = True) -> list[tuple[str, str]]:
    """Return interpreter references and effective PATHs, without executing script content.

    Simple PATH assignments are followed. A dynamic PATH is unmeasurable rather than guessed.
    A standard selector is evidence of intent, but cannot mask another interpreter reference.
    """
    refs: list[tuple[str, str]] = []
    if shell:
        lines = list(code_lines(text))
    else:
        # Python strings can contain subprocess interpreter paths; Python comments cannot.
        active = " ".join(
            token.string
            for token in tokenize.generate_tokens(StringIO(text).readline)
            if token.type != tokenize.COMMENT
        )
        pins = re.findall(
            r"/(?:usr/bin/python3|opt/anaconda3/bin/python3?)(?:\.\d+)?(?![\w.])", active
        )
        return [(pin, path) for pin in dict.fromkeys(pins)] + (
            [("standard", path)] if STANDARD.search(active) else []
        )
    standard = any(STANDARD.search(" ".join(tokens)) for tokens in lines)
    for tokens in lines:
        for token in tokens:
            if shell and token.startswith("PATH="):
                path = expand(token[5:], home, path)
                if "$" in path or "`" in path:
                    raise ValueError("a resolver for the entry script's dynamic PATH")
            # Assignments, command substitutions and semicolons may adjoin the executable.
            token = expand(token, home, path)
            interpreters = re.findall(
                r"/(?:[^/\s'\";$()`]+/)*python(?:3(?:\.\d+)?)?(?![\w.-])"
                r"|(?<![\w./-])python(?:3(?:\.\d+)?)?(?![\w.-])",
                token,
            )
            candidate = token.split("=", 1)[1] if re.match(r"[A-Za-z_]\w*=", token) else token
            if candidate.startswith("/") and PYTHON.fullmatch(candidate):
                interpreters = [candidate]
            for interpreter in interpreters:
                refs.append((expand(interpreter, home, path), path))
    if standard:
        refs.append(("standard", path))
    return list(dict.fromkeys(refs))


def shell_command(args: list[str]) -> tuple[str, bool, str]:
    """Read only shell options preceding its command/script, never script arguments."""
    index, login = 1, False
    while index < len(args):
        option = args[index]
        if option == "--":
            index += 1
            break
        if not option.startswith(("-", "+")) or option == "-":
            break
        if option in ("-o", "+o", "--rcfile", "--init-file"):
            index += 2
            continue
        if option == "--login":
            login = True
        elif not option.startswith("--"):
            login = login or "l" in option[1:]
            if "c" in option[1:]:
                if index + 1 >= len(args):
                    raise ValueError("a command operand after shell -c")
                return args[index + 1], login, ""
        index += 1
    entry = args[index] if index < len(args) and args[index] != "-" else ""
    return "", login, entry


def inspect_job(job: dict, *, home: Path, resolver: Resolver) -> dict:
    args = job.get("ProgramArguments") or [job.get("Program", "")]
    if (
        not isinstance(args, list)
        or not args
        or not all(isinstance(a, str) for a in args)
        or not args[0]
    ):
        raise ValueError("valid ProgramArguments")
    environment = job.get("EnvironmentVariables") or {}
    if not isinstance(environment, dict):
        raise ValueError("valid EnvironmentVariables")
    path = environment.get("PATH", DEFAULT_PATH)
    if not isinstance(path, str):
        raise ValueError("a string EnvironmentVariables.PATH")
    shell = Path(args[0]).name in ("bash", "zsh", "sh")
    command, login, shell_entry = shell_command(args) if shell else ("", False, "")
    tokens = shlex.split(command, comments=True) if command else args
    # Scan only executable command text, never arbitrary arguments to a direct Python job.
    refs = references(command, path, home) if command else []
    if not shell:
        executable = args[1] if Path(args[0]).name == "env" and len(args) > 1 else args[0]
        if PYTHON.fullmatch(executable):
            refs.append((executable, path))
    entries = [
        expand(t, home, path)
        for t in tokens
        if t.endswith((".sh", ".py")) and not t.endswith("/py-standard.sh")
    ]
    if shell_entry:
        entries.insert(0, expand(shell_entry, home, path))
    elif not shell and not PYTHON.fullmatch(args[0]):
        executable = Path(expand(args[0], home, path))
        if executable.is_file():
            with executable.open("rb") as stream:
                if stream.read(2) == b"#!":
                    entries.insert(0, str(executable))
    entries = list(dict.fromkeys(entries))
    for entry in entries:
        script = Path(entry)
        if not script.is_absolute():
            working = job.get("WorkingDirectory")
            if not working:
                raise ValueError(f"absolute entry script or WorkingDirectory for {entry}")
            script = Path(working) / script
        # Python source is not shell. Its shebang matters only for direct execution.
        text = script.read_text(encoding="utf-8")
        shebang = shlex.split(text.splitlines()[0][2:]) if text.startswith("#!") else []
        if script.suffix == ".py" or any(PYTHON.fullmatch(token) for token in shebang):
            if refs:
                text = "\n".join(text.splitlines()[1:]) if text.startswith("#!") else text
            elif text.startswith("#!"):
                refs.extend(references(text.splitlines()[0] if text else "", path, home))
            # Hard-coded paths/standard selectors in source still belong to the audit.
            refs.extend(references(text, path, home, shell=False))
        else:
            refs.extend(references(text, path, home))
    evidence = []
    for executable, effective_path in dict.fromkeys(refs):
        if executable == "standard":
            evidence.append({"class": "standard", "reference": "standard selector"})
        else:
            resolved = resolver(executable, effective_path, login and effective_path == path, home)
            evidence.append({"class": classify(resolved), **asdict(resolved)})
    classes = {item["class"] for item in evidence}
    category = next(
        (c for c in ("system-3.9", "anaconda-3.12", "other", "standard") if c in classes),
        "no-python",
    )
    return {
        "class": category,
        "login_shell": login,
        "path": path,
        "entries": entries,
        "evidence": evidence,
    }


def audit(
    directory: Path | None = None,
    prefixes: tuple[str, ...] = PREFIXES,
    *,
    home: Path | None = None,
    resolver: Resolver = resolve_python,
) -> dict:
    home = Path.home() if home is None else home
    directory = home / "Library/LaunchAgents" if directory is None else directory
    rows, needs = [], []
    cache: dict[tuple[str, str, bool], Resolution] = {}

    def cached(command: str, path: str, login: bool, home: Path) -> Resolution:
        key = command, path, login
        if key not in cache:
            cache[key] = resolver(command, path, login, home)
        return cache[key]

    try:
        if not directory.is_dir():
            raise ValueError(f"LaunchAgents directory {directory}")
        files = sorted(directory.iterdir())
    except (OSError, ValueError) as exc:
        return {"status": "unmeasured", "jobs": None, "bad": None, "rows": [], "needs": [str(exc)]}
    for file in files:
        if file.suffix != ".plist":
            continue
        try:
            job = plistlib.loads(file.read_bytes())
            label = job.get("Label", "")
            if not isinstance(label, str) or not label.startswith(prefixes):
                continue
        except (
            OSError,
            ValueError,
            TypeError,
            AttributeError,
            plistlib.InvalidFileException,
            ExpatError,
        ) as exc:
            needs.append(f"readable plist {file.name} ({type(exc).__name__})")
            continue
        try:
            row = inspect_job(job, home=home, resolver=cached)
        except (
            OSError,
            ValueError,
            TypeError,
            SyntaxError,
            tokenize.TokenError,
            subprocess.SubprocessError,
        ) as exc:
            needs.append(f"{label}: {exc}")
            row = {"class": "unmeasured"}
        rows.append({"label": label, **row})
    offenders = [row["label"] for row in rows if row["class"] in BAD]
    other = [row["label"] for row in rows if row["class"] == "other"]
    bad = len(offenders) + len(other)
    if not rows and not needs:
        needs.append(">=1 matching job")
    return {
        "status": "unmeasured" if needs else "measured",
        "jobs": len(rows),
        "bad": bad,
        "offenders": offenders,
        "drainable": bad,
        "other": other,
        "rows": rows,
        "needs": needs,
    }


def names(items: list[str]) -> str:
    return ", ".join(items[:5]) + (f", +{len(items) - 5} more" if len(items) > 5 else "")


def format_line(report: dict) -> str:
    if report.get("jobs") is None:
        return "Python standard: unmeasured — would need " + "; ".join(
            report.get("needs") or ["an audit snapshot"]
        )
    if report["jobs"] == 0:
        return "Python standard: unmeasured — would need " + "; ".join(
            report.get("needs") or [">=1 matching job"]
        )
    if report["jobs"] and not report["bad"] and report.get("status") == "measured":
        return f"Python standard: all {report['jobs']} scheduled entrypoints resolve to 3.14/3.13 (drained)"
    state = "blocking" if report["bad"] else "unmeasured"
    other = report.get("other", [])
    line = (
        f"Python standard: {report['bad']}/{report['jobs']} scheduled entrypoints on unsupported Python ({state}); "
        f"offenders: {names(report['offenders']) or 'none'}; "
        f"other: {len(other)} ({names(other) or 'none'}); drainable: {report['drainable']} "
        "(each is a one-line change: see ~/.codex/orchestrator/python-migration/inventory.md)"
    )
    if report.get("status") != "measured":
        line += "; unmeasured: " + "; ".join(report.get("needs") or ["an audit snapshot"])
    return line


def snapshot() -> dict:
    """Preserve the weekly artifact even if an unexpected reader/probe failure occurs."""
    try:
        return audit()
    except Exception as exc:  # noqa: BLE001 — this health reader cannot discard the weekly sweep
        return {
            "status": "unmeasured",
            "jobs": None,
            "bad": None,
            "rows": [],
            "needs": [f"a working scheduled-Python audit ({type(exc).__name__})"],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = snapshot()
    print(json.dumps(report, indent=2) if args.json else format_line(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
