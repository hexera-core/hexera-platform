#!/usr/bin/env python3
# Responsibility: Run the whole repair loop over a directory of real CAD files and report what it did to each.
# Owns: the run, the per-file record, and the summary. It asserts nothing and gates nothing.
# Boundaries: it reads CAD and writes repairs into a scratch directory. It touches no database, no
#             object store and no job; this is the pipeline's repair path exercised directly.

# WHY THIS EXISTS.
#
# The tests prove the repair behaves correctly on geometry authored to provoke one defect each.
# They cannot tell you what it does to a real part, because every fixture is a part somebody wrote
# to make a point. This runs inspect -> locate -> triage -> repair -> promote-check -> re-inspect
# over whatever files you point it at and prints the outcome for each, so the answer to "what does
# it actually repair" is evidence rather than an argument.
#
# WHAT A ROW MEANS. `before` is what the inspection located in the file as delivered; `after` is
# what the inspection locates in the file the repair produced. A repair is only credited when the
# second is emptier than the first, measured by re-reading the written file - never by the repair
# reporting its own success.
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# The switches this tool is here to exercise. Set before the settings module is imported, because
# these are read once at import.
os.environ.setdefault("CAD_REPAIR_ENABLED", "true")
os.environ.setdefault("CAD_REPAIR_AUTONOMOUS", "true")

_SUFFIXES = {".step", ".stp"}


def _inspect(path: Path) -> dict:
    from meshpipeline.cad.repair.brep import inspect_brep_file

    report = inspect_brep_file(path)
    measured = {m.name: m.value for m in report.measurements}
    return {
        "report": report,
        "defects": [d.code.value for d in report.defects],
        "entities": [dict(e) for e in report.entities],
        "valid": measured.get("is_valid"),
        "counts": {k: measured.get(k) for k in
                   ("solids", "shells", "faces", "edges", "vertices")},
        "located": measured.get("located", {}),
    }


