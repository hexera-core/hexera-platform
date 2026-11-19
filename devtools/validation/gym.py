#!/usr/bin/env python3
"""The corpus gym: a v3 policy or prompt change is scored on a stratified sample before it ships.

Spec pillar 5. Three jobs, and no CAD in any of them:

  policy   print the fingerprint of every owner-tunable v3 policy constant, and each value
  score    compare two corpus bundles run on the SAME stratified sample: what the arm newly
           meshes, what it newly breaks, what it does to the clean control, the score mean, the
           cost per fixed part, and all of that per stratum
  record   append {policy, evidence, sample_sha256, summary} to gym/scored-policies.json, which is
           what `tests/unit/dev/test_policy_is_gym_scored.py` gates on

THE FINGERPRINT IS READ FROM SOURCE, NOT IMPORTED. Importing `cad.repair.oracle` to read a float
would drag the repair stack - and on some paths gmsh - into whatever process runs the host gate.
The constants are read with `ast`, so this module stays hermetic, millisecond-fast, importable with
no dependencies, and able to describe a policy module a concurrent lane has not landed yet (that
one reads `<absent>`, and the fingerprint MOVES the day the lane lands it, which is correct: an
unlanded knob and a landed knob are different policies).

The sample, the strata and the manifest helpers are repair_corpus.py's and are consumed, never
reimplemented, so a gym stratum is by construction the same stratum the sample was drawn on.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import statistics
import sys
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
POLICY_DB = Path(__file__).resolve().parent / "gym" / "scored-policies.json"

#: A declared policy whose module or attribute does not exist in the tree being read. Declared and
#: absent is INFORMATION - it says a lane still owes this knob - and is never silently dropped.
ABSENT = "<absent>"

#: Bumped only if the fingerprint's own recipe changes, which invalidates every recorded score.
FINGERPRINT_SCHEMA = 1


class PolicySource(NamedTuple):
    """One owner-tunable policy constant: where it lives and how it enters the fingerprint."""

    label: str
    relpath: str
    attr: str
    #: A prompt is thousands of characters. `hashed` enters its sha256 instead of its text, so the
    #: record stays readable and no prompt is duplicated into a committed JSON file.
    hashed: bool = False


#: THE POLICY SURFACE, from the master plan's Task 25 interface list. Every entry is a knob an
#: owner can turn without touching logic - exactly the changes a diff review waves through and a
#: corpus pass catches. Adding a knob here is how a later lane puts it under the gate.
POLICY_SOURCES: tuple[PolicySource, ...] = (
    PolicySource("investigator_budget.BUDGETS",
                 "meshpipeline/cad/repair/investigator_budget.py", "BUDGETS"),
    PolicySource("grind.ATTEMPTS", "meshpipeline/cad/repair/grind.py", "ATTEMPTS"),
    PolicySource("grind.HEAVY_FACES", "meshpipeline/cad/repair/grind.py", "HEAVY_FACES"),
    PolicySource("tournament.K_MIN", "meshpipeline/cad/repair/tournament.py", "K_MIN"),
    PolicySource("tournament.K_MAX", "meshpipeline/cad/repair/tournament.py", "K_MAX"),
    PolicySource("tournament.REHEARSAL_MIN_S",
                 "meshpipeline/cad/repair/tournament.py", "REHEARSAL_MIN_S"),
    PolicySource("review_tier.VERTEX_MOVING_OPS",
                 "meshpipeline/cad/repair/review_tier.py", "VERTEX_MOVING_OPS"),
    PolicySource("memory.NOTES_PER_TURN", "meshpipeline/cad/repair/memory.py", "NOTES_PER_TURN"),
    PolicySource("oracle.MESH_PROBE_FRACTION",
                 "meshpipeline/cad/repair/oracle.py", "MESH_PROBE_FRACTION"),
    PolicySource("oracle.FINAL_PROBE_SLACK_S",
                 "meshpipeline/cad/repair/oracle.py", "FINAL_PROBE_SLACK_S"),
    PolicySource("oracle.TIERS", "meshpipeline/cad/repair/oracle.py", "TIERS"),
    PolicySource("investigator.INVESTIGATOR_SYSTEM",
                 "meshpipeline/cad/repair/investigator.py", "INVESTIGATOR_SYSTEM", hashed=True),
)
PROMPT_SOURCE = POLICY_SOURCES[-1]


# --- the policy fingerprint ---------------------------------------------------------------------
def _assigned_source(path: Path, attr: str) -> str | None:
    """The source text of the module-level expression assigned to `attr`, or None.

    Source text, not an evaluated value: `ATTEMPTS = _from_env(3)` cannot be evaluated without
    importing, and its TEXT changing is exactly as much of a policy change as a literal's would be.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        # A TUPLE, not `ast.Assign | ast.AnnAssign`: this module is deliberately runnable by any
        # stock python on the box, and `type | type` needs 3.10.
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):  # noqa: UP038
            continue
        targets = list(node.targets) if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(t, ast.Name) and t.id == attr for t in targets) and node.value:
            return ast.unparse(node.value)
    return None


