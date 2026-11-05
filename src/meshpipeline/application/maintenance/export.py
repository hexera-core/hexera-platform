# Responsibility: Export one finished run's conversation into the training corpus.
# Boundaries: runs only when data collection is enabled, and never fails a job - capture is fail-open by design.
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.runtime as rtcfg
from meshpipeline.capture.source import ExportSourceError, select_export_source
from meshpipeline.pipeline.enums import FailureSection

logger = logging.getLogger(__name__)


# The corpus label for a run whose last classified failure was in a given section. Keyed by
# FailureSection; a test asserts every member is present, so adding a section to the enum
# without labelling it fails the suite rather than silently degrading to "mesh_quality".
_SECTION_FAILURE_LABELS: dict[str, str] = {
    FailureSection.GEOMETRY: "geometry_error",
    FailureSection.DOMAIN:   "domain_too_small",
    FailureSection.MESH:     "mesh_quality",
    FailureSection.GROUPS:   "patch_assignment",
    FailureSection.LAYERS:   "layer_configuration",
    FailureSection.MANIFEST: "manifest_invalid",
    FailureSection.PATCHES:  "patch_assignment",
    FailureSection.TOPOLOGY: "wrong_topology",
}

# OOM signatures in real process/kernel stderr. `\boom\b` so the bare token matches the
# kernel's "oom-killer" but not "room"/"zoom"; the phrases are already specific.
_OOM_RE = re.compile(
    r"\boom[-_ ]?kill|out of memory|cannot allocate memory|std::bad_alloc|"
    r"\bmemoryerror\b|\bkilled\b",
    re.IGNORECASE,
)



def _write_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("export: failed to write %s: %s", path, exc)


def _copy_file(src: Path, dst: Path) -> bool:
    try:
        if src.exists():
            if src.stat().st_size == 0:
                logger.warning("export: skipping zero-byte source file: %s", src)
                return False
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return True
    except Exception as exc:
        logger.warning("export: failed to copy %s → %s: %s", src, dst, exc)
    return False


# Coarse geometry class for corpus stratification. Matched on WHOLE WORDS: a substring
# scan labelled a 90-degree pipe elbow a "ground_vehicle" because "carve" contains "car".
# Ordered - the first category with a matching word wins.
_OBJECT_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("rocket",          ("rocket", "missile")),
    ("airfoil",         ("wing", "naca", "airfoil", "aerofoil")),
    ("ground_vehicle",  ("car", "vehicle", "sedan", "suv", "truck")),
    ("uav",             ("drone", "uav", "quadcopter")),
    ("spacecraft",      ("satellite", "spacecraft")),
    ("marine_vessel",   ("submarine", "hull", "ship")),
    ("building",        ("building", "structure", "facade")),
    ("turbomachinery",  ("turbine", "blade", "compressor")),
    ("piping",          ("pipe", "elbow", "duct", "manifold", "nozzle")),
    ("vascular",        ("vessel", "artery", "aorta", "lumen", "vascular")),
)


def _derive_object_category(source_filename: str, domain: str = "") -> str:
    raw = re.findall(r"[a-z]+", f"{os.path.basename(source_filename or '')} {domain}".lower())
    words = set(raw) | {w[:-1] for w in raw if w.endswith("s") and len(w) > 3}
    for category, keywords in _OBJECT_CATEGORIES:
        if words & set(keywords):
            return category
    return "unknown"



def export_conversation_sample(
    job_id: str,
    state: dict,
    created_at: str | None = None,
    ended_at: str | None = None,
    owner_id: str = "",
) -> dict:
    if not polcfg.MODES.data_collection_enabled:
        logger.info("export_conversation_sample: data collection disabled - nothing to export for job_id=%s", job_id)
        return {"status": "disabled",
                "message": "data collection is disabled (DATA_COLLECTION_ENABLED=false) - no sample was exported"}
    try:
        return _export(job_id, state, created_at=created_at, ended_at=ended_at,
                       owner_id=owner_id or str(state.get("user_id", "") or ""))
    except ExportSourceError as exc:
        logger.error(
            "export_conversation_sample: export_source_error for job_id=%s - %s",
            job_id, exc,
        )
        return {"status": "export_source_error", "reason": str(exc), "job_id": job_id}
    except Exception as exc:
        logger.error("export_conversation_sample: FAILED for job_id=%s - %s", job_id, exc, exc_info=True)
        return {"status": "error", "job_id": job_id}


