#!/usr/bin/env python3
# Responsibility: Own the release record: its schema, its verdict, and how it is written.
# Boundaries: one authority shared by the gate that writes it and the deploy scripts that read it.
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys
import tempfile

SCHEMA_VERSION = 2

#: Fields that must be present and non-empty before a record may be promoted. A record that
#: cannot name these cannot identify what would ship.
REQUIRED_IDENTITY = (
    "record_schema", "commit", "tree", "product_version",
    "schema_versions.final_result", "schema_versions.pipeline_state",
    "wheel.filename", "wheel.sha256", "wheel.bytes",
)

#: A component is deployable only with all of these.
REQUIRED_COMPONENT_FIELDS = ("local_image_id", "registry_digest", "reference")


def _dig(rec: dict, dotted: str):
    cur = rec
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def compute_verdict(checks: list[dict]) -> str:
    required = [c for c in checks if c.get("required")]
    if not required:
        return "failed"                     # a record with no required checks proves nothing
    if any(c.get("status") == "failed" for c in required):
        return "failed"
    if any(c.get("status") != "passed" for c in required):
        return "incomplete"
    return "passed"


def promotability(rec: dict) -> tuple[bool, list[str]]:
    problems: list[str] = []
    if not isinstance(rec, dict):
        return False, ["the record is not a JSON object"]

    schema = rec.get("record_schema")
    if schema != SCHEMA_VERSION:
        problems.append(f"record_schema {schema!r} is not the supported version {SCHEMA_VERSION}")

    for field in REQUIRED_IDENTITY:
        v = _dig(rec, field)
        if v is None or (isinstance(v, str) and not v.strip()):
            problems.append(f"required field {field} is missing or empty")

    checks = rec.get("checks")
    if not isinstance(checks, list) or not checks:
        problems.append("the record carries no checks")
        checks = []
    verdict = compute_verdict(checks)
    if rec.get("verdict") != verdict:
        problems.append(f"stored verdict {rec.get('verdict')!r} disagrees with the checks "
                        f"({verdict!r}) - the record was edited or written by an older tool")
    if verdict != "passed":
        problems.append(f"verdict is {verdict!r}")
        for c in checks:
            if c.get("required") and c.get("status") != "passed":
                problems.append(f"  required check {c.get('check')!r} is {c.get('status')!r}")

    state = rec.get("state")
    if state != "published":
        problems.append(f"state is {state!r} - nothing has been published, so there is no "
                        f"registry digest to deploy")

    comps = rec.get("components")
    if not isinstance(comps, dict) or not comps:
        problems.append("the record declares no components")
    else:
        for name, c in sorted(comps.items()):
            if not isinstance(c, dict):
                problems.append(f"component {name} is malformed")
                continue
            for f in REQUIRED_COMPONENT_FIELDS:
                v = c.get(f)
                if v is None or (isinstance(v, str) and not v.strip()):
                    problems.append(f"component {name}: {f} is missing")
            ref = c.get("reference") or ""
            if ref and "@sha256:" not in ref:
                problems.append(f"component {name}: reference {ref!r} is tag-only, not digest-qualified")
            dig = c.get("registry_digest") or ""
            if dig and not dig.startswith("sha256:"):
                problems.append(f"component {name}: registry_digest {dig!r} is not a sha256 digest")
            if ref and dig and not ref.endswith(dig):
                problems.append(f"component {name}: reference does not carry its recorded digest")

    return (not problems), problems


