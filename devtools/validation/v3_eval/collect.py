#!/usr/bin/env python3
# Responsibility: Pull the CRS v3 verdict's shard outputs back from GCS, refuse anything that is not
#                 comparable (a missing shard, a failed shard, mixed commits, images or samples),
#                 merge each arm's rows, seal one bundle per arm and kind through repair_corpus's own
#                 packaging, and fill the verdict README's numbers.
# Owns: shard discovery, the comparability gate, the merged-row and cost arithmetic, verdict.json.
# Boundaries: EVERY METRIC IS repair_corpus's: corpus_summary (meshing, endings, catalogue tally,
#             investigator block) and bundle_from_rows (the seal) are called, never re-derived. The
#             only numbers computed here are sums the rows cannot hold: model spend from telemetry
#             and VM time from PROVENANCE. No CAD is read or written.
"""Collect, check, seal and report the v3 verdict.

    collect.py --commit <40-hex> --pull            # gcloud storage cp the outputs, then seal
    collect.py --commit <40-hex> --from <dir>      # seal from an already-pulled out/ directory
    collect.py ... --dry-run                       # print the pull, check what is local, seal nothing
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
VERDICT_DIR = REPO / "devtools/validation/evidence/2026-11-20-crs-v3-verdict"
PREFIX = "gs://dev-crsv3-transfer-224734058693/eval/v3-verdict"
SHARD_RE = re.compile(r"^shard-(\d+)-of-(\d+)$")

#: On-demand list prices, us-central1, USD per vCPU-hour and per GB-hour (Compute Engine pricing
#: page, 2026). Used only to put a dollar figure beside wall time; stated as approximate.
VM_PRICE = {"c2d": (0.029563, 0.003959), "c3": (0.03465, 0.003938),
            "c3d": (0.030806, 0.004127), "c2": (0.03398, 0.00455)}
GB_PER_VCPU = {"highcpu": 2, "standard": 4, "highmem": 8}


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evalspec = _load("evalspec", HERE / "evalspec.py")


def _rc():
    return _load("repair_corpus", REPO / "devtools/validation/repair_corpus.py")


# --------------------------------------------------------------------------- discovery and checks

def load_runs(root: Path) -> dict:
    """Every <arm>/<kind>/shard-I-of-N under `root`, checked. Raises ValueError on a missing,
    failed or unfinished shard, on shards of one arm disagreeing on N, and - through
    evalspec.check_comparable - on mixed commits, images or samples across ALL arms."""
    arms: dict[tuple[str, str], dict] = {}
    for arm_dir in sorted(p for p in Path(root).iterdir() if p.is_dir()):
        for kind_dir in sorted(p for p in arm_dir.iterdir() if p.is_dir()):
            shards: dict[int, Path] = {}
            counts = set()
            for shard_dir in sorted(kind_dir.iterdir()):
                m = SHARD_RE.match(shard_dir.name)
                if m:
                    shards[int(m.group(1))] = shard_dir
                    counts.add(int(m.group(2)))
            if not shards:
                continue
            label = f"{arm_dir.name}/{kind_dir.name}"
            if len(counts) != 1:
                raise ValueError(f"{label}: shards disagree on N ({sorted(counts)}); one launch "
                                 f"per arm, or move the stale shards aside")
            n = counts.pop()
            missing = [f"{i}/{n}" for i in range(1, n + 1) if i not in shards]
            if missing:
                raise ValueError(f"{label}: shard(s) {', '.join(missing)} have no output")
            provs, rows = [], []
            for i in range(1, n + 1):
                d = shards[i]
                if not (d / "DONE").is_file():
                    why = next((m for m in ("FAILED", "INCOMPLETE") if (d / m).is_file()), "no DONE")
                    raise ValueError(f"{label}: shard {i}/{n} did not finish ({why})")
                prov = json.loads((d / "PROVENANCE.json").read_text())
                if int(prov.get("exit_code", -1)) != 0:
                    raise ValueError(f"{label}: shard {i}/{n} exited {prov.get('exit_code')}")
                if prov.get("arm") != arm_dir.name or prov.get("kind") != kind_dir.name:
                    raise ValueError(f"{label}: shard {i}/{n} says it is "
                                     f"{prov.get('arm')}/{prov.get('kind')}")
                prov["_dir"] = str(d)
                provs.append(prov)
                rows.extend(json.loads((d / "rows.json").read_text()))
            files = [r.get("file") for r in rows]
            dupes = sorted({f for f in files if files.count(f) > 1})
            if dupes:
                raise ValueError(f"{label}: file(s) appear in more than one shard: {dupes[:5]}")
            arms[(arm_dir.name, kind_dir.name)] = {"rows": rows, "provenances": provs, "n": n}
    if not arms:
        raise ValueError(f"no shard output under {root}")
    checked = evalspec.check_comparable([p for a in arms.values() for p in a["provenances"]])
    return {"checked": checked, "arms": arms}


# --------------------------------------------------------------------------- numbers

def summarise(rows: list[dict], catalogue: list[str] | None = None) -> dict:
    """repair_corpus's aggregate, unchanged."""
    return _rc().corpus_summary(rows, "off", catalogue=catalogue or None)


