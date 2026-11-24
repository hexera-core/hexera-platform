#!/usr/bin/env python3
# Responsibility: Re-score an arm wave's RECORDED rows under the 2026-11-24 definition (clean = valid + panel),
#                 from the layers those rows already carry. No checker, no mesher, no rerun.
# Owns: `rescore`, `render`, the CLI.
# Boundaries: it recomputes nothing about the geometry - every number comes from a layer the row recorded. It never
#             reads the stored `out_clean` for the new count (that flag was written under the old rule); it reads it
#             only to report the difference. A row missing a layer is REFUSED, never guessed.
"""Re-score recorded rows under "clean = valid + panel".

    rescore.py --from <dir with shard-I-of-N/> --title "dev snapshot v1" [--json out.json] [--md out.md]

Why a separate tool: report.py derives clean with the CURRENT rule and refuses a row whose stored flag disagrees,
which is exactly what every row written before the decision does. This reads the rows directly, states both
numbers side by side, and names the files whose verdict the decision moved.

WHAT IT CANNOT DO. The gaming review (volume / bbox) needs `out_volume_ratio` / `out_bbox_ratio`, which rows written
before the decision do not carry. Those rows are counted as unmeasured and said to be so; the flag is not guessed
from `out_faithful_ok`, which says only that SOME fidelity reason fired.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report as _report  # noqa: E402  (GAMING_* limits and the one gaming rule)

#: the layers a row must carry to be re-scored at all
REQUIRED = ("out_valid", "out_panel_passes", "panel_size")


class RescoreRefused(RuntimeError):
    """A row cannot be re-scored from what it recorded. Refuse rather than invent a layer."""


def _clean(valid: Any, passes: Any, size: Any) -> bool:
    """The 2026-11-24 rule: layer 1 valid AND every panel member passed."""
    return bool(valid) and passes is not None and size is not None and int(passes) == int(size)


def read_rows(root: Path) -> tuple[list[dict], list[str]]:
    """Every shard's rows, and the shards that are still STREAMING.

    A finished shard writes `rows.jsonl` / `rows.json`; a running one streams `rows.partial.jsonl` per file, so a
    wave in flight can be re-scored early (the lead would rather have a provisional table now). The final file wins
    where both exist, and the partial shards are named so the table can say what it is."""
    rows: list[dict] = []
    partial: list[str] = []
    for shard in sorted(root.glob("shard-*")):
        one, many, streaming = shard / "rows.jsonl", shard / "rows.json", shard / "rows.partial.jsonl"
        if one.is_file():
            rows += [json.loads(line) for line in one.read_text().splitlines() if line.strip()]
        elif many.is_file():
            rows += json.loads(many.read_text())
        elif streaming.is_file():
            rows += [json.loads(line) for line in streaming.read_text().splitlines() if line.strip()]
            partial.append(shard.name)
    if not rows:
        raise RescoreRefused(f"no rows under {root}")
    return rows, sorted(partial)


def rescore(root: Path | str, *, arms: Sequence[str] | None = None) -> dict[str, Any]:
    """Both numbers per arm, and the files the decision moved."""
    rows, partial = read_rows(Path(root))
    for r in rows:
        missing = [k for k in REQUIRED if r.get(k) is None]
        if missing and not r.get("inconclusive"):
            raise RescoreRefused(f"{r.get('name')} [{r.get('arm')}] carries no {', '.join(missing)}: "
                                 "it cannot be re-scored, and a missing layer is never assumed")
    names = list(arms) if arms else sorted({str(r["arm"]) for r in rows})
    out: dict[str, Any] = {}
    for a in names:
        mine = [r for r in rows if r["arm"] == a]
        con = [r for r in mine if not r.get("inconclusive")]
        new = [r for r in con if _clean(r["out_valid"], r["out_panel_passes"], r["panel_size"])]
        old = [r for r in con if r.get("out_clean")]
        in_new = [r for r in con if _clean(r.get("in_valid"), r.get("in_panel_passes"), r["panel_size"])]
        in_old = [r for r in con if r.get("in_clean")]
        newly = [r for r in new if not r.get("out_clean")]
        lost = [r for r in old if not _clean(r["out_valid"], r["out_panel_passes"], r["panel_size"])]
        out[a] = {
            "files": len(mine), "conclusive": len(con), "inconclusive": len(mine) - len(con),
            "clean_old": len(old), "clean_new": len(new),
            "clean_in_old": len(in_old), "clean_in_new": len(in_new),
            "clean_gain_old": len(old) - len(in_old), "clean_gain_new": len(new) - len(in_new),
            "newly_clean": [{"name": r["name"], "sha256": r["sha256"], "source": r.get("source"),
                             "why": _why(r)} for r in newly],
            "no_longer_clean": [{"name": r["name"], "sha256": r["sha256"]} for r in lost],
            "gaming": _gaming(new),
        }
    return {"definition": "clean = valid + panel (owner decision 2026-11-24)", "rows": len(rows), "arms": out,
            "partial": bool(partial), "partial_shards": partial}


def _why(row: Mapping[str, Any]) -> str:
    """Why the new rule calls this row clean when the stored flag did not."""
    if row.get("out_faithful_ok") is False:
        return "layer 3 only"
    return "stored flag disagreed with its own layers"


def _gaming(clean_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The owner's review list over the re-scored clean rows, where the rows measured it."""
    measured = [r for r in clean_rows
                if r.get("out_volume_ratio") is not None or r.get("out_bbox_ratio") is not None]
    flagged = []
    for r in measured:
        why = _report.gaming_why(_report.ArmRow(
            name=str(r["name"]), sha256=str(r["sha256"]), source=str(r.get("source") or ""), arm=str(r["arm"]),
            inconclusive=False, model_usd=0.0, wall_s=0.0,
            out_volume_ratio=r.get("out_volume_ratio"), out_bbox_ratio=r.get("out_bbox_ratio"),
            out_max_dev_mm=r.get("out_max_dev_mm")))
        if why:
            flagged.append({"name": r["name"], "sha256": r["sha256"], "volume_ratio": r.get("out_volume_ratio"),
                            "bbox_ratio": r.get("out_bbox_ratio"), "why": ", ".join(why)})
    return {
        "measurable": bool(measured),
        "reviewed": len(measured),
        "unmeasured": len(clean_rows) - len(measured),
        "flagged": sorted(flagged, key=lambda x: str(x["sha256"])),
        "limits": {"volume_change": _report.GAMING_VOLUME_CHANGE, "bbox_shrink": _report.GAMING_BBOX_SHRINK},
        "note": ("rows written before 2026-11-24 carry no volume or bbox ratio, so the review list cannot be "
                 "rebuilt for them; they are counted as unmeasured, not as within the limits"),
    }


