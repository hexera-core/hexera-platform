# Responsibility: Print one run's "why" record in plain sentences, and replay its decisions against the rulebook.
# Boundaries: an operator tool; it reads a record (a file, or the admin API) and changes nothing.
"""WHY DID THIS RUN DO WHAT IT DID?

    python devtools/why.py <run_reproducibility.json | any JSON holding a "why" record>
    python devtools/why.py <job id> --api https://api.example     (reads ADMIN_API_KEY)

Prints one sentence per decision ("Engine snappy, rule ENG-4: ..."), then replays every decision
from its recorded inputs through the rule functions (contracts/decision_record.replay) and checks
every rule ID it cites is in force in docs/RULES.md. Exit 1 when either check fails.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from meshpipeline.contracts import decision_record as dr  # noqa: E402

RULE_ROW = re.compile(r"^\|\s*([A-Z]+-[A-Za-z0-9-]+)\s*\|\s*([^|]+?)\s*\|")


def in_force_rules(book: Path = ROOT / "docs" / "RULES.md") -> set[str]:
    out = set()
    for line in book.read_text(encoding="utf-8").splitlines():
        m = RULE_ROW.match(line)
        if m and m.group(2).strip().lower() == "in force":
            out.add(m.group(1))
    return out


def find_why(data):
    """The first `why` record ({"decisions": [...]}) anywhere in `data`."""
    if isinstance(data, dict):
        if isinstance(data.get("decisions"), list) and "rules_version" in data:
            return data
        if isinstance(data.get("why"), dict):
            return data["why"]
        for v in data.values():
            got = find_why(v)
            if got is not None:
                return got
    elif isinstance(data, list):
        for v in data:
            got = find_why(v)
            if got is not None:
                return got
    return None


def _load(target: str, api: str | None) -> dict:
    path = Path(target)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    if not api:
        raise SystemExit(f"{target}: not a file; give --api to read a job id from the admin API")
    req = urllib.request.Request(f"{api.rstrip('/')}/api/v1/admin/ops/jobs/{target}/why",
                                 headers={"X-Admin-Key": os.environ.get("ADMIN_API_KEY", "")})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - an operator-given URL
        return json.loads(resp.read().decode())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="a JSON file holding a why record, or a job id (with --api)")
    ap.add_argument("--api", help="the product API base URL, to read a job id's record")
    ap.add_argument("--json", action="store_true", help="print the record itself")
    args = ap.parse_args(argv)

    why = find_why(_load(args.target, args.api))
    if why is None:
        print("no why record found", file=sys.stderr)
        return 1
    decisions = [d for d in why.get("decisions") or [] if isinstance(d, dict)]
    if args.json:
        print(json.dumps(why, indent=1, default=str))
    else:
        print(f"rulebook version {why.get('rules_version')} - {len(decisions)} decisions")
        for line in dr.sentences(decisions):
            print(f"  {line}")
    problems = dr.replay(decisions)
    known = in_force_rules()
    unknown = sorted({r for r in dr.cited_rules(decisions) if r not in known})
    for p in problems:
        print(f"REPLAY DISAGREES: {p}", file=sys.stderr)
    if unknown:
        print(f"NOT IN FORCE IN docs/RULES.md: {', '.join(unknown)}", file=sys.stderr)
    if not problems and not unknown:
        print("replay: every decision follows from its recorded inputs by the rule it names")
    return 1 if problems or unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