def model_usage(provenances: list[dict]) -> dict:
    calls = tokens_in = tokens_out = 0
    cost = 0.0
    by_role: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for prov in provenances:
        path = Path(prov["_dir"]) / "model-calls.jsonl"
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            calls += 1
            tokens_in += int(rec.get("input_tokens") or 0)
            tokens_out += int(rec.get("output_tokens") or 0)
            cost += float(rec.get("estimated_cost_usd") or 0.0)
            by_role[str(rec.get("role"))] = by_role.get(str(rec.get("role")), 0) + 1
            status = str(rec.get("provider_status") or rec.get("failure_category") or "?")
            statuses[status] = statuses.get(status, 0) + 1
    return {"calls": calls, "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost_usd": round(cost, 4), "by_role": by_role, "statuses": statuses}


def vm_cost(provenances: list[dict]) -> dict:
    seconds = sum(float(p.get("wall_seconds") or 0) for p in provenances)
    dollars = 0.0
    for p in provenances:
        mt = str(p.get("machine_type") or "")
        parts = mt.split("-")
        if len(parts) == 3 and parts[0] in VM_PRICE and parts[1] in GB_PER_VCPU:
            vcpu = int(parts[2])
            per_cpu, per_gb = VM_PRICE[parts[0]]
            hourly = vcpu * per_cpu + vcpu * GB_PER_VCPU[parts[1]] * per_gb
            dollars += hourly * float(p.get("wall_seconds") or 0) / 3600.0
    return {"shard_seconds": round(seconds, 1), "longest_shard_s": max(
        (float(p.get("wall_seconds") or 0) for p in provenances), default=0.0),
        "container_cost_usd": round(dollars, 4),
        "note": "container wall only; VM boot, docker install and image pull add ~5-10 min/VM"}


def clean_untouched(rows: list[dict], summary: dict) -> dict:
    """The clean control is untouched when nothing it meshed stops meshing AND no operation was
    kept on any clean part - the delivered geometry is the input's."""
    touched = sorted(r["file"] for r in rows
                     if any(op.get("status") == "kept" for op in r.get("operations") or []))
    broken = int((summary.get("meshing") or {}).get("newly_broken") or 0)
    return {"untouched": not touched and broken == 0, "files_with_kept_ops": touched,
            "newly_broken": broken}


def arm_report(entry: dict) -> dict:
    provs = entry["provenances"]
    catalogue = next((p.get("operations_catalogue") for p in provs
                      if p.get("operations_catalogue")), None)
    summary = summarise(entry["rows"], catalogue)
    usage = model_usage(provs)
    vm = vm_cost(provs)
    meshing = summary.get("meshing") or {}
    fixed = int(meshing.get("newly_meshed") or 0)
    total = usage["cost_usd"] + vm["container_cost_usd"]
    return {"files": len(entry["rows"]), "shards": entry["n"], "summary": summary,
            "model": usage, "vm": vm, "total_cost_usd": round(total, 4),
            "cost_per_fixed_part_usd": round(total / fixed, 4) if fixed else None,
            "wall_per_fixed_part_s": round(vm["shard_seconds"] / fixed, 1) if fixed else None}


# --------------------------------------------------------------------------- seal and report

