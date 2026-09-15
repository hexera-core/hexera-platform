#!/usr/bin/env python3
# Responsibility: Prove every flag the deploy scripts pass to gcloud is a flag the INSTALLED gcloud still accepts.
# Owns: finding the gcloud invocations in the shell scripts and deciding which flags they pass.
# Boundaries: it reads `--help` and nothing else; it makes no authenticated call and mutates nothing.

# STALE-FLAG GATE for the deploy scripts.
#
# gcloud removes flags. `--min-ready` was valid on `rolling-action start-update` when the fleet roll
# was written and is now absent from every track; passing it aborts the command with
# `unrecognized arguments`, exit 2. That stopped the v0.1.5 release at stage 18 of 19 - two from the
# end, after the images were built, the schema migrated and the services rolled.
#
# NOTHING IN THE REPOSITORY COULD HAVE CAUGHT IT. The scripts are shell, so a removed flag is not a
# type error; a merge to main deploys `images,migrate,console,admin` and never rolls the fleet, so
# the stage that carried the flag only ran on a tag. The flag went stale in the gap between releases
# and the release is where it surfaced. Fixing the one flag leaves every other flag in the deploy
# with the same property: valid when written, unverified since.
#
# This reads the scripts, collects what each gcloud invocation actually passes, and asks the
# installed gcloud whether those flags exist. It is the whole surface, checked in one pass, against
# the CLI the operator will really run.
#
#   python devtools/quality/check_gcloud_flags.py            # every deploy script
#   python devtools/quality/check_gcloud_flags.py FILE [...] # named scripts
#   --jobs N                                                 # parallel `--help` reads (default 12)
#
# Exit 0 when every flag resolves, 1 when any does not, and 77 when gcloud is not installed - a
# distinct code so a caller can tell "checked and clean" from "could not check", which is the
# distinction Gate D exists to keep.
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Exit code for "gcloud is not installed", kept apart from a clean pass so a caller never reads an
#: unrun check as a passed one.
EXIT_NO_GCLOUD = 77

#: Flags every gcloud command accepts. `--help` prints these as the single token `GCLOUD_WIDE_FLAG`
#: rather than listing them, so they cannot be discovered from the help text and are named here.
WIDE_FLAGS = {
    "--account", "--billing-project", "--configuration", "--flags-file", "--flatten", "--format",
    "--help", "--impersonate-service-account", "--log-http", "--project", "--quiet", "--verbosity",
    "--access-token-file", "--trace-token", "--user-output-enabled", "--no-user-output-enabled",
}

#: Shell words that leave the NEXT word in command position (`if gcloud ... ; then`).
COMMAND_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "time", "exec"}

#: Where a command ENDS. Flags after one of these belong to the next command, not this one.
COMMAND_TERMINATORS = ("|", "&&", "||", ";", ")")


@dataclass(frozen=True)
class Invocation:
    """One `gcloud ...` call found in a script."""

    path: Path
    line: int
    command: tuple[str, ...]
    flags: frozenset[str]


def _code_lines(text: str) -> list[str | None]:
    """The script with comments, heredoc bodies and multi-line string bodies removed.

    `None` marks a line that is not code the parser should read - a comment, a heredoc body, or the
    continuation of a string opened on an earlier line. That last case is why this exists: a
    multi-line `die "... run: gcloud beta run jobs executions logs read ..."` puts what looks like an
    invocation at the start of a line, and reading it as one reports a flag error against advice
    printed to a human. Quote state is therefore carried ACROSS lines, not restarted at each.
    """
    out: list[str | None] = []
    quote: str | None = None
    heredoc: str | None = None
    for raw in text.split("\n"):
        if heredoc is not None:
            out.append(None)
            if raw.strip() == heredoc:
                heredoc = None
            continue
        if quote is not None:
            out.append(None)
            # Find the close; everything after it on this line IS code, but a string that ends
            # mid-line is rare enough here that the whole line is conceded rather than half-parsed.
            i = 0
            while i < len(raw):
                if quote == '"' and raw[i] == "\\":
                    i += 2
                    continue
                if raw[i] == quote:
                    quote = None
                    break
                i += 1
            continue
        if raw.strip().startswith("#"):
            out.append(None)
            continue
        code: list[str] = []
        i = 0
        while i < len(raw):
            ch = raw[i]
            if quote is None and ch == "#" and (i == 0 or raw[i - 1].isspace()):
                break
            if quote is None and ch in "'\"":
                quote = ch
            elif quote is not None and ch == quote:
                quote = None
            elif quote == '"' and ch == "\\":
                code.append(" ")
                i += 2
                continue
            code.append(ch)
            i += 1
        line = "".join(code)
        opener = re.search(r"<<-?\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?", line)
        if opener and quote is None:
            heredoc = opener.group(1)
        out.append(line)
    return out


