#!/usr/bin/env python3
# Responsibility: Reproduce the survey's corpus figures in docs/reference/configuration.md, through this platform's own functions.
# Boundaries: a measurement tool; it calls no model, writes only the documents it is told to cache, and changes nothing it reads.
#
# Every figure it prints is deterministic. It measures each corpus file with the upload path's own
# `measure_local_file`, composes the survey twice with `application/geometry_survey.py` (once for the
# purpose the upload assumes, once for the case's own purpose, brief and ports), and counts what each
# composition would put to a customer and hand the builder. With --looks it also attaches cached look
# replies with `geometry_vision.attach_look` and counts the placed findings in the planner's block and
# in the intake prompt; those replies are model draws, so each draw is printed and none is averaged.
#
#   python devtools/quality/measure_survey_on_corpus.py \
#       --cases <geometry_agent>/eval/corpus_export_cases --docs /tmp/corpus_docs \
#       [--looks <geometry_agent>/eval/vision/results/combo_sa]
#
# The case directories are the measurement package's corpus export: an `expected.json` carrying
# `source`, `purpose`, `brief`, `declared` and `cell_cap`. The package must be importable.
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path


def _ports(declared) -> list[dict]:
    """The corpus declaration in the shape intake's patches carry."""
    out = []
    for d in declared or []:
        row = {"name": d.get("name") or d.get("role"), "type": d.get("role")}
        if d.get("near") is not None:
            row["near_mm"] = list(d["near"])
        if d.get("bore_diameter"):
            row["diameter_mm"] = d["bore_diameter"]
        out.append(row)
    return out


def _measured(cases: Path, docs: Path) -> dict[str, tuple[dict, dict]]:
    from meshpipeline.application.geometry_measurement import measure_local_file

    docs.mkdir(parents=True, exist_ok=True)
    out = {}
    for case in sorted(p for p in cases.iterdir() if (p / "expected.json").is_file()):
        expected = json.loads((case / "expected.json").read_text(encoding="utf-8"))
        cached = docs / f"{case.name}.json"
        if not cached.is_file():
            src = Path(expected["source"])
            cached.write_text(json.dumps(measure_local_file(src, timeout_s=180,
                                                            source={"original_filename": src.name}),
                                         default=str), encoding="utf-8")
        out[case.name] = (expected, json.loads(cached.read_text(encoding="utf-8")))
    return out


def _survey_figures(measured: dict[str, tuple[dict, dict]]) -> None:
    from meshpipeline.application import geometry_survey as gs
    from meshpipeline.engines.snappy.planner import _validated_agent_block

    tally: collections.Counter = collections.Counter()
    changed: collections.Counter = collections.Counter()
    sizes: list[int] = []
    for _name, (expected, doc) in measured.items():
        tally["parts"] += 1
        if doc.get("status") != "ok":
            tally["not measured"] += 1
            continue
        purpose = expected.get("purpose") or "internal_cfd"
        before = gs.carry_answers(None, gs.compose(doc, purpose="internal_cfd"))
        after = gs.carry_answers(None, gs.compose(doc, purpose=purpose, brief=expected.get("brief") or "",
                                                  declared=_ports(expected.get("declared")) or None))
        b4 = [v for v in gs.question_views(before) if v["route"] == gs.ROUTE_INTAKE]
        a4 = [v for v in gs.question_views(after) if v["route"] == gs.ROUTE_INTAKE]
        tally["step 4 questions, at upload"] += len(b4)
        tally["parts asked, at upload"] += bool(b4)
        tally["step 4 questions, after intake"] += len(a4)
        tally["parts asked, after intake"] += bool(a4)
        if purpose == "external_cfd":
            tally["external parts"] += 1
            tally["external asked for a role, at upload"] += any(v["about"] == "opening.role" for v in b4)
            tally["external asked for a role, after intake"] += any(v["about"] == "opening.role" for v in a4)
        if before["composed_for"]["representation"] != after["composed_for"]["representation"]:
            changed[f"{before['composed_for']['representation']} -> {after['composed_for']['representation']}"] += 1
        tally["customer_cell_cap set, at upload"] += bool((doc.get("planner_block") or {}).get("customer_cell_cap"))
        tally["customer_cell_cap set, after intake"] += bool(after["planner_block"].get("customer_cell_cap"))
        tally["budget trades"] += any(v["route"] == gs.ROUTE_TRADE for v in gs.question_views(after))
        block = gs.builder_block(after)
        kept = _validated_agent_block(block, "corpus") if block else None
        if kept and "survey" in kept:
            tally["survey reaching the planner"] += 1
            sizes.append(len(json.dumps(kept["survey"], default=str)))
    for key, value in tally.items():
        print(f"{key:45s} {value}")
    print(f"{'representation changed by the customer':45s} {sum(changed.values())} {dict(changed)}")
    if sizes:
        print(f"{'survey characters, min / max':45s} {min(sizes)} / {max(sizes)}")


def _placed_figures(measured: dict[str, tuple[dict, dict]], looks: Path) -> None:
    from geometry_agent.agent import hexera

    from meshpipeline.agents.intake.geometry_brief import look_lines
    from meshpipeline.application.geometry_vision import attach_look

    print("draw | parts | placed findings in the planner's block | lines in the intake prompt")
    for draw in sorted(p for p in looks.iterdir() if p.is_dir() and p.name.startswith("looks")):
        parts = rows = shown = 0
        for rec_path in sorted(draw.glob("*.json")):
            if rec_path.stem not in measured:
                continue
            rec = json.loads(rec_path.read_text(encoding="utf-8"))
            doc = measured[rec_path.stem][1]
            if doc.get("status") != "ok" or not isinstance(rec.get("impression"), dict):
                continue
            look = hexera.look_block(rec["impression"], model=str(rec.get("model") or ""), status="ok",
                                     drawn={"marks": rec.get("mark_places") or [],
                                            "stops": rec.get("stopped") or []})
            updated = attach_look(doc, look)
            parts += 1
            rows += len(((updated.get("planner_block") or {}).get("look") or {}).get("at_places") or [])
            shown += sum(1 for line in look_lines(updated)
                         if line.strip().startswith(("marked place", "passage end")))
        print(f"{draw.name} | {parts} | {rows} | {shown}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True, type=Path)
    ap.add_argument("--docs", required=True, type=Path)
    ap.add_argument("--looks", type=Path)
    args = ap.parse_args()
    measured = _measured(args.cases, args.docs)
    _survey_figures(measured)
    if args.looks:
        _placed_figures(measured, args.looks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
