#!/usr/bin/env python3
# Responsibility: Turn the CRS v3 verdict results JSON (results.schema.json) into the LaTeX macros
#                 and tables the three whitepapers \input, and compute the T29 verdict from the
#                 arms themselves. Also adapts collect.py's output (verdict.json plus the sealed
#                 bundles' summary.json) and T6's local rows into that JSON.
# Owns: the macro names, the PENDING rendering, the verdict rule as rendered.
# Boundaries: no CAD, no cloud. A value that is missing, null, NaN or infinite is NOT MEASURED and
#             renders as a visible \textbf{[PENDING]} - never as 0, because a zero in a results
#             table is a claim. collect.py owns every metric; this file only moves numbers.
"""Render the CRS v3 results JSON into docs/whitepapers/overleaf/crs-v3-results.tex.

    results_to_tex.py render [--results R.json] [--out crs-v3-results.tex]
    results_to_tex.py from-collect --verdict-dir <collect --out dir> [--t6-rows rows-*.json ...]
                                   [--out results.json]

With no --results, `render` reads results.pending.json (every value null), which is what the
papers carry until the runs land. The minute collect.py has sealed the bundles:

    results_to_tex.py from-collect --verdict-dir devtools/validation/evidence/2026-11-20-crs-v3-verdict \\
        --t6-rows ~/archductor/eval-results/t6-rules-only-0bf78625/rows-*.json --out /tmp/r.json
    results_to_tex.py render --results /tmp/r.json
    render_pr_section.py --results /tmp/r.json
    (cd docs/whitepapers/overleaf && latexmk -pdf main.tex full-paper.tex customer-brief.tex)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PENDING_JSON = HERE / "results.pending.json"
DEFAULT_TEX = REPO / "docs/whitepapers/overleaf/crs-v3-results.tex"

PENDING_TEX = r"\textbf{[PENDING]}"
PENDING_MD = "**[PENDING]**"
NA = "n/a"
RATIO = 4

#: arm id -> (macro infix, column header). t29_v3's infix is empty so its macros read
#: \vThreeNewlyMeshed - the headline number of the paper.
ARMS: dict[str, tuple[str, str]] = {
    "t6_rules_only_local": ("RulesOnly", "T6 rules-only"),
    "t29_deterministic": ("Det", "T29 deterministic"),
    "t29_v3": ("", "T29 v3"),
    "t19_tournament_off": ("TourOff", "T19 tourn.\\ off"),
    "t19_tournament_on": ("TourOn", "T19 tourn.\\ on"),
}
CLEAN = "clean_control"

#: The labels the papers must use. T6's is fixed by the owner: at 0bf78625 the tournament and memory
#: had no settings rows, so they were inert whatever the env said, and no model was configured.
LABELS = {
    "t6_rules_only_local": "rules-only baseline",
    "t29_deterministic": "deterministic catalogue (investigator and tournament off)",
    "t29_v3": "CRS v3 (investigator live, tournament live, K=3)",
    "t19_tournament_off": "investigator live, tournament off",
    "t19_tournament_on": "investigator live, tournament live, K=3",
    CLEAN: "clean control",
}

ARM_FIELDS = ("arm", "label", "commit", "image_digest", "sample_sha256", "n_files", "newly_meshed",
              "newly_broken", "meshes_before", "meshes_after", "endings", "ops_kept",
              "ops_rolled_back", "model_calls", "cost_usd", "wall_s", "cost_per_fixed_part")
CLEAN_FIELDS = ("arm", "label", "commit", "image_digest", "sample_sha256", "n_files",
                "byte_unchanged", "newly_broken")


# --------------------------------------------------------------------------- values

def measured(value) -> bool:
    """True for a value that was measured. None, NaN and +/-inf are not; neither is a bool
    pretending to be a count. 0 IS measured."""
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, (int, str, dict)):
        return True
    return False


def get(results: dict, arm: str, field: str):
    return ((results.get("arms") or {}).get(arm) or {}).get(field)


def tex_escape(text: str) -> str:
    out = []
    for ch in str(text):
        out.append({"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
                    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
                    "^": r"\textasciicircum{}"}.get(ch, ch))
    return "".join(out)


def _int_tex(n: int) -> str:
    s = f"{int(n):,}"
    return s.replace(",", "{,}")


def fmt(value, kind: str = "int", *, md: bool = False) -> str:
    """One value, rendered. kind: int | usd | seconds | hash | text | pct."""
    pending = PENDING_MD if md else PENDING_TEX
    if isinstance(value, str) and value.strip().lower() == NA:
        return NA
    if not measured(value):
        return pending
    if kind == "int":
        if isinstance(value, float) and not value.is_integer():
            return f"{value:.1f}"
        return f"{int(value):,}" if md else _int_tex(int(value))
    if kind == "usd":
        return (f"${float(value):,.2f}" if md else rf"\${float(value):,.2f}".replace(",", "{,}"))
    if kind == "seconds":
        v = float(value)
        if md:
            return f"{v:,.0f} s"
        return _int_tex(round(v)) + r"\,s"
    if kind == "pct":
        return f"{100 * float(value):.1f}%" if md else f"{100 * float(value):.1f}\\%"
    if kind == "hash":
        text = str(value)
        short = text.split(":", 1)[-1][:12] if text.startswith("sha256:") else text[:12]
        return f"`{short}`" if md else rf"\texttt{{{tex_escape(short)}}}"
    return str(value) if md else tex_escape(str(value))


def fmt_tally(tally, *, md: bool = False) -> str:
    if isinstance(tally, str) and tally.strip().lower() == NA:
        return NA
    if not isinstance(tally, dict):
        return PENDING_MD if md else PENDING_TEX
    items = sorted(((k, v) for k, v in tally.items() if measured(v)), key=lambda kv: (-kv[1], kv[0]))
    if not items:
        return "none"
    if md:
        return ", ".join(f"`{k}` {v}" for k, v in items)
    return ", ".join(rf"\texttt{{{tex_escape(k)}}}~{_int_tex(v)}" for k, v in items)


# --------------------------------------------------------------------------- the verdict

def compute_verdict(results: dict) -> dict:
    """THE T29 RULE, from the arms: v3.newly_meshed >= 4 x deterministic.newly_meshed AND
    newly_broken == 0 (both arms) AND clean byte_unchanged == n. If the deterministic arm newly
    meshed nothing the ratio is degenerate: the 4x goal is UNDEFINED, the verdict is judged on v3's
    absolute count (> 0), and the statement says so. Anything missing makes the verdict PENDING."""
    det = get(results, "t29_deterministic", "newly_meshed")
    v3 = get(results, "t29_v3", "newly_meshed")
    det_b = get(results, "t29_deterministic", "newly_broken")
    v3_b = get(results, "t29_v3", "newly_broken")
    c_n = get(results, CLEAN, "n_files")
    c_u = get(results, CLEAN, "byte_unchanged")
    c_b = get(results, CLEAN, "newly_broken")
    out = {"rule": (f"v3 newly_meshed >= {RATIO} x deterministic newly_meshed, newly_broken == 0 "
                    f"in both arms, and every clean-control part unchanged"),
           "deterministic_newly_meshed": det if measured(det) else None,
           "v3_newly_meshed": v3 if measured(v3) else None,
           "ratio": None, "ratio_defined": None, "judged_on": None, "ratio_ok": None,
           "newly_broken_zero": None, "clean_ok": None, "status": "PENDING",
           "statement": "Pending: the T29 arms have not reported."}
    needed = (det, v3, det_b, v3_b, c_n, c_u, c_b)
    if not all(measured(x) for x in needed):
        return out
    det, v3, c_n, c_u = int(det), int(v3), int(c_n), int(c_u)
    broken = int(det_b) + int(v3_b) + int(c_b)
    out["newly_broken_zero"] = broken == 0
    out["clean_ok"] = c_u == c_n
    if det == 0:
        out.update(ratio=None, ratio_defined=False, judged_on="absolute", ratio_ok=v3 > 0)
        head = (f"4x goal: undefined (deterministic ceiling 0). Judged on v3's absolute count "
                f"instead: v3 newly meshed {v3}.")
    else:
        ratio = v3 / det
        out.update(ratio=round(ratio, 3), ratio_defined=True, judged_on="ratio",
                   ratio_ok=v3 >= RATIO * det)
        head = f"v3 newly meshed {v3} against {RATIO} x {det} = {RATIO * det} ({ratio:.2f}x)."
    ok = bool(out["ratio_ok"] and out["newly_broken_zero"] and out["clean_ok"])
    out["status"] = "PASS" if ok else "FAIL"
    tail = []
    if not out["newly_broken_zero"]:
        tail.append(f"{broken} part(s) newly broken")
    if not out["clean_ok"]:
        tail.append(f"clean control {c_u} of {c_n} unchanged")
    if not out["ratio_ok"]:
        tail.append("the count rule is not met")
    out["statement"] = (f"{out['status']}. {head}" + (f" Fails on: {'; '.join(tail)}." if tail else "")
                        + (" The 4x goal is not claimed." if det == 0 else ""))
    return out


def zero_break_bound(results: dict):
    """Rule of three: with zero breaks over n parts, the 95% upper bound on the break rate is 3/n.
    n pools the v3 arm's failing sample with the clean control (120 + 100 = 220 -> 1.4%). None
    until both counts exist, and None unless both newly_broken counts are measured zeros."""
    n1, n2 = get(results, "t29_v3", "n_files"), get(results, CLEAN, "n_files")
    if not (measured(n1) and measured(n2)) or int(n1) + int(n2) == 0:
        return None
    breaks = (get(results, "t29_v3", "newly_broken"), get(results, CLEAN, "newly_broken"))
    if not all(measured(b) and int(b) == 0 for b in breaks):
        return None  # the rule of three bounds a zero-break observation, nothing else
    return 3.0 / (int(n1) + int(n2))


# --------------------------------------------------------------------------- validation

def validate(results: dict) -> None:
    """The structural half of results.schema.json, enough to refuse a file the renderer would
    misread. (jsonschema is not a dependency of this repo.)"""
    if results.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    arms = results.get("arms")
    if not isinstance(arms, dict):
        raise ValueError("arms must be an object")
    want = set(ARMS) | {CLEAN}
    if set(arms) != want:
        raise ValueError(f"arms must be exactly {sorted(want)}; got {sorted(arms)}")
    for arm_id, arm in arms.items():
        fields = CLEAN_FIELDS if arm_id == CLEAN else ARM_FIELDS
        missing = [f for f in fields if f not in arm]
        if missing:
            raise ValueError(f"{arm_id}: missing field(s) {missing} (use null for not measured)")
        if arm.get("arm") != arm_id:
            raise ValueError(f"{arm_id}: its 'arm' field says {arm.get('arm')!r}")
    if arms["t6_rules_only_local"].get("label") != LABELS["t6_rules_only_local"]:
        raise ValueError("t6_rules_only_local must be labelled exactly 'rules-only baseline'")


def load(path: Path) -> dict:
    results = json.loads(Path(path).read_text(encoding="utf-8"))
    validate(results)
    return results


# --------------------------------------------------------------------------- LaTeX

FIELD_MACROS = (  # (field, macro suffix, kind)
    ("n_files", "NFiles", "int"), ("newly_meshed", "NewlyMeshed", "int"),
    ("newly_broken", "NewlyBroken", "int"), ("meshes_before", "MeshesBefore", "int"),
    ("meshes_after", "MeshesAfter", "int"), ("model_calls", "ModelCalls", "int"),
    ("cost_usd", "CostUsd", "usd"), ("wall_s", "WallS", "seconds"),
    ("cost_per_fixed_part", "CostPerFixed", "usd"), ("commit", "Commit", "hash"),
    ("image_digest", "Image", "hash"), ("sample_sha256", "Sample", "hash"),
)


def macros(results: dict) -> dict[str, str]:
    """macro name (without backslash) -> rendered body."""
    out: dict[str, str] = {}
    for arm_id, (infix, _) in ARMS.items():
        arm = (results.get("arms") or {}).get(arm_id) or {}
        for field, suffix, kind in FIELD_MACROS:
            out[f"vThree{infix}{suffix}"] = fmt(arm.get(field), kind)
        out[f"vThree{infix}Endings"] = fmt_tally(arm.get("endings"))
        out[f"vThree{infix}OpsKept"] = fmt_tally(arm.get("ops_kept"))
        out[f"vThree{infix}OpsRolledBack"] = fmt_tally(arm.get("ops_rolled_back"))
        out[f"vThree{infix}Label"] = tex_escape(arm.get("label") or LABELS[arm_id])
    clean = (results.get("arms") or {}).get(CLEAN) or {}
    for field, suffix, kind in (("n_files", "NFiles", "int"), ("byte_unchanged", "ByteUnchanged", "int"),
                                ("newly_broken", "NewlyBroken", "int"),
                                ("commit", "Commit", "hash"), ("sample_sha256", "Sample", "hash")):
        out[f"vThreeClean{suffix}"] = fmt(clean.get(field), kind)
    verdict = compute_verdict(results)
    out["vThreeVerdict"] = PENDING_TEX if verdict["status"] == "PENDING" else rf"\textbf{{{verdict['status']}}}"
    out["vThreeVerdictRatio"] = (PENDING_TEX if verdict["status"] == "PENDING" else
                                 (f"{verdict['ratio']:.2f}" + r"$\times$") if verdict["ratio_defined"]
                                 else "undefined (deterministic ceiling 0)")
    out["vThreeVerdictStatement"] = (PENDING_TEX if verdict["status"] == "PENDING"
                                     else tex_escape(verdict["statement"]))
    out["vThreeVerdictRule"] = ("the v3 arm newly meshes at least four times as many parts as the "
                                "deterministic arm, neither arm newly breaks a part, and every "
                                "clean-control part comes back unchanged")
    out["vThreeZeroBreakBound"] = fmt(zero_break_bound(results), "pct")
    return out


def _row(label: str, cells: list[str]) -> str:
    return label + " & " + " & ".join(cells) + r" \\"


def results_table(results: dict, m: dict[str, str]) -> str:
    cols = list(ARMS)
    infix = {a: ARMS[a][0] for a in cols}
    lines = [
        r"\begin{table}[h]", r"\centering",
        r"\caption{CRS v3 measured results. Columns are the five arms; T19 ``tournament on'' is the "
        r"same run as T29 v3. Every count is a part count from the coarse gmsh probe. A "
        r"\textbf{[PENDING]} cell has not been measured; it is not a zero.}",
        r"\label{tab:v3results}", r"\footnotesize",
        r"\begin{tabular}{@{}l" + "r" * len(cols) + r"@{}}", r"\toprule",
        _row("", [r"\textbf{" + ARMS[a][1] + "}" for a in cols]), r"\midrule",
    ]
    for label, suffix in (("Files", "NFiles"), ("Meshed before", "MeshesBefore"),
                          ("Meshed after", "MeshesAfter"), (r"\textbf{Newly meshed}", "NewlyMeshed"),
                          (r"\textbf{Newly broken}", "NewlyBroken"), ("Model calls", "ModelCalls"),
                          (r"Cost (model + VM)", "CostUsd"), ("Shard wall time", "WallS"),
                          ("Cost per fixed part", "CostPerFixed")):
        lines.append(_row(label, [m[f"vThree{infix[a]}{suffix}"] for a in cols]))
    lines += [r"\midrule",
              r"\multicolumn{" + str(len(cols) + 1) + r"}{@{}l}{Clean control: "
              + m["vThreeCleanByteUnchanged"] + " of " + m["vThreeCleanNFiles"]
              + " parts unchanged, " + m["vThreeCleanNewlyBroken"] + r" newly broken.} \\",
              r"\multicolumn{" + str(len(cols) + 1) + r"}{@{}l}{T29 verdict: "
              + m["vThreeVerdict"] + r"; ratio " + m["vThreeVerdictRatio"] + r".} \\",
              r"\bottomrule", r"\end{tabular}", r"\end{table}"]
    return "\n".join(lines)


def ops_table(results: dict) -> str:
    det = (results.get("arms") or {}).get("t29_deterministic") or {}
    v3 = (results.get("arms") or {}).get("t29_v3") or {}
    tallies = [det.get("ops_kept"), det.get("ops_rolled_back"), v3.get("ops_kept"),
               v3.get("ops_rolled_back")]
    head = [r"\begin{table}[h]", r"\centering",
            r"\caption{Operations kept and rolled back, T29 deterministic against T29 v3.}",
            r"\label{tab:v3ops}", r"\footnotesize", r"\begin{tabular}{@{}l r r r r@{}}", r"\toprule",
            r" & \multicolumn{2}{c}{\textbf{Deterministic}} & \multicolumn{2}{c}{\textbf{v3}}\\",
            r"\textbf{Operation} & kept & rolled back & kept & rolled back\\", r"\midrule"]
    if not all(isinstance(t, dict) for t in tallies):
        body = [r"\multicolumn{5}{@{}l}{" + PENDING_TEX + r"} \\"]
    else:
        ops = sorted(set().union(*[set(t) for t in tallies]))
        body = [_row(rf"\texttt{{{tex_escape(op)}}}", [fmt(t.get(op, 0)) for t in tallies])
                for op in ops] or [r"\multicolumn{5}{@{}l}{no operation attempted} \\"]
    return "\n".join(head + body + [r"\bottomrule", r"\end{tabular}", r"\end{table}"])


def render_tex(results: dict, *, source: str = "") -> str:
    m = macros(results)
    lines = [
        "% GENERATED by devtools/validation/v3_eval/results_to_tex.py - do not edit by hand.",
        f"% source: {source or results.get('source') or 'unknown'}",
        f"% generated_at: {results.get('generated_at') or 'unknown'}",
        "% A [PENDING] value has not been measured. It is never a zero.",
        "% \\providecommand so the file can be \\input more than once and override nothing.",
    ]
    for name, body in m.items():
        lines.append(rf"\providecommand{{\{name}}}{{{body}}}")
    lines.append(r"\providecommand{\vThreeResultsTable}{%")
    lines.append(results_table(results, m) + "}")
    lines.append(r"\providecommand{\vThreeOpsTable}{%")
    lines.append(ops_table(results) + "}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- from collect.py

def _blank_arm(arm_id: str) -> dict:
    out = dict.fromkeys(ARM_FIELDS)
    out.update(arm=arm_id, label=LABELS[arm_id], same_run_as=None, notes=None)
    return out


def blank_results() -> dict:
    arms = {a: _blank_arm(a) for a in ARMS}
    arms["t19_tournament_on"]["same_run_as"] = "t29_v3"
    arms[CLEAN] = {**dict.fromkeys(CLEAN_FIELDS), "arm": CLEAN, "label": LABELS[CLEAN],
                   "arms_covered": None, "basis": None, "notes": None}
    return {"schema_version": 1, "generated_at": None, "source": None, "arms": arms,
            "verdict": None}


def _from_summary(arm: dict, summary: dict) -> None:
    meshing = summary.get("meshing") or {}
    arm["meshes_before"] = meshing.get("meshed_before")
    arm["meshes_after"] = meshing.get("meshed_after")
    arm["newly_meshed"] = meshing.get("newly_meshed")
    arm["newly_broken"] = meshing.get("newly_broken")
    arm["endings"] = summary.get("endings")
    tally = (summary.get("catalogue") or {}).get("tally")
    if isinstance(tally, dict):
        arm["ops_kept"] = {op: int(c.get("kept") or 0) for op, c in tally.items()}
        arm["ops_rolled_back"] = {op: int(c.get("rolled_back") or 0) for op, c in tally.items()}


#: collect.py's arm names (evalspec.ARMS) -> ours. v3_full is both T29's v3 arm and T19's "on".
COLLECT_ARMS = {"t29_deterministic": ("t29_deterministic",),
                "t19_tournament_off": ("t19_tournament_off",),
                "v3_full": ("t29_v3", "t19_tournament_on")}


def from_collect(verdict_dir: Path, results: dict | None = None) -> dict:
    """collect.py's --out directory (verdict.json + bundle-<arm>-<kind>/summary.json) -> results.
    verdict.json carries cost and model usage but NOT the meshing summary, which is read from each
    sealed bundle's summary.json (repair_corpus's own aggregate, recomputed from the rows)."""
    verdict_dir = Path(verdict_dir)
    results = results or blank_results()
    data = json.loads((verdict_dir / "verdict.json").read_text(encoding="utf-8"))
    checked = data.get("checked") or {}
    samples = checked.get("samples") or {}
    clean_runs = []
    for key, rep in (data.get("arms") or {}).items():
        name, _, kind = key.partition("/")
        summary_path = verdict_dir / f"bundle-{name}-{kind}" / "summary.json"
        summary = (json.loads(summary_path.read_text(encoding="utf-8"))
                   if summary_path.is_file() else {})
        if kind == "clean":
            clean_runs.append((name, rep, summary))
            continue
        for arm_id in COLLECT_ARMS.get(name, ()):
            arm = results["arms"][arm_id]
            arm.update(commit=checked.get("commit"), image_digest=checked.get("image"),
                       sample_sha256=samples.get("failing"), n_files=rep.get("files"),
                       model_calls=(rep.get("model") or {}).get("calls"),
                       cost_usd=rep.get("total_cost_usd"),
                       wall_s=(rep.get("vm") or {}).get("shard_seconds"),
                       cost_per_fixed_part=rep.get("cost_per_fixed_part_usd"))
            if summary:
                _from_summary(arm, summary)
            else:
                arm["notes"] = f"no {summary_path.name} under bundle-{name}-{kind}: meshing pending"
    if clean_runs:
        clean = results["arms"][CLEAN]
        ns = [int(rep.get("files") or 0) for _, rep, _ in clean_runs]
        unchanged = [int(rep.get("files") or 0) - len((rep.get("clean") or {}).get(
            "files_with_kept_ops") or []) for _, rep, _ in clean_runs]
        broken = [(rep.get("clean") or {}).get("newly_broken") for _, rep, _ in clean_runs]
        clean.update(commit=checked.get("commit"), image_digest=checked.get("image"),
                     sample_sha256=samples.get("clean"), n_files=min(ns),
                     byte_unchanged=min(unchanged),
                     newly_broken=(max(int(b) for b in broken) if all(measured(b) for b in broken)
                                   else None),
                     arms_covered=sorted(n for n, _, _ in clean_runs),
                     basis=("collect.py clean_untouched: a part is unchanged when no operation was "
                            "kept on it (the delivered geometry is the input's); minimum over arms"))
    results["source"] = str(verdict_dir / "verdict.json")
    return results


def from_t6_rows(paths: list[Path], results: dict, *, expected: int = 120) -> dict:
    """T6's local rows (repair_corpus --json, one file per shard) -> the rules-only arm, through
    repair_corpus.corpus_summary so no metric is re-derived here."""
    rows: list[dict] = []
    for p in paths:
        rows.extend(json.loads(Path(p).read_text(encoding="utf-8")))
    spec = importlib.util.spec_from_file_location(
        "repair_corpus", REPO / "devtools/validation/repair_corpus.py")
    rc = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("repair_corpus", rc)
    spec.loader.exec_module(rc)
    summary = rc.corpus_summary(rows, "off")
    arm = results["arms"]["t6_rules_only_local"]
    _from_summary(arm, summary)
    arm.update(commit="0bf78625", image_digest=NA, n_files=len(rows), model_calls=0,
               cost_usd=NA, cost_per_fixed_part=NA,
               wall_s=sum(float(r.get("seconds") or 0) for r in rows) or None,
               notes=("macOS host, no fTetWild; tournament and memory inert (no settings rows at "
                      "0bf78625); no model configured"
                      + ("" if len(rows) == expected else
                         f"; PARTIAL: {len(rows)} of {expected} files")))
    return results


# --------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="results JSON -> crs-v3-results.tex")
    r.add_argument("--results", default=str(PENDING_JSON))
    r.add_argument("--out", default=str(DEFAULT_TEX))
    c = sub.add_parser("from-collect", help="collect.py output (+ T6 rows) -> results JSON")
    c.add_argument("--verdict-dir", default="")
    c.add_argument("--t6-rows", nargs="*", default=[])
    c.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    if args.cmd == "render":
        results = load(Path(args.results))
        src = Path(args.results).resolve()
        shown = str(src.relative_to(REPO)) if src.is_relative_to(REPO) else src.name
        Path(args.out).write_text(render_tex(results, source=shown), encoding="utf-8")
        v = compute_verdict(results)
        print(f"wrote {args.out}; verdict {v['status']}: {v['statement']}")
        return 0
    results = blank_results()
    if args.verdict_dir:
        results = from_collect(Path(args.verdict_dir), results)
    if args.t6_rows:
        results = from_t6_rows([Path(p) for p in args.t6_rows], results)
    results["generated_at"] = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
    results["verdict"] = compute_verdict(results)
    validate(results)
    Path(args.out).write_text(json.dumps(results, indent=2, sort_keys=False) + "\n",
                              encoding="utf-8")
    print(f"wrote {args.out}; verdict {results['verdict']['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
