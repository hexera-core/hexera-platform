# Responsibility: Compose the bars and axes a mesh is judged against, from the engine and the purpose together.
# Owns: criteria lookup, evaluation against measurements, and the rendered rubric the reviewer reads.
# Boundaries: it composes and evaluates criteria; it measures nothing itself.
# Collaborates with: engines/review_types.py, engines/purposes.py and agents/reviewer/.
from __future__ import annotations

from meshpipeline.engines.registry import engine_label, engine_names, get_spec
from meshpipeline.engines.review_types import Criterion


def criteria_for(engine: str) -> tuple[Criterion, ...]:
    from meshpipeline.engines.registry import spec_or_default
    return tuple(spec_or_default(engine).criteria)


def evaluate(engine: str, measurements: dict) -> list[dict]:
    rows = []
    for c in criteria_for(engine):
        rows.append({
            "key": c.key, "label": c.label, "gating": c.gating,
            "ok_when": ("empty" if c.op == "empty" else f"{c.op} {c.threshold}"),
            "measured": measurements.get(c.key),
            "passed": c.evaluate(measurements),
            "rationale": c.rationale,
            "evidence_url": c.evidence_url,
        })
    return rows


def production_grade(engine: str, measurements: dict) -> tuple[bool, dict | None]:
    for row in evaluate(engine, measurements):
        if row["gating"] and row["passed"] is False:
            return False, row
    return True, None


#: Gating criteria that CANNOT be measured after the build. `rc` and `timed_out` describe the native
#: process, which has already exited by the time a manifest is written, so they read as
#: not-evaluated there (see write_manifest). The build judge gates them before finalize runs; the
#: pre-review quality gate below must therefore skip them rather than treat their absence as a fault.
BUILD_TIME_KEYS = frozenset({"rc", "timed_out"})


def measurements_from_manifest(manifest: dict) -> dict:
    quality = manifest.get("quality") or {}
    fc = manifest.get("patch_face_counts") or {}
    roles = manifest.get("patch_types") or {}
    # wall_faces is manifest-derived, not engine-reported: it is the face total over the patches the
    # delivered mesh actually roles as `wall`.
    wall_faces = sum(fc.get(p, 0) for p, r in roles.items() if r == "wall") or None
    # `fatal` is passed through exactly as the engine reported it and is deliberately NOT defaulted
    # to []. An absent fatal list means checkMesh never ran, which must read as not-measured (and so
    # fail the gate below) rather than as a clean bill of health.
    return {**{k: v for k, v in quality.items() if k != "bounds"},
            "wall_faces": wall_faces}


def gate_declared_criteria(engine: str, manifest: dict) -> tuple[bool, str]:
    if not manifest:
        return False, ("[QUALITY] no mesh manifest to judge quality from - the mesh was not "
                       "quality-checked; run_mesh again")
    measurements = measurements_from_manifest(manifest)
    for row in evaluate(engine, measurements):
        if not row["gating"] or row["key"] in BUILD_TIME_KEYS:
            continue
        if row["passed"] is None:
            return False, (
                f"[QUALITY] {row['label']}: `{row['key']}` is missing from the quality report "
                f"(measured={row['measured']!r}) - the mesh was not quality-checked against a bar "
                f"it must clear. Re-run run_mesh so the engine reports it.")
        if row["passed"] is False:
            return False, (
                f"[QUALITY] {row['label']}: measured {row['measured']!r}, required "
                f"{row['ok_when']} - {row['rationale']} (source: {row['evidence_url']})")
    return True, ""


def render_report(rows: list[dict], *, include_advisory: bool = True) -> str:
    lines = []
    for r in rows:
        if not include_advisory and not r["gating"]:
            continue
        status = {True: "PASS", False: "FAIL", None: "n/a"}[r["passed"]]
        kind = "required" if r["gating"] else "advisory"
        lines.append(
            f"- [{status}] {r['label']} ({kind}): measured={r['measured']} "
            f"bar={r['ok_when']} - {r['rationale']} (source: {r['evidence_url']})")
    return "\n".join(lines)


# #
# REVIEW RUBRIC - the SEMANTIC layer of the QA map (the interpretation-needing
# concerns; the machine-checkable subset is the criteria above). Composed from the
# ENGINE's mesh-class axes ∪ the user-declared PURPOSE's use-case axes, deduped by
# axis name, owner stamped at composition. The shared reviewer prompt embeds the
# render of this - it holds NO engine/domain vocabulary itself.
# #
#: THE HUMAN-FEEDBACK AXIS. Composed here, at the shared engine ∪ purpose authority, and not
#: copied into five engine bundles: a human disputing a mesh is cross-cutting, and five identical
#: copies would be five things to keep in step. It appends - every engine and purpose axis still
#: applies in full, because a human's complaint adds criteria and replaces none.
HUMAN_FEEDBACK_AXIS_NAME = "human_flagged_regions"