def policy_constants(root: Path = SRC) -> dict[str, str]:
    """label -> the constant's source text (or its sha256, for a prompt), or `ABSENT`."""
    out: dict[str, str] = {}
    for source in POLICY_SOURCES:
        text = _assigned_source(Path(root) / source.relpath, source.attr)
        if text is None:
            out[source.label] = ABSENT
            continue
        if source.hashed:
            try:  # a plain string literal hashes its TEXT, so reflowing it is not a policy change
                value = ast.literal_eval(text)
                text = value if isinstance(value, str) else text
            except (ValueError, SyntaxError):
                pass
            text = hashlib.sha256(text.encode("utf-8")).hexdigest()
        out[source.label] = text
    return out


def policy_fingerprint(root: Path = SRC) -> str:
    """The sha256 of the canonical JSON of every declared policy constant."""
    payload = {"schema": FINGERPRINT_SCHEMA, "constants": policy_constants(root)}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- the corpus helpers, consumed from repair_corpus.py -----------------------------------------
def _repair_corpus():
    """Path-load the sibling module. `devtools/validation` is not a package, so this is the
    established pattern here (repair_corpus does the same for ITS siblings)."""
    path = Path(__file__).resolve().parent / "repair_corpus.py"
    spec = importlib.util.spec_from_file_location("repair_corpus", path)
    if not spec or not spec.loader:  # pragma: no cover - a broken checkout
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def stratum_label(row: dict) -> str:
    """`family/solids` for one row, from repair_corpus's own `stratum_of`."""
    family, solids = _repair_corpus().stratum_of(row)
    return f"{family}/{solids}"


# --- reading a bundle ---------------------------------------------------------------------------
def bundle_sample_sha256(directory: Path) -> str:
    """The sha256 of the bundle's `sample.json` bytes - the arm's whole provenance.

    Refuses rather than guessing: an arm that did not record the sample it ran cannot be compared
    to anything, because "the same sample" would be an assumption instead of a hash.
    """
    directory = Path(directory)
    path = directory / "sample.json"
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = directory / "manifest.json"
    if manifest.is_file():
        stated = (json.loads(manifest.read_text(encoding="utf-8")) or {}).get("sample_sha256")
        if stated:
            return str(stated)
    raise ValueError(f"{directory} records no sample.json (and no sample_sha256 in manifest.json): "
                     f"an arm with no stated sample cannot be scored")