def seal(entry: dict, arm: str, kind: str, checked: dict, out: Path) -> Path:
    rc = _rc()
    provs = entry["provenances"]
    first = provs[0]
    merged = out / f"rows-{arm}-{kind}.json"
    merged.write_text(json.dumps(entry["rows"], indent=1, default=str))
    settings = {
        "planner": "off", "arm": arm, "kind": kind, "arm_env": first.get("arm_env"),
        "harness_flags": first.get("harness_flags"), "shards": entry["n"],
        "image": checked["image"], "sample_sha256": checked["samples"].get(kind),
        "accepted_inert": first.get("accepted_inert"),
        "operations_catalogue": first.get("operations_catalogue") or rc.operation_catalogue(),
        "machine_type": first.get("machine_type"), "ftetwild": first.get("ftetwild"),
    }
    return rc.bundle_from_rows(
        merged, out / f"bundle-{arm}-{kind}", track="mesh-failure-real" if kind == "failing"
        else "clean-control", corpus_name=f"crs-v3-verdict {kind}",
        provenance=f"devtools/validation/v3_eval, {arm}, {entry['n']} GCE shard(s)",
        settings=settings, random_seed=evalspec.SEED, engine_commit=checked["commit"],
        hardware_class=f"gce-{first.get('machine_type') or 'unknown'}")


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, dict):
        return ", ".join(f"{k} {v}" for k, v in sorted(value.items(), key=lambda kv: -kv[1]
                                                          if isinstance(kv[1], int) else 0))
    return str(value)


def placeholders(reports: dict, checked: dict, verdicts: dict) -> dict[str, str]:
    out = {"COMMIT": checked["commit"], "IMAGE": checked["image"],
           "SAMPLE_SHA256": checked["samples"].get("failing", "n/a"),
           "CLEAN_SHA256": checked["samples"].get("clean", "n/a")}
    for (arm, kind), rep in reports.items():
        key = f"{arm}.{kind}".upper().replace("-", "_")
        meshing = rep["summary"].get("meshing") or {}
        inv = rep["summary"].get("investigator") or {}
        tally = (rep["summary"].get("catalogue") or {}).get("tally") or {}
        out.update({
            f"{key}.FILES": str(rep["files"]),
            f"{key}.NEWLY_MESHED": str(meshing.get("newly_meshed", "n/a")),
            f"{key}.NEWLY_BROKEN": str(meshing.get("newly_broken", "n/a")),
            f"{key}.MESHED_BEFORE": str(meshing.get("meshed_before", "n/a")),
            f"{key}.MESHED_AFTER": str(meshing.get("meshed_after", "n/a")),
            f"{key}.ENDINGS": _fmt(rep["summary"].get("endings")),
            f"{key}.OPS": "; ".join(f"{op} kept {c['kept']}/rolled back {c['rolled_back']}"
                                    for op, c in sorted(tally.items())) or "none attempted",
            f"{key}.MODEL_CALLS": str(rep["model"]["calls"]),
            f"{key}.INVESTIGATOR_CALLS": str(inv.get("model_calls", 0)),
            f"{key}.TOKENS": f"{rep['model']['input_tokens']} in / {rep['model']['output_tokens']} out",
            f"{key}.MODEL_USD": f"{rep['model']['cost_usd']:.2f}",
            f"{key}.VM_USD": f"{rep['vm']['container_cost_usd']:.2f}",
            f"{key}.WALL_S": f"{rep['vm']['shard_seconds']:.0f}",
            f"{key}.LONGEST_SHARD_S": f"{rep['vm']['longest_shard_s']:.0f}",
            f"{key}.USD_PER_FIXED": _fmt(rep["cost_per_fixed_part_usd"]),
            f"{key}.WALL_PER_FIXED_S": _fmt(rep["wall_per_fixed_part_s"]),
            f"{key}.STILL_FAILING": _fmt(meshing.get("still_failing_by_kind")),
        })
        if "clean" in rep:
            out[f"{key}.UNTOUCHED"] = "yes" if rep["clean"]["untouched"] else (
                f"NO - kept ops on {rep['clean']['files_with_kept_ops']}, newly broken "
                f"{rep['clean']['newly_broken']}")
    for name, v in verdicts.items():
        for k, val in v.items():
            out[f"{name}.{k}".upper()] = _fmt(val)
    return out


