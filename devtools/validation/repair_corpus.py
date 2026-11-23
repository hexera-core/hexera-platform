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
import asyncio
import hashlib
import importlib.util
import json
import math
import os
import random
import signal
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

# The switches this tool is here to exercise. Set before the settings module is imported, because
# these are read once at import.
os.environ.setdefault("CAD_REPAIR_ENABLED", "true")
os.environ.setdefault("CAD_REPAIR_AUTONOMOUS", "true")

# Imported at the top so that a broken import fails before any output, not after the summaries and
# before the --json write. It must follow the environment defaults above: settings read them once.
# THE ENDING VOCABULARY, imported rather than restated: the words a row reports and the words
# cad/repair/ending.py derives must be the same strings, and a tool spelling one of them itself
# would split the tally the moment either side was renamed.
from meshpipeline.cad.repair import ending as repair_ending  # noqa: E402
from meshpipeline.cad.repair.operations import OPERATIONS  # noqa: E402
from meshpipeline.cad.repair.session import MESHES_AFTER_TARGET_REMAINS  # noqa: E402


def _load_benchmark_bundle():
    """benchmark_bundle.py lives beside this file, but devtools/validation is not a package: this
    tool runs as a script (where a bare import would resolve) AND is loaded by its tests via
    spec_from_file_location (where it would not). Loading by path works in both worlds. The module
    is registered in sys.modules before exec because it defines a dataclass, and dataclasses
    resolves string field annotations through sys.modules."""
    if "benchmark_bundle" in sys.modules:
        return sys.modules["benchmark_bundle"]
    path = Path(__file__).resolve().with_name("benchmark_bundle.py")
    spec = importlib.util.spec_from_file_location("benchmark_bundle", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


benchmark_bundle = _load_benchmark_bundle()


def _load_sibling(name: str):
    """Another devtools/validation script, loaded BY PATH for the same reason benchmark_bundle is:
    this directory is not a package, so a bare import resolves when run as a script and not when
    the tests load this file through spec_from_file_location. Called lazily - `corpus_harvest` and
    `mesh_probe` are only needed by the manifest paths, and nothing here imports gmsh or OCP."""
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_SUFFIXES = {".step", ".stp"}


def policy_fingerprint() -> str:
    """gym.py's v3 policy fingerprint over the source this run executes. Loaded by path under a
    private module name, so a third-party `gym` package can never stand in for the sibling."""
    path = Path(__file__).resolve().with_name("gym.py")
    spec = importlib.util.spec_from_file_location("repair_corpus_gym_sibling", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.policy_fingerprint()


def repair_settings_of(rcfg) -> dict:
    """Every switch and cap the repair actually read, BY REFLECTION over the settings module.

    A published number is only reproducible if the bundle beside it names the permissions the
    engine ran under. Hand-listing them does not survive the module growing: CAD_REPAIR_FIX_SMALL_FACES
    took a corpus from 0 to 5 repaired files while the sealed settings - and so the settings
    digest - stayed byte-identical to the run that had it off. Reflection means a flag cannot
    change an outcome without appearing in the evidence; the hygiene test holds this to the module.

    The CAD_REPAIR_ prefix is dropped (`enabled`, `autonomous`) and CAD_INSPECT_ keeps its own
    (`inspect_timeout_s`), which is what the earlier hand-written dict called them.
    """
    settings: dict = {}
    for name, value in vars(rcfg).items():
        if not name.isupper():
            continue
        if name.startswith("CAD_REPAIR_"):
            settings[name[len("CAD_REPAIR_"):].lower()] = value
        elif name.startswith("CAD_INSPECT_"):
            settings[name[len("CAD_"):].lower()] = value
    return dict(sorted(settings.items()))


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


# THE MODEL-DIRECTED PORTION'S INSTRUMENT. Everything from here to `_one` drives the
# model-directed portion of the repair surface - NOT a second engine: it chooses from the same
# operation catalogue, runs the same session and is judged by the same caps as the deterministic
# portion. A reply cache wrapped AROUND the model seam (llm_plan is untouched), plus the per-file
# pre-pass. This tool owns the async edge, so `asyncio.run` is called here, once per file.
def _supported_keys() -> tuple[str, ...]:
    """The credential names the routing layer actually reads - one table, so this tool cannot
    disagree with the router about which providers exist."""
    from meshpipeline.settings.providers import LLM_PROVIDER_KEY_ENV
    return tuple(LLM_PROVIDER_KEY_ENV.values())


_PROVIDER_KEYS = _supported_keys()
PLANNER_SOURCE = "llm-planner"


def prompt_key(messages) -> str:
    """sha256 over the FULL prompt: every message's role and content, in order.

    The planner prompt carries the catalogue text, the defects and the story, so a change to any of
    them changes the key; a stale reply can never answer a different question.
    """
    canon = json.dumps([[str(m.get("role", "")), str(m.get("content", ""))] for m in messages],
                       ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _usable(text: str) -> bool:
    from meshpipeline.agents.repair_review._wire import parse_object
    reply = parse_object(text or "")
    return reply is not None and isinstance(reply.get("proposals"), list)


class CacheError(Exception):
    """The reply cache file cannot be used; the message names the path."""


class CachingRouter:
    """A model router that answers from a JSON file of recorded replies before asking `inner`.

    Wraps the seam (`call_repair_review_model`); a hit makes no call, a miss delegates to `inner`
    and records the raw reply. With no `inner`, a miss is a failed call (replay-only).
    """

    def __init__(self, path: Path | None, inner=None):
        self.path, self.inner = (Path(path) if path else None), inner
        self.hits = self.misses = 0
        self.table: dict[str, str] = {}
        if self.path is not None and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text() or "{}")
                if not isinstance(loaded, dict):
                    raise ValueError("not a JSON object")
                self.table = {str(k): str(v) for k, v in loaded.items()}
            except (ValueError, OSError) as exc:
                raise CacheError(f"reply cache {self.path} is unreadable: {exc}") from exc

    async def call_repair_review_model(self, messages, job_id="", name="RepairReview"):
        key = prompt_key(messages)
        if key in self.table:
            self.hits += 1
            return SimpleNamespace(ok=True, assistant_text=self.table[key])
        self.misses += 1
        if self.inner is None:
            return SimpleNamespace(ok=False, assistant_text="")
        result = await self.inner.call_repair_review_model(messages, job_id=job_id, name=name)
        text = getattr(result, "assistant_text", "") or ""
        # ONLY A USABLE REPLY IS RECORDED. The planner retries an unusable one with the identical
        # prompt, so caching it would answer the retry with the same bad reply - in memory even
        # without --reply-cache. Usable is what the planner itself reads: a JSON object carrying
        # a `proposals` list.
        if getattr(result, "ok", True) and _usable(text):
            self.table[key] = text
            if self.path is not None:
                # Not atomic on purpose: this is a scratch cache; a torn write is reported by
                # CacheError on the next load and the file is simply deleted.
                self.path.write_text(json.dumps(self.table, indent=2, sort_keys=True))
        return result

    def __getattr__(self, name):  # every other seam method goes straight to the real router
        if self.inner is None:
            raise AttributeError(name)
        return getattr(self.inner, name)


def install_planner_router(cache_path: str = "") -> tuple[CachingRouter | None, str]:
    """Install the model router the planner will use; return (cache, honest note).

    No secrets are fetched: a real model is used only when a provider key is already in the
    environment. Without one the planner has no model (or replays the cache alone when
    --reply-cache names a populated one), and the note says so.
    """
    from meshpipeline.contracts import model_inference

    inner = None
    if any(os.environ.get(k) for k in _PROVIDER_KEYS):
        from meshpipeline.adapters.model_inference import router as real
        inner = real
    cache = CachingRouter(Path(cache_path) if cache_path else None, inner)
    if inner is None and not cache.table:
        return None, ("no provider key in the environment and no replies cached: the planner has "
                      "no model, so every file reports no proposals")
    model_inference.set_model_router(cache)
    note = "model: real router" if inner is not None else "model: cache replay only"
    return cache, note


def ai_posture_without_router(rcfg) -> str:
    """A non-empty warning when an AI posture is LIVE but no model router is installed.

    `select_author` falls back to the rules author when the investigator is live with no router
    (and a live tournament gathers only its rules source), silently: no model call, no transcript,
    nothing in the row. Such a run would pass for an AI measurement. The harness installs a router
    only for `--planner`; run_arm.py installs its own, so it never hits this."""
    from meshpipeline.contracts import model_inference

    live = [name for name in ("CAD_REPAIR_INVESTIGATOR", "CAD_REPAIR_TOURNAMENT")
            if getattr(rcfg, name, "off") == "live"]
    if not live or getattr(model_inference, "_router", None) is not None:
        return ""
    return (f"{' and '.join(live)}=live but no model router is installed: the repair runs the "
            f"RULES author only (no model calls, no transcripts). This is not an AI measurement; "
            f"every row is marked investigator_status=no_router. Install a router (run_arm.py "
            f"does) or pass --planner with a provider key.")


def plan_for(entities: list[dict], *, job_id: str = "corpus"):
    """The LLM planner's plan for one file's located defects (the async edge lives here)."""
    from meshpipeline.agents.repair_review import _wire
    from meshpipeline.agents.repair_review.planner import llm_plan

    story = _wire.story_of([{"defect_code": e.get("code")} for e in entities])
    return asyncio.run(llm_plan({"entities": entities}, story=story, job_id=job_id))


def _plan_row(plan) -> dict:
    return {
        "proposals": [{"op": d["op"], "targets": list(d["targets"]),
                       "defect_code": d["defect_code"], "confidence": d["planner_confidence"],
                       "directive_id": d["directive_id"]} for d in plan.directives],
        "declines": len(plan.declines), "call_failures": len(plan.call_failures),
        "called": plan.called,
    }


def agreement(proposals: list[dict], operations: list[dict]) -> bool | None:
    """The model-directed portion's FIRST proposal == the deterministic portion's first attempted
    op and target; None if no proposal or no attempt exists to compare.

    Only the first, because the model-directed portion plans once ahead of the loop."""
    if not proposals or not operations:
        return None
    a, b = proposals[0], operations[0]
    return a["op"] == b.get("op") and sorted(a["targets"]) == sorted(b.get("targets") or [])


def planner_summary(rows: list[dict], mode: str) -> dict:
    """Aggregate the model-directed portion's evidence from the rows (pure; printer and tests
    share it).

    The `rules_only_*` / `live_*` key names are HISTORICAL and describe two passes of one repair
    surface - deterministic-only, then deterministic plus model-directed - not two engines. They
    are left as they are because sealed evidence bundles already carry them and renaming a key
    would make an old bundle and a new one incomparable."""
    out: dict = {"mode": mode}
    planned = [r for r in rows if r.get("plan") is not None]
    if mode == "shadow":
        checked = [agreement(r["plan"]["proposals"], r.get("operations") or []) for r in planned]
        checked = [c for c in checked if c is not None]
        out.update(files_planned=len(planned),
                   coverage=sum(1 for r in planned if r["plan"]["proposals"]),
                   agreed=sum(1 for c in checked if c), compared=len(checked),
                   declines=sum(r["plan"]["declines"] for r in planned),
                   call_failures=sum(r["plan"]["call_failures"] for r in planned))
    if mode == "live":
        def fixed(rs):
            total: dict[str, int] = {}
            for r in rs:
                for code, n in (r.get("repaired_defects") or {}).items():
                    total[code] = total.get(code, 0) + n
            return total
        rules = [r["rules_only"] for r in rows if r.get("rules_only")]
        out.update(rules_only_repaired=fixed(rules), live_repaired=fixed(rows),
                   rules_only_total=sum(fixed(rules).values()),
                   live_total=sum(fixed(rows).values()))
        per_op: dict[str, dict[str, int]] = {}
        for r in rows:
            for op in r.get("operations") or []:
                if op.get("source") != PLANNER_SOURCE:
                    continue
                e = per_op.setdefault(op.get("op", "?"), {"kept": 0, "rolled_back": 0,
                                                          "declined": 0, "failed": 0})
                key = op.get("status", "failed")
                if key == "rolled_back" and str(op.get("reason", "")).startswith("declined:"):
                    key = "declined"
                if key == "kept" and r.get("result_discarded"):
                    key = "rolled_back"   # the after-check threw the whole result away
                if key not in e:
                    key = "failed"
                e[key] += 1
        out["llm_ops"] = per_op
    return out


def _mesh_located(measured: dict | None) -> list[dict]:
    """The mesh_* defects a repair session still holds when the run ends, as (code, entity,
    fingerprint), read from the region run's `defects_remaining`. The `before`/`after` reports in
    the measurements come from `caps.measure`, which never runs the mesh probe, so they cannot
    carry these; the row's pre-session inspection does not either."""
    remaining = ((measured or {}).get("regions") or {}).get("defects_remaining") or []
    return [{"code": e.get("code", ""), "entity": e.get("entity", ""),
             "fingerprint": e.get("fingerprint", "")}
            for e in remaining if str(e.get("code", "")).startswith("mesh_")]


def _operation_rows(operations) -> list[dict]:
    return [{"op": op.get("name", ""), "status": op.get("status", ""),
             "reason": op.get("reason", ""), "basis": op.get("basis", ""),
             "defect_code": op.get("defect_code", ""),
             "targets": op.get("targets", []),
             **({"source": op["source"]} if op.get("source") else {})}
            for op in operations if isinstance(op, dict)]


def _tessellate_to_stl(shape, path: Path) -> Path:
    """Mesh a B-rep shape to a binary STL at `path` (the mesh-level checker reads triangles)."""
    from OCP.StlAPI import StlAPI_Writer

    from meshpipeline.cad.occ_box import mesh_to_size
    mesh_to_size(shape, 1.0 / 2500.0, 0.2)
    if not StlAPI_Writer().Write(shape, str(path)):
        raise RuntimeError("the shape could not be written to STL")
    return path


def heal_corroborated(shape) -> bool | None:
    """True when the repaired shape's triangles do not pass through each other, False when they do,
    None when the check cannot run. The B-rep analyzer cannot see a mesh-level self-intersection
    (ec_turbine-A's), so a kept heal is only believed once this agrees; a heal counts toward the
    turbine class only when this is True."""
    from meshpipeline.cad import surface_checks
    # surface_checks answers None both for "no crossing" and "could not tell", so the second is
    # ruled out here first: the render stack must import, and a tessellation that exists must hold
    # a checkable number of triangles. True is only ever returned for a check that actually ran.
    try:
        import pyvista as pv
        from vtkmodules.vtkCommonDataModel import vtkTriangle  # noqa: F401
    except Exception:  # noqa: BLE001
        return None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            stl = _tessellate_to_stl(shape, Path(tmp) / "healed.stl")
            if Path(stl).exists():
                cells = pv.read(str(stl)).n_cells
                if cells == 0 or cells > 400_000:  # surface_checks' own max_faces default
                    return None
            return surface_checks.self_intersection_report(stl) is None
    except Exception:  # noqa: BLE001 - an unrunnable check is "unknown", not "failed"
        return None


def operation_catalogue() -> list[str]:
    """The operation names this engine build carries, in declaration order."""
    return [op.name for op in OPERATIONS]


def summarise_catalogue(rows: list[dict], catalogue: list[str] | None = None) -> dict:
    """Kept / rolled back / declined / failed per operation, the catalogue operations no row
    reached, and those only ever declined (proposed, never ran).

    `catalogue` is THE RUN'S operation list, not today's. `never_exercised` is the one field here
    that is not derivable from the rows alone - it needs to know which operations existed - so
    recomputing a year-old bundle against a grown OPERATIONS would report different absences and
    make untouched evidence look edited. A sealed bundle records its own catalogue and verification
    passes it back; None means "this build's", which is right while the run is happening.
    """
    tally: dict[str, dict[str, int]] = {}
    for row in rows:
        for op in row.get("operations", []) or []:
            entry = tally.setdefault(op.get("op", "?"),
                                     {"kept": 0, "rolled_back": 0, "declined": 0, "failed": 0})
            key = op.get("status", "failed")
            if key == "rolled_back" and str(op.get("reason", "")).startswith("declined:"):
                key = "declined"
            entry[key] = entry.get(key, 0) + 1
    never = [name for name in (operation_catalogue() if catalogue is None else catalogue)
             if name not in tally]
    only_declined = [name for name, c in tally.items()
                     if c["declined"] and not (c["kept"] or c["rolled_back"] or c["failed"])]
    return {"tally": tally, "never_exercised": never, "only_declined": only_declined}


def _distribution(values: list[float]) -> dict:
    return {"count": len(values), "min": min(values), "max": max(values),
            "mean": statistics.fmean(values), "median": statistics.median(values)}


def _count_value(counts: dict, key: str) -> float | None:
    value = counts.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def cad_simplicity(rows: list[dict]) -> dict:
    """Before/after topology deltas that say whether a kept repair simplified the CAD.

    Defect yield says *what got fixed*. Simplicity says whether the written STEP became easier or
    harder downstream: fewer faces/edges is usually good; a face explosion or solid-count change is
    evidence a repair may be valid but costly.
    """
    deltas = {"face_delta": [], "edge_delta": [], "vertex_delta": [], "operations_kept": []}
    measured = 0
    more_faces = fewer_faces = solid_changed = 0
    for row in rows:
        before, after = row.get("counts_before") or {}, row.get("counts_after") or {}
        if not isinstance(before, dict) or not isinstance(after, dict):
            continue
        face_before, face_after = _count_value(before, "faces"), _count_value(after, "faces")
        edge_before, edge_after = _count_value(before, "edges"), _count_value(after, "edges")
        vertex_before = _count_value(before, "vertices")
        vertex_after = _count_value(after, "vertices")
        if face_before is None or face_after is None:
            continue
        measured += 1
        face_delta = face_after - face_before
        deltas["face_delta"].append(face_delta)
        more_faces += int(face_delta > 0)
        fewer_faces += int(face_delta < 0)
        if edge_before is not None and edge_after is not None:
            deltas["edge_delta"].append(edge_after - edge_before)
        if vertex_before is not None and vertex_after is not None:
            deltas["vertex_delta"].append(vertex_after - vertex_before)
        solids_before = _count_value(before, "solids")
        solids_after = _count_value(after, "solids")
        if solids_before is not None and solids_after is not None:
            solid_changed += int(solids_after != solids_before)
        kept = _count_value(row, "operations_kept")
        if kept is not None:
            deltas["operations_kept"].append(kept)
    return {
        "files_measured": measured,
        "files_with_more_faces": more_faces,
        "files_with_fewer_faces": fewer_faces,
        "solid_count_changed": solid_changed,
        **{key: _distribution(values) for key, values in deltas.items() if values},
    }


def settings_operation_catalogue(bundle_dir: Path, stored_summary: dict | None = None
                                 ) -> list[str] | None:
    """The operation catalogue a sealed bundle RAN WITH, so verification recomputes against the
    engine of the day rather than today's.

    Preference order, and why: the catalogue sealed into settings.json, which the manifest's
    settings digest protects; failing that - a bundle sealed before the catalogue was recorded -
    the operation universe reconstructed from the bundle's own stored catalogue block, which is
    the run's list as far as it can still be recovered. The fallback cannot police
    `never_exercised` itself, only everything around it; that is the honest limit of an older
    bundle and better than reporting valid evidence as edited.
    """
    try:
        body = json.loads((Path(bundle_dir) / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        body = {}
    sealed = body.get("operations_catalogue") if isinstance(body, dict) else None
    if isinstance(sealed, list) and all(isinstance(name, str) for name in sealed):
        return sealed
    stored = (stored_summary or {}).get("catalogue") or {}
    tally, never = stored.get("tally") or {}, stored.get("never_exercised") or []
    if tally or never:
        # ORDER MATTERS: never_exercised keeps the catalogue's declaration order, so the recovered
        # list has to put those names back in that order - the ones in the tally are filtered out
        # when never_exercised is recomputed, so where they sit cannot change the answer.
        return list(never) + sorted(set(tally) - set(never))
    return None


def settings_planner_mode(bundle_dir: Path) -> str:
    """The planner posture a sealed bundle ran under, read from its own settings.json.

    corpus_summary adds the shadow-planner block only for a shadow/live run, so a verifier that
    assumed "off" would recompute a different aggregate for an honest bundle. The settings are
    digest-checked by the loader before this matters.
    """
    try:
        body = json.loads((Path(bundle_dir) / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "off"
    mode = body.get("planner") if isinstance(body, dict) else None
    return mode if mode in ("shadow", "live") else "off"


def corpus_summary(rows: list[dict], planner_mode: str = "off",
                   catalogue: list[str] | None = None) -> dict:
    """THE one aggregate over a run's rows: the printer and the publication bundle both read this,
    so the table a human saw and the summary.json a reviewer loads can never disagree. Pure over
    the rows - and the rows themselves were credited by re-inspection, never by the repair's own
    success claim, so that invariant survives into anything built from this.

    `catalogue` is the operation list the RUN knew (see summarise_catalogue); None means this
    build's, which is what a live run wants."""
    outcomes: dict[str, int] = {}
    for row in rows:
        key = row["outcome"].split(":")[0] if row["outcome"].startswith("not_attempted") \
            else row["outcome"]
        outcomes[key] = outcomes.get(key, 0) + 1
    # THE SAME RUN IN THE PRECISE VOCABULARY, beside `outcomes` and not instead of it. `outcomes`
    # is what older bundles stored and what `load_bundle` rechecks field by field, so it is
    # untouched; this is the tally that does not say `refused: 66` for 65 parts no gate ever
    # judged. Rows written before `ending` existed are backfilled from what they do carry
    # (cad/repair/ending.backfill_ending), so a --verify of an old bundle recomputes a real
    # histogram rather than a column of `refused`.
    endings: dict[str, int] = {}
    for row in rows:
        key = repair_ending.backfill_ending(row)
        key = key.split(":")[0] if key.startswith("not_attempted") else key
        endings[key] = endings.get(key, 0) + 1
    repaired_by_code: dict[str, int] = {}
    for row in rows:
        for code, count in (row.get("repaired_defects") or {}).items():
            repaired_by_code[code] = repaired_by_code.get(code, 0) + count
    routes: dict[str, int] = {}
    for row in rows:
        if row.get("route"):
            routes[row["route"]] = routes.get(row["route"], 0) + 1
    stops: dict[str, int] = {}
    for row in rows:
        if row.get("stopped_because"):
            stops[row["stopped_because"]] = stops.get(row["stopped_because"], 0) + 1
    refusals_by_reason: dict[str, int] = {}
    for row in rows:
        for record in row.get("refusals") or []:
            key = str(record.get("reason", "?"))
            refusals_by_reason[key] = refusals_by_reason.get(key, 0) + 1
    kept_heals = [r for r in rows if "heal_corroborated" in r]
    heals = {
        "kept": len(kept_heals),
        "corroborated": sum(1 for r in kept_heals if r["heal_corroborated"] is True),
        "unknown": sum(1 for r in kept_heals if r["heal_corroborated"] is None),
        "uncorroborated_files": [r["file"] for r in kept_heals
                                 if r["heal_corroborated"] is False],
        "unknown_files": [r["file"] for r in kept_heals if r["heal_corroborated"] is None],
    }
    out = {"outcomes": outcomes, "endings": endings,
           "repaired_by_code": repaired_by_code, "routes": routes,
           "stopped_because": stops, "catalogue": summarise_catalogue(rows, catalogue),
           "refusals_by_reason": refusals_by_reason, "heals": heals,
           "cad_simplicity": cad_simplicity(rows)}
    if planner_mode != "off":
        out["planner"] = planner_summary(rows, planner_mode)
    if any("meshes_before" in r for r in rows):
        out["meshing"] = meshing_summary(rows)
    # ABSENT unless a row was scored with --meshability, so every older bundle verifies unchanged.
    if any("returned_step" in r for r in rows):
        out["meshability"] = meshability_summary(rows)
    # Only a goal-scored run has this block, so a run without --goal keeps today's summary exactly
    # - which is also what --verify recomputes off the rows.
    if any(r.get("goal_profile") for r in rows):
        out["goal"] = goal_summary(rows)
    # Same rule for the investigator: ABSENT unless a row carries a block, so --verify recomputes
    # the identical summary for a run that never asked for one.
    investigator = investigator_summary(rows)
    if investigator:
        out["investigator"] = investigator
    return out


# THE PUBLICATION BUNDLE. docs/research/public-benchmarks.md, "First Implementation Slice" items
# 1-2: a run that may be published writes an evidence bundle - per-file rows streamed as the run
# goes, and a sealed manifest carrying provenance, redistribution status, input hashes, engine
# commit, hardware class and the frozen settings. The envelope itself (layout, digests, loader
# refusals) is benchmark_bundle's; this section only fills it with what a corpus run knows.
def _hardware_class() -> str:
    import platform
    return f"{platform.system().lower()}-{platform.machine().lower()}"


def _engine_commit() -> str:
    """The engine commit, or an honest "unknown": a bundle built outside a git checkout (a bare
    image, an exported tree) is still evidence, just evidence that cannot name its commit."""
    try:
        return benchmark_bundle.current_engine_commit(Path(__file__).resolve().parents[2])
    except Exception:  # noqa: BLE001 - no git, not a repo, detached worktree: all mean "unknown"
        return "unknown"


def root_labels(roots: list[Path]) -> dict[Path, str]:
    """A distinct key prefix per corpus root: the SHORTEST TRAILING PATH that tells them apart.

    The directory's own name is not enough. `/runs/a/corpus` and `/runs/b/corpus` are two corpora
    both named `corpus`, and both holding a `part.step` would key it `corpus/part.step` - the
    second hash replacing the first, leaving the bundle unable to name every input behind its
    totals. So the label grows leftwards (`a/corpus`, `b/corpus`) until every root is unique.
    """
    if len(roots) < 2:
        return dict.fromkeys(roots, "")
    depth = 1
    while depth <= max(len(root.parts) for root in roots):
        labels = {root: "/".join(root.parts[-depth:]) for root in roots}
        if len(set(labels.values())) == len(roots):
            return {root: f"{label}/" for root, label in labels.items()}
        depth += 1
    # Unreachable for distinct absolute paths; two identical roots would land here.
    return {root: f"{'/'.join(root.parts[1:])}/" for root in roots}


def hash_corpus_inputs(roots: list[Path], files: list[Path]) -> dict[str, str]:
    """sha256 per processed input, keyed relative to the corpus directory it came from. With more
    than one corpus directory the keys carry a per-root prefix (`root_labels`), because two
    corpora may both hold a `part.step` and a merged map must not let one silently shadow the
    other. A key that would still collide is REFUSED rather than overwritten: a bundle whose
    input map is missing a file it hashed cannot answer "what was behind this number"."""
    hashes: dict[str, str] = {}
    resolved_roots = [Path(root).resolve() for root in roots]
    labels = root_labels(resolved_roots)
    for root in resolved_roots:
        members = [f.resolve() for f in files if f.resolve().is_relative_to(root)]
        for key, value in benchmark_bundle.hash_inputs(root, members).items():
            prefixed = labels[root] + key
            if prefixed in hashes and hashes[prefixed] != value:
                raise ValueError(
                    f"two corpus inputs both key to {prefixed!r} with different hashes; the "
                    f"bundle would record only one of them. Pass corpus roots that can be told "
                    f"apart by their trailing path.")
            hashes[prefixed] = value
    return hashes


def _corpus_manifest(*, track: str, corpus_name: str, provenance: str, redistribution: str,
                     input_hashes: dict[str, str], timeout_seconds: float | None,
                     random_seed: int | None, engine_commit: str | None = None,
                     hardware_class: str | None = None):
    """`engine_commit` and `hardware_class` default to THIS host at THIS commit, which is the
    truth only when the engine just ran here. A caller packaging a recorded run passes what that
    run was measured on, or "unknown" - see bundle_from_rows."""
    return benchmark_bundle.BundleManifest(
        benchmark="cad-repair", track=track, corpus_name=corpus_name,
        corpus_provenance=provenance, redistribution_status=redistribution,
        input_hashes=input_hashes,
        engine_commit=_engine_commit() if engine_commit is None else engine_commit,
        settings_digest="",  # the writer computes this at seal time from settings.json
        hardware_class=_hardware_class() if hardware_class is None else hardware_class,
        timeout_seconds=timeout_seconds, random_seed=random_seed)


def write_bundle_sample(directory: Path, entries, *, seed: int):
    """Record WHICH files an arm ran, beside its sealed bundle, as `sample.json`.

    The gym refuses to compare two arms unless both state the same sample, by sha256 of these
    bytes - the one guard that stops a 300-file run being scored against a different 300 files
    and the difference being read as a gain. Without this nothing writes it, so every real
    bundle was unscoreable.

    Name and sha256 only: a sample is a statement about WHICH parts ran, never their geometry,
    and no path is recorded, so the file is safe to keep and to publish beside results.
    Returns the path written, or None when the run drew no sample (the whole track ran).
    """
    if not entries:
        return None
    body = {"seed": int(seed),
            "files": [{"name": e["name"], "sha256": e["sha256"]}
                      for e in sorted(entries, key=lambda e: e["sha256"])]}
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "sample.json"
    path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    return path


def seal_corpus_bundle(writer, rows: list[dict], *, roots: list[Path], files: list[Path],
                       settings: dict, track: str = "unspecified", corpus_name: str = "",
                       provenance: str = "", redistribution: str = "internal",
                       planner_mode: str = "off", timeout_seconds: float | None = None,
                       random_seed: int | None = None) -> Path:
    """Seal a bundle whose rows were streamed in during the run. The summary is recomputed HERE,
    from the rows - never handed in by the caller - so a bundle can only ever carry the aggregate
    of what its own rows.jsonl says."""
    manifest = _corpus_manifest(track=track, corpus_name=corpus_name, provenance=provenance,
                                redistribution=redistribution,
                                input_hashes=hash_corpus_inputs(roots, files),
                                timeout_seconds=timeout_seconds, random_seed=random_seed)
    return writer.seal(manifest, settings=settings, summary=corpus_summary(rows, planner_mode))


def bundle_from_rows(rows_json: Path, bundle_dir: Path, *, track: str = "unspecified",
                     corpus_name: str = "", provenance: str = "",
                     redistribution: str = "internal", input_hashes: dict[str, str] | None = None,
                     settings: dict | None = None, timeout_seconds: float | None = None,
                     random_seed: int | None = None, engine_commit: str = "",
                     hardware_class: str = "") -> Path:
    """Package a previously recorded run (the --json output) without re-running any CAD.

    The summary is recomputed from the rows; any summary stored beside them is ignored, because a
    stored aggregate is a claim and the rows are the evidence. Input hashes default to empty - the
    corpus files may no longer be on disk - and a caller that still has them passes its own map.

    NO ENGINE RUNS HERE, so neither the current HEAD nor this host is a fact about these rows:
    packaging a run from commit A while sitting on commit B would name B as the tested engine,
    which is the one thing a published bundle must not get wrong. Both fields are "unknown" unless
    the caller states what the run was actually measured on.
    """
    data = json.loads(Path(rows_json).read_text())
    rows = data["rows"] if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"{rows_json} does not hold a list of rows")
    writer = benchmark_bundle.BundleWriter(Path(bundle_dir))
    for row in rows:
        writer.write_row(row)
    # The summary must be the aggregate OF THE CATALOGUE THIS BUNDLE RECORDS, or sealing and
    # verifying would disagree the moment a caller states the original run's operation list.
    sealed_settings = settings or {"rebundled_from": str(rows_json)}
    stated_catalogue = sealed_settings.get("operations_catalogue")
    manifest = _corpus_manifest(track=track, corpus_name=corpus_name, provenance=provenance,
                                redistribution=redistribution, input_hashes=input_hashes or {},
                                timeout_seconds=timeout_seconds, random_seed=random_seed,
                                engine_commit=engine_commit or "unknown",
                                hardware_class=hardware_class or "unknown")
    # THE PLANNER POSTURE COMES FROM THE SETTINGS BEING SEALED, exactly as the catalogue does, and
    # for the same reason: corpus_summary only adds the shadow-planner block for a shadow or live
    # run, so sealing with "off" while the settings say "shadow" writes a summary that `--verify`
    # - which reads the posture back out of those settings - recomputes differently and rejects.
    # That would make a recorded model-planner run impossible to package with its own settings.
    stated_planner = sealed_settings.get("planner")
    return writer.seal(
        manifest, settings=sealed_settings,
        summary=corpus_summary(
            rows,
            stated_planner if stated_planner in ("shadow", "live") else "off",
            catalogue=stated_catalogue if isinstance(stated_catalogue, list) else None))


# THE MESHING SCORE. Whether a file meshes is the outcome the repair exists for, so with
# --mesh-probe each row also says whether gmsh could mesh the input and the written file. Both
# probes use ONE mesh size (the input's OCP bounding-box diagonal), so before and after differ only
# by the repair. The keys appear only under the flag: a bundle sealed without it is unchanged.
MESH_PROBE_BUDGET_S = 120.0
_REPAIRED_OUTCOMES = ("repaired", "no_change", "changed_without_fixing")


def _diagonal_of(path: Path) -> float:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    from meshpipeline.cad.repair.brep import _read_shape

    box = Bnd_Box()
    BRepBndLib.Add_s(_read_shape(path), box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return float(((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5)


def _verdict(result) -> tuple[bool, str]:
    return bool(result.meshed), "" if result.meshed else str(
        getattr(result.failure, "kind", "") or "other")


def _score_meshing(row: dict, path: Path, scratch: Path) -> None:
    """Add the four mesh keys to `row`. The after probe runs only on a file THIS run wrote (a
    stale `-repaired.step` from an earlier run is never read): `meshes_after` is None otherwise."""
    from meshpipeline.cad.repair import mesh_probe

    try:
        diagonal = _diagonal_of(path)
    except Exception:  # noqa: BLE001 - no size, no honest probe
        row.update(meshes_before=False, mesh_kind_before="unreadable",
                   meshes_after=None, mesh_kind_after="")
        return
    if "meshes_before" not in row:  # --force-repair probes first, and the verdict is kept
        row["meshes_before"], row["mesh_kind_before"] = _verdict(
            mesh_probe.probe_file(path, diagonal_mm=diagonal, budget_s=MESH_PROBE_BUDGET_S))
    row["meshes_after"], row["mesh_kind_after"] = None, ""
    if row.get("outcome") in _REPAIRED_OUTCOMES:
        written = scratch / f"{path.stem}-repaired.step"
        if written.is_file():
            row["meshes_after"], row["mesh_kind_after"] = _verdict(
                mesh_probe.probe_file(written, diagonal_mm=diagonal,
                                      budget_s=MESH_PROBE_BUDGET_S))
    # A FILE THAT STILL WILL NOT MESH IS NOT "REPAIRED". The re-inspection credits defects
    # cleared, but the headline is parts that newly mesh from the exported STEP: a `repaired`
    # row whose written file gmsh ANSWERED and refused ends `unmeshable` (the loop's own word for
    # it, cad/repair/ending.py). `outcome` is left as the re-inspection's legacy word and
    # `loop_ending` as the loop's, so the change is additive. A timeout or a dead probe is no
    # answer and relabels nothing; with --mesh-probe off this function never runs.
    if (row.get("ending") == repair_ending.REPAIRED and row["meshes_after"] is False
            and row["mesh_kind_after"] not in (mesh_probe.TIMEOUT, mesh_probe.PROBE_ERROR)):
        row["ending"] = repair_ending.UNMESHABLE
    # AND THE OTHER DIRECTION: A PART THAT NEWLY MESHES AFTER KEPT OPERATIONS IS "REPAIRED"
    # (eval wave 6, bfa7c9a3: every such part ended no_change, unmeshable or escalated). The
    # re-inspection is geometry-only, so a mesh-only cure clears no located defect and `outcome`
    # says no_change; and the loop's `unmeshable`/`escalated` come from its in-session scan or
    # another region, not from the file delivered. The written STEP meshing where the input
    # definitively did not is the truth. `meshes_after` True is always a real mesh (a timeout is
    # False); the before verdict must be a real refusal too, not a timeout, a dead probe or an
    # unreadable input. `outcome`, `loop_ending` and `stopped_because` are left as they were.
    kept = int(row.get("operations_kept", 0) or 0)
    if (kept > 0 and row["meshes_after"] is True and row.get("meshes_before") is False
            and row.get("mesh_kind_before") not in (mesh_probe.TIMEOUT, mesh_probe.PROBE_ERROR,
                                                     "unreadable")):
        row["ending"] = repair_ending.ending_for(
            kept=kept, attempted=int(row.get("operations_attempted", 0) or 0),
            stopped_because=str(row.get("stopped_because", "")), newly_meshes=True)


#: The hard ceiling on one delivery-settings tolerant pass (`_tolerant_delivery`), in seconds.
TOLERANT_DELIVERY_BUDGET_S = 300.0


def _tolerant_delivery(row: dict, path: Path, scratch: Path, *, probe=None) -> None:
    """THE TOLERANT ROUTE, SCORED APART. For a file the repair did not leave gmsh-meshing, and
    whose tier-2b reading said the tolerant rung would eat it, run fTetWild at the production
    rung's envelope and iterations (`tolerant_probe.MODE_DELIVERY`, plan-free - see
    `DELIVERY_SETTINGS`) on THE FILE THAT WOULD BE DELIVERED: the repaired STEP this run wrote,
    else the upload. In a fresh subprocess with a hard ceiling (`tolerant_probe`'s own reaper).

    RECORDED AS ITS OWN COLUMN, NEVER AS A REPAIR: `tolerant_delivery` holds what happened and
    `delivered_via_tolerant_mesher` whether it meshed. Neither touches `outcome`, `meshes_after`
    or `newly_meshed` - a part the tolerant mesher eats was not repaired, it was meshed another way.
    `probe` is the test seam (`tolerant_probe.tolerant_probe`'s signature)."""
    from meshpipeline.cad.repair import tolerant_probe as tp

    if row.get("tolerant_route") != tp.EATS:
        return
    repaired = row.get("outcome") in _REPAIRED_OUTCOMES
    if row.get("meshes_after") is True or (row.get("meshes_before") is True
                                           and row.get("meshes_after") is None):
        return  # gmsh meshes what would be delivered: the tolerant route is not needed
    written = scratch / f"{path.stem}-repaired.step"
    delivered = written if repaired and written.is_file() else path
    try:
        diagonal = _diagonal_of(delivered)
    except Exception as exc:  # noqa: BLE001 - recorded, never raised
        row["tolerant_delivery"] = {"attempted": False, "meshes": False, "elements": 0,
                                    "seconds": 0.0, "failure": f"unreadable: {exc}"[:200],
                                    "file": "repaired" if delivered == written else "upload",
                                    "settings": tp.DELIVERY_SETTINGS}
        row["delivered_via_tolerant_mesher"] = False
        return
    result = (probe or tp.tolerant_probe)(delivered, diagonal_mm=diagonal,
                                          budget_s=TOLERANT_DELIVERY_BUDGET_S,
                                          mode=tp.MODE_DELIVERY)
    meshes = result.verdict == tp.EATS
    row["tolerant_delivery"] = {
        "attempted": result.verdict != tp.UNAVAILABLE,
        "meshes": meshes, "elements": int(result.tets), "seconds": round(float(result.seconds), 2),
        "failure": "" if meshes else (result.message or result.verdict)[:200],
        "verdict": result.verdict, "stopped": bool(result.stopped),
        "file": "repaired" if delivered == written else "upload",
        "settings": tp.DELIVERY_SETTINGS}
    row["delivered_via_tolerant_mesher"] = meshes


# THE MESHABILITY REPORT (--meshability, owner goal 2026-11-21: CRS always returns a STEP, and it
# should mesh). Scored on THE RETURNED STEP, whatever the repair's outcome word says: the file the
# customer receives is the one judged. Three gmsh verdicts, labelled apart and never summed:
#   meshes_after_default       - the headline: cad/repair/mesh_probe (MeshSizeMax = diagonal / 40),
#                                the probe every wave since wave 1 was scored on;
#   meshes_after_gmsh_default  - gmsh with no size setting at all (what a customer gets out of
#                                the box), with its own before-verdict on the upload;
#   meshes_after_hints         - the repair's crs-mesh-hints/1 sidecar applied by LANE G'S code
#                                (cad/repair/mesh_hints via mesh_probe.probe_file(hints=)), at the
#                                sidecar's own diagonal/40 - the one implementation, so this verdict
#                                is the one lane G measured.
#                                Hints only ever ADD to a returned STEP; with no STEP there is no
#                                hinted verdict.
# A readable file with no returned STEP is a BUG, counted as `no_returned_step`.
PASS_KEYS = ("meshability_pass", "delivery_source")


def _pass_rows(measured: dict) -> dict:
    """The meshability pass's own records (lane H, PR #524 on), copied verbatim when present."""
    return {k: measured[k] for k in PASS_KEYS if k in measured}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _kept_key(op: dict) -> tuple:
    targets = op.get("targets") or [op.get("fingerprint")]
    return (op.get("op"), str(targets[0]) if targets else "")


def _untouched(row: dict) -> bool:
    """The harness never called the repair for this file (triage routed it on as uploaded)."""
    outcome = str(row.get("outcome") or "")
    return outcome == "nothing_to_repair" or outcome.startswith("not_attempted:")


def meshed_by(row: dict) -> dict | None:
    """WHICH OPERATION MADE THE RETURNED STEP MESH, for a file that did not mesh before and does
    now at the headline settings. The pass is credited first: the first KEPT pass application
    whose own after-probe meshes. Then the AI loop's kept operations (row.operations, minus the
    pass's): one op is credited alone, several as an ordered combination. A file the pass flipped
    with no single op's probe saying so is `pass_combo`. None when there is nothing to credit."""
    if row.get("meshes_before") is not False or row.get("meshes_after_default") is not True:
        return None
    passed = [r for r in (row.get("meshability_pass") or []) if isinstance(r, dict)
              and str(r.get("verdict", "")) == "kept"]
    for rec in passed:
        if (rec.get("probe_after") or {}).get("meshes") is True:
            return {"by": "pass", "op": rec.get("op"), "rung": rec.get("rung")}
    pass_keys = {(r.get("op"), str(r.get("fingerprint") or "")) for r in passed}
    loop = [op for op in (row.get("operations") or []) if isinstance(op, dict)
            and op.get("status") == "kept" and _kept_key(op) not in pass_keys]
    if loop:
        names = [str(op.get("op")) for op in loop]
        return ({"by": "loop", "op": names[0]} if len(names) == 1
                else {"by": "loop_combo", "ops": names})
    if passed:
        return {"by": "pass_combo", "ops": [str(r.get("op")) for r in passed]}
    return {"by": "unattributed"}


HINTS_SUFFIX = ".mesh-hints.json"


def _mesh_hints():
    """Lane G's hints module (cad/repair/mesh_hints.py: the ONE implementation of reading a
    crs-mesh-hints/1 sidecar and applying it in gmsh), or None on a build without it."""
    try:
        from meshpipeline.cad.repair import mesh_hints
    except ImportError:
        return None
    return mesh_hints


def _count(value) -> int:
    """crs-mesh-hints/1 records `unplaced_defects` as a COUNT (an int); tolerate a list too, so a
    scoring read never takes down the shard it runs in."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return len(value) if isinstance(value, (list, tuple)) else 0


def _hinted(written: Path, *, hints_mod, hinted_probe) -> dict:
    """The hinted verdict on the returned STEP, read and applied by lane G's code only.
    {"status": none|no_consumer|unreadable|stale|empty|probed, ...}."""
    sidecar = Path(f"{written}{HINTS_SUFFIX}")
    if not sidecar.is_file():
        return {"status": "none"}
    if hints_mod is None:
        return {"status": "no_consumer"}
    try:
        doc = hints_mod.read_hints(sidecar)
    except (OSError, ValueError) as exc:
        return {"status": "unreadable", "note": str(exc)[:200]}
    if (doc.get("step") or {}).get("sha256") != _sha256_file(written):
        return {"status": "stale", "note": "sidecar is for different STEP bytes"}
    faces = doc.get("faces") or []
    meta = {"attempts": doc.get("attempts"), "hint_status": doc.get("status"),
            "faces": len(faces), "unplaced_defects": _count(doc.get("unplaced_defects"))}
    if not faces:
        return {"status": "empty", **meta}
    diagonal = float(doc.get("diagonal_mm") or 0.0)
    if diagonal <= 0.0:
        return {"status": "unreadable", "note": "sidecar has no diagonal_mm", **meta}
    result = hinted_probe(written, diagonal_mm=diagonal, hints=faces, quality=True,
                          budget_s=MESH_PROBE_BUDGET_S)
    meshed, kind = _verdict(result)
    return {"status": "probed", "meshed": meshed, "kind": kind,
            "quality": dict(getattr(result, "quality", None) or {}),
            "applied": int(getattr(result, "hints_applied", 0) or 0),
            "unmapped": len(getattr(result, "hints_unmapped", ()) or ()), **meta}


#: "Use lane G's module if this build has it" - distinct from an explicit None ("no consumer").
_BUILD_DEFAULT = object()


def _score_returned(row: dict, path: Path, scratch: Path, *, since: float,
                    extra=None, default_probe=None, inspect=None, hints_mod=_BUILD_DEFAULT,
                    hinted_probe=None) -> None:
    """Add the meshability keys to `row` (see the block comment above). `since` is the wall time
    the file's run started: a `-repaired.step` older than it is a stale file, not a return.
    `extra`, `default_probe`, `inspect`, `hints_mod` and `hinted_probe` are test seams
    (meshability_probe.probe, mesh_probe.probe_file, _inspect, cad/repair/mesh_hints,
    mesh_probe.probe_file with hints=)."""
    if row.get("outcome") in ("unreadable", CRASHED, TIMEOUT):
        return
    if extra is None:
        extra = _load_sibling("meshability_probe").probe
    if hints_mod is _BUILD_DEFAULT:
        hints_mod = _mesh_hints()
    if hinted_probe is None:
        from meshpipeline.cad.repair import mesh_probe as _mp
        hinted_probe = _mp.probe_file
    if default_probe is None:
        from meshpipeline.cad.repair import mesh_probe
        default_probe = mesh_probe.probe_file
    inspect = inspect or _inspect
    try:
        diagonal = _diagonal_of(path)
    except Exception:  # noqa: BLE001 - the before-probe already recorded it as unreadable
        return
    before_gd = extra(path, diagonal_mm=diagonal, mode="gmsh_default",
                      budget_s=MESH_PROBE_BUDGET_S)
    row["meshes_before_gmsh_default"] = bool(before_gd.get("meshed"))
    written = scratch / f"{path.stem}-repaired.step"
    present = written.is_file() and written.stat().st_mtime >= since - 1.0
    if not present and _untouched(row):
        # CRS WAS NEVER ASKED: the route sent the file on as uploaded (nothing to repair, or a
        # mesh-as-is / manual route), so what the customer receives IS the upload. Judged as such,
        # labelled `untouched` - not a missing return, and not a passthrough the repair chose.
        written, present = path, True
    if not present:
        row.update(returned_step={"present": False}, meshes_after_default=None,
                   mesh_kind_after_default="no_returned_step", meshes_after_gmsh_default=None,
                   meshes_after_hints=None, valid_to_invalid=None)
        return
    sha = _sha256_file(written)
    source = ("untouched" if written == path else row.get("delivery_source") or (
        "passthrough" if sha == _sha256_file(path) else "repaired"))
    try:
        valid = (row.get("valid_before") if source == "untouched" else
                 row["valid_after"] if "valid_after" in row else inspect(written)["valid"])
    except Exception:  # noqa: BLE001 - an unreadable return is invalid
        valid = False
    hinted = _hinted(written, hints_mod=hints_mod, hinted_probe=hinted_probe)
    row["returned_step"] = {"present": True, "source": source, "sha256": sha,
                            "valid": None if valid is None else bool(valid),
                            "hints": "ok" if hinted["status"] == "probed" else hinted["status"]}
    if source == "untouched" and "meshes_before" in row:
        # The returned file IS the upload: its before-verdicts are its after-verdicts.
        ok, kind = bool(row["meshes_before"]), str(row.get("mesh_kind_before") or "")
    elif row.get("meshes_after") is not None and row.get("outcome") in _REPAIRED_OUTCOMES:
        ok, kind = bool(row["meshes_after"]), str(row.get("mesh_kind_after") or "")
    else:
        ok, kind = _verdict(default_probe(written, diagonal_mm=diagonal,
                                          budget_s=MESH_PROBE_BUDGET_S))
    row["meshes_after_default"], row["mesh_kind_after_default"] = ok, kind
    gd = (before_gd if source == "untouched" else
          extra(written, diagonal_mm=diagonal, mode="gmsh_default", budget_s=MESH_PROBE_BUDGET_S))
    row["meshes_after_gmsh_default"] = bool(gd.get("meshed"))
    if hinted["status"] == "probed":
        quality = {**hinted["quality"], "attempts": hinted.get("attempts"),
                   "hint_status": hinted.get("hint_status"), "hints_applied": hinted["applied"],
                   "hints_unmapped": hinted["unmapped"], "hint_faces": hinted["faces"]}
        # AN INVERTED OR DEGENERATE MESH IS NOT A MESH: min SICN <= 0 fails the hinted verdict, and
        # the reason says so. Size is reported, never judged here (summary: over_element_cap).
        degenerate = hinted["meshed"] and (quality.get("min_sicn") is not None
                                           and float(quality["min_sicn"]) <= 0.0)
        row["meshes_after_hints"] = bool(hinted["meshed"]) and not degenerate
        row["mesh_kind_after_hints"] = "degenerate" if degenerate else hinted["kind"]
        row["mesh_quality_after_hints"] = quality
    else:
        row["meshes_after_hints"] = None
        if hinted["status"] != "none":
            row["mesh_kind_after_hints"] = hinted["status"]
    row["valid_to_invalid"] = bool(row.get("valid_before") is True and valid is False)
    credit = meshed_by(row)
    if credit is not None:
        row["meshed_by"] = credit


#: THE QUALITY-FLOOR SENSITIVITY (owner decision pending, LEAD 2026-11-21): for each min-SICN floor,
#: how many files would still count as "meshes with the hints". Report-only - the headline
#: `meshes_with_hints` applies no floor beyond SICN > 0 (no inverted element).
SICN_FLOORS = (0.0, 0.001, 0.01, 0.05)


def _with_hints_at_floor(scored: list[dict], floor: float, *, newly: bool) -> int:
    """Files meshing at default settings, plus files meshing ONLY with the hints whose hinted
    mesh's min SICN is above `floor` (a hinted mesh with no SICN reading does not count)."""
    n = 0
    for r in scored:
        if newly and r.get("meshes_before"):
            continue
        if r.get("meshes_after_default") is True:
            n += 1
        elif r.get("meshes_after_hints") is True:
            sicn = (r.get("mesh_quality_after_hints") or {}).get("min_sicn")
            if sicn is not None and float(sicn) > floor:
                n += 1
    return n


#: Above this many volume elements a hinted mesh is reported as enormous (`hints_over_element_cap`).
#: The same ceiling the oracle puts on a tier-2 probe (cad/repair/oracle.TIER2_ELEMENT_CAP).
HINTS_ELEMENT_CAP = 1_000_000


def _hints_quality(scored: list[dict]) -> dict:
    """Over the files that mesh ONLY with the hints: the worst min SICN, the smallest edge, the
    largest element count and the most attempts - the numbers a reader needs before trusting it."""
    q = [r.get("mesh_quality_after_hints") or {} for r in scored
         if r.get("meshes_after_hints") is True and r.get("meshes_after_default") is not True]

    def pick(key: str, fn):
        values = [x[key] for x in q if x.get(key) is not None]
        return fn(values) if values else None

    return {"files": len(q), "worst_min_sicn": pick("min_sicn", min),
            "worst_min_gamma": pick("min_gamma", min), "smallest_edge_mm": pick("min_edge_mm", min),
            "most_elements": pick("elements", max), "most_attempts": pick("attempts", max)}



#: The row key that says why a readable file carries no meshability score. A file is either scored
#: (`returned_step` present) or says why not here; collect refuses a wave where neither holds.
SCORE_ERROR = "meshability_score_error"


def _scorable(row: dict) -> bool:
    """A file the meshability report must account for: anything that was read and did not crash
    or time out (scoring runs in the child, which died with the file)."""
    return row.get("outcome") not in ("unreadable", CRASHED, TIMEOUT)


def score_or_mark(row: dict, path: Path, scratch: Path, *, since: float,
                  score=None) -> None:
    """Score `row` (`_score_returned`; `score` is a test seam). A scoring exception, or a scoring
    run that returns without a `returned_step` (an early exit such as an unreadable diagonal),
    becomes an explicit SCORE_ERROR on the row instead of a file that silently drops out of every
    count. Wave 11210343 lost three whole shards to one sidecar field read as the wrong type."""
    score = score or _score_returned
    try:
        score(row, path, scratch, since=since)
    except Exception as exc:  # noqa: BLE001 - scoring must never abort the shard
        row[SCORE_ERROR] = f"{type(exc).__name__}: {exc}"[:300]
        return
    if _scorable(row) and "returned_step" not in row and SCORE_ERROR not in row:
        row[SCORE_ERROR] = "not scored: scoring returned without a returned_step"


def unscored_rows(rows: list[dict]) -> list[str]:
    """Readable rows of a meshability run with NEITHER a score NOR a SCORE_ERROR: collect refuses
    a wave that has any, because such a file would vanish from every count unnoticed."""
    return [str(r.get("file")) for r in rows
            if _scorable(r) and "returned_step" not in r and SCORE_ERROR not in r]

def meshability_summary(rows: list[dict]) -> dict:
    """The meshability report's counts. {} for rows scored without --meshability. Every count is
    over the files whose row was scored (`files`), crashed and unreadable files reported apart."""
    scored = [r for r in rows if "returned_step" in r]
    if not scored:
        return {}
    got = [r for r in scored if r["returned_step"].get("present")]

    def meshes(r: dict, key: str) -> bool:
        return r.get(key) is True

    def with_hints(r: dict) -> bool:
        return meshes(r, "meshes_after_default") or meshes(r, "meshes_after_hints")

    credit: dict[str, int] = {}
    for r in scored:
        c = r.get("meshed_by")
        if c:
            name = f"{c['by']}:{c.get('op') or '+'.join(c.get('ops') or [])}"
            credit[name] = credit.get(name, 0) + 1
    return {
        "files": len(scored),
        "crashed": sum(1 for r in rows if r.get("outcome") == CRASHED),
        "timeouts": sum(1 for r in rows if r.get("outcome") == TIMEOUT),
        # COUNTED APART, never silently missing: a readable file whose scoring failed.
        "score_errors": sum(1 for r in rows if SCORE_ERROR in r),
        "unreadable": sum(1 for r in rows if r.get("outcome") == "unreadable"),
        "steps_returned": len(got),
        "no_returned_step": len(scored) - len(got),
        "source_repaired": sum(1 for r in got if r["returned_step"].get("source") == "repaired"),
        "source_passthrough": sum(1 for r in got
                                  if r["returned_step"].get("source") == "passthrough"),
        "source_untouched": sum(1 for r in got
                                if r["returned_step"].get("source") == "untouched"),
        "meshed_before": sum(1 for r in scored if r.get("meshes_before")),
        "meshes_default": sum(1 for r in scored if meshes(r, "meshes_after_default")),
        "newly_meshes_default": sum(1 for r in scored if meshes(r, "meshes_after_default")
                                    and not r.get("meshes_before")),
        "hints_present": sum(1 for r in got if r["returned_step"].get("hints") == "ok"),
        "meshes_with_hints": sum(1 for r in scored if with_hints(r)),
        "newly_meshes_with_hints": sum(1 for r in scored if with_hints(r)
                                       and not r.get("meshes_before")),
        "meshes_only_with_hints": sum(1 for r in scored if meshes(r, "meshes_after_hints")
                                      and not meshes(r, "meshes_after_default")),
        "hints_degenerate": sum(1 for r in scored
                                if r.get("mesh_kind_after_hints") == "degenerate"),
        "hints_over_element_cap": sum(
            1 for r in scored if meshes(r, "meshes_after_hints")
            and int((r.get("mesh_quality_after_hints") or {}).get("elements") or 0)
            > HINTS_ELEMENT_CAP),
        "hints_quality": _hints_quality(scored),
        "meshes_with_hints_by_sicn_floor": {
            str(f): _with_hints_at_floor(scored, f, newly=False) for f in SICN_FLOORS},
        "newly_meshes_with_hints_by_sicn_floor": {
            str(f): _with_hints_at_floor(scored, f, newly=True) for f in SICN_FLOORS},
        "meshed_before_gmsh_default": sum(1 for r in scored
                                          if r.get("meshes_before_gmsh_default")),
        "meshes_gmsh_default": sum(1 for r in scored if meshes(r, "meshes_after_gmsh_default")),
        "newly_meshes_gmsh_default": sum(1 for r in scored
                                         if meshes(r, "meshes_after_gmsh_default")
                                         and not r.get("meshes_before_gmsh_default")),
        "newly_broken_default": sum(1 for r in scored if r.get("meshes_before")
                                    and not meshes(r, "meshes_after_default")),
        "valid_to_invalid": sum(1 for r in scored if r.get("valid_to_invalid")),
        "meshed_by": dict(sorted(credit.items())),
    }


def meshing_summary(rows: list[dict]) -> dict:
    """Aggregate the mesh keys. A file repair did not write keeps its before verdict as its after
    verdict, so `meshed_after` is the count of files that mesh once repair has had its go."""
    scored = [r for r in rows if "meshes_before" in r]
    after = [r["meshes_before"] if r.get("meshes_after") is None else r["meshes_after"]
             for r in scored]
    still: dict[str, int] = {}
    for row, ok in zip(scored, after, strict=True):
        if not ok:
            kind = (row.get("mesh_kind_before") if row.get("meshes_after") is None
                    else row.get("mesh_kind_after")) or "other"
            still[kind] = still.get(kind, 0) + 1
    forced = sum(1 for r in scored if r.get("forced_repair"))
    # A CRASHED FILE (the isolated child died) is its own count. It enters newly_broken only when
    # the input meshed before the crash (its row carries meshes_before=True, meshes_after=False);
    # one that did not mesh before has meshes_after=None and is neither broken nor meshed. The key
    # appears only when a crash happened, so a run without one summarises byte-identically.
    crashed = sum(1 for r in rows if r.get("outcome") == CRASHED)
    timeouts = sum(1 for r in rows if r.get("outcome") == TIMEOUT)  # killed at --file-timeout-s
    # THE TOLERANT ROUTE IS ITS OWN COUNT, present only when a row carries the reading, and never
    # added to meshed_after or newly_meshed.
    tolerant = [r for r in rows if "tolerant_delivery" in r]
    return {
        **({"forced_repairs": forced} if forced else {}),
        **({"crashed": crashed} if crashed else {}),
        **({"timeouts": timeouts} if timeouts else {}),
        **({"tolerant_delivery_attempted": sum(
                1 for r in tolerant if r["tolerant_delivery"].get("attempted")),
            "delivered_via_tolerant_mesher": sum(
                1 for r in tolerant if r.get("delivered_via_tolerant_mesher") is True)}
           if tolerant else {}),
        "files": len(scored),
        "meshed_before": sum(1 for r in scored if r["meshes_before"]),
        "meshed_after": sum(1 for ok in after if ok),
        "newly_meshed": sum(1 for r, ok in zip(scored, after, strict=True)
                            if ok and not r["meshes_before"]),
        "newly_broken": sum(1 for r, ok in zip(scored, after, strict=True)
                            if r["meshes_before"] and not ok),
        "still_failing_by_kind": dict(sorted(still.items())),
    }


# THE GOAL PROFILE'S COLUMNS. With `--goal` the repair is told what it is finishing for, and the
# finishing verdict it records (cad/repair/goal.py, measurements["goal"]) is flattened into the row
# here. Flattened rather than nested because these columns are what a reviewer sorts a corpus by:
# "which files end kernel-clean but still refuse tier 2" is a question about columns.
#
# WITHOUT --goal NOTHING CHANGES. No kwarg reaches repair_step_file, no key reaches the row, and
# `goal` is absent from the summary, so a run's rows stay byte-identical to today's.
GOAL_PROFILES = ("standalone", "benchmark")


def goal_name(goal) -> str:
    """The profile's wire name, whether the caller handed in a RepairGoal or the CLI's string."""
    return str(getattr(goal, "value", goal) or "")


def goal_row(verdict: dict | None, goal) -> dict:
    """The row's goal columns, from one finishing verdict.

    A verdict is absent for a file whose repair never ran (`nothing_to_repair`, `not_attempted:*`,
    `unreadable`): that is `not_run`, NOT a tier-2 refusal, and `tier2_meshes` is None. The two
    must stay apart - counting a file nobody meshed as a file that would not mesh is how a corpus
    run invents a failure. A verdict goal.py could not produce carries its error type instead.
    """
    profile = goal_name(goal)
    if not verdict:
        return {"goal_profile": profile, "goal_verdict": "not_run", "tier2_meshes": None}
    out: dict = {"goal_profile": str(verdict.get("profile") or profile)}
    if verdict.get("error"):
        out.update(goal_verdict="error", goal_error=str(verdict["error"]), tier2_meshes=None)
        return out
    mesh = dict(verdict.get("meshability") or {})
    routing = verdict.get("routing")
    out.update(
        kernel_clean=verdict.get("kernel_clean"),
        tier2_meshes=mesh.get("meshes"),
        tier2_kind=str(mesh.get("kind") or ""),
        tier2_score=verdict.get("score"),
        grind_tier=str((verdict.get("grind") or {}).get("tier") or ""),
    )
    # ABSENT, NOT EMPTY, when tier 2b is off: the column's presence is what says the flag was live.
    if routing:
        out["tolerant_route"] = str(routing.get("verdict") or "")
    # What the verdict COST, summed by the runner from the probe seconds each tier recorded: the
    # price of asking, which is what decides whether the ladder is affordable per file.
    out["goal_verdict_seconds"] = round(float(mesh.get("seconds") or 0.0)
                                        + float((routing or {}).get("seconds") or 0.0), 2)
    return out


def goal_summary(rows: list[dict]) -> dict:
    """Aggregate the goal columns over the rows that carry one. Pure.

    `tier2_meshes`/`tier2_refuses`/`tier2_no_answer`/`not_run` partition the scored files, and
    `score_mean` averages the files that got an answer - a timeout scores None and is left out of
    the mean rather than counted as a zero.
    """
    scored = [r for r in rows if r.get("goal_profile")]
    profiles = sorted({str(r["goal_profile"]) for r in scored})
    not_run = [r for r in scored if r.get("goal_verdict") == "not_run"]
    answered = [r for r in scored if r.get("goal_verdict") != "not_run"]
    values = [r["tier2_score"] for r in answered if r.get("tier2_score") is not None]
    grind: dict[str, int] = {}
    for row in scored:
        tier = str(row.get("grind_tier") or "")
        if tier:
            grind[tier] = grind.get(tier, 0) + 1
    routing: dict[str, int] = {}
    for row in scored:
        if "tolerant_route" in row:
            key = str(row["tolerant_route"] or "")
            routing[key] = routing.get(key, 0) + 1
    out = {
        "files": len(scored),
        "profile": ",".join(profiles),
        "kernel_clean": sum(1 for r in scored if r.get("kernel_clean") is True),
        "tier2_meshes": sum(1 for r in answered if r.get("tier2_meshes") is True),
        "tier2_refuses": sum(1 for r in answered if r.get("tier2_meshes") is False),
        "tier2_no_answer": sum(1 for r in answered if r.get("tier2_meshes") is None),
        "not_run": len(not_run),
        "score_mean": round(statistics.fmean(values), 4) if values else None,
        "grind_by_tier": dict(sorted(grind.items())),
    }
    if routing:
        out["tolerant_routing"] = dict(sorted(routing.items()))
    return out


# THE STRATIFIED SAMPLE. The committed manifest is 9,956 real failures, and a 300-file arm drawn
# off the top of it would be whatever the harvest happened to download first. The strata are the
# two things that decide what a repair has to do: the failure family and whether the part is one
# solid or many. Pure and deterministic - a seed is the whole provenance of a sample, and the
# entries carry manifest fields only (name, sha256, counts), never geometry.
def stratum_of(entry: dict) -> tuple[str, str]:
    """`(family, solids)` for one manifest entry. `solids` is single/multi/unknown; a row with no
    solid count, or zero, is unknown rather than guessed."""
    corpus_harvest = _load_sibling("corpus_harvest")
    solids = entry.get("solids")
    kind = "single" if solids == 1 else "multi" if solids else "unknown"
    return corpus_harvest.legacy_family(str(entry.get("gmsh_error") or "")), kind


def _quotas(sizes: dict[tuple[str, str], int], n: int) -> dict[tuple[str, str], int]:
    """`n` places shared out by share, with a floor of one per stratum so a rare family cannot
    round away, then trimmed largest-first (and topped up largest-first) to exactly `n`."""
    total = sum(sizes.values())
    quotas = {key: min(size, max(1, round(n * size / total))) for key, size in sizes.items()}
    order = sorted(sizes, key=lambda k: (-sizes[k], k))
    while sum(quotas.values()) > n:
        movable = [k for k in order if quotas[k] > 1]
        if not movable:  # every stratum is at its floor of one: the floors alone exceed n
            break
        for key in movable:
            quotas[key] -= 1
            if sum(quotas.values()) == n:
                break
    while sum(quotas.values()) < n:
        movable = [k for k in order if quotas[k] < sizes[k]]
        if not movable:  # every stratum is exhausted: the manifest holds fewer than n entries
            break
        for key in movable:
            quotas[key] += 1
            if sum(quotas.values()) == n:
                break
    return quotas


def stratified_sample(manifest: dict, n: int, *, seed: int) -> list[dict]:
    """`n` manifest entries, spread over the (family, solids) strata, sorted by sha256.

    Deterministic in `seed` alone: the entries are sorted by sha256 before the draw, so a manifest
    whose file order changed - a re-harvest appends - still yields the same sample.
    """
    strata: dict[tuple[str, str], list[dict]] = {}
    for entry in manifest.get("files") or []:
        strata.setdefault(stratum_of(entry), []).append(entry)
    for entries in strata.values():
        entries.sort(key=lambda e: str(e.get("sha256", "")))
    quotas = _quotas({key: len(v) for key, v in strata.items()}, n)
    drawn: list[dict] = []
    for key in sorted(strata):
        entries = strata[key]
        take = quotas.get(key, 0)
        drawn.extend(entries if take >= len(entries)
                     else random.Random(seed).sample(entries, take))
    return sorted(drawn, key=lambda e: str(e.get("sha256", "")))


def shard_of(sample: list[dict], spec: str) -> list[dict]:
    """`I/K` of a sample: the sorted entries sliced `[I-1::K]`, so the K shards of one sample
    partition it exactly and each shard holds the same files whoever runs it."""
    index, _, count = spec.partition("/")
    i, k = int(index), int(count or 0)
    if k < 1 or not 1 <= i <= k:
        raise ValueError(f"--shard wants I/K with 1 <= I <= K, not {spec!r}")
    return sorted(sample, key=lambda e: str(e.get("sha256", "")))[i - 1::k]


def excluded_sha256s(paths: list[Path]) -> set[str]:
    """Every sha256 named by these sample/manifest JSON files. A held-out sample is stated by hash
    rather than by name, because a corpus file's name is whatever the root it was found under
    calls it."""
    out: set[str] = set()
    for path in paths:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = loaded.get("files") if isinstance(loaded, dict) else loaded
        for entry in entries or []:
            digest = entry.get("sha256") if isinstance(entry, dict) else entry
            if digest:
                out.add(str(digest))
    return out


def without_excluded(manifest: dict, excluded: set[str]) -> dict:
    """The manifest minus those hashes. A new dict: the caller's manifest is left alone."""
    return {**manifest, "files": [f for f in manifest.get("files") or []
                                  if str(f.get("sha256", "")) not in excluded]}


def repair_goal(name: str):
    """The `RepairGoal` the repair is handed, or None for "" - which is today's behaviour.

    Resolved by name off the contracts module, and only when a profile was asked for: a run
    without --goal imports nothing new and passes no kwarg.
    """
    if not name:
        return None
    from meshpipeline.cad.repair import contracts

    goal = getattr(contracts, "RepairGoal", None)
    if goal is None:
        raise ValueError("this build has no RepairGoal, so --goal has nothing to hand the repair")
    return goal(name)


def manifest_files(path: Path, roots: list[Path], *, sample: int = 0, seed: int = 0,
                   shard: str = "", exclude: list[Path] | None = None
                   ) -> tuple[list[Path], list[dict]]:
    """The track manifest's files as they exist on this host: excluded, sampled, sharded, then
    resolved by sha256 under `roots`. Returns (paths to run, the manifest entries selected).

    A selected file that is under no root is PRINTED, never skipped silently - a sample is only a
    sample if every entry was either run or named as missing.
    """
    probe = _load_sibling("mesh_probe")
    manifest = probe.load_manifest(path)
    if exclude:
        manifest = without_excluded(manifest, excluded_sha256s(list(exclude)))
    entries = stratified_sample(manifest, sample, seed=seed) if sample else list(
        manifest.get("files") or [])
    if shard:
        entries = shard_of(entries, shard)
    found, missing = probe.resolve_inputs({**manifest, "files": entries}, list(roots))
    if missing:
        print(f"{len(missing)} of {len(entries)} manifest file(s) are not under the given roots:",
              file=sys.stderr)
        for name in missing:
            print(f"  missing {name}", file=sys.stderr)
    return [found[e["name"]] for e in entries if e["name"] in found], entries


# ------------------------------------------------------------------ the investigator (gym records)
#
# WHAT THIS IS FOR. The investigator's value cannot be argued from a row: a row says a region was
# committed, not whether the model looked at the right thing to get there. The transcript IS the
# evidence, and a transcript is only worth keeping if the same conversation can be re-run without
# a model - otherwise every policy comparison costs another live corpus pass. So the runner does
# three things here and nothing more: it collects one record per author call, joins each record to
# the verdict the session gave that package, and loads a previous run's records back for replay.
#
# WHAT NEVER GOES IN. No CAD, ever (plan: Global Constraints). A record carries names, hashes,
# fingerprints, measurements, verdicts and the conversation; `scrub_record` is the gate, and it
# drops geometry keys and raw bytes wherever they appear rather than trusting the producer. The
# records are customer-derived data: they go to this devtools sink, never to a service.
INVESTIGATOR_POSTURES = ("off", "shadow", "live")

#: Keys a record may never carry out of the process, whatever nests them. Named rather than
#: sniffed: a key called `brep` holding a str is a geometry leak just as much as one holding bytes.
_GEOMETRY_KEYS = frozenset({
    "shape", "shapes", "brep", "brep_bytes", "step", "step_bytes", "stl", "stl_bytes",
    "geometry", "original_shape", "region_shape", "scratch_shape", "tshape",
})


def investigator_posture(value) -> str:
    """The posture this run asked for - FAIL-CLOSED, like every other posture in this plan.

    Only the exact on-values read as on. Anything else, including "on", "true", "LIVE" and None,
    is `off`: a mistyped flag must not be the thing that decides a model ran over a customer's
    part.
    """
    text = value if isinstance(value, str) else ""
    return text if text in ("shadow", "live") else "off"


def _plain(value):
    """`value` as something json.dumps(allow_nan=False) accepts, with geometry removed.

    Non-finite floats become None rather than raising or becoming a string: a `seconds` that came
    back as nan is a missing measurement, and None is how the rest of this file says missing.
    """
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items() if str(k) not in _GEOMETRY_KEYS}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None  # raw bytes are the geometry leak this guard exists for
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)


def scrub_record(record: dict) -> dict:
    """One gym record, safe to write: JSON-safe, geometry-free, and a new dict either way."""
    return _plain(dict(record))


class TranscriptSink:
    """What the investigator's `sink` is in a corpus run: one scrubbed record per author call.

    Streams each line AS IT ARRIVES, for the same reason BundleWriter streams rows: a run that
    dies half way through 300 files leaves the transcripts it earned on disk rather than in a list
    nobody wrote. `path=None` collects in memory only, which is what the tests use.
    """

    def __init__(self, path=None) -> None:
        self.records: list[dict] = []
        #: The file the records now arriving belong to; every line carries it, so a shard's
        #: transcripts read file by file (lane D). Set by `begin`, which `_repair_row` calls.
        self.file = ""
        self.path = Path(path) if path else None
        self._handle = None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("w", encoding="utf-8")

    def begin(self, file: str) -> None:
        """Tag the records that arrive from now on with `file`."""
        self.file = str(file)

    def __call__(self, record: dict) -> dict:
        clean = scrub_record(record)
        if self.file and "file" not in clean:
            clean["file"] = self.file
        self.records.append(clean)
        if self._handle is not None:
            self._handle.write(json.dumps(clean, allow_nan=False) + "\n")
            self._handle.flush()
        return clean

    def since(self, start: int) -> list[dict]:
        """The records collected after index `start` - this file's, when the caller marked the
        offset before the repair ran."""
        return self.records[start:]

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def package_verdicts(measured: dict | None) -> dict:
    """`(region index, attempt) -> PackageVerdict dict`, from `measurements["regions"]`.

    THE ATTEMPT IS THE ORDINAL, because a PackageVerdict records the region it judged and not
    which try it was: the n-th package the session judged for a region IS that region's n-th
    attempt, and the region loop appends them in order.
    """
    out: dict = {}
    seen: dict = {}
    for package in (measured or {}).get("packages") or []:
        index = package.get("region_index")
        attempt = seen[index] = seen.get(index, 0) + 1
        out[(index, attempt)] = package
    return out


def _region_index(record: dict):
    """The region a record is about. `_Investigation.to_dict()` writes it as a bare int; an older
    record shape nested it as `{"index": ...}`. Both read; anything else is None."""
    region = record.get("region")
    if isinstance(region, dict):
        return region.get("index")
    return region if isinstance(region, int) and not isinstance(region, bool) else None


def join_verdicts(records, verdicts: dict) -> list[dict]:
    """Each record with the verdict its package got, under `"verdict"`.

    ABSENT RATHER THAN INVENTED when no package matches: a replay miss, or an investigation that
    concluded nothing, produced no package, and a row that claimed one would be a fabrication.
    Returns new dicts; the caller's records are not mutated.
    """
    joined = []
    seen: dict = {}
    for record in records:
        index = _region_index(record)
        # THE PRODUCER WRITES NO `attempt` (cad/repair/investigator.py closes one region's
        # investigation once per author call, so the n-th record for a region IS its n-th
        # attempt - the same ordinal package_verdicts keys on). A stated one is kept.
        ordinal = seen[index] = seen.get(index, 0) + 1
        attempt = record.get("attempt")
        key = (index, attempt if isinstance(attempt, int) else ordinal)
        verdict = verdicts.get(key)
        joined.append({**record, "verdict": verdict} if verdict is not None else dict(record))
    return joined


#: What an investigation spent, as the record may state it: nested under `spent`, or flat.
_SPEND_KEYS = ("tool_calls", "model_calls", "seconds", "input_tokens", "output_tokens")


def _spent(record: dict) -> dict:
    nested = record.get("spent") or {}
    return {key: (nested.get(key) if nested.get(key) is not None else record.get(key)) or 0
            for key in _SPEND_KEYS}


def investigator_row(records) -> dict:
    """The row's `investigator` block: what the investigations on this file cost, and how they
    ended. EMPTY for no records, so a file nothing investigated gains no key."""
    if not records:
        return {}
    totals = dict.fromkeys(_SPEND_KEYS, 0.0)
    ends: dict[str, int] = {}
    for record in records:
        spend = _spent(record)
        for key in _SPEND_KEYS:
            totals[key] += float(spend[key])
        end = str(record.get("end") or "")
        if end:
            ends[end] = ends.get(end, 0) + 1
    return {"regions": len({_region_index(r) for r in records}),
            "tool_calls": int(totals["tool_calls"]),
            "model_calls": int(totals["model_calls"]),
            "seconds": round(totals["seconds"], 2),
            "input_tokens": int(totals["input_tokens"]),
            "output_tokens": int(totals["output_tokens"]),
            "ends": dict(sorted(ends.items())),
            # ONE ROW PER AUTHOR CALL: how the region ended and why no package, so a shard's
            # results read file by file and region by region without the transcripts.
            "outcomes": [_outcome_of(r) for r in records]}


def _outcome_of(record: dict) -> dict:
    from meshpipeline.cad.repair import investigation_trace

    row = investigation_trace.outcome(record)
    verdict = record.get("verdict")
    if isinstance(verdict, dict):
        # THE REVIEW AS MEASURED FIELDS (verdict, tier, objection), never the reviewer's prose.
        review = investigation_trace.review_of(verdict)
        row["package"] = {k: v for k, v in review.items() if k != "region"}
    return row


def review_rows(measured: dict | None) -> dict:
    """The row's `packages` block: every package the session judged, with its review verdict and
    measured objection (investigation_trace.review_of: prose-free). EMPTY when no region run
    judged a package, so a row gains no key it did not have."""
    from meshpipeline.cad.repair import investigation_trace

    packages = ((measured or {}).get("regions") or {}).get("packages") or []
    if not packages:
        return {}
    return {"packages": [investigation_trace.review_of(p) for p in packages
                         if isinstance(p, dict)]}


def investigator_summary(rows: list[dict]) -> dict:
    """The run's investigator aggregate over the rows that carry a block. Pure, and EMPTY when no
    row does - so a run without the flag prints and seals nothing new."""
    blocks = [r["investigator"] for r in rows if r.get("investigator")]
    if not blocks:
        return {}
    ends: dict[str, int] = {}
    for block in blocks:
        for end, count in (block.get("ends") or {}).items():
            ends[end] = ends.get(end, 0) + count
    out = {"files": len(blocks),
           "regions": sum(int(b.get("regions") or 0) for b in blocks),
           "ends": dict(sorted(ends.items()))}
    for key in _SPEND_KEYS:
        total = sum(float(b.get(key) or 0) for b in blocks)
        out[key] = round(total, 2) if key == "seconds" else int(total)
    return out


def load_replay(path) -> list[dict]:
    """A previous run's transcripts, for `--investigator-replay`. One JSON object per line.

    A bad line NAMES ITSELF. A replay file is produced by a run that may have died mid-write, and
    "Expecting value: line 1 column 1" over a 300-file transcript tells the operator nothing.
    """
    records: list[dict] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError as exc:
            raise ValueError(f"{path} line {number} is not a JSON record: {exc}") from exc
    return records


def investigator_settings(posture: str, transcripts: str, replay: str) -> dict:
    """The three flags, frozen into a bundle's settings.json - and NOTHING when none was given,
    so a run that asked for no investigator keeps the settings digest it has today."""
    if investigator_posture(posture) == "off" and not transcripts and not replay:
        return {}
    return {"investigator": investigator_posture(posture),
            "transcripts": transcripts, "investigator_replay": replay}


def _investigator_module():
    """`cad/repair/investigator.py`, or None on a build that has not got one yet (Task 11).

    Imported BY NAME and tolerantly: this runner is useful on a branch where the investigator does
    not exist, and an ImportError at that point would take the whole corpus pass with it.
    """
    try:
        import importlib

        return importlib.import_module("meshpipeline.cad.repair.investigator")
    except Exception:  # noqa: BLE001 - absent or broken both mean "this build has no investigator"
        return None


def _offer(module, name: str, value) -> str:
    """Hand `value` to `module.name` ONLY IF THE MODULE DECLARES IT. Returns a seam note or "".

    The runner never invents an attribute on another module: a sink set on a name nothing reads is
    a silent nothing, and this plan has lanes landing in parallel. Saying so is the whole point.
    """
    if not hasattr(module, name):
        return (f"seam: {getattr(module, '__name__', module)} declares no `{name}`, so the runner "
                f"could not install it")
    setattr(module, name, value)
    return ""


def _apply_investigator_posture(mode: str) -> None:
    """Make `mode` the posture the repair READS. The settings module is already imported by the
    time a flag is parsed (the `ending` import at the top of this file loads it), so writing the
    environment alone changes nothing: the module's value is set too. The investigator and the
    conservative seam read `rcfg.CAD_REPAIR_INVESTIGATOR` at call time."""
    import meshpipeline.settings.cad_repair as rcfg

    os.environ["CAD_REPAIR_INVESTIGATOR"] = mode
    rcfg.CAD_REPAIR_INVESTIGATOR = mode


def install_investigator(posture: str | None, *, transcripts: str = "", replay: str = ""
                         ) -> tuple[TranscriptSink | None, str]:
    """Switch the investigator on for this run and give it somewhere to write. Returns the sink
    (None when nothing was asked for) and a note the operator must read.

    A GIVEN POSTURE (`--investigator off|shadow|live`) is applied to the settings the repair reads
    - including an explicit `off` over an inherited one. `None` (the flag not given) keeps the
    posture the process was launched with, so an environment-driven run is never overridden by a
    default. The SINK and the REPLAY RECORDS are offered to `cad/repair/investigator.py`; a build
    without one, or without the name, gets a note rather than a crash.

    A REPLAY IS REFUSED, before anything is set: nothing in cad/repair reads
    `investigator.replay_records` yet, so an accepted replay would call the model (or, with the
    investigator off, replay nothing) while its settings claimed a reproducible run.
    """
    if replay:
        raise ValueError("--investigator-replay is not implemented: nothing in cad/repair reads "
                         "the recorded conclusions, so the run would call the model instead")
    if posture is not None:
        _apply_investigator_posture(investigator_posture(posture))
    mode = investigator_posture(posture)
    if mode == "off" and not transcripts and not replay:
        return None, ""
    sink = TranscriptSink(transcripts or None)
    module = _investigator_module()
    if module is None:
        return sink, ("seam: this build has no cad/repair/investigator.py, so nothing will write "
                      "an investigator transcript (Task 11)")
    return sink, _offer(module, "sink", sink)


def _probe_before(path: Path) -> tuple[bool, str] | None:
    """The file probe's verdict on the input as delivered, or None when it cannot be measured.
    Taken BEFORE repair only under --force-repair, which needs it to decide whether to run one."""
    from meshpipeline.cad.repair import mesh_probe

    try:
        diagonal = _diagonal_of(path)
    except Exception:  # noqa: BLE001
        return None
    return _verdict(mesh_probe.probe_file(path, diagonal_mm=diagonal,
                                          budget_s=MESH_PROBE_BUDGET_S))


def _one(path: Path, scratch: Path, *, mode: str = "off", mesh_probe: bool = False,
         force_repair: bool = False, goal=None, sink=None, stage=None) -> dict:
    # `stage(name, **facts)` is told where the file is, so a run that isolates files can say at
    # which stage a crashed child died and whether the input meshed before it did.
    stage = stage or (lambda *_a, **_k: None)
    # FORCE-REPAIR IS A BENCHMARK SWITCH, NOT A PRODUCT PATH. A file the before-probe says will not
    # mesh runs the repair session even where triage routed it away (a clean inspection goes to
    # mesh-as-is), so the in-session probe can locate what the inspector could not see.
    forced = False
    stage("probe_before")
    taken = _probe_before(path) if (force_repair and mesh_probe) else None
    if taken is not None:
        forced = not taken[0]
    stage("repair", **({"meshes_before": bool(taken[0])} if taken is not None else {}))
    row = _repair_row(path, scratch, mode=mode, force=forced, goal=goal, sink=sink)
    if taken is not None and row.get("outcome") != "unreadable":
        row["meshes_before"], row["mesh_kind_before"] = taken
    if mesh_probe and row.get("outcome") != "unreadable":
        stage("mesh_probe")
        _score_meshing(row, path, scratch)
    elif mesh_probe:
        row.update(meshes_before=False, mesh_kind_before="unreadable",
                   meshes_after=None, mesh_kind_after="")
    return row


def _repair_row(path: Path, scratch: Path, *, mode: str = "off", force: bool = False,
                goal=None, sink=None) -> dict:
    from meshpipeline.cad.repair.conservative import (
        RepairRefused,
        RepairUnavailable,
        repair_step_file,
    )
    from meshpipeline.cad.repair.contracts import RepairStatus, status_from_defects
    from meshpipeline.cad.repair.triage import ROUTE_CONSERVATIVE_REPAIR, recommend

    row: dict = {"file": path.name, "size_bytes": path.stat().st_size}
    started = time.monotonic()
    # THE OFFSET BEFORE ANYTHING RAN, so the records this file produced can be told from the ones
    # the files before it produced. `sink=None` is today's behaviour exactly: no key, no join.
    transcripts_from = len(sink.records) if sink is not None else 0
    if sink is not None and callable(getattr(sink, "begin", None)):
        sink.begin(path.name)

    def scored(verdict: dict | None = None) -> dict:
        """The goal columns for this row, and nothing at all when no profile was asked for."""
        return goal_row(verdict, goal) if goal is not None else {}

    def investigated(measured: dict | None = None) -> dict:
        """This file's `investigator` block, and the verdict joined ONTO THE SINK'S OWN RECORDS -
        the transcript is the thing a later replay reads, so the join has to reach it, not a copy
        the row threw away. Empty when no investigation ran, whatever the posture asked for."""
        if sink is None:
            return {}
        mine = sink.since(transcripts_from)
        verdicts = package_verdicts((measured or {}).get("regions"))
        for index, joined in enumerate(join_verdicts(mine, verdicts)):
            sink.records[transcripts_from + index] = joined
        block = investigator_row(sink.since(transcripts_from))
        return {"investigator": block} if block else {}

    try:
        before = _inspect(path)
    except Exception as exc:  # noqa: BLE001 - a file the kernel will not read is a real outcome
        row.update(outcome="unreadable", error=f"{type(exc).__name__}: {exc}", **scored())
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

    # A crossing only the face analyzer measured does not make a file repair work (floor-fix
    # ruling 2): with review off it is the flag-off file's outcome, the finding still in the row.
    skipped = ((status is RepairStatus.clean and not [e for e in before["entities"]
                                                      if not e.get("analyzer_only")])
               or advice.route != ROUTE_CONSERVATIVE_REPAIR)
    if skipped and force:
        # Runner-only: the route triage chose is kept in the row, and nothing under src/ changes.
        row.update(forced_repair=True, triage_route=advice.route)
    elif status is RepairStatus.clean and not [e for e in before["entities"]
                                               if not e.get("analyzer_only")]:
        row.update(outcome="nothing_to_repair", seconds=round(time.monotonic() - started, 2),
                   **scored())
        return row
    elif advice.route != ROUTE_CONSERVATIVE_REPAIR:
        row.update(outcome=f"not_attempted:{advice.route}",
                   seconds=round(time.monotonic() - started, 2), **scored())
        return row

    destination = scratch / f"{path.stem}-repaired.step"
    extra: dict = {}
    if mode in ("shadow", "live"):
        plan = plan_for(before["entities"], job_id=path.stem)
        row["plan"] = _plan_row(plan)
        if mode == "live" and plan.directives:  # shadow hands the kernel nothing
            extra["directives"] = tuple(plan.directives)
    # NO PROFILE, NO KWARG: `goal=None` would be the same default, but passing nothing is what
    # makes a run without --goal byte-identical to a run on a build that has no goal parameter.
    if goal is not None:
        extra["goal"] = goal
    try:
        repaired = repair_step_file(path, destination, **extra)
    except RepairUnavailable as exc:
        row.update(outcome="unavailable", error=str(exc), **scored(), **investigated())
        return row
    except RepairRefused as exc:
        # THE ATTEMPTS GO WITH THE REFUSAL (conservative.py puts them in its measurements), so a
        # model operation that rolled back and left nothing kept is still counted. A result the
        # after-check discarded kept nothing either: its `kept` operations count as rolled back.
        measured = dict(getattr(exc, "measurements", None) or {})
        # `outcome` IS LEFT EXACTLY AS IT WAS - every `RepairRefused` is still `refused` there, so
        # the benchmark track, `_REPAIRED_OUTCOMES` and every sealed bundle's `outcomes` tally keep
        # the numbers they have. `ending` is the ADDED precise field and says which of the five
        # answers this actually was; `stopped_because` now travels on this row too, because a
        # refusal that did not record why the loop stopped was the reason the old word had to be a
        # guess (cad/repair/ending.py).
        row.update(outcome="refused", ending=exc.ending, error=str(exc),
                   stopped_because=str(measured.get("stopped_because", "")),
                   resolved=bool(measured.get("resolved", False)),
                   complete_scan=bool(measured.get("complete_scan", True)),
                   mesh_located_remaining=_mesh_located(measured),
                   timings=dict(measured.get("timings") or {}),
                   operations=_operation_rows(measured.get("operations") or ()),
                   result_discarded=bool(measured.get("result_discarded")),
                   seconds=round(time.monotonic() - started, 2),
                   # THE VERDICT SURVIVES THE REFUSAL: goal.py records it before the after-check
                   # refuses, so a refused repair still says what it would have finished as.
                   **scored(measured.get("goal")), **investigated(measured), **review_rows(measured),
                   **_pass_rows(measured))
        return row
    except Exception as exc:  # noqa: BLE001
        row.update(outcome="repair_failed", error=f"{type(exc).__name__}: {exc}", **scored(),
                   **investigated())
        return row

    measured = {m.name: m.value for m in repaired.measurements}
    row.update(
        # EVERY ATTEMPT, KEPT OR NOT. The rolled-back ones are the interesting half: they are
        # where the rules planner ran out of ideas, which is exactly what a model later has to
        # beat, and a corpus run that only listed successes could not show it.
        operations=_operation_rows(repaired.operations),
        operations_attempted=int(measured.get("operations_attempted", 0) or 0),
        operations_kept=int(measured.get("operations_kept", 0) or 0),
        # THE KEPT OPERATIONS THAT CAME THROUGH THE MESH-CURE DOOR: target finding still there,
        # part meshes as delivered. Counted apart so the evidence can say how many keeps it bought.
        operations_kept_meshes_after_target_remains=sum(
            1 for op in repaired.operations if isinstance(op, dict)
            and op.get("status") == "kept" and op.get("reason") == MESHES_AFTER_TARGET_REMAINS),
        # FINISHED OR OUT OF ROOM. The single most useful number from a corpus run: a corpus full
        # of `op_budget` says raise the budget, one full of `stalled` says the catalogue is missing
        # an operation, and one full of `no_candidate` says the planner cannot aim at what real
        # files actually carry.
        stopped_because=str(measured.get("stopped_because", "")),
        resolved=bool(measured.get("resolved", False)),
        complete_scan=bool(measured.get("complete_scan", True)),
        planner=str(measured.get("planner", "")),
        refusals=list(measured.get("refusals", []) or []),
        declared_area_mm2=float(measured.get("declared_area_mm2", 0.0) or 0.0),
        mesh_located_remaining=_mesh_located(measured),
        timings=dict(measured.get("timings") or {}),
        **scored(measured.get("goal")),
        **investigated(measured), **review_rows(measured), **_pass_rows(measured),
    )

    after = _inspect(destination)
    row.update(valid_after=after["valid"], counts_after=after["counts"],
               defects_after=sorted(set(after["defects"])),
               located_after=after["located"].get("by_code", {}))

    # THE CREDIT IS GIVEN BY THE RE-INSPECTION, not by the repair. A repair that reports success
    # over a file whose defects are still there has not repaired anything.
    fixed = {code: count for code, count in row["located_before"].items()
             if count > row["located_after"].get(code, 0)}
    row["repaired_defects"] = fixed
    # A KEPT HEAL IS CORROBORATED AT THE TRIANGLE LEVEL, where the turbine's crossing actually is.
    if any(op.get("op") == "heal_self_intersections" and op.get("status") == "kept"
           for op in row["operations"]):
        try:
            from meshpipeline.cad.ingest.cad import read_step
            row["heal_corroborated"] = heal_corroborated(read_step(destination))
        except Exception:  # noqa: BLE001
            row["heal_corroborated"] = None
    row["outcome"] = ("repaired" if fixed else
                      "no_change" if row["located_after"] == row["located_before"] else
                      "changed_without_fixing")
    # THE CREDIT STILL COMES FROM THE RE-INSPECTION, never from the loop's own word: a run the
    # loop called `repaired` whose defects are all still there is `no_change` here and the ending
    # follows that, so no part can be made to look delivered by the repair's own claim. The loop's
    # own ending is kept beside it (`loop_ending`) - that is where `escalated` and `unmeshable`
    # live, and a reader comparing the two sees exactly where they disagree.
    loop_ending = str(measured.get("ending") or "")
    row["ending"] = (loop_ending
                     if loop_ending in (repair_ending.ESCALATED, repair_ending.UNMESHABLE,
                                        repair_ending.SCAN_INCOMPLETE)
                     else row["outcome"])
    if loop_ending:
        row["loop_ending"] = loop_ending
    row["seconds"] = round(time.monotonic() - started, 2)
    return row


#: The manifest fields a row carries so its stratum can be re-derived (`stratum_of` reads these).
_STRATUM_FIELDS = ("gmsh_error", "solids")


def _input_stamp(path: Path, by_sha: dict) -> dict:
    """`{"sha256": ...}` for the input as read, plus its manifest stratum fields when the run
    selected it from a manifest. {} for a file that cannot be read - its row says why."""
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return {}
    sha = digest.hexdigest()
    entry = by_sha.get(sha) or {}
    return {"sha256": sha, **{k: entry[k] for k in _STRATUM_FIELDS if k in entry}}


CRASHED = "crashed"
TIMEOUT = "timeout"


def _isolated(path: Path, body, *, mesh_probe: bool, file_timeout_s: float = 0.0) -> dict:
    """Run `body(stage)` for one file in a FORKED CHILD and return its row. A child that dies (a
    native segfault in OCC's ShapeFix took down a whole shard on a real ABC part) becomes a
    `crashed` row naming the signal or exit code and the last stage it reported, and the run goes
    on to the next file. In-memory state the child changes (counters, collected records) does not
    reach the parent; anything that must survive is written to a file as it happens.

    A child that HANGS (OCCT and gmsh can hang in native code no in-child timeout interrupts) is
    SIGKILLed by the parent after `file_timeout_s` seconds (0 = no limit) and becomes a `timeout`
    row; SIGKILL is the only signal guaranteed to land.

    PRODUCT NOTE: this protects the benchmark only. The same crash in a worker is a product bug."""
    import pickle
    import threading

    # A pending stdout/stderr buffer would be inherited and printed twice: empty it first.
    sys.stdout.flush()
    sys.stderr.flush()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # the child: report stages, send the row, never return into the parent's code
        os.close(read_fd)
        out = os.fdopen(write_fd, "wb")

        def send(msg):
            pickle.dump(msg, out)
            out.flush()

        code = 0
        try:
            send(("row", body(stage=lambda name, **facts: send(("stage", name, facts)))))
        except BaseException as exc:  # noqa: BLE001 - a Python error is the parent's to record
            try:
                send(("error", f"{type(exc).__name__}: {exc}"))
            except Exception:  # noqa: BLE001
                code = 70
        finally:
            try:
                out.close()
                sys.stdout.flush()
                sys.stderr.flush()
            finally:
                os._exit(code)
    os.close(write_fd)
    stage, facts, row, error = "start", {}, None, None
    timed_out = threading.Event()

    def _wall_kill():
        timed_out.set()
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    # The kill closes the child's end of the pipe, so the read below ends in EOF. Grandchildren
    # (bounded_child, mesh_probe, tolerant_probe: fTetWild, gmsh) do not hold the pipe's write end
    # (Popen close_fds=True, os.pipe fds non-inheritable), so the EOF is reliable. They run in
    # their own sessions with their own budgets and reapers, so this kill does not reach them: a
    # timeout may leave a bounded orphan burning CPU until its own reaper fires.
    watchdog = threading.Timer(file_timeout_s, _wall_kill) if file_timeout_s > 0 else None
    if watchdog is not None:
        watchdog.daemon = True
        watchdog.start()
    with os.fdopen(read_fd, "rb") as src:
        while True:
            try:
                msg = pickle.load(src)  # noqa: S301 - our own forked child, a private pipe
            except (EOFError, pickle.UnpicklingError):
                break
            if msg[0] == "stage":
                stage = msg[1]
                facts.update(msg[2])
            elif msg[0] == "row":
                row = msg[1]
            elif msg[0] == "error":
                error = msg[1]
    if watchdog is not None:
        watchdog.cancel()  # before reaping: a late fire must not signal a reused pid
    while True:  # poll, never block forever on a child that closed its pipe and hung
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        time.sleep(0.01)
    if timed_out.is_set() and os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL:
        timed: dict = {"file": path.name, "outcome": TIMEOUT,
                     "timeout": {"stage": stage, "file_timeout_s": file_timeout_s},
                     "error": f"child killed after {file_timeout_s:g}s wall clock during {stage}"}
        if mesh_probe and "meshes_before" in facts:
            before = bool(facts["meshes_before"])
            timed.update(meshes_before=before, mesh_kind_before="" if before else "other",
                       meshes_after=False if before else None,
                       mesh_kind_after=TIMEOUT if before else "")
        return timed
    if row is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0:
        return row
    if error is not None and os.WIFEXITED(status):
        raise RuntimeError(f"{path.name}: {error}")  # a Python error, as an inline run raises it
    sig = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
    code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
    crash = {"signal": sig, "exit_code": code if sig is None else 128 + sig, "stage": stage}
    out = {"file": path.name, "outcome": CRASHED, "crash": crash,
           "error": f"child died ({'signal ' + str(sig) if sig else 'exit ' + str(code)}) "
                    f"during {stage}"}
    if mesh_probe and "meshes_before" in facts:
        before = bool(facts["meshes_before"])
        # meshed before and crashed: the repair broke it. Did not mesh: no after verdict.
        out.update(meshes_before=before, mesh_kind_before="" if before else "other",
                   meshes_after=False if before else None,
                   mesh_kind_after=CRASHED if before else "")
    return out


def run_files(files: list[Path], scratch: Path, *, mode: str = "off", loop: bool = False,
              bundle=None, mesh_probe: bool = False, force_repair: bool = False,
              goal=None, sink=None, entries=None, isolate: bool = False,
              tolerant_delivery: bool = False, row_marks: dict | None = None,
              meshability: bool = False, file_timeout_s: float = 0.0) -> list[dict]:
    """Run the repair loop over `files`, printing a line per file; returns the rows. When `bundle`
    is a BundleWriter every row is streamed into it AS IT IS PRODUCED, so a run that dies half way
    leaves its evidence-so-far on disk rather than in a list that was never written.

    EVERY ROW NAMES ITS INPUT BY sha256, and a manifest run (`entries`, the selected manifest
    entries) also carries the two fields its stratum is drawn on (`gmsh_error`, `solids`): the
    gym keys arms by that hash and stratifies on those fields, and a row without them cannot be
    matched to the same part in another arm."""
    by_sha = {str(e.get("sha256")): e for e in (entries or []) if isinstance(e, dict)}
    rows: list[dict] = []
    for path in files:
        stamp = _input_stamp(path, by_sha)

        def body(stage=None, path=path):
            if mode == "live":
                # The SAME surface run twice, deterministic-only first and into its own directory
                # so the second pass cannot overwrite it. Two passes to attribute a difference to
                # the model direction, not two engines competing.
                rules_dir = scratch / "rules_only"
                rules_dir.mkdir(parents=True, exist_ok=True)
                if stage:
                    stage("rules_only")
                rules_row = _one(path, rules_dir)
                row = _one(path, scratch, mode="live", mesh_probe=mesh_probe,
                           force_repair=force_repair, goal=goal, sink=sink, stage=stage)
                row["rules_only"] = rules_row
                return row
            return _one(path, scratch, mode=mode, mesh_probe=mesh_probe,
                        force_repair=force_repair, goal=goal, sink=sink, stage=stage)

        since = time.time()

        def scored(stage=None, path=path, since=since):
            # SCORING RUNS WHERE THE REPAIR RAN: inside the per-file child under --isolate-files,
            # because it reads CAD again (_diagonal_of, _inspect on a passthrough, the probes). In
            # the parent, a native crash or a stuck read there would take the whole shard down.
            row = body(stage=stage)
            if meshability:
                if stage:
                    stage("meshability_score")
                score_or_mark(row, path, scratch, since=since)
            return row

        row = (_isolated(path, scored, mesh_probe=mesh_probe, file_timeout_s=file_timeout_s)
               if isolate else scored())
        if tolerant_delivery:
            _tolerant_delivery(row, path, scratch)
        row.update(stamp)
        # RUN-WIDE MARKS (e.g. investigator_status=no_router) go on every row BEFORE it streams,
        # so a run that dies half way is marked as honestly as one that finishes.
        row.update(row_marks or {})
        rows.append(row)
        if bundle is not None:
            bundle.write_row(row)
        print(f"{row['outcome']:<28} {row['file'][:52]:<54} "
              f"before={row.get('defects_before', [])} "
              f"located={row.get('located_before', {})} "
              f"-> fixed={row.get('repaired_defects', {})}"
              + (f" [{row['error'][:90]}]" if row.get("error") else ""))
        if loop:
            for op in row.get("operations", []) or []:
                mark = "+" if op.get("status") == "kept" else "-"
                detail = f" ({op['reason']})" if op.get("reason") else ""
                print(f"      {mark} {op.get('op', '?'):<20} {op.get('defect_code', ''):<22}"
                      f"{op.get('status', '')}{detail}")
            for record in row.get("refusals") or []:
                print(f"      ! refused {record.get('reason', '?')}: "
                      f"{record.get('detail', '')}")
            if row.get("stopped_because"):
                print(f"      = stopped: {row['stopped_because']} "
                      f"(resolved={row.get('resolved')})")
    return rows


def _print_planner(summary: dict, cache: CachingRouter | None) -> None:
    print(f"\n== repair: model-directed portion ({summary['mode']}) ==")
    if cache is None:
        print("  no model was available: nothing was directed (declared off)")
    else:
        print(f"  reply cache: {cache.hits} hit(s), {cache.misses} miss(es)")
    if summary["mode"] == "shadow":
        print(f"  coverage: {summary['coverage']}/{summary['files_planned']} file(s) with a proposal")
        print(f"  agreement with the deterministic portion (first op+target): "
              f"{summary['agreed']}/{summary['compared']}")
        print(f"  declines={summary['declines']}  call_failures={summary['call_failures']}")
    else:
        print(f"  defects repaired, deterministic only:      {summary['rules_only_total']} "
              f"{summary['rules_only_repaired']}")
        print(f"  defects repaired, with model direction:    {summary['live_total']} "
              f"{summary['live_repaired']}")
        for name, c in sorted(summary["llm_ops"].items()):
            print(f"  llm-planner {name:<20} kept={c['kept']:<4} "
                  f"rolled_back={c['rolled_back']:<4} declined={c['declined']:<4} "
                  f"failed={c['failed']}")
        if not summary["llm_ops"]:
            print("  no model-directed operation was attempted")


#: Where the run writes each `-repaired.step` (and anything written beside it, such as a mesh-hints
#: sidecar). Unset, it is the same directory as always; the v3 eval points it inside the shard's
#: output directory, because only that directory leaves the VM and a file left in the container's
#: /tmp is gone when the container exits.
SCRATCH_ENV = "REPAIR_CORPUS_OUT"


def scratch_dir() -> Path:
    """The directory repaired files go to: `$REPAIR_CORPUS_OUT` when set, else the system temp
    directory's `repair_corpus_out` (unchanged behaviour)."""
    override = os.environ.get(SCRATCH_ENV, "").strip()
    return Path(override) if override else Path(tempfile.gettempdir()) / "repair_corpus_out"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="*", help="directories holding .step/.stp files")
    parser.add_argument("--limit", type=int, default=0, help="stop after N files")
    parser.add_argument("--loop", action="store_true",
                        help="print each file's operation sequence, not just the summary")
    parser.add_argument("--planner", choices=("off", "shadow", "live"), default="off",
                        help="LLM planner: shadow records proposals only; live also runs a "
                             "rules-only pass first so the two can be compared")
    parser.add_argument("--reply-cache", default="",
                        help="JSON file of recorded planner replies keyed by prompt hash")
    parser.add_argument("--mesh-probe", dest="mesh_probe", action="store_true",
                        help="also mesh each input and its repaired file in gmsh (slow) and "
                             "report which files mesh after repair")
    parser.add_argument("--force-repair", dest="force_repair", action="store_true",
                        help="with --mesh-probe: a file whose before-probe fails runs the repair "
                             "session even when triage routed it away (benchmark only)")
    parser.add_argument("--json", dest="json_out", default="", help="write the rows here")
    parser.add_argument("--tolerant-delivery", dest="tolerant_delivery", action="store_true",
                        help="for a file gmsh still refuses whose tier-2b reading is "
                             "tolerant_rung_eats_it, mesh the delivered file with fTetWild at "
                             "production envelope/iterations (<= 300 s) and record it as its own "
                             "column - never as a repair")
    parser.add_argument("--meshability", dest="meshability", action="store_true",
                        help="with --mesh-probe: score THE RETURNED STEP (a missing one is a bug, "
                             "no_returned_step) at the headline probe, at gmsh's own defaults, and "
                             "with the repair's .mesh-hints.json sidecar; credit the op that "
                             "made it mesh (meshed_by)")
    parser.add_argument("--isolate-files", dest="isolate", action="store_true",
                        help="repair each file in a forked child: a native crash becomes a "
                             "`crashed` row and the run continues (POSIX; benchmark harness)")
    parser.add_argument("--file-timeout-s", dest="file_timeout_s", type=float, default=0.0,
                        help="with --isolate-files: SIGKILL a file's child after this many "
                             "seconds and record a `timeout` row (0 = no limit)")
    # THE INVESTIGATOR (cad/repair/investigator.py). Off is today's behaviour exactly: no record,
    # no row key, no summary block and no settings key. `shadow` investigates and keeps its
    # packages out of the kernel; `live` authors with them. NOT GIVEN (None) keeps the
    # CAD_REPAIR_INVESTIGATOR the process was launched with; given, it overrides it.
    parser.add_argument("--investigator", choices=INVESTIGATOR_POSTURES, default=None,
                        help="bounded investigator author: shadow records only, live authors "
                             "(default: the CAD_REPAIR_INVESTIGATOR environment value)")
    parser.add_argument("--transcripts", default="",
                        help="write one gym record per author call here, as JSONL (names, hashes, "
                             "fingerprints, measurements and the conversation - never geometry)")
    parser.add_argument("--investigator-replay", dest="investigator_replay", default="",
                        help="NOT IMPLEMENTED - refused: nothing in cad/repair reads recorded "
                             "conclusions yet, so a replay would still call the model")
    # THE GOAL PROFILE. Absent means the repair is not told what it is finishing for, which is
    # today's behaviour exactly: no kwarg, no row key, no summary block. `meshing` is not offered
    # here - its verdict is the product mesher, which this tool does not run.
    parser.add_argument("--goal", choices=GOAL_PROFILES, default="",
                        help="tell the repair what it is finishing for, and score each file's "
                             "finishing verdict (tier-2 meshability, grind, tier-2b routing)")
    # SELECTING FROM THE TRACK MANIFEST instead of walking directories: the files are found by
    # sha256 under --roots, so a corpus whose local names differ still runs the stated sample.
    parser.add_argument("--manifest", default="",
                        help="mesh-failure-real manifest: take the files from it, resolved by "
                             "sha256 under --roots, instead of walking directories")
    parser.add_argument("--roots", nargs="*", default=[],
                        help="with --manifest: directories to resolve the manifest files under")
    parser.add_argument("--sample", type=int, default=0,
                        help="with --manifest: draw N files stratified by (failure family, "
                             "single/multi solid)")
    parser.add_argument("--seed", type=int, default=0,
                        help="with --sample: the seed, which is the sample's whole provenance")
    parser.add_argument("--shard", default="",
                        help="with --sample: run only I/K of the sample (the sorted sample "
                             "sliced [I-1::K], so the K shards partition it)")
    parser.add_argument("--exclude", nargs="*", default=[],
                        help="with --manifest: sample/manifest JSON files whose sha256s are "
                             "dropped before sampling, to draw a held-out sample")
    # The publication bundle (public-benchmarks.md). The manifest flags are free text on purpose:
    # provenance is a story only the person running the corpus knows. The one closed set is the
    # redistribution status, closed by benchmark_bundle so a typo cannot launder a corpus.
    parser.add_argument("--bundle", default="",
                        help="also write a publication evidence bundle into this directory")
    parser.add_argument("--track", default="unspecified",
                        help="benchmark track for the bundle manifest, e.g. occt-real-defect")
    parser.add_argument("--corpus-name", dest="corpus_name", default="",
                        help="human name of the corpus, for the bundle manifest")
    parser.add_argument("--provenance", default="",
                        help="where the inputs came from, with fetch instructions")
    parser.add_argument("--redistribution", default="internal",
                        choices=benchmark_bundle.REDISTRIBUTION_STATUSES,
                        help="redistribution status of the corpus (default: internal)")
    parser.add_argument("--rebundle", default="",
                        help="rows JSON from a previous --json run: package it into --bundle "
                             "without re-running any CAD (the summary is recomputed from the rows)")
    # No engine runs in a --rebundle, so the manifest records "unknown" for what measured the rows
    # unless the person packaging them states it. Stating the wrong commit is worse than unknown.
    parser.add_argument("--rebundle-engine-commit", dest="rebundle_engine_commit", default="",
                        help="with --rebundle: the engine commit the recorded rows were measured "
                             "on (default: unknown, because this host did not measure them)")
    parser.add_argument("--rebundle-hardware-class", dest="rebundle_hardware_class", default="",
                        help="with --rebundle: the hardware class the recorded rows were measured "
                             "on (default: unknown)")
    parser.add_argument("--verify", default="",
                        help="check a sealed bundle and print what it claims: the settings digest, "
                             "the row count, and the summary RE-DERIVED from rows.jsonl")
    args = parser.parse_args()

    # VERIFYING IS READING, and it reads the rows rather than the stored headline: a figure edited
    # in place keeps row_count intact, so recomputing the aggregate is the only thing that catches
    # it. This is what gives load_bundle's recompute a caller outside the tests.
    if args.verify:
        directory = Path(args.verify)
        try:
            stored = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        catalogue = settings_operation_catalogue(directory, stored)
        try:
            manifest, rows, summary, settings = benchmark_bundle.load_bundle(
                directory,
                lambda loaded: corpus_summary(
                    loaded, settings_planner_mode(directory), catalogue))
        except (FileNotFoundError, ValueError) as exc:
            print(f"bundle at {directory} is NOT verified: {exc}", file=sys.stderr)
            return 1
        print(f"bundle at {directory} verified: {len(rows)} row(s)\n"
              f"  benchmark      {manifest['benchmark']} / {manifest['track']}\n"
              f"  corpus         {manifest['corpus_name']} ({manifest['redistribution_status']})\n"
              f"  engine commit  {manifest['engine_commit']}\n"
              f"  hardware       {manifest['hardware_class']}\n"
              f"  settings       {len(settings)} key(s), digest {manifest['settings_digest'][:16]}\n"
              f"  endings        {summary.get('endings')}\n"
              f"  outcomes       {summary.get('outcomes')}")
        return 0

    # REBUNDLE IS A PACKAGING RUN, NOT A CAD RUN: a recorded rows file already paid for its
    # evidence, so it is wrapped and sealed here and nothing else happens.
    if args.rebundle:
        if not args.bundle:
            print("--rebundle needs --bundle <dir> to write into", file=sys.stderr)
            return 2
        sealed = bundle_from_rows(Path(args.rebundle), Path(args.bundle), track=args.track,
                                  corpus_name=args.corpus_name, provenance=args.provenance,
                                  redistribution=args.redistribution,
                                  engine_commit=args.rebundle_engine_commit,
                                  hardware_class=args.rebundle_hardware_class)
        print(f"bundle sealed at {sealed}")
        return 0
    if not args.directories and not args.manifest:
        parser.error("directories are required unless --rebundle or --manifest is given")
    if args.manifest and not args.roots:
        parser.error("--manifest needs --roots <dir>... to resolve the files under")
    if args.meshability and not args.mesh_probe:
        parser.error("--meshability needs --mesh-probe: the headline verdict is the mesh probe's")

    files: list[Path] = []
    sample_entries: list[dict] = []
    if args.manifest:
        try:
            files, sample_entries = manifest_files(
                Path(args.manifest), [Path(r) for r in args.roots], sample=args.sample,
                seed=args.seed, shard=args.shard, exclude=[Path(p) for p in args.exclude])
        except (OSError, ValueError) as exc:
            print(f"--manifest is not usable: {exc}", file=sys.stderr)
            return 2
    for directory in args.directories:
        for path in sorted(Path(directory).rglob("*")):
            if path.suffix.lower() in _SUFFIXES and path.is_file():
                files.append(path)
    if args.limit:
        files = files[: args.limit]
    if not files:
        print("no .step/.stp files found", file=sys.stderr)
        return 2

    try:
        goal = repair_goal(args.goal)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # BEFORE THE SETTINGS ARE READ, because the posture is an environment switch the settings
    # module reads once at import - the same contract as the CAD_REPAIR_ defaults at the top of
    # this file. A run that asked for nothing installs nothing and reads exactly today's settings.
    try:
        sink, note = install_investigator(args.investigator, transcripts=args.transcripts,
                                          replay=args.investigator_replay)
    except (OSError, ValueError) as exc:
        print(f"--investigator-replay is not usable: {exc}", file=sys.stderr)
        return 2
    if sink is not None:
        print(f"investigator={investigator_posture(args.investigator)}"
              + (f"  transcripts -> {args.transcripts}" if args.transcripts else "")
              + (f"  replaying {args.investigator_replay}" if args.investigator_replay else ""))
    if note:
        print(f"  {note}", file=sys.stderr)

    import meshpipeline.settings.cad_repair as rcfg
    repair_settings = repair_settings_of(rcfg)
    print("  ".join(f"{k}={v}" for k, v in repair_settings.items()))
    print(f"{len(files)} file(s)\n")

    # THE RESOLVED OPTIONS, frozen verbatim into a bundle's settings.json: the CLI knobs and the
    # repair configuration this run actually saw - everything another engineer needs to re-run it.
    settings = {
        "directories": list(args.directories), "limit": args.limit, "planner": args.planner,
        "reply_cache": args.reply_cache, "mesh_probe": args.mesh_probe,
        **({"meshability": True} if args.meshability else {}),
        "cad_repair": repair_settings,
        # THE CATALOGUE THIS RUN KNEW. summary.catalogue.never_exercised is the one aggregate
        # field the rows cannot re-derive on their own, so it is recorded here - under the
        # settings digest - and a later verification recomputes against it instead of against
        # however many operations the engine has grown by then.
        "operations_catalogue": operation_catalogue(),
        # THE V3 POLICY THIS RUN MEASURED (gym.py's fingerprint over the source it ran), sealed
        # under the digest: `gym.py record` labels the evidence with THIS, never with whatever
        # checkout records it later.
        "policy_fingerprint": policy_fingerprint(),
    }

    if args.force_repair:
        settings["force_repair"] = True  # only when set: a run without it keeps its old digest
    # ONLY WHEN SET, for the same reason: a run that states none of these keeps its old digest,
    # and one that states them cannot be confused with a different sample of the same corpus.
    if args.goal:
        settings["goal"] = args.goal
    settings.update(investigator_settings(args.investigator, args.transcripts,
                                          args.investigator_replay))
    if args.manifest:
        settings.update(manifest=args.manifest, roots=list(args.roots),
                        sample=args.sample, seed=args.seed, shard=args.shard,
                        exclude=list(args.exclude), sample_size=len(sample_entries))

    # A real directory rather than a per-run temp one: the repaired files ARE the evidence, and
    # a reviewer wants to open them after the run rather than watch them be deleted.
    scratch = scratch_dir()
    scratch.mkdir(parents=True, exist_ok=True)
    print(f"repaired files are written to {scratch}")

    cache = None
    if args.planner != "off":
        try:
            cache, note = install_planner_router(args.reply_cache)
        except CacheError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"planner={args.planner}  {note}")

    # ROWS STREAM INTO THE BUNDLE AS THEY ARE PRODUCED; the seal at the end adds the manifest,
    # settings and the recomputed summary (see public-benchmarks.md, First Implementation Slice).
    # AN AI POSTURE WITH NO MODEL is the rules author under another name: say so loudly, and mark
    # every row, so the run cannot be read as an AI measurement.
    row_marks: dict = {}
    gap = ai_posture_without_router(rcfg)
    if gap:
        bar = "!" * 78
        print(f"{bar}\nWARNING: {gap}\n{bar}", file=sys.stderr)
        row_marks["investigator_status"] = "no_router"
    writer = benchmark_bundle.BundleWriter(Path(args.bundle)) if args.bundle else None
    rows = run_files(files, scratch, mode=args.planner, loop=args.loop, bundle=writer,
                     mesh_probe=args.mesh_probe, force_repair=args.force_repair,
                     goal=goal, sink=sink, entries=sample_entries, isolate=args.isolate,
                     file_timeout_s=args.file_timeout_s,
                     tolerant_delivery=args.tolerant_delivery, row_marks=row_marks,
                     meshability=args.meshability)
    if sink is not None:
        sink.close()

    # ONE AGGREGATE. Every section printed below reads this dict, and the bundle seal recomputes
    # the same function over the same rows, so the console and the evidence cannot disagree.
    summary = corpus_summary(rows, args.planner)

    # THE HEADLINE IS THE PRECISE VOCABULARY. The old `outcomes` tally is still in summary.json
    # for anything that reads it, but it is not what a human is shown: a column saying
    # `refused: 66` where 65 parts were never refused by anything is the reporting defect this
    # replaces. The coarse tally is printed underneath, named for what it is.
    print("\n== summary (how each run ended) ==")
    for key, count in sorted(summary["endings"].items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {key}")
    print("\n== the same runs in the older, coarser words (kept for compatibility) ==")
    for key, count in sorted(summary["outcomes"].items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {key}")

    total_fixed = summary["repaired_by_code"]
    if total_fixed:
        print("\n== defects actually repaired, by kind (credited by re-inspection) ==")
        for code, count in sorted(total_fixed.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>4}  {code}")
    else:
        print("\n  no defect was repaired in this corpus")

    print("\n== what triage advised ==")
    for route, count in sorted(summary["routes"].items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {route}")

    print("\n== why the loop stopped ==")
    stops = summary["stopped_because"]
    for reason, count in sorted(stops.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {reason}")
    if not stops:
        print("  no file reached the loop")

    print("\n== operations, kept vs rolled back ==")
    # THE PLANNER'S HIT RATE, PER OPERATION. An operation proposed often and kept rarely is
    # either aimed at the wrong defect or missing from the catalogue in its useful form - and it
    # is the baseline a model planner gets scored against.
    # A DECLINE IS NOT A FAILURE. An operation that said no, with a reason, is recorded as
    # rolled_back by the session; the `declined:` prefix is what tells the two apart here, so
    # `rolled_back` below counts only operations that ran and did not repair their target.
    catalogue = summary["catalogue"]
    tally = catalogue["tally"]
    for name, counts in sorted(tally.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"  {name:<22} kept={counts['kept']:<4} rolled_back={counts['rolled_back']:<4} "
              f"declined={counts['declined']:<4} failed={counts['failed']}")
    if not tally:
        print("  nothing was attempted")

    print("\n== what the planner refused to attempt ==")
    by_reason = summary["refusals_by_reason"]
    for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {reason}")
    if not by_reason:
        print("  no refusals")

    print("\n== operations never exercised on this corpus ==")
    # Names the catalogue holds that no file gave the loop a reason to try. "Exercised" means the
    # operation actually ran: one that was proposed but only ever declined never touched a shape,
    # so it is listed separately below rather than counted as exercised.
    never = catalogue["never_exercised"]
    for name in never:
        print(f"        {name}")
    if not never:
        print("  every operation was attempted at least once")
    only_declined = catalogue["only_declined"]
    if only_declined:
        print("  proposed but only ever declined (never ran):")
        for name in only_declined:
            print(f"        {name}")

    print("\n== heals corroborated by the triangle check ==")
    heals = summary["heals"]
    print(f"  corroborated {heals['corroborated']} / kept {heals['kept']}, "
          f"unknown {heals['unknown']}")
    for name in heals["uncorroborated_files"]:
        print(f"        uncorroborated: {name}")
    for name in heals["unknown_files"]:
        print(f"        unknown (check could not run): {name}")

    if "meshing" in summary:
        meshing = summary["meshing"]
        print("\n== meshing, before and after repair ==")
        print(f"  meshed before {meshing['meshed_before']} / {meshing['files']}, "
              f"after {meshing['meshed_after']}  "
              f"(newly meshed {meshing['newly_meshed']}, newly broken {meshing['newly_broken']})")
        for kind, count in meshing["still_failing_by_kind"].items():
            print(f"  {count:>4}  still failing: {kind}")
        if "delivered_via_tolerant_mesher" in meshing:
            print(f"  separately, NOT a repair: delivered via the tolerant mesher "
                  f"{meshing['delivered_via_tolerant_mesher']} of "
                  f"{meshing['tolerant_delivery_attempted']} attempted")

    if "goal" in summary:
        scores = summary["goal"]
        print(f"\n== the {scores['profile']} finishing verdict ==")
        print(f"  kernel clean  {scores['kernel_clean']} / {scores['files']}")
        print(f"  tier 2        meshes {scores['tier2_meshes']}, refuses "
              f"{scores['tier2_refuses']}, no answer {scores['tier2_no_answer']}, "
              f"not run {scores['not_run']}")
        print(f"  score mean    {scores['score_mean']}")
        for tier, count in scores["grind_by_tier"].items():
            print(f"  {count:>4}  grind: {tier}")
        for verdict, count in scores.get("tolerant_routing", {}).items():
            print(f"  {count:>4}  tier 2b: {verdict}")

    if "investigator" in summary:
        block = summary["investigator"]
        print("\n== the investigator ==")
        print(f"  files {block['files']}, regions {block['regions']}, "
              f"tool calls {block['tool_calls']}, model calls {block['model_calls']}, "
              f"{block['seconds']}s")
        print(f"  tokens in {block['input_tokens']}, out {block['output_tokens']}")
        for end, count in block["ends"].items():
            print(f"  {count:>4}  ended: {end}")

    if args.planner != "off":
        _print_planner(summary["planner"], cache)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rows, indent=2, default=str))
        print(f"\nrows written to {args.json_out}")

    if writer is not None:
        sealed = seal_corpus_bundle(
            writer, rows, roots=[Path(d) for d in args.directories], files=files,
            settings=settings, track=args.track, corpus_name=args.corpus_name,
            provenance=args.provenance, redistribution=args.redistribution,
            planner_mode=args.planner)
        print(f"bundle sealed at {sealed}")
        written = write_bundle_sample(Path(sealed).parent, sample_entries, seed=args.seed)
        if written is not None:
            print(f"sample recorded at {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