def render(out: Mapping[str, Any], *, title: str, source: str) -> str:
    """The table, labelled for what it is."""
    lines = [f"# {title}: RE-SCORE under clean = valid + panel"
             + (" (PROVISIONAL, PARTIAL WAVE)" if out.get("partial") else ""),
             ""]
    if out.get("partial"):
        lines += [f"**PROVISIONAL: the wave is still running.** {len(out['partial_shards'])} shard(s) are streaming "
                  f"their rows ({', '.join(out['partial_shards'])}), so the counts below cover only the files those "
                  "shards have finished so far and will grow. Re-run this when the wave completes.", ""]
    lines += [
             f"**This is a RE-SCORE of an existing run, not a re-run.** Source rows: `{source}`. Every number below "
             f"comes from a layer those rows already recorded ({out['rows']} rows); no checker, mesher or model ran, "
             "and no geometry was measured again.",
             "",
             "The definition changed on 2026-11-24 (owner): **clean = layer 1 valid + layer 2 panel**. Layer 3 "
             "(faithfulness) is still measured and reported, and no longer gates. `old` below is the number the run "
             "published under the previous rule (valid + panel + faithful); `new` is the same rows under the current "
             "one.",
             "",
             "| arm | files | conclusive | clean old | clean new | clean in (new) | gain old | gain new |",
             "|---|---|---|---|---|---|---|---|"]
    for a, b in out["arms"].items():
        lines.append(f"| {a} | {b['files']} | {b['conclusive']} | {b['clean_old']} | **{b['clean_new']}** | "
                     f"{b['clean_in_new']} | {b['clean_gain_old']} | **{b['clean_gain_new']}** |")
    for a, b in out["arms"].items():
        if b["newly_clean"] or b["no_longer_clean"]:
            lines += ["", f"## {a}: files the decision moved"]
            for f in b["newly_clean"]:
                lines.append(f"- now clean: `{f['name']}` ({f['source']}) - {f['why']}")
            for f in b["no_longer_clean"]:
                lines.append(f"- no longer clean: `{f['name']}` (its stored flag did not follow its own layers)")
    g = next(iter(out["arms"].values()))["gaming"] if out["arms"] else {}
    if g:
        lines += ["", "## Gaming review (volume / bbox)", ""]
        if not g["measurable"]:
            lines.append(f"Not available for this run: {g['note']}.")
        else:
            for a, b in out["arms"].items():
                gg = b["gaming"]
                lines.append(f"- {a}: {len(gg['flagged'])} flagged of {gg['reviewed']} reviewed"
                             + (f", {gg['unmeasured']} unmeasured" if gg["unmeasured"] else ""))
                lines += [f"  - `{x['name']}`: {x['why']}" for x in gg["flagged"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="src", required=True, help="directory holding shard-I-of-N/")
    ap.add_argument("--title", required=True)
    ap.add_argument("--source", default="", help="what to cite as the source of the rows (default: --from)")
    ap.add_argument("--json", default="")
    ap.add_argument("--md", default="")
    args = ap.parse_args(argv)
    try:
        out = rescore(Path(args.src))
    except RescoreRefused as exc:
        print(f"REFUSED {exc}", file=sys.stderr)
        return 1
    text = render(out, title=args.title, source=args.source or args.src)
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    if args.md:
        Path(args.md).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
