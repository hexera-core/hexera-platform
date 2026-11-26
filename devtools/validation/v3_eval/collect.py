#!/usr/bin/env python3
# Responsibility: Pull the CRS v3 evaluation's shard outputs back from GCS, refuse anything that is not
#                 comparable (a missing shard, a failed shard, mixed commits, images or samples),
#                 merge each arm's rows, seal one bundle per arm and kind through repair_corpus's own
#                 packaging, and fill the evidence README's numbers. There is NO pass/fail: the
#                 numbers are reported as measured (owner decision, 2026-11-20).
# Owns: shard discovery, the comparability gate, the merged-row and cost arithmetic, the per-part
#       clean-control rule (clean_good), verdict.json (the file name is historical; it holds no
#       verdict).
# Boundaries: EVERY METRIC IS repair_corpus's: corpus_summary (meshing, endings, catalogue tally,
#             investigator block) and bundle_from_rows (the seal) are called, never re-derived. The
#             only numbers computed here are sums the rows cannot hold: model spend from telemetry
#             and VM time from PROVENANCE. No CAD is read or written.
"""Collect, check, seal and report the v3 evaluation.

    collect.py --commit <40-hex> --status          # which shards have DONE / FAILED / partial rows
    collect.py --commit <40-hex> --pull            # gcloud storage cp the outputs, then seal
    collect.py --commit <40-hex> --from <dir>      # seal from an already-pulled directory
    collect.py ... --dry-run                       # print the pull, check what is local, seal nothing

It needs GCS and nothing else: every arm's shards live under
<bucket>/eval/<arm>-<commit12>/<kind>/shard-I-of-N/ (launch_fleet.results_root), so it can run
from any session, any machine, after the fleet is long gone.
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
BUCKET = "gs://dev-crsv3-transfer-224734058693"
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
meshability_report = _load("meshability_report", HERE / "meshability_report.py")


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


def missing_units(present) -> list[str]:
    """Every (arm, kind) unit of the launch plan (evalspec.UNITS) with no output, as "arm/kind".
    load_runs only sees directories that exist, so a unit that never ran at all - a whole clean
    control, say - is invisible to it; this is the check that it is there."""
    return [f"{a}/{k}" for a, k in evalspec.UNITS if (a, k) not in present]


# --------------------------------------------------------------------------- numbers

def summarise(rows: list[dict], catalogue: list[str] | None = None) -> dict:
    """repair_corpus's aggregate, unchanged."""
    return _rc().corpus_summary(rows, "off", catalogue=catalogue or None)


def model_usage(provenances: list[dict]) -> dict:
    calls = tokens_in = tokens_out = 0
    cost = 0.0
    by_role: dict[str, int] = {}
    by_purpose: dict[str, int] = {}
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
            # WHO ASKED (investigator / reviewer / author / tournament:<source>). Calls written
            # before the field existed read as "unlabelled", never guessed from the route.
            purpose = str(rec.get("purpose") or "unlabelled")
            by_purpose[purpose] = by_purpose.get(purpose, 0) + 1
            status = str(rec.get("provider_status") or rec.get("failure_category") or "?")
            statuses[status] = statuses.get(status, 0) + 1
    return {"calls": calls, "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost_usd": round(cost, 4), "by_role": by_role, "by_purpose": by_purpose,
            "statuses": statuses}


def vm_cost(provenances: list[dict]) -> dict:
    """Each shard is charged ITS SHARE of the VM it ran on, not the whole VM: launch_fleet packs
    one shard process per vCPU (evalspec.vcpus_of), so a shard costs hourly / shards-per-VM. A
    PROVENANCE `shards_per_vm`, when present, overrides the launch plan's vCPU count."""
    seconds = sum(float(p.get("wall_seconds") or 0) for p in provenances)
    dollars = 0.0
    for p in provenances:
        mt = str(p.get("machine_type") or "")
        parts = mt.split("-")
        if len(parts) == 3 and parts[0] in VM_PRICE and parts[1] in GB_PER_VCPU:
            vcpu = int(parts[2])
            per_cpu, per_gb = VM_PRICE[parts[0]]
            hourly = vcpu * per_cpu + vcpu * GB_PER_VCPU[parts[1]] * per_gb
            per_vm = max(1, int(p.get("shards_per_vm") or vcpu))
            dollars += hourly / per_vm * float(p.get("wall_seconds") or 0) / 3600.0
    return {"shard_seconds": round(seconds, 1), "longest_shard_s": max(
        (float(p.get("wall_seconds") or 0) for p in provenances), default=0.0),
        "container_cost_usd": round(dollars, 4),
        "note": "container wall only; VM boot, docker install and image pull add ~5-10 min/VM"}