def human_feedback_axis(phase: str):
    from meshpipeline.contracts.human_flags import PHASE_REBUILT
    from meshpipeline.engines.review_types import ReviewAxis

    rebuilt = phase == PHASE_REBUILT
    return ReviewAxis(
        name=HUMAN_FEEDBACK_AXIS_NAME,
        validation_axis="quality",
        guidance=(
            "The engineer who owns this study flagged specific regions on the mesh they were "
            + ("given, and this is the REBUILD they asked for. For every flag: navigate to it, "
               "measure the rebuilt mesh there, compare against the baseline recorded on the "
               "parent mesh and against what the builder says it changed, and record a per-flag "
               "result. A flag you cannot judge is not a flag you cleared."
               if rebuilt else
               "delivered. For every flag: navigate to it, measure what is actually there, and "
               "record whether the concern is present. This establishes the baseline the rebuild "
               "will be judged against.")),
        concern=("whether the changes the engineer asked for were actually made"
                 if rebuilt else "whether the engineer's reported problems are present"),
        failure_signals=(
            ("a flagged region still shows the reported defect",
             "a flagged region was not inspected at all",
             "the builder claims a change the mesh does not show")
            if rebuilt else
            ("a flagged region shows the reported defect",
             "a flagged region could not be inspected")),
        evidence=("render", "quality_metrics", "brief"),
    )


def compose_review_rubric(engine: str, purpose_key: str = "", user_dispute=None,
                          phase: str = "") -> tuple:
    import dataclasses

    from meshpipeline.engines.purposes import PURPOSES

    eng = (engine or "").lower()
    eng_axes = tuple(get_spec(eng).review_rubric)
    _pur = PURPOSES.get(purpose_key or "")
    pur_axes = tuple(_pur.review_axes) if _pur else ()

    out: list = []
    seen: set[str] = set()
    for ax, owner in ((a, f"engine:{eng}") for a in eng_axes):
        if ax.name in seen:
            continue
        seen.add(ax.name)
        out.append(dataclasses.replace(ax, owner=owner))
    for ax in pur_axes:
        if ax.name in seen:   # an engine axis of the same name wins (more specific to the mesh)
            continue
        seen.add(ax.name)
        out.append(dataclasses.replace(ax, owner=f"purpose:{purpose_key}"))

    # APPENDED LAST, and only when a human actually raised flags. Absent a dispute the composed
    # rubric is byte-for-byte what it always was, so no ordinary run changes shape.
    from meshpipeline.contracts.human_flags import PHASE_PARENT, expected_ordinals
    if expected_ordinals(user_dispute):
        # The PHASE is the caller's fact (it knows whether this is the parent mesh or the rebuild)
        # and is passed as an argument rather than smuggled into the user's own dispute dict, which
        # is their data and travels into the dispatch payload verbatim.
        axis = human_feedback_axis(phase or PHASE_PARENT)
        if axis.name not in seen:
            out.append(dataclasses.replace(axis, owner="human:dispute"))
    return tuple(out)


def render_review_rubric(axes) -> str:
    if not axes:
        return "(no review axes declared for this engine/purpose)"
    lines: list[str] = []
    for ax in axes:
        lines.append(f"- [{ax.validation_axis}] {ax.name} ({ax.owner}): {ax.guidance}")
        if ax.failure_signals:
            lines.append("    failure signals: " + "; ".join(ax.failure_signals))
        if ax.evidence:
            lines.append("    evidence to weigh: " + ", ".join(ax.evidence))
        # make the grounding explicit so the reviewer knows a visual_only axis is a genuine
        # eye judgment (the render), not a number it might confabulate.
        if getattr(ax, "visual_only", False):
            lines.append("    visual judgment: no single number captures this - judge from the "
                         "render, and say so if the view can't support the call")
    return "\n".join(lines)


def render_defaults_block(engine: str = "") -> str:
    engines = [engine.lower()] if engine else sorted(engine_names())
    parts: list[str] = []
    for e in engines:
        parts.append(f"[{engine_label(e)}] production-grade criteria:")
        for c in criteria_for(e):
            kind = "required" if c.gating else "advisory"
            bar = "empty" if c.op == "empty" else f"{c.op} {c.threshold}"
            parts.append(f"  - {c.label} ({kind}, bar: {c.key} {bar}) - {c.rationale} "
                         f"(source: {c.evidence_url})")
    return "\n".join(parts)
