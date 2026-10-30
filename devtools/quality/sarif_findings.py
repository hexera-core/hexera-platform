#!/usr/bin/env python3
# Responsibility: Print a scanner's SARIF findings in the job log, and with --gate fail on NEW serious ones.
# Boundaries: it reads SARIF files and the checked-in baseline; it runs no scanner and uploads nothing.
"""Show SARIF findings where a reader can see them, and gate on them when nothing else will.

With code scanning available, findings go to the Security tab and its pull-request check gates
them. Without it (GitHub Advanced Security off), the SARIF never leaves the runner, so the lane
calls this with --gate: every finding is printed, and the job fails on the serious ones code
scanning's default check would fail on - a security severity of 7.0 or more (high, critical),
or for a rule without a security severity, the `error` level.

Serious findings that already existed when the gate was introduced are listed in a baseline
(--baseline) so they are shown but do not fail every run; a NEW one does. Like the mypy ratchet,
the baseline is meant to shrink: fix a listed finding and delete its entry. Entries are matched
on rule, file and CodeQL's line-content fingerprint, so moving code does not unlist a finding,
and changing the flagged line makes it count as new and be looked at again.

A run that produced no SARIF at all is no verdict, and no verdict never passes.

Regenerate the baseline from a run's SARIF artifact (only to accept findings someone reviewed):
    python devtools/quality/sarif_findings.py --write-baseline devtools/quality/codeql_baseline.json python.sarif
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

BLOCKING_SECURITY_SEVERITY = 7.0


@dataclass(frozen=True)
class Finding:
    rule: str
    level: str
    security_severity: float | None
    path: str
    line: int | None
    message: str
    fingerprint: str = ""

    @property
    def serious(self) -> bool:
        if self.security_severity is not None:
            return self.security_severity >= BLOCKING_SECURITY_SEVERITY
        return self.level == "error"

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}" if self.line else self.path

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.rule, self.path, self.fingerprint or f"line:{self.line}")


def _rules(run: dict) -> dict[tuple[int | None, int], dict]:
    """Rules keyed by (tool component index, rule index); the driver is component None."""
    tool = run.get("tool", {})
    keyed: dict[tuple[int | None, int], dict] = {}
    for index, rule in enumerate(tool.get("driver", {}).get("rules", []) or []):
        keyed[(None, index)] = rule
    for component, extension in enumerate(tool.get("extensions", []) or []):
        for index, rule in enumerate(extension.get("rules", []) or []):
            keyed[(component, index)] = rule
    return keyed


def _rule_for(result: dict, rules: dict[tuple[int | None, int], dict], by_id: dict[str, dict]) -> dict:
    ref = result.get("rule") or {}
    if "index" in ref:
        component = (ref.get("toolComponent") or {}).get("index")
        found = rules.get((component, ref["index"]))
        if found is not None:
            return found
    if "ruleIndex" in result:
        found = rules.get((None, result["ruleIndex"]))
        if found is not None:
            return found
    rule_id = result.get("ruleId") or ref.get("id") or ""
    return by_id.get(rule_id, {})


def _severity(rule: dict) -> float | None:
    raw = (rule.get("properties") or {}).get("security-severity")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _suppressed(result: dict) -> bool:
    return any(s.get("status", "accepted") == "accepted" for s in result.get("suppressions") or [])


def findings(document: dict) -> list[Finding]:
    out: list[Finding] = []
    for run in document.get("runs", []) or []:
        rules = _rules(run)
        by_id = {r.get("id", ""): r for r in rules.values()}
        for result in run.get("results", []) or []:
            if _suppressed(result):
                continue
            rule = _rule_for(result, rules, by_id)
            level = (
                result.get("level")
                or (rule.get("defaultConfiguration") or {}).get("level")
                or "warning"
            )
            location = ((result.get("locations") or [{}])[0]).get("physicalLocation") or {}
            out.append(Finding(
                rule=result.get("ruleId") or rule.get("id") or "?",
                level=level,
                security_severity=_severity(rule),
                path=(location.get("artifactLocation") or {}).get("uri", "?"),
                line=(location.get("region") or {}).get("startLine"),
                message=" ".join(((result.get("message") or {}).get("text") or "").split()),
                fingerprint=(result.get("partialFingerprints") or {}).get("primaryLocationLineHash", ""),
            ))
    return out


def _sarif_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        files.extend(sorted(path.rglob("*.sarif")) if path.is_dir() else [path] if path.is_file() else [])
    return files


def load_baseline(path: Path) -> set[tuple[str, str, str]]:
    entries = json.loads(path.read_text(encoding="utf-8")).get("findings", [])
    return {(e["rule"], e["path"], e["fingerprint"]) for e in entries}


def write_baseline(path: Path, found: list[Finding]) -> int:
    entries = [
        {"rule": f.rule, "path": f.path, "fingerprint": f.key[2], "line": f.line, "message": f.message}
        for f in sorted({f.key: f for f in found if f.serious}.values(), key=lambda f: f.key)
    ]
    path.write_text(json.dumps({"findings": entries}, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(entries)} baselined finding(s) to {path}")
    return 0


def _escape(text: str) -> str:
    """Workflow-command data escaping, so a message cannot end or forge the annotation."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape(text).replace(":", "%3A").replace(",", "%2C")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("paths", nargs="+", help="SARIF files, or directories searched for *.sarif")
    parser.add_argument("--gate", action="store_true",
                        help="exit 1 on a serious finding that is not in the baseline")
    parser.add_argument("--baseline", type=Path, help="JSON of serious findings already accepted")
    parser.add_argument("--write-baseline", type=Path, help="write every serious finding here and exit")
    parser.add_argument("--title", default="Scanner", help="names the scanner in the output")
    args = parser.parse_args(argv)
    title = _escape_property(args.title)

    files = _sarif_files(args.paths)
    if not files:
        print(f"::error title={title} produced no SARIF::"
              f"no *.sarif under {', '.join(args.paths)} - the scan gave no verdict")
        return 2

    found: list[Finding] = []
    for file in files:
        try:
            found.extend(findings(json.loads(file.read_text(encoding="utf-8"))))
        except (OSError, ValueError) as exc:
            print(f"::error title={title} SARIF unreadable::{file}: {_escape(str(exc))}")
            return 2

    if args.write_baseline:
        return write_baseline(args.write_baseline, found)

    try:
        baseline = load_baseline(args.baseline) if args.baseline else set()
    except (OSError, ValueError, KeyError) as exc:
        print(f"::error title={title} baseline unreadable::{args.baseline}: {_escape(str(exc))}")
        return 2

    def status(f: Finding) -> str:
        if not f.serious:
            return "minor"
        return "baselined" if f.key in baseline else "NEW"

    order = {"NEW": 0, "baselined": 1, "minor": 2}
    ranked = sorted(found, key=lambda f: (order[status(f)], f.path, f.line or 0))
    new = [f for f in found if status(f) == "NEW"]
    baselined = [f for f in found if status(f) == "baselined"]
    stale = baseline - {f.key for f in found}

    print(f"{args.title}: {len(found)} finding(s) in {len(files)} SARIF file(s): {len(new)} new serious "
          f"(high/critical or error level), {len(baselined)} serious already in the baseline, "
          f"{len(found) - len(new) - len(baselined)} minor.")
    for f in ranked:
        severity = f"{f.security_severity:.1f}" if f.security_severity is not None else "-"
        print(f"  [{status(f):9}] {f.rule} (level {f.level}, severity {severity}) {f.where}: {f.message}")
        if status(f) != "NEW":
            continue
        kind = "error" if args.gate else "warning"
        props = f"file={_escape_property(f.path)},title={_escape_property(f.rule)}"
        if f.line:
            props += f",line={f.line}"
        print(f"::{kind} {props}::{_escape(f.message)}")
    for rule, path, _ in sorted(stale):
        print(f"  [gone     ] {rule} {path}: listed in the baseline but no longer found - delete its entry")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### {args.title} findings\n\n{len(found)} finding(s): {len(new)} new serious, "
                         f"{len(baselined)} baselined serious.\n\n")
            if found:
                handle.write("| Status | Rule | Severity | Where | Message |\n|---|---|---|---|---|\n")
                for f in ranked:
                    severity = f"{f.security_severity:.1f}" if f.security_severity is not None else f.level
                    message = f.message.replace("|", "\\|")
                    handle.write(f"| {status(f)} | `{f.rule}` | {severity} | `{f.where}` | {message} |\n")

    if args.gate and new:
        print(f"{args.title}: {len(new)} new serious finding(s); fix them, or suppress each with a reason.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
