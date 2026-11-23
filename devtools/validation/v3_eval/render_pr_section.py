#!/usr/bin/env python3
# Responsibility: Render the "Evidence" section of PR #494 from the CRS v3 results JSON, with the
#                 same PENDING rule as the whitepapers: a number nobody measured reads **[PENDING]**.
# Owns: the PR section's wording. The numbers and the formatting are results_to_tex's. There is
#       no pass/fail: the section reports what was measured.
# Boundaries: writes a markdown file only. It never edits the live PR body; a human pastes it.
"""Render docs/superpowers/pr-494-evidence-section.md from a results JSON.

    render_pr_section.py [--results R.json] [--out docs/superpowers/pr-494-evidence-section.md]
    render_pr_section.py --meshability <collect out>/meshability.json --out <section>.md

With --meshability the section is the meshability wave's (meshability_report.render): the two
headlines, the clean control, which op made each file mesh, and the AI loop's marginal gain.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DEFAULT_OUT = REPO / "docs/superpowers/pr-494-evidence-section.md"


def _rt():
    name = "results_to_tex"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / "results_to_tex.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


COLUMNS = (("t6_rules_only_local", "T6 rules-only baseline"),
           ("t29_deterministic", "T29 deterministic"), ("t29_v3", "T29 v3"),
           ("t19_tournament_off", "T19 tournament off"),
           ("t19_tournament_on", "T19 tournament on (= T29 v3 run)"))

ROWS = (("files", "n_files", "int"), ("meshed before", "meshes_before", "int"),
        ("meshed after", "meshes_after", "int"),
        ("**files fixed** (newly meshed by CAD repair)", "newly_meshed", "int"),
        ("**newly broken**", "newly_broken", "int"),
        ("model calls", "model_calls", "int"),
        ("cost (model + VM)", "cost_usd", "usd"), ("shard wall time", "wall_s", "seconds"),
        ("cost per fixed part", "cost_per_fixed_part", "usd"), ("commit", "commit", "hash"),
        ("image", "image_digest", "hash"), ("sample sha256", "sample_sha256", "hash"))

BEYOND_PLAN = """\
## What this PR fixes beyond the plan

Found while landing or running the plan, each with its own commit and test:

- **Naming grid, one per run** (`f0ac2c28`, `5d28389a`). The grid scaled off the part's *current*
  diagonal, so a repair that moved the extent renamed every entity and the progress rule read the
  renamed defect as gone: a false keep. The grid now snaps to a binary octave and is fixed per run.
  This is why the earlier "130 small edges repaired" figure is **withdrawn**: four of four keeps
  probed were false credit.
- **`refused` means a gate said no** (`d304667a`, `2b493788`). Four other answers no longer share
  the word: nothing to repair, no operation claims it, attempted and none held, scan incomplete.
- **Whole-shape sweeps declare what they rebuilt and removed** (`309cc285`). `fix_small_edges`,
  `fix_shape_global` and `sew_faces` returned an empty `rebuilt` set, so the held-for-review guard
  never fired for them. On the way: the sewing reader called a zero-argument `DeletedFace()` the
  binding does not have and swallowed the `TypeError`, so every sewing reported "deleted nothing".
- **The live investigator could never edit** (`2cf9a586`). Three wiring faults meant every `edit`
  was refused with "no scratch clone"; the tests injected a fake factory and hid it.
- **Rehearsal clock** (`812ae022`). A rehearsal's clone got a number where the session calls a
  function, so every tournament candidate that reached a mesh probe failed with a swallowed
  `TypeError`, whatever its geometry did.
- **fTetWild in the pipeline image** (`e087833a`). Tier 2b read `unavailable` on the workers that
  hold the parts. Not yet confirmed on a deployed worker.
- **Five v3 flags plumbed, with settings rows** (`e087833a`, `5d496416`). Tournament, memory,
  review objection, handback and standalone had no settings rows, so every consumer's fallback read
  `off` whatever the environment said. T6 at `0bf78625` ran before this fix, which is why it is
  reported as the rules-only baseline and never as "everything on".
- **CI fixes** (`afb8a5d3`, `feeb1596`, `43053255`). The standalone route joins the operator
  allowlist, the editor seam is typed, and the publication-authority manifest is reconciled with
  the new call sites.