def write_atomic(path: pathlib.Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".release-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(rec, fh, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def _tool_versions() -> dict:
    out = {"python": sys.version.split()[0]}
    for name, cmd in (("docker", ["docker", "--version"]), ("git", ["git", "--version"])):
        try:
            out[name] = subprocess.run(cmd, capture_output=True, text=True,
                                       timeout=10).stdout.strip()
        except Exception:
            out[name] = None
    return out


def cmd_write(a: argparse.Namespace) -> int:
    checks = [json.loads(l) for l in pathlib.Path(a.results).read_text().splitlines() if l.strip()]
    facts = json.loads(a.artifact_facts or "{}")
    components = json.loads(a.components)
    for c in components.values():
        c.setdefault("registry_repository", None)
        c.setdefault("publication_tag", None)
        c.setdefault("registry_digest", None)
        c.setdefault("reference", None)

    rec = {
        "record_schema": SCHEMA_VERSION,
        "state": "validated",
        "gate": "C: release-artifact validation",
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "published_at": None,
        "commit": a.commit,
        "tree": a.tree,
        "branch": a.branch,
        "clean_tree": True,
        "product_version": facts.get("product_version"),
        "schema_versions": {"final_result": facts.get("final_result"),
                            "pipeline_state": facts.get("pipeline_state")},
        "artifact_facts_read_from": "the app image" if facts else "UNAVAILABLE",
        "wheel": {"filename": a.wheel_name, "sha256": a.wheel_sha256,
                  "bytes": int(a.wheel_bytes)},
        "local_image_tag": a.image_tag,
        "components": components,
        "checks": checks,
        "verdict": compute_verdict(checks),
        "tool_versions": _tool_versions(),
    }
    out = pathlib.Path(a.out)
    write_atomic(out, rec)
    promotable, reasons = promotability(rec)
    print(f"   wrote {out}")
    n = {s: sum(1 for c in checks if c.get("status") == s)
         for s in ("passed", "failed", "skipped", "not_run")}
    print(f"   verdict: {rec['verdict']}   required checks: "
          f"{n['passed']} passed, {n['failed']} failed, "
          f"{n['skipped']} skipped, {n['not_run']} not run")
    print(f"   state: {rec['state']} (promotable: {promotable})")
    return 0 if rec["verdict"] == "passed" else 1


def cmd_verdict(a: argparse.Namespace) -> int:
    try:
        rec = json.loads(pathlib.Path(a.record).read_text())
    except Exception:
        print("failed")
        return 1
    print(compute_verdict(rec.get("checks") or []))
    return 0


def cmd_check(a: argparse.Namespace) -> int:
    p = pathlib.Path(a.record)
    if not p.is_file():
        print(f"no release record at {p}")
        return 1
    try:
        rec = json.loads(p.read_text())
    except Exception as exc:
        print(f"the release record is malformed: {exc}")
        return 1
    promotable, reasons = promotability(rec)
    if promotable:
        print("promotable")
        return 0
    for r in reasons:
        print(r)
    return 1


def cmd_publish_record(a: argparse.Namespace) -> int:
    p = pathlib.Path(a.record)
    rec = json.loads(p.read_text())
    published = json.loads(pathlib.Path(a.published).read_text())
    for name, info in published.items():
        comp = rec.setdefault("components", {}).setdefault(name, {})
        comp.update(info)
    rec["state"] = "published"
    rec["published_at"] = datetime.datetime.now(datetime.UTC).isoformat()
    rec["verdict"] = compute_verdict(rec.get("checks") or [])
    write_atomic(p, rec)
    promotable, reasons = promotability(rec)
    print(f"   record updated: state={rec['state']} promotable={promotable}")
    for r in reasons:
        print(f"   - {r}")
    return 0 if promotable else 1


_CLI_DESCRIPTION = (
    "The release record: Gate C writes it, release-publish promotes it, Gate D reads it. "
    "Only a published record carrying registry digests may be deployed."
)


def main() -> int:
    ap = argparse.ArgumentParser(description=_CLI_DESCRIPTION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write", help="write a fresh Gate C record")
    for f in ("out", "results", "commit", "tree", "branch", "wheel-name", "wheel-sha256",
              "wheel-bytes", "image-tag", "components", "artifact-facts"):
        w.add_argument(f"--{f}", required=True)
    w.add_argument("--schema", default=str(SCHEMA_VERSION))
    w.set_defaults(fn=cmd_write)

    v = sub.add_parser("verdict", help="print the verdict computed from the checks")
    v.add_argument("--record", required=True)
    v.set_defaults(fn=cmd_verdict)

    c = sub.add_parser("check", help="read-only promotability report (exit 0 = promotable)")
    c.add_argument("--record", required=True)
    c.set_defaults(fn=cmd_check)

    p = sub.add_parser("publish-record", help="fold publication results into the record")
    p.add_argument("--record", required=True)
    p.add_argument("--published", required=True)
    p.set_defaults(fn=cmd_publish_record)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