def _strip_quoted(text: str) -> str:
    """Blank out quoted spans, keeping `$(...)` inside them.

    A quoted span is DATA, and data that looks like a flag is not one: the Cloud Run job passes
    `--args "apps/admin-console/outreach-worker.js,--once"`, where `--once` is an argument to node.
    Reporting it as a gcloud flag would be a false failure on a correct script - and a gate that
    cries wolf gets the whole check disabled rather than the one line fixed.
    """
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is None and ch in "'\"":
            quote = ch
            out.append(" ")
        elif quote is not None and ch == quote:
            quote = None
            out.append(" ")
        elif quote == '"' and text.startswith("$(", i):
            # A command substitution inside double quotes runs a command; its text is not data.
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += (text[j] == "(") - (text[j] == ")")
                j += 1
            out.append(text[i:j])
            i = j
            continue
        else:
            out.append(" " if quote else ch)
        i += 1
    return "".join(out)


def _command_offsets(line: str) -> list[int]:
    """Offsets in `line` where a command word may start, ignoring quoted text.

    A `gcloud` anywhere else is a word in another command's arguments, and reading it as a call
    turns `die "gcloud CLI not installed"` into a flag report.
    """
    offsets: list[int] = []
    quote: str | None = None
    at_start = True
    i = 0
    while i < len(line):
        ch = line[i]
        if quote is not None:
            if ch == quote:
                quote = None
            elif quote == '"' and line.startswith("$(", i):
                # A command substitution runs a command even inside a quoted string.
                offsets.append(i + 2)
                i += 2
                continue
            i += 1
            continue
        if line.startswith("$(", i):
            offsets.append(i + 2)
            at_start = True
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            at_start = False
        elif ch.isspace():
            pass
        elif ch in "|&;(){}`":
            at_start = True
        else:
            if at_start:
                offsets.append(i)
            at_start = False
        i += 1
    # A leading shell keyword (`if gcloud ... ; then`) leaves the next word in command position too.
    for match in re.finditer(r"(?<![\w-])([a-z!]+)\s+", line):
        if match.group(1) in COMMAND_KEYWORDS:
            offsets.append(match.end())
    offsets.append(0)
    return sorted(set(offsets))


def _array_flags(text: str, name: str) -> set[str]:
    """Flags assigned to a bash array, for invocations that expand one.

    The Cloud Run stages build `deploy_args=( --image ... )` over thirty lines and then call
    `gc run deploy "${SERVICE}" "${deploy_args[@]}"`. Reading only the call site would check the
    two flags on that line and none of the thirty that actually reach gcloud.
    """
    flags: set[str] = set()
    for match in re.finditer(rf"(?<![\w-]){re.escape(name)}\+?=\(", text):
        depth, i = 1, match.end()
        while i < len(text) and depth:
            depth += (text[i] == "(") - (text[i] == ")")
            i += 1
        body = "\n".join(
            line for line in text[match.end():i].split("\n") if not line.strip().startswith("#")
        )
        flags |= _flags_in(body)
    return flags


def _flags_in(fragment: str) -> set[str]:
    return {
        flag.split("=", 1)[0]
        for flag in re.findall(r"(?<![\w=-])(--[a-zA-Z0-9][a-zA-Z0-9-]*)", _strip_quoted(fragment))
    }


def invocations(path: Path) -> list[Invocation]:
    """Every gcloud call in one script, with its command path and the flags it passes."""
    lines = _code_lines(path.read_text(encoding="utf-8", errors="replace"))
    code = "\n".join(line or "" for line in lines)
    found: list[Invocation] = []
    i = 0
    while i < len(lines):
        raw, start = lines[i], i
        if raw is None:
            i += 1
            continue
        # Join continuations first: a call's flags are usually on the lines below its command.
        while raw.rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            raw = raw.rstrip()[:-1] + " " + (lines[i] or "").strip()
        for offset in _command_offsets(raw):
            word = re.match(r"(gcloud|gc)\s+(.*)", raw[offset:])
            if not word:
                continue
            rest = word.group(2)
            for terminator in COMMAND_TERMINATORS:
                cut = rest.find(terminator)
                if cut >= 0:
                    rest = rest[:cut]
            command: list[str] = []
            for token in rest.split():
                if not re.fullmatch(r"[a-z][a-z0-9-]*", token):
                    break
                command.append(token)
            if not command:
                continue
            flags = _flags_in(rest)
            for array in re.findall(r'\$\{([a-zA-Z_][a-zA-Z0-9_]*)\[@\]\}', rest):
                flags |= _array_flags(code, array)
            found.append(Invocation(path, start + 1, tuple(command), frozenset(flags)))
        i += 1
    return found