"""


def render(results: dict) -> str:
    rt = _rt()
    md = True
    arms = results.get("arms") or {}
    clean = arms.get(rt.CLEAN) or {}
    det, v3 = arms.get("t29_deterministic") or {}, arms.get("t29_v3") or {}
    lines = ["## Evidence (CRS v3 evaluation)", "",
             "> Rendered by `devtools/validation/v3_eval/render_pr_section.py` from the results "
             "JSON. **[PENDING]** means not measured yet; it is never a zero. No arm passes or "
             "fails: the numbers are reported as measured.", "",
             "### Files fixed", "",
             f"- CRS v3 files fixed (CAD-repaired, and the repaired STEP newly meshes in gmsh): "
             f"{rt.fmt(v3.get('newly_meshed'), md=md)} of {rt.fmt(v3.get('n_files'), md=md)}; "
             f"deterministic catalogue: {rt.fmt(det.get('newly_meshed'), md=md)} of "
             f"{rt.fmt(det.get('n_files'), md=md)}.",
             f"- Clean control: {rt.fmt(clean.get('still_meshes_no_new_defects'), md=md)} of "
             f"{rt.fmt(clean.get('n_files'), md=md)} parts still mesh with no new defect, "
             f"{rt.fmt(clean.get('newly_broken'), md=md)} newly broken"
             + (f" ({clean['basis']})" if clean.get("basis") else "") + ". For information: "
             f"repair changed {rt.fmt(clean.get('changed'), md=md)} clean parts (a kept "
             "operation or a crash).",
             f"- Zero-break bound (information): with no break in the v3 arm's failing sample plus "
             f"the clean control, the 95% upper bound on the break rate is "
             f"{rt.fmt(rt.zero_break_bound(results), 'pct', md=md)} (rule of three over the pooled "
             "parts). It is a bound, not a proof of safety.", "",
             "### Per arm", "",
             "| | " + " | ".join(h for _, h in COLUMNS) + " |",
             "|---|" + "---|" * len(COLUMNS)]
    for label, field, kind in ROWS:
        lines.append(f"| {label} | " + " | ".join(
            rt.fmt((arms.get(a) or {}).get(field), kind, md=md) for a, _ in COLUMNS) + " |")
    lines += ["", "Endings, operations kept and rolled back:", ""]
    for a, h in COLUMNS:
        arm = arms.get(a) or {}
        lines.append(f"- **{h}**: endings {rt.fmt_tally(arm.get('endings'), md=md)}; kept "
                     f"{rt.fmt_tally(arm.get('ops_kept'), md=md)}; rolled back "
                     f"{rt.fmt_tally(arm.get('ops_rolled_back'), md=md)}"
                     + (f". _{arm['notes']}_" if arm.get("notes") else ""))
    lines += ["", "### Diagnostic (appendix, not a result)", "",
              "Off by default (`repair_corpus --tolerant-delivery`). Not a fix, not a product "
              "fallback, and never counted in files fixed.", "",
              "| | " + " | ".join(h for _, h in COLUMNS) + " |",
              "|---|" + "---|" * len(COLUMNS),
              f"| {rt.TOLERANT_LABEL} | " + " | ".join(
                  rt.fmt((arms.get(a) or {}).get("delivered_via_tolerant_mesher"), md=md)
                  for a, _ in COLUMNS) + " |"]
    lines += ["", "### What this does not show", "",
              "- **T6 is the rules-only baseline.** macOS, commit `0bf78625`, no model configured, "
              "and the tournament and memory inert because their settings rows did not exist yet. "
              "On its first 90 files it newly meshed **0** parts.",
              "- **The agentic arm had never been measured before this run.** Every v3 number above "
              "is the first.",
              "- **The probe is not the product mesher.** `meshes_before/after` is the coarse gmsh "
              "probe. A part it meshes can still fail the product ladder, and the reverse.",
              "- **Sample size.** 120 failing files (stratified, seed 20261120) plus 100 clean. "
              "A difference of one or two parts is noise. One run per arm; model arms are "
              "nondeterministic.",
              "- **The earlier \"130 small edges repaired\" is withdrawn.** It was substantially "
              "false credit from the naming-grid bug fixed below.", "",
              BEYOND_PLAN]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    rt = _rt()
    parser.add_argument("--results", default=str(rt.PENDING_JSON))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--meshability", default="",
                        help="render the meshability wave's section from collect's "
                             "meshability.json instead of the T19/T29 results")
    args = parser.parse_args(argv)
    if args.meshability:
        import json

        spec = importlib.util.spec_from_file_location("meshability_report",
                                                      HERE / "meshability_report.py")
        mr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mr)
        Path(args.out).write_text(mr.render(json.loads(Path(args.meshability).read_text())),
                                  encoding="utf-8")
        print(f"wrote {args.out}")
        return 0
    Path(args.out).write_text(render(rt.load(Path(args.results))), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