def fill(template: str, values: dict[str, str]) -> str:
    return re.sub(r"\{\{([A-Z0-9_.]+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)


def build_verdicts(reports: dict) -> dict:
    verdicts = {}
    det = reports.get(("deterministic", "failing"))
    v3 = reports.get(("v3-full", "failing"))
    if det and v3:
        cleans = [reports[k]["clean"]["untouched"] for k in
                  (("deterministic", "clean"), ("v3-full", "clean")) if k in reports]
        verdicts["T29"] = evalspec.t29_verdict(
            det["summary"].get("meshing") or {}, v3["summary"].get("meshing") or {},
            clean_untouched=bool(cleans) and all(cleans) and len(cleans) == 2,
            cost_per_fixed_part=v3["cost_per_fixed_part_usd"])
    off = reports.get(("v3-no-tournament", "failing"))
    if off and v3:
        a, b = (off["summary"].get("meshing") or {}), (v3["summary"].get("meshing") or {})
        verdicts["T19"] = {"off_newly_meshed": a.get("newly_meshed"),
                           "on_newly_meshed": b.get("newly_meshed"),
                           "off_newly_broken": a.get("newly_broken"),
                           "on_newly_broken": b.get("newly_broken"),
                           "off_usd_per_fixed": off["cost_per_fixed_part_usd"],
                           "on_usd_per_fixed": v3["cost_per_fixed_part_usd"]}
    return verdicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--prefix", default=PREFIX)
    parser.add_argument("--from", dest="source", default="",
                        help="an already-pulled out/ directory (<arm>/<kind>/shard-I-of-N)")
    parser.add_argument("--pull", action="store_true", help="gcloud storage cp the outputs first")
    parser.add_argument("--out", default=str(VERDICT_DIR))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("--commit must be the full 40-hex sha")

    source = Path(args.source) if args.source else Path(tempfile.gettempdir()) / (
        f"v3-eval-out-{args.commit[:12]}")
    if args.pull:
        cmd = ["gcloud", "storage", "cp", "-r",
               f"{args.prefix.rstrip('/')}/{args.commit[:12]}/out/*", str(source)]
        print(("DRY-RUN  " if args.dry_run else "") + " ".join(shlex.quote(c) for c in cmd))
        if not args.dry_run:
            source.mkdir(parents=True, exist_ok=True)
            subprocess.run(cmd, check=True)
    if not source.is_dir():
        print(f"nothing pulled at {source}", file=sys.stderr)
        return 0 if args.dry_run else 2

    try:
        runs = load_runs(source)
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    checked = runs["checked"]
    if checked["commit"] != args.commit:
        print(f"REFUSED: the shards ran {checked['commit']}, not {args.commit}", file=sys.stderr)
        return 2
    expected = {"failing": evalspec.SAMPLE_SIZE, "clean": evalspec.CLEAN_SIZE}
    reports = {}
    for (arm, kind), entry in sorted(runs["arms"].items()):
        if len(entry["rows"]) != expected[kind]:
            print(f"REFUSED: {arm}/{kind} has {len(entry['rows'])} rows, the sample has "
                  f"{expected[kind]}", file=sys.stderr)
            return 2
        rep = arm_report(entry)
        if kind == "clean":
            rep["clean"] = clean_untouched(entry["rows"], rep["summary"])
        reports[(arm, kind)] = rep
    verdicts = build_verdicts(reports)
    print(json.dumps({"checked": checked, "verdicts": verdicts}, indent=1, default=str))
    if args.dry_run:
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for (arm, kind), entry in sorted(runs["arms"].items()):
        sealed = seal(entry, arm, kind, checked, out)
        print(f"sealed {sealed}")
    (out / "verdict.json").write_text(json.dumps({
        "checked": checked, "verdicts": verdicts,
        "arms": {f"{a}/{k}": {key: val for key, val in rep.items() if key != "summary"}
                 for (a, k), rep in reports.items()}}, indent=1, sort_keys=True, default=str) + "\n")
    readme = out / "README.md"
    if readme.is_file():
        readme.write_text(fill(readme.read_text(encoding="utf-8"),
                               placeholders(reports, checked, verdicts)), encoding="utf-8")
        left = sorted(set(re.findall(r"\{\{([A-Z0-9_.]+)\}\}", readme.read_text())))
        print(f"README filled; {len(left)} placeholder(s) left for a human: {', '.join(left[:12])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