def read_rows(directory: Path) -> list[dict]:
    """The bundle's rows. `rows.jsonl` is the bundle format; `rows.json` is a --json run."""
    directory = Path(directory)
    lines = directory / "rows.jsonl"
    if lines.is_file():
        return [json.loads(line) for line in lines.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    plain = directory / "rows.json"
    if plain.is_file():
        loaded = json.loads(plain.read_text(encoding="utf-8"))
        rows = loaded.get("rows") if isinstance(loaded, dict) else loaded
        return list(rows or [])
    raise ValueError(f"{directory} holds no rows.jsonl or rows.json")


def effective_after(row: dict) -> bool | None:
    """Did the part mesh at the END of the run? A file whose repair never ran keeps its before
    verdict - counting an unattempted file as a failure is how an arm invents a regression. This
    mirrors repair_corpus's own mesh summary exactly."""
    if "meshes_before" not in row:
        return None
    return row["meshes_before"] if row.get("meshes_after") is None else row["meshes_after"]


# --- the comparison -----------------------------------------------------------------------------
def _arm_numbers(rows: list[dict]) -> dict:
    scored = [r for r in rows if "meshes_before" in r]
    after = [effective_after(r) for r in scored]
    values = [r["tier2_score"] for r in scored if r.get("tier2_score") is not None]
    newly_meshed = sum(1 for r, ok in zip(scored, after, strict=True)
                       if ok and not r["meshes_before"])
    seconds = sum(float(r.get("seconds") or 0.0) for r in scored)
    return {
        "files": len(scored),
        "newly_meshed": newly_meshed,
        "newly_broken": sum(1 for r, ok in zip(scored, after, strict=True)
                            if r["meshes_before"] and not ok),
        "clean_files": sum(1 for r in scored if r["meshes_before"]),
        "clean_still_meshes": sum(1 for r, ok in zip(scored, after, strict=True)
                                  if r["meshes_before"] and ok),
        "score_mean": round(statistics.fmean(values), 4) if values else None,
        "seconds": round(seconds, 2),
        # COST PER FIXED PART. None, not zero, when nothing was fixed: an arm that fixed nothing
        # has no cost per fix, and a 0.0 there would read as "free".
        "cost_per_fixed_part_s": round(seconds / newly_meshed, 2) if newly_meshed else None,
    }


def _pair(arm: dict, baseline: dict, key: str) -> dict:
    a, b = arm[key], baseline[key]
    return {"arm": a, "baseline": b, "delta": a - b}


def compare(arm: list[dict], baseline: list[dict]) -> dict:
    """The two arms' numbers over the files BOTH ran, headline and per stratum. Pure.

    Only the intersection is scored: a file one arm crashed on and the other never reached is not
    evidence about the policy, and silently averaging over different file sets is the classic way
    a gym reports a win that is really a missing row.
    """
    arm_by_sha = {str(r.get("sha256", "")): r for r in arm}
    base_by_sha = {str(r.get("sha256", "")): r for r in baseline}
    shared = sorted(set(arm_by_sha) & set(base_by_sha))
    arm_rows = [arm_by_sha[s] for s in shared]
    base_rows = [base_by_sha[s] for s in shared]
    a, b = _arm_numbers(arm_rows), _arm_numbers(base_rows)

    strata: dict[str, dict] = {}
    for sha in shared:
        strata.setdefault(stratum_label(arm_by_sha[sha]), {"arm": [], "baseline": []})
        strata[stratum_label(arm_by_sha[sha])]["arm"].append(arm_by_sha[sha])
        strata[stratum_label(arm_by_sha[sha])]["baseline"].append(base_by_sha[sha])
    per_stratum = {}
    for label in sorted(strata):
        sa, sb = _arm_numbers(strata[label]["arm"]), _arm_numbers(strata[label]["baseline"])
        per_stratum[label] = {
            "files": sa["files"],
            "newly_meshed": _pair(sa, sb, "newly_meshed"),
            "newly_broken": _pair(sa, sb, "newly_broken"),
            "score_mean": {"arm": sa["score_mean"], "baseline": sb["score_mean"]},
        }

    return {
        "files": len(shared),
        "arm_only_files": sorted(set(arm_by_sha) - set(base_by_sha)),
        "baseline_only_files": sorted(set(base_by_sha) - set(arm_by_sha)),
        "newly_meshed": _pair(a, b, "newly_meshed"),
        "newly_broken": _pair(a, b, "newly_broken"),
        # THE CLEAN CONTROL: the parts that already meshed. A policy that fixes two and breaks one
        # clean part is not an improvement, and this is the line that says so.
        "clean_control": {"files": a["clean_files"],
                          "arm_still_meshes": a["clean_still_meshes"],
                          "baseline_still_meshes": b["clean_still_meshes"],
                          "delta": a["clean_still_meshes"] - b["clean_still_meshes"]},
        "score_mean": {"arm": a["score_mean"], "baseline": b["score_mean"]},
        "cost_per_fixed_part_s": {"arm": a["cost_per_fixed_part_s"],
                                  "baseline": b["cost_per_fixed_part_s"]},
        "seconds": {"arm": a["seconds"], "baseline": b["seconds"]},
        "per_stratum": per_stratum,
    }


def score(arm_dir: Path, baseline_dir: Path) -> dict:
    """Compare two bundles. REFUSES unless both ran the same sample.json."""
    arm_sha = bundle_sample_sha256(arm_dir)
    base_sha = bundle_sample_sha256(baseline_dir)
    if arm_sha != base_sha:
        raise ValueError(
            f"the two arms ran DIFFERENT samples, so the comparison would measure the sample and "
            f"not the policy: arm {arm_sha[:12]} ({arm_dir}) vs baseline {base_sha[:12]} "
            f"({baseline_dir}). Re-run both on one `--sample N --seed S` draw.")
    out = compare(read_rows(arm_dir), read_rows(baseline_dir))
    out["sample_sha256"] = arm_sha
    return out


def render(out: dict) -> str:
    """The comparison as an operator reads it."""
    def pair(key: str) -> str:
        entry = out[key]
        return f"{entry['arm']} vs {entry['baseline']} ({entry['delta']:+d})"

    clean = out["clean_control"]
    lines = [
        f"sample           {out['sample_sha256'][:16]}  ({out['files']} file(s) in both arms)",
        f"newly meshed     {pair('newly_meshed')}",
        f"newly broken     {pair('newly_broken')}",
        f"clean control    {clean['arm_still_meshes']}/{clean['files']} vs "
        f"{clean['baseline_still_meshes']}/{clean['files']} ({clean['delta']:+d})",
        f"score mean       {out['score_mean']['arm']} vs {out['score_mean']['baseline']}",
        f"cost per fix     {out['cost_per_fixed_part_s']['arm']} s vs "
        f"{out['cost_per_fixed_part_s']['baseline']} s",
        "per stratum:",
    ]
    for label, entry in out["per_stratum"].items():
        lines.append(f"  {label:<34} n={entry['files']:<5} "
                     f"fixed {entry['newly_meshed']['arm']} vs "
                     f"{entry['newly_meshed']['baseline']}  "
                     f"broke {entry['newly_broken']['arm']} vs "
                     f"{entry['newly_broken']['baseline']}")
    return "\n".join(lines)


# --- the record the gate reads ------------------------------------------------------------------
def _load_db(db: Path) -> dict:
    try:
        loaded = json.loads(Path(db).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"records": []}
    return loaded if isinstance(loaded, dict) else {"records": list(loaded)}


def scored_fingerprints(db: Path = POLICY_DB) -> list[str]:
    """Every policy fingerprint that has been scored, in the order recorded."""
    return [str(r.get("policy", "")) for r in _load_db(db).get("records") or []
            if r.get("policy")]


def record(evidence: Path, *, baseline: Path | None = None, db: Path = POLICY_DB,
           fingerprint: str = "") -> dict:
    """Append one scored policy to `db` and return the entry.

    NO CAD, BY CONSTRUCTION: the entry carries the evidence directory (relative to the repo), the
    sample's sha256, the headline numbers, and nothing else. Row-level file names never enter it -
    a corpus file's name is whatever the root it was found under calls it, and committing one is
    how a geometry path leaks into git.
    """
    evidence = Path(evidence)
    summary = (score(evidence, baseline) if baseline is not None
               else {"sample_sha256": bundle_sample_sha256(evidence),
                     **{k: v for k, v in compare(read_rows(evidence), read_rows(evidence)).items()
                        if k != "per_stratum"}})
    try:
        where = str(evidence.resolve().relative_to(REPO))
    except ValueError:
        where = evidence.name
    entry = {
        "policy": fingerprint or policy_fingerprint(),
        "evidence": where,
        "sample_sha256": summary["sample_sha256"],
        "summary": {k: summary[k] for k in
                    ("files", "newly_meshed", "newly_broken", "clean_control", "score_mean",
                     "cost_per_fixed_part_s") if k in summary},
    }
    loaded = _load_db(db)
    loaded.setdefault("records", []).append(entry)
    Path(db).write_text(json.dumps(loaded, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return entry


def unscored_reason(fingerprint: str, *, db: Path = POLICY_DB,
                    labels: list[str] | None = None) -> str | None:
    """None if this policy has been scored; otherwise WHY the gate fails, in words to act on."""
    if fingerprint in scored_fingerprints(db):
        return None
    known = scored_fingerprints(db)
    listing = ("\n  policies under the gate: " + ", ".join(labels)) if labels else ""
    return (
        f"THIS POLICY HAS NEVER BEEN SCORED ON THE CORPUS.\n"
        f"  fingerprint   {fingerprint}\n"
        f"  record        {db}\n"
        f"  scored so far {', '.join(f[:12] for f in known) or 'nothing'}"
        f"{listing}\n"
        f"A v3 policy constant or the investigator prompt changed, so every recorded score was "
        f"measured under a different policy. Score the change on a stratified sample of the "
        f"9,956 and record it:\n"
        f"    make gym ARM=<new-bundle> BASELINE=<old-bundle>\n"
        f"then commit the updated {Path(db).name}. Do not edit the record by hand: a fingerprint "
        f"nobody ran is worse than no record at all.")


# --- CLI ----------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("policy", help="print the policy fingerprint and every constant in it")

    scorer = sub.add_parser("score", help="compare two bundles run on the same sample")
    scorer.add_argument("--arm", required=True, help="the candidate policy's bundle directory")
    scorer.add_argument("--baseline", required=True, help="the bundle it must beat")
    scorer.add_argument("--out", default="", help="also write the comparison JSON here")

    recorder = sub.add_parser("record", help="append a scored policy to scored-policies.json")
    recorder.add_argument("--evidence", required=True, help="the arm's bundle directory")
    recorder.add_argument("--baseline", default="", help="the baseline bundle, if there was one")
    recorder.add_argument("--db", default=str(POLICY_DB), help="the record to append to")
    recorder.add_argument("--fingerprint", default="",
                          help="override the fingerprint (only for a replayed historical run)")

    args = parser.parse_args(argv)

    if args.command == "policy":
        constants = policy_constants()
        print(f"policy fingerprint {policy_fingerprint()}")
        for label in sorted(constants):
            value = constants[label]
            print(f"  {label:<36} {value if len(value) <= 60 else value[:57] + '...'}")
        return 0

    if args.command == "score":
        try:
            out = score(Path(args.arm), Path(args.baseline))
        except ValueError as exc:
            print(f"refusing to score: {exc}", file=sys.stderr)
            return 2
        print(render(out))
        if args.out:
            Path(args.out).write_text(json.dumps(out, indent=2, sort_keys=True) + "\n",
                                      encoding="utf-8")
            print(f"written {args.out}")
        return 0

    try:
        entry = record(Path(args.evidence),
                       baseline=Path(args.baseline) if args.baseline else None,
                       db=Path(args.db), fingerprint=args.fingerprint)
    except ValueError as exc:
        print(f"refusing to record: {exc}", file=sys.stderr)
        return 2
    print(f"recorded policy {entry['policy']} from {entry['evidence']} "
          f"(sample {entry['sample_sha256'][:16]})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