def new_defects(row: dict) -> dict[str, int]:
    """The defects repair GAVE this part, by code: every code whose located count after the
    re-inspection exceeds its count before (a code absent before counts from 0), plus any code
    the after inspection names that the before one did not (counted 1 when it was not located).

    Read from the row's own before/after inspections of the WHOLE part (`located_before/after`,
    `defects_before/after`, repair_corpus._repair_row). A part repair did not re-inspect (no
    `located_after`: nothing written) gained nothing. Per-code counts compare totals, not
    instances, so a repair that fixed one small edge and made another reads as no change; a rise
    always reads as introduced - the direction that blames the repair, never excuses it."""
    after = row.get("located_after")
    if not isinstance(after, dict):
        return {}
    before = row.get("located_before") or {}
    out = {code: int(n) - int(before.get(code, 0)) for code, n in after.items()
           if int(n) > int(before.get(code, 0))}
    for code in set(row.get("defects_after") or []) - set(row.get("defects_before") or []):
        out.setdefault(code, 1)
    return dict(sorted(out.items()))


def clean_good(row: dict) -> bool:
    """THE CLEAN-CONTROL RULE, per part: it meshed before, it still meshes after repair (a part
    repair did not rewrite keeps its before verdict, as repair_corpus.meshing_summary reads it),
    and repair introduced no new defect. A crashed or timed-out part is never good: nothing was delivered."""
    if row.get("outcome") in ("crashed", "timeout") or row.get("meshes_before") is not True:
        return False
    after = row.get("meshes_after")
    still = row["meshes_before"] if after is None else after
    return still is True and not new_defects(row)


def clean_report(rows: list[dict], summary: dict) -> dict:
    """One arm's clean control: how many parts are good (clean_good), and - AS INFORMATION ONLY,
    nothing fails on it - how many parts repair changed (a kept operation or a crash)."""
    kept = sorted(r["file"] for r in rows
                  if any(op.get("status") == "kept" for op in r.get("operations") or []))
    crashed = sorted(r["file"] for r in rows if r.get("outcome") == "crashed")
    timed_out = sorted(r["file"] for r in rows if r.get("outcome") == "timeout")
    introduced = {r["file"]: new_defects(r) for r in rows if new_defects(r)}
    return {"still_meshes_no_new_defects": sum(1 for r in rows if clean_good(r)),
            "not_good": sorted(r["file"] for r in rows if not clean_good(r)),
            "files_with_new_defects": sorted(introduced), "new_defects": introduced,
            "changed": len(set(kept) | set(crashed) | set(timed_out)), "files_with_kept_ops": kept,
            "crashed_files": crashed,
            "newly_broken": int((summary.get("meshing") or {}).get("newly_broken") or 0)}


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


def gather_transcripts(entry: dict, arm: str, kind: str, out: Path) -> dict:
    """The arm's investigator transcripts, file by file. Every shard's `transcripts.jsonl` is
    concatenated into `transcripts-<arm>-<kind>.jsonl` (each line tagged with its shard), and the
    rows' per-region outcomes become `outcomes-<arm>-<kind>.json`: one entry per file with how it
    meshed and, per region, how the investigation ended and why no package. A shard with no
    transcript (a rules-only arm, or a file nothing investigated) contributes no lines."""
    records = with_transcripts = 0
    # THE LATEST RECORD PER (file, region): a re-call continues the same investigation.
    latest: dict[str, dict] = {}
    with (out / f"transcripts-{arm}-{kind}.jsonl").open("w", encoding="utf-8") as sink:
        for prov in entry["provenances"]:
            path = Path(prov["_dir"]) / "transcripts.jsonl"
            if not path.is_file():
                continue
            with_transcripts += 1
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # a line cut off when a shard died mid-write
                record["shard"] = prov.get("shard", "")
                if record.get("file") is not None:
                    latest.setdefault(str(record["file"]), {})[repr(record.get("region"))] = record
                sink.write(json.dumps(record, sort_keys=True, default=str) + "\n")
                records += 1
    table = [{"file": r.get("file"), "outcome": r.get("outcome"),
              "meshes_before": r.get("meshes_before"), "meshes_after": r.get("meshes_after"),
              "ends": (r.get("investigator") or {}).get("ends", {}),
              "regions": _regions(r, latest.get(str(r.get("file")), {}))}
             for r in entry["rows"]]
    (out / f"outcomes-{arm}-{kind}.json").write_text(
        json.dumps(table, indent=1, sort_keys=True, default=str) + "\n")
    return {"records": records, "shards_with_transcripts": with_transcripts,
            "files": len(table)}