def _write_deliverable(sample_dir: Path, workspace: str, engine: str) -> dict:
    import tarfile
    out: dict = {"bundle": None, "manifest": False, "preview": False}
    if not workspace:
        return out
    ws = Path(workspace)
    if not ws.is_dir():
        return out
    dst = sample_dir / "deliverable"
    dst.mkdir(exist_ok=True)
    try:
        from meshpipeline.engines.registry import get_spec
        dl = get_spec(engine).deliverable
    except Exception as exc:  # noqa: BLE001 - unknown engine must not sink the export
        logger.warning("export: no deliverable recipe for engine %r: %s", engine, exc)
        dl = None

    if dl and (ws / dl.marker).exists():
        bundle_path = dst / dl.bundle
        try:
            with tarfile.open(bundle_path, "w:gz") as tf:
                for rel in (m.path for m in dl.members):
                    src = ws / rel
                    if src.exists():
                        tf.add(str(src), arcname=f"{dl.prefix}/{rel}")
            out["bundle"] = dl.bundle
        except Exception as exc:  # noqa: BLE001
            logger.warning("export: failed to tar deliverable: %s", exc)
    else:
        logger.warning("export: deliverable marker missing in workspace %s - no case bundled", ws)

    # bundle ONLY: the manifest lives in the executor event payload + the
    # workspace snapshot; the preview lives in data/jobs/<job-id>/preview.msh
    return out


def _copy_renders(sample_dir: Path, review_dirs: list) -> int:
    renders = sample_dir / "renders"
    n = 0
    for r_idx, rdir in enumerate(review_dirs, 1):
        src = Path(rdir) if rdir else None
        if not src or not src.is_dir():
            continue
        for item in sorted(src.iterdir()):
            if item.is_file() and item.suffix.lower() in (".png", ".jpg", ".jpeg"):
                renders.mkdir(exist_ok=True)
                if _copy_file(item, renders / f"review_{r_idx}_{item.name}"):
                    n += 1
    return n


def _render_corpus_projection(job_id: str, owner_id: str, sample_dir: Path) -> None:
    from meshpipeline.persistence.repositories import capture_repository as cap
    try:
        # ONE generation per sample. Capture deliberately keeps a superseded worker's records,
        # stamped with the generation that produced them; blending those with the live epoch would
        # hand a training consumer two different runs as one. The generation is read from the job
        # row ONCE here and held for the whole projection, so a takeover mid-render cannot move it.
        generation = cap.authoritative_generation(owner_id=owner_id, job_id=job_id)
        if generation is None:
            logger.warning(
                "export: no authoritative execution generation for job %s owned by %s - the corpus "
                "projection is skipped rather than exporting every generation as one sample",
                job_id, owner_id)
            return
        rows = cap.trusted_operations(owner_id=owner_id, job_id=job_id,
                                      execution_generation=generation)
    except Exception as exc:  # noqa: BLE001 - a projection is a convenience, not the export
        logger.warning("export: could not render the corpus projection for %s: %s", job_id, exc)
        return
    path = sample_dir / "events.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps({
                "schema_version": 2, "record_type": row["record_type"],
                "trace_id": job_id, "seq": row["seq"],
                # Spans are a diagnostic concept with no durable counterpart, but the
                # envelope consumers validate requires the field. Derived from the
                # durable sequence so it is stable across renders rather than random.
                "span_id": f'{row["seq"]:016x}', "parent_span_id": None,
                "ts": row["created_at"].isoformat(), "name": row["name"], "kind": row["kind"],
                "component": "event",
                "attributes": ({"attempt": row["attempt"]}
                               if row["attempt"] is not None else {}),
                # PROVENANCE. Which epoch produced this record, carried explicitly so a consumer
                # never has to infer it from the file it happens to sit in.
                "execution_generation": row["execution_generation"],
                "payload": row["payload"], "payload_sha256": row["payload_sha256"],
            }, ensure_ascii=False, default=str) + "\n")


