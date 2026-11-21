#!/usr/bin/env python3
# Responsibility: The meshability wave's report: per arm, the two headline numbers (the returned
#                 STEP meshes at default settings / with the hints), gmsh's own defaults as a
#                 labelled secondary column, newly broken, valid->invalid, steps not returned (a
#                 bug), the clean control, which op made each file mesh, and the AI loop's
#                 marginal gain over the deterministic pass - per file, both directions.
# Owns: meshability.json and the markdown section rendered from it.
# Boundaries: PURE over sealed rows. Every count is repair_corpus.meshability_summary's or read off
#             the rows' meshability keys; nothing here probes, reads CAD or decides pass/fail.
"""The meshability wave's report.

    build(rows_by_unit)  -> dict (meshability.json)
    render(report)       -> str  (markdown)

`rows_by_unit` maps (arm, kind) -> rows. The baseline and the AI arm are evalspec.COMPARISONS
["AI_MARGINAL"]; a file is credited to the AI only when it meshes (headline settings) in the AI arm
and NOT in the pass arm, and the reverse is reported as the AI's loss.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

HEADLINE = "meshes_after_default"


def _meshes(row: dict, key: str = HEADLINE) -> bool:
    return row.get(key) is True


def _with_hints(row: dict) -> bool:
    return _meshes(row) or _meshes(row, "meshes_after_hints")


def per_file(rows: list[dict]) -> dict[str, dict]:
    """file -> the before/after facts the report shows for one arm."""
    out = {}
    for r in rows:
        rs = r.get("returned_step") or {}
        out[str(r.get("file"))] = {
            "meshes_before": r.get("meshes_before"),
            "returned": bool(rs.get("present")),
            "source": rs.get("source"),
            "default": r.get(HEADLINE),
            "hints": r.get("meshes_after_hints"),
            "gmsh_default": r.get("meshes_after_gmsh_default"),
            "valid_to_invalid": r.get("valid_to_invalid"),
            "meshed_by": r.get("meshed_by"),
            "outcome": r.get("outcome"),
        }
    return out


def ai_marginal(base: list[dict], ai: list[dict]) -> dict:
    """Files meshing (headline, and with hints) in one arm and not the other. Only files present
    in BOTH arms are compared; the rest are listed as unpaired."""
    b, a = {r["file"]: r for r in base}, {r["file"]: r for r in ai}
    both = sorted(set(b) & set(a))

    def diff(test: Callable[[dict], bool]) -> dict:
        gain = [f for f in both if test(a[f]) and not test(b[f])]
        loss = [f for f in both if test(b[f]) and not test(a[f])]
        return {"ai_only": gain, "pass_only": loss, "net": len(gain) - len(loss)}

    return {"paired_files": len(both), "unpaired": sorted(set(b) ^ set(a)),
            "default": diff(_meshes), "with_hints": diff(_with_hints)}


def clean_control(rows: list[dict]) -> dict:
    """The clean 100 under one arm: how many still mesh at the headline settings as RETURNED."""
    scored = [r for r in rows if "returned_step" in r]
    return {"files": len(scored),
            "still_meshes_default": sum(1 for r in scored if _meshes(r)),
            "newly_broken_default": sum(1 for r in scored if r.get("meshes_before")
                                        and not _meshes(r)),
            "no_returned_step": sum(1 for r in scored
                                    if not (r.get("returned_step") or {}).get("present")),
            "valid_to_invalid": sum(1 for r in scored if r.get("valid_to_invalid")),
            "changed": sum(1 for r in scored
                           if (r.get("returned_step") or {}).get("source") == "repaired")}


def build(rows_by_unit: Mapping[tuple[str, str], list[dict]], *, summarise: Callable,
          comparison: tuple[str, str]) -> dict:
    """`summarise(rows)` is repair_corpus.meshability_summary; `comparison` is (pass, ai)."""
    arms: dict = {}
    for (arm, kind), rows in sorted(rows_by_unit.items()):
        entry = arms.setdefault(arm, {})
        if kind == "failing":
            entry["failing"] = summarise(rows)
            entry["files"] = per_file(rows)
        else:
            entry["clean"] = clean_control(rows)
    base, ai = comparison
    report = {"schema": "crs-meshability-report/1", "headline_settings": {
        "default": "gmsh via cad/repair/mesh_probe, MeshSizeMax = diagonal/40 (waves 1-9's probe)",
        "with_hints": "the default settings plus the returned STEP's .mesh-hints.json sizes",
        "gmsh_default": "gmsh with no size setting (secondary; what a customer gets out of the box)",
    }, "comparison": {"pass": base, "ai": ai}, "arms": arms}
    if (base, "failing") in rows_by_unit and (ai, "failing") in rows_by_unit:
        report["ai_marginal"] = ai_marginal(rows_by_unit[(base, "failing")],
                                            rows_by_unit[(ai, "failing")])
    return report


def _v(x) -> str:
    return "n/a" if x is None else str(x)


def render(report: dict) -> str:
    """The markdown section: the headline table, the clean control, credit, the AI marginal."""
    arms = report.get("arms") or {}
    names = [report["comparison"]["pass"], report["comparison"]["ai"]]
    lines = ["## Meshability", "",
             "Every count is a file count on the RETURNED STEP. The two headlines are never summed;",
             "hints only add to a returned STEP.", "",
             "| | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    rows = [("files", "files"), ("STEPs returned", "steps_returned"),
            ("NO STEP RETURNED (bug)", "no_returned_step"),
            ("meshed before", "meshed_before"),
            ("**STEP meshes at default settings**", "meshes_default"),
            ("newly meshing, default settings", "newly_meshes_default"),
            ("**STEP meshes with the hints**", "meshes_with_hints"),
            ("newly meshing, with the hints", "newly_meshes_with_hints"),
            ("meshes only with the hints", "meshes_only_with_hints"),
            ("gmsh defaults (secondary): meshes after", "meshes_gmsh_default"),
            ("gmsh defaults (secondary): newly meshing", "newly_meshes_gmsh_default"),
            ("newly broken (default settings)", "newly_broken_default"),
            ("valid -> invalid (delivered file)", "valid_to_invalid"),
            ("returned unchanged (passthrough)", "source_passthrough"),
            ("crashed", "crashed")]
    for label, key in rows:
        lines.append(f"| {label} | " + " | ".join(
            _v((arms.get(n, {}).get("failing") or {}).get(key)) for n in names) + " |")
    lines += ["", "**Clean control** (still meshes at default settings, as returned):", ""]
    for n in names:
        c = arms.get(n, {}).get("clean")
        if c:
            lines.append(f"- {n}: {c['still_meshes_default']}/{c['files']} still mesh; "
                         f"{c['newly_broken_default']} newly broken; {c['no_returned_step']} "
                         f"without a returned STEP; {c['valid_to_invalid']} valid->invalid; "
                         f"{c['changed']} changed")
    lines += ["", "**Which op made the file mesh** (default settings):", ""]
    for n in names:
        credit = (arms.get(n, {}).get("failing") or {}).get("meshed_by") or {}
        lines.append(f"- {n}: " + (", ".join(f"{k} {v}" for k, v in credit.items()) or "none"))
    m = report.get("ai_marginal")
    if m:
        d, h = m["default"], m["with_hints"]
        lines += ["", f"**AI marginal over the pass** ({m['paired_files']} paired files):", "",
                  f"- default settings: +{len(d['ai_only'])} AI-only, -{len(d['pass_only'])} "
                  f"pass-only, net {d['net']:+d}",
                  f"- with the hints: +{len(h['ai_only'])} AI-only, -{len(h['pass_only'])} "
                  f"pass-only, net {h['net']:+d}"]
        for f in d["ai_only"]:
            lines.append(f"  - AI-only: `{f}`")
        for f in d["pass_only"]:
            lines.append(f"  - pass-only (AI lost it): `{f}`")
    return "\n".join(lines) + "\n"