def _regions(row: dict, transcripts: dict) -> list[dict]:
    """A file's per-region outcomes, REBUILT FROM ITS TRANSCRIPT when it has one. The row's own
    outcomes were computed in the shard (at b04befe8, from the elided conversation, undercounting
    edits); the transcript's `turns` and kept-edit log are never elided. The row keeps what only it
    knows - the package verdict - joined back by region."""
    baked = list((row.get("investigator") or {}).get("outcomes") or [])
    if not transcripts:
        return baked
    from meshpipeline.cad.repair import investigation_trace

    verdicts = {repr(o.get("region")): o["package"] for o in baked
                if isinstance(o, dict) and o.get("package")}
    out = []
    for key, record in transcripts.items():
        region = investigation_trace.outcome(record)
        if key in verdicts:
            region["package"] = verdicts[key]
        out.append(region)
    return out


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, dict):
        return ", ".join(f"{k} {v}" for k, v in sorted(value.items(), key=lambda kv: -kv[1]
                                                          if isinstance(kv[1], int) else 0))
    return str(value)


def placeholders(reports: dict, checked: dict, comparisons: dict) -> dict[str, str]:
    out = {"COMMIT": checked["commit"], "IMAGE": checked["image"],
           "IMAGE_COMMIT": str(checked.get("image_commit") or "n/a"),
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
            f"{key}.FILES_FIXED": str(meshing.get("newly_meshed", "n/a")),
            f"{key}.NEWLY_BROKEN": str(meshing.get("newly_broken", "n/a")),
            f"{key}.MESHED_BEFORE": str(meshing.get("meshed_before", "n/a")),
            f"{key}.MESHED_AFTER": str(meshing.get("meshed_after", "n/a")),
            # DIAGNOSTIC ONLY (off by default): never part of files fixed.
            f"{key}.DIAG_TOLERANT_MESHER": str(meshing.get("delivered_via_tolerant_mesher",
                                                           "n/a")),
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
            out[f"{key}.STILL_GOOD"] = str(rep["clean"]["still_meshes_no_new_defects"])
            out[f"{key}.CHANGED"] = str(rep["clean"]["changed"])
    for name, v in comparisons.items():
        for k, val in v.items():
            out[f"{name}.{k}".upper()] = _fmt(val)
    return out


def fill(template: str, values: dict[str, str]) -> str:
    return re.sub(r"\{\{([A-Z0-9_.]+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)


def build_comparisons(reports: dict) -> dict:
    """T19's two arms side by side: numbers only, no rule, nothing passes or fails."""
    out = {}
    off, on = reports.get(("t19_tournament_off", "failing")), reports.get(("v3_full", "failing"))
    if off and on:
        a, b = (off["summary"].get("meshing") or {}), (on["summary"].get("meshing") or {})
        out["T19"] = {"off_newly_meshed": a.get("newly_meshed"),
                      "on_newly_meshed": b.get("newly_meshed"),
                      "off_newly_broken": a.get("newly_broken"),
                      "on_newly_broken": b.get("newly_broken"),
                      "off_usd_per_fixed": off["cost_per_fixed_part_usd"],
                      "on_usd_per_fixed": on["cost_per_fixed_part_usd"]}
    return out


# --------------------------------------------------------------------------- results.json

#: Arm ids of the evidence lane's results.schema.json (schema_version 2) -> the run that fills it.
#: t19_tournament_on IS t29_v3: one run (v3_full), referenced twice.
RESULT_ARMS = {"t29_deterministic": "t29_deterministic", "t29_v3": "v3_full",
               "t19_tournament_off": "t19_tournament_off", "t19_tournament_on": "v3_full"}
RESULT_LABELS = {
    "t6_rules_only_local": "rules-only baseline",
    "t29_deterministic": "deterministic (everything-on, investigator/tournament off; reviewer live)",
    "t29_v3": "v3 full (investigator live, tournament live K=3)",
    "t19_tournament_off": "tournament off (investigator live)",
    "t19_tournament_on": "tournament on, K=3 (same run as t29_v3)",
}
T6_URI = "gs://dev-crsv3-transfer-224734058693/eval/t6-rules-only-0bf78625"


#: How the clean-control count is decided - stated in results.json beside the number.
CLEAN_BASIS = ("a clean part is good when it meshed before, still meshes after repair (a part "
               "repair did not rewrite keeps its before verdict), and repair introduced no new "
               "defect (no defect code's located count rose between the before and after "
               "inspections); a crashed part is never good. Minimum over the arms covered. "
               "`changed` (a kept operation or a crash; the most over the arms) is information, "
               "not a failure.")


def _tallies(summary: dict) -> tuple[dict, dict]:
    tally = (summary.get("catalogue") or {}).get("tally") or {}
    return ({op: int(c.get("kept", 0)) for op, c in tally.items()},
            {op: int(c.get("rolled_back", 0)) for op, c in tally.items()})


def _pending(result_id: str) -> dict:
    """An arm with nothing measured: every number null (renders [PENDING], never 0)."""
    return {"arm": result_id, "label": RESULT_LABELS[result_id], "commit": None,
            "image_digest": None, "sample_sha256": None, "n_files": None, "newly_meshed": None,
            "newly_broken": None, "meshes_before": None, "meshes_after": None, "endings": None,
            "ops_kept": None, "ops_rolled_back": None, "model_calls": None, "cost_usd": None,
            "wall_s": None, "cost_per_fixed_part": None, "notes": "not measured"}


def result_arm(result_id: str, rep: dict, checked: dict, kind_sha: str) -> dict:
    meshing = rep["summary"].get("meshing") or {}
    kept, rolled = _tallies(rep["summary"])
    run = RESULT_ARMS[result_id]
    out = {"arm": result_id, "label": RESULT_LABELS[result_id], "commit": checked["commit"],
           "image_digest": checked["image"], "sample_sha256": kind_sha, "n_files": rep["files"],
           "newly_meshed": meshing.get("newly_meshed"), "newly_broken": meshing.get("newly_broken"),
           "meshes_before": meshing.get("meshed_before"), "meshes_after": meshing.get("meshed_after"),
           # DIAGNOSTIC ONLY: files fTetWild meshed as delivered (null unless the run asked for
           # --tolerant-delivery). Never part of newly_meshed / files fixed.
           "delivered_via_tolerant_mesher": meshing.get("delivered_via_tolerant_mesher"),
           "endings": rep["summary"].get("endings"), "ops_kept": kept, "ops_rolled_back": rolled,
           "model_calls": rep["model"]["calls"], "cost_usd": rep["total_cost_usd"],
           "wall_s": rep["vm"]["shard_seconds"],
           "cost_per_fixed_part": rep["cost_per_fixed_part_usd"],
           "notes": (f"run {run}; "
                     f"{evalspec.ARMS.get(run, {}).get('model_role', 'historical arm')}; "
                     + (f"{meshing['crashed']} file(s) CRASHED (native; isolated, counted in "
                        f"newly_broken only if they meshed before); "
                        if meshing.get("crashed") else "")
                     + "model "
                     f"${rep['model']['cost_usd']} + VM ${rep['vm']['container_cost_usd']}; "
                     f"code {checked['commit'][:12]} from the archive on an image built "
                     f"from {str(checked.get('image_commit') or '?')[:12]}; "
                     + "; ".join(evalspec.KNOWN_NOOPS.values()))}
    if result_id == "t19_tournament_on":
        out["same_run_as"] = "t29_v3"
    return out


def t6_arm(rows: list[dict] | None, sample_sha: str | None) -> dict:
    """The local rules-only T6 run (0bf78625, shards 1-3 of 4 on a laptop): no image, no model."""
    base = {**_pending("t6_rules_only_local"), "commit": "0bf78625", "image_digest": "n/a",
            "model_calls": "n/a", "cost_usd": "n/a", "cost_per_fixed_part": "n/a"}
    if not rows:
        return base
    summary = summarise(rows)
    meshing = summary.get("meshing") or {}
    kept, rolled = _tallies(summary)
    base.update(n_files=len(rows), newly_meshed=meshing.get("newly_meshed"),
                newly_broken=meshing.get("newly_broken"),
                meshes_before=meshing.get("meshed_before"),
                meshes_after=meshing.get("meshed_after"), endings=summary.get("endings"),
                ops_kept=kept, ops_rolled_back=rolled,
                wall_s=round(sum(float(r.get("seconds") or 0) for r in rows), 1),
                sample_sha256=sample_sha,
                notes=(f"{T6_URI}: shards 1-3 of 4 ({len(rows)} of the same 120-file sample; "
                       f"shard 4 never ran); macOS host, no model router (investigator and "
                       f"reviewer had no model); wall_s is summed per-file repair seconds"))
    return base


def results_json(reports: dict, checked: dict, *, source: str,
                 t6_rows: list[dict] | None = None) -> dict:
    """The evidence lane's results.schema.json (schema_version 2). A null is NOT MEASURED YET.
    There is no verdict block: nothing in it passes or fails."""
    import datetime as dt

    failing_sha = checked["samples"].get("failing")
    arms: dict = {"t6_rules_only_local": t6_arm(t6_rows, failing_sha)}
    for result_id, run in RESULT_ARMS.items():
        rep = reports.get((run, "failing"))
        arms[result_id] = (result_arm(result_id, rep, checked, failing_sha) if rep is not None
                           else _pending(result_id))
    cleans = {run: rep for (run, kind), rep in reports.items() if kind == "clean"}
    arms["clean_control"] = {
        "arm": "clean_control", "label": "100-part clean control",
        "commit": checked["commit"], "image_digest": checked["image"],
        "sample_sha256": checked["samples"].get("clean"),
        "n_files": min((rep["files"] for rep in cleans.values()), default=None),
        "still_meshes_no_new_defects": min(
            (rep["clean"]["still_meshes_no_new_defects"] for rep in cleans.values()),
            default=None),
        "changed": max((rep["clean"]["changed"] for rep in cleans.values()), default=None),
        "newly_broken": max((rep["clean"]["newly_broken"] for rep in cleans.values()),
                            default=None),
        "arms_covered": sorted(cleans) or None,
        "basis": CLEAN_BASIS,
        "notes": None,
    }
    return {"schema_version": 2,
            "generated_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": source, "arms": arms}


def status(bucket: str, commit: str) -> int:
    """Read-only: per arm and kind, which shards wrote DONE, FAILED, INCOMPLETE or only partial
    rows - what a resume needs to know which shards to relaunch."""
    for arm in evalspec.ARMS:
        root = f"{bucket}/eval/{arm}-{commit[:12]}"
        out = subprocess.run(["gcloud", "storage", "ls", "-r", root], capture_output=True,
                             text=True)
        if out.returncode:
            print(f"{arm}: no results under {root}")
            continue
        shards: dict[tuple[str, str], set[str]] = {}
        for line in out.stdout.splitlines():
            m = re.search(r"/(failing|clean)/(shard-\d+-of-\d+)/(.+)$", line.strip())
            if m:
                shards.setdefault((m.group(1), m.group(2)), set()).add(m.group(3))
        for (kind, shard), files in sorted(shards.items()):
            state = next((s for s in ("DONE", "FAILED", "INCOMPLETE") if s in files), "running")
            rows = "rows.json" if "rows.json" in files else (
                "partial bundle/rows.jsonl" if "bundle/rows.jsonl" in files else "no rows yet")
            print(f"{arm:<18} {kind:<8} {shard:<16} {state:<10} {rows}")
    return 0


#: Shard paths the collector never downloads: the delivered per-file artefacts (repaired STEPs,
#: mesh-hints sidecars) stay in the bucket. Anchored so it reads the same whether the matcher
#: searches or matches the path relative to the arm's root.
PULL_EXCLUDE = r"^(.*/)?artefacts/.*$"


def pull_command(root: str, dest: Path) -> list[str]:
    """The argv that copies one arm's results to `dest`, without the artefacts (PULL_EXCLUDE)."""
    return ["gcloud", "storage", "rsync", "-r", "-x", PULL_EXCLUDE, root, str(dest)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument("--status", action="store_true",
                        help="list each arm's shard markers in GCS and exit (read-only)")
    parser.add_argument("--from", dest="source", default="",
                        help="an already-pulled out/ directory (<arm>/<kind>/shard-I-of-N)")
    parser.add_argument("--pull", action="store_true", help="gcloud storage cp the outputs first")
    parser.add_argument("--out", default=str(VERDICT_DIR))
    parser.add_argument("--t6", action="store_true",
                        help="also pull the local T6 rules-only rows into results.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("--commit must be the full 40-hex sha")

    source = Path(args.source) if args.source else Path(tempfile.gettempdir()) / (
        f"v3-eval-out-{args.commit[:12]}")
    bucket = args.bucket.rstrip("/")
    if args.status:
        return status(bucket, args.commit)
    if args.pull:
        for arm in evalspec.ARMS:
            root = f"{bucket}/eval/{arm}-{args.commit[:12]}"
            cmd = pull_command(root, source / arm)
            print(("DRY-RUN  " if args.dry_run else "") + " ".join(shlex.quote(c) for c in cmd))
            if args.dry_run:
                continue
            if subprocess.run(["gcloud", "storage", "ls", root], capture_output=True).returncode:
                print(f"  {root}: no results (arm not run)")
                continue
            (source / arm).mkdir(parents=True, exist_ok=True)
            subprocess.run(cmd, check=True)
    if not source.is_dir():
        print(f"nothing pulled at {source}", file=sys.stderr)
        return 0 if args.dry_run else 2

    try:
        runs = load_runs(source)
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    missing = missing_units(runs["arms"])
    if missing:
        print(f"REFUSED: planned unit(s) with no output: {', '.join(missing)} - every arm x kind "
              f"of the launch plan must be present before a report or a seal", file=sys.stderr)
        return 2
    checked = runs["checked"]
    if checked["commit"] != args.commit:
        print(f"REFUSED: the shards ran {checked['commit']}, not {args.commit}", file=sys.stderr)
        return 2
    # EVERY READABLE FILE OF A MESHABILITY RUN IS ACCOUNTED FOR: scored, or carrying
    # meshability_score_error. A row with neither would drop out of every count while the row
    # total still checks out. Checked before anything is sealed or written.
    if any("returned_step" in r or "meshability_score_error" in r
           for e in runs["arms"].values() for r in e["rows"]):
        unscored = {f"{a}/{k}": files for (a, k), e in sorted(runs["arms"].items())
                    if (files := _rc().unscored_rows(e["rows"]))}
        if unscored:
            print("REFUSED: readable row(s) with neither a meshability score nor a "
                  f"meshability_score_error: {unscored}", file=sys.stderr)
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
            rep["clean"] = clean_report(entry["rows"], rep["summary"])
        reports[(arm, kind)] = rep
    comparisons = build_comparisons(reports)
    print(json.dumps({"checked": checked, "comparisons": comparisons}, indent=1, default=str))
    if args.dry_run:
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for (arm, kind), entry in sorted(runs["arms"].items()):
        sealed = seal(entry, arm, kind, checked, out)
        print(f"sealed {sealed}")
        print(f"  transcripts {arm}/{kind}: {gather_transcripts(entry, arm, kind, out)}")
    t6_rows = None
    if args.t6:
        t6_dir = Path(tempfile.gettempdir()) / "v3-eval-t6"
        t6_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["gcloud", "storage", "cp", f"{T6_URI}/rows-*.json", str(t6_dir)],
                       check=False)
        t6_rows = [r for f in sorted(t6_dir.glob("rows-*.json"))
                   for r in json.loads(f.read_text())] or None
    (out / "results.json").write_text(json.dumps(
        results_json(reports, checked, source=f"collect.py from {source}",
                     t6_rows=t6_rows), indent=1, sort_keys=True, default=str) + "\n")
    (out / "verdict.json").write_text(json.dumps({
        "checked": checked, "comparisons": comparisons,
        "arms": {f"{a}/{k}": {key: val for key, val in rep.items() if key != "summary"}
                 for (a, k), rep in reports.items()}}, indent=1, sort_keys=True, default=str) + "\n")
    # THE MESHABILITY REPORT, written whenever the rows were scored with --meshability (the one
    # meshability wave); absent for the T19/T29 waves, whose rows carry no returned_step.
    if any("returned_step" in r for e in runs["arms"].values() for r in e["rows"]):
        report = meshability_report.build(
            {key: entry["rows"] for key, entry in runs["arms"].items()},
            summarise=_rc().meshability_summary,
            comparison=evalspec.COMPARISONS["AI_MARGINAL"])
        (out / "meshability.json").write_text(
            json.dumps(report, indent=1, sort_keys=True, default=str) + "\n")
        (out / "meshability.md").write_text(meshability_report.render(report))
        print(f"meshability report: {out / 'meshability.json'}, {out / 'meshability.md'}")
    readme = out / "README.md"
    if readme.is_file():
        readme.write_text(fill(readme.read_text(encoding="utf-8"),
                               placeholders(reports, checked, comparisons)), encoding="utf-8")
        left = sorted(set(re.findall(r"\{\{([A-Z0-9_.]+)\}\}", readme.read_text())))
        print(f"README filled; {len(left)} placeholder(s) left for a human: {', '.join(left[:12])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