def _one(path: Path, scratch: Path) -> dict:
    from meshpipeline.cad.repair.conservative import (
        RepairRefused,
        RepairUnavailable,
        repair_step_file,
    )
    from meshpipeline.cad.repair.contracts import RepairStatus, status_from_defects
    from meshpipeline.cad.repair.triage import ROUTE_CONSERVATIVE_REPAIR, recommend

    row: dict = {"file": path.name, "size_bytes": path.stat().st_size}
    started = time.monotonic()
    try:
        before = _inspect(path)
    except Exception as exc:  # noqa: BLE001 - a file the kernel will not read is a real outcome
        row.update(outcome="unreadable", error=f"{type(exc).__name__}: {exc}")
        return row

    status = status_from_defects(before["report"].defects)
    row.update(
        valid_before=before["valid"],
        counts_before=before["counts"],
        defects_before=sorted(set(before["defects"])),
        located_before=before["located"].get("by_code", {}),
        regions_before=before["located"].get("by_region", {}),
        status=status.value,
    )

    advice = recommend(repair_status=status.value, report=before["report"].to_dict(),
                       target_engine="gmsh")
    row.update(route=advice.route, confidence=advice.confidence,
               targets=[{"entity": t["entity"], "code": t["code"], "region": t["region"]}
                        for t in advice.targets],
               abstain_reason=advice.abstain_reason)

    if status is RepairStatus.clean and not before["entities"]:
        row.update(outcome="nothing_to_repair", seconds=round(time.monotonic() - started, 2))
        return row
    if advice.route != ROUTE_CONSERVATIVE_REPAIR:
        row.update(outcome=f"not_attempted:{advice.route}",
                   seconds=round(time.monotonic() - started, 2))
        return row

    destination = scratch / f"{path.stem}-repaired.step"
    try:
        repaired = repair_step_file(path, destination)
    except RepairUnavailable as exc:
        row.update(outcome="unavailable", error=str(exc))
        return row
    except RepairRefused as exc:
        row.update(outcome="refused", error=str(exc),
                   seconds=round(time.monotonic() - started, 2))
        return row
    except Exception as exc:  # noqa: BLE001
        row.update(outcome="repair_failed", error=f"{type(exc).__name__}: {exc}")
        return row

    measured = {m.name: m.value for m in repaired.measurements}
    row.update(operations=[op["name"] for op in repaired.operations],
               holes_filled=measured.get("holes_filled", []),
               holes_left=measured.get("holes_left", []))

    after = _inspect(destination)
    row.update(valid_after=after["valid"], counts_after=after["counts"],
               defects_after=sorted(set(after["defects"])),
               located_after=after["located"].get("by_code", {}))

    # THE CREDIT IS GIVEN BY THE RE-INSPECTION, not by the repair. A repair that reports success
    # over a file whose defects are still there has not repaired anything.
    fixed = {code: count for code, count in row["located_before"].items()
             if count > row["located_after"].get(code, 0)}
    row["repaired_defects"] = fixed
    row["outcome"] = ("repaired" if fixed else
                      "no_change" if row["located_after"] == row["located_before"] else
                      "changed_without_fixing")
    row["seconds"] = round(time.monotonic() - started, 2)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+", help="directories holding .step/.stp files")
    parser.add_argument("--limit", type=int, default=0, help="stop after N files")
    parser.add_argument("--json", dest="json_out", default="", help="write the rows here")
    args = parser.parse_args()

    files: list[Path] = []
    for directory in args.directories:
        for path in sorted(Path(directory).rglob("*")):
            if path.suffix.lower() in _SUFFIXES and path.is_file():
                files.append(path)
    if args.limit:
        files = files[: args.limit]
    if not files:
        print("no .step/.stp files found", file=sys.stderr)
        return 2

    import meshpipeline.settings.cad_repair as rcfg
    print(f"CAD_REPAIR_ENABLED={rcfg.CAD_REPAIR_ENABLED}  "
          f"AUTONOMOUS={rcfg.CAD_REPAIR_AUTONOMOUS}  "
          f"FILL_PLANAR_HOLES={rcfg.CAD_REPAIR_FILL_PLANAR_HOLES}  "
          f"max_deviation={rcfg.CAD_REPAIR_MAX_DEVIATION_RATIO}  "
          f"max_tolerance_mm={rcfg.CAD_REPAIR_MAX_TOLERANCE_MM}")
    print(f"{len(files)} file(s)\n")

    # A real directory rather than a per-run temp one: the repaired files ARE the evidence, and
    # a reviewer wants to open them after the run rather than watch them be deleted.
    scratch = Path(tempfile.gettempdir()) / "repair_corpus_out"
    scratch.mkdir(parents=True, exist_ok=True)
    print(f"repaired files are written to {scratch}")

    rows = []
    for path in files:
        row = _one(path, scratch)
        rows.append(row)
        print(f"{row['outcome']:<28} {row['file'][:52]:<54} "
              f"before={row.get('defects_before', [])} "
              f"located={row.get('located_before', {})} "
              f"-> fixed={row.get('repaired_defects', {})}"
              + (f" [{row['error'][:90]}]" if row.get("error") else ""))

    print("\n== summary ==")
    outcomes: dict[str, int] = {}
    for row in rows:
        key = row["outcome"].split(":")[0] if row["outcome"].startswith("not_attempted") \
            else row["outcome"]
        outcomes[key] = outcomes.get(key, 0) + 1
    for key, count in sorted(outcomes.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {key}")

    total_fixed: dict[str, int] = {}
    for row in rows:
        for code, count in (row.get("repaired_defects") or {}).items():
            total_fixed[code] = total_fixed.get(code, 0) + count
    if total_fixed:
        print("\n== defects actually repaired, by kind (credited by re-inspection) ==")
        for code, count in sorted(total_fixed.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>4}  {code}")
    else:
        print("\n  no defect was repaired in this corpus")

    routes: dict[str, int] = {}
    for row in rows:
        if row.get("route"):
            routes[row["route"]] = routes.get(row["route"], 0) + 1
    print("\n== what triage advised ==")
    for route, count in sorted(routes.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {route}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rows, indent=2, default=str))
        print(f"\nrows written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