def _export(
    job_id: str,
    state: dict,
    created_at: str | None = None,
    ended_at: str | None = None,
    owner_id: str = "",
) -> dict:
    # Resolve the source FIRST: an export that cannot be assembled must leave no directory
    # behind for a later reader to mistake for a sample.
    state, _export_source = select_export_source(job_id, state, owner_id=owner_id)
    logger.info("export: source=%s for job_id=%s", _export_source, job_id)

    # corpus/<job-id>/ - the training events themselves live in the durable capture
    # authority; this task writes the surrounding file record for the sample.
    sample_dir = Path(rtcfg.CORPUS_DIR) / job_id
    sample_dir.mkdir(parents=True, exist_ok=True)
    logger.info("export: writing corpus/%s", job_id)

    workspace   = state.get("openfoam_workspace", "")
    retry_count = state.get("retry_count", 0)
    engine      = state.get("engine", "") or ""
    # Provenance comes from the durable reference. The uploaded NAME is display text used to
    # guess an object category; the CHECKSUM is the identity an export is worth keeping.
    from meshpipeline.pipeline.geometry_state import geometry_ref
    _source_ref = geometry_ref(state)
    source_filename = _source_ref.original_filename if _source_ref else ""

 # workspace/attempt_N/ - the FILES the models saw, per attempt
    # Text/config/log/report files + review_N/ dirs + result.json. Bulk mesh
    # binaries are excluded (the final mesh is in deliverable/).
    _SKIP_DIRS = {"constant", "VTK", "0", "processor0", "__pycache__"}
    _SKIP_SUFFIXES = {".stl", ".fms", ".vtu", ".vtp", ".msh", ".gz", ".step",
                      ".stp", ".iges", ".igs", ".png", ".mp4", ".inp", ".bdf",
                      ".unv"}
    _MAX_SNAP_BYTES = 10 * 1024 * 1024
    snapped = 0
    if workspace:
        _attempts_root = Path(workspace).parent
        for n in range(1, (retry_count or 0) + 2):
            _ws = _attempts_root / f"attempt_{n}"
            if not _ws.is_dir():
                continue
            _dst_root = sample_dir / "workspace" / f"attempt_{n}"
            for src in _ws.rglob("*"):
                if not src.is_file():
                    continue
                rel = src.relative_to(_ws)
                if any(part in _SKIP_DIRS for part in rel.parts[:-1]):
                    # system/ dicts live under a skipped-looking tree? no -
                    # system/ is not in _SKIP_DIRS; constant/ (polyMesh) is.
                    continue
                if src.suffix.lower() in _SKIP_SUFFIXES:
                    continue
                try:
                    if src.stat().st_size > _MAX_SNAP_BYTES:
                        continue
                except OSError:
                    continue
                dst = _dst_root / rel
                if _copy_file(src, dst):
                    snapped += 1

 # deliverable/ (THE MESH, bundle only) + renders/
    _deliverable = _write_deliverable(sample_dir, workspace, engine)
    reviewer_review_dirs = state.get("reviewer_review_dirs", []) or []
    _n_renders = _copy_renders(sample_dir, reviewer_review_dirs)

 # episodes/ (one uniform record per agent; SFT is a CLI-derived view)
    from meshpipeline.capture.events import EventLog
    from meshpipeline.capture.trajectory import (
        build_episodes,
        episode_filename,
        read_conversation_jsonl,
    )
    events = EventLog(job_id, owner_id=owner_id).load()
    model_configs = state.get("agent_model_configs", {}) or {}
    from meshpipeline.agents.intake.agent import INTAKE_TOOLS
    from meshpipeline.agents.reviewer.tools import REVIEWER_TOOLS
    _agent_tools = {"intake": list(INTAKE_TOOLS), "reviewer": list(REVIEWER_TOOLS)}
    episodes = build_episodes(events, model_configs=model_configs,
                              read_conversation=read_conversation_jsonl,
                              agent_tools=_agent_tools)
    episodes_dir = sample_dir / "episodes"
    episodes_dir.mkdir(exist_ok=True)
    for ep in episodes:
        _write_json(episodes_dir / episode_filename(ep), ep)

 # job_summary - the FINAL record of the stream (events carries the payload;
 # the trace line is payload-free). Replaces sample.json. Scalars only.
    from meshpipeline.pipeline.outcome import classify_quality, normalize_verdict
    final_verdict = normalize_verdict(state.get("reviewer_verdict", ""))
    classifier_sections = state.get("classifier_sections", []) or []
    _all_stderrs = " ".join(state.get("executor_stderrs", [])).lower()
    sections_repaired: list[str] = []
    for sec in classifier_sections:
        if sec and sec not in sections_repaired:
            sections_repaired.append(sec)
    _section_failure_reason = ("none" if not (classifier_sections and classifier_sections[-1])
                               else _SECTION_FAILURE_LABELS.get(classifier_sections[-1], "mesh_quality"))
    quality = classify_quality(
        reviewer_verdict=final_verdict, api_failure=state.get("api_failure", ""),
        solvability_failed=bool(state.get("solvability_failed", False)),
        executor_ever_succeeded=any(state.get("executor_successes", [])),
    )
    failure_reason = {"succeeded": "none", "api_failure": "api_failure",
                      "unsolvable": "mesh_unsolvable"}.get(quality, _section_failure_reason)

    # The approved digest, taken from the reference rather than re-hashed from a workspace file.
    # That file is deleted with the workspace and, on a hosted run, was never on this machine -
    # so a record built from it was empty exactly when the export mattered most.
    _input_sha = _source_ref.sha256 if _source_ref else ""
    _prompt_hashes: dict[str, str] = {}
    try:
        import hashlib as _hl

        from meshpipeline.engines.registry import get_spec
        _prompt_hashes["engine_pack"] = _hl.sha256(
            get_spec(engine).system_prompt.encode()).hexdigest()[:16]
    except Exception:
        pass

    job_summary = {
        "schema_version": "4.0",
        "job_id":     job_id,
        "created_at": created_at,
        # A DURABLE fact about the job, never the moment of export. Falling back to now()
        # made the summary differ every time the same job was exported, which - now that
        # the summary carries a stable operation identity - quarantined it as a
        # disagreeing replay and dropped the job's own summary from training. An unknown
        # end time is also not something to fabricate into a training label.
        "completed_at": ended_at or None,
        "engine":     engine or None,
        "purpose":    state.get("purpose", "") or None,
        "input_kind": state.get("input_kind", "") or None,
        "dimensionality": state.get("dimensionality", "") or None,
        "original_filename": state.get("original_filename", "")
                             or os.path.basename(source_filename),
        "input_sha256": _input_sha,
        "object_category": _derive_object_category(source_filename, state.get("domain", "")),
        "final_verdict": final_verdict,
        "quality":       quality,
        "failure_reason": failure_reason,
        "attempts":      retry_count,
        "sections_repaired": sections_repaired,
        "is_api_failure": bool(state.get("api_failure")),
        "is_oom":        bool(_OOM_RE.search(_all_stderrs)),
        "agent_model_configs": model_configs,
        "prompt_hashes": _prompt_hashes,
    }
    from meshpipeline.capture import trace as _capture
    # A stable occurrence identity, because only identified records reach the durable authority.
    # Without one this summary - a training record like any other - was written to the diagnostic
    # trace and silently dropped from the corpus the export then rendered.
    _capture.add_event(job_id, "job_summary", job_summary, kind="summary",
                       attributes={"op_id": "job-summary"})

    # Render the JSONL projection downstream consumers still read (the episode builder and the
    # trace-export CLI). It is generated FROM the durable rows in their sequence, never appended
    # to live and never read back to decide what exists.
    _render_corpus_projection(job_id, owner_id, sample_dir)

    _sentinel = json.dumps({
        "complete": True, "job_id": job_id,
        "timestamp": datetime.now(UTC).isoformat(),
    })
    try:
        (sample_dir / ".export_complete").write_text(_sentinel, encoding="utf-8")
    except Exception as exc:
        logger.warning("export: failed to write .export_complete sentinel: %s", exc)

    logger.info("export: corpus/%s written (%d episodes, %d reviews, %d workspace files, deliverable=%s)",
                job_id, len(episodes), len(reviewer_review_dirs), snapped,
                _deliverable.get("bundle"))

    return {"status": "ok", "job_id": job_id,
            "episodes": len(episodes)}