#: gcloud's word for "that is not a command", with the token it choked on.
INVALID_CHOICE = re.compile(r"Invalid choice: '([^']+)'")

#: A command group that exists but is not installed on THIS machine. Not a finding: the command is
#: real, and the operator who runs the deploy may well have the component. Reported as unchecked.
NOT_INSTALLED = "do not currently have this command group installed"


@dataclass(frozen=True)
class Surface:
    """What gcloud says about one command path."""

    #: The flags it documents, or None when gcloud has no such command.
    flags: frozenset[str] | None
    #: Why the surface could not be read at all, when it could not be.
    unreadable: str | None = None


def accepted_flags(command: tuple[str, ...]) -> Surface:
    """Ask the installed gcloud what `gcloud <command>` accepts.

    THE LAST WORD MAY BE A POSITIONAL, not a subcommand: `gcloud config get-value account` and
    `gcloud secrets versions access latest` each read as lowercase words all the way down, and the
    tail is an argument. `--help` tolerates a trailing positional, so those resolve on the first
    ask; only a path gcloud rejects is shortened, and only when the token it named is the LAST one.
    A rejected token anywhere earlier is a command that does not exist - `compute autoscalers` -
    and shortening past it would report the parent group's flags as though they were the command's.
    """
    for depth in range(len(command), 0, -1):
        result = subprocess.run(
            ["gcloud", *command[:depth], "--help"],
            capture_output=True,
            text=True,
            check=False,
            # A missing component makes gcloud offer to INSTALL it. This check reads; it does not
            # change the operator's SDK, so the prompt is given nothing to read and gcloud declines.
            stdin=subprocess.DEVNULL,
        )
        if NOT_INSTALLED in result.stdout or NOT_INSTALLED in result.stderr:
            return Surface(None, unreadable=f"the `{command[0]}` component is not installed here")
        if result.returncode != 0:
            rejected = INVALID_CHOICE.search(result.stderr or result.stdout)
            if rejected and rejected.group(1) != command[depth - 1]:
                break
            continue
        documented = set(re.findall(r"--[a-zA-Z0-9][a-zA-Z0-9-]*", result.stdout))
        # `--help` documents a boolean as `--no-x` alone when the default is on, and as `--x` alone
        # when it is off; gcloud accepts both spellings either way.
        for flag in list(documented):
            documented.add(f"--no-{flag[2:]}" if not flag.startswith("--no-") else f"--{flag[5:]}")
        return Surface(frozenset(documented))
    return Surface(None)


def _relative(path: Path) -> str:
    """Repository-relative where possible; a caller may name a script outside the tree."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def default_scripts() -> list[Path]:
    return sorted((ROOT / "deploy").rglob("*.sh"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scripts", nargs="*", type=Path)
    parser.add_argument("--jobs", type=int, default=12)
    args = parser.parse_args(argv)

    if shutil.which("gcloud") is None:
        print("gcloud is not installed - the deploy's flags were NOT checked", file=sys.stderr)
        return EXIT_NO_GCLOUD

    scripts = args.scripts or default_scripts()
    calls = [call for script in scripts for call in invocations(script)]
    commands = sorted({call.command for call in calls})
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        resolved = dict(zip(commands, pool.map(accepted_flags, commands)))

    failures = 0
    unreadable: list[str] = []
    for call in calls:
        where = f"{_relative(call.path)}:{call.line}"
        name = " ".join(call.command)
        surface = resolved[call.command]
        if surface.unreadable is not None:
            note = f"{where}: `gcloud {name}` NOT CHECKED - {surface.unreadable}"
            if note not in unreadable:
                unreadable.append(note)
            continue
        if surface.flags is None:
            print(f"{where}: gcloud has no command `gcloud {name}`")
            failures += 1
            continue
        for flag in sorted(call.flags - surface.flags - WIDE_FLAGS):
            print(f"{where}: `gcloud {name}` does not accept {flag}")
            failures += 1

    for note in unreadable:
        print(note, file=sys.stderr)
    if failures:
        print(
            f"\n{failures} argument(s) the deploy passes are not ones this gcloud accepts. gcloud "
            f"aborts on an unrecognized argument with exit 2, mid-deploy.",
            file=sys.stderr,
        )
        return 1
    print(f"{len(calls)} gcloud invocations across {len(commands)} commands: every flag accepted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
