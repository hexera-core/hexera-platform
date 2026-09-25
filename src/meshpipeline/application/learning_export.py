# Responsibility: Turn one FINISHED job into the durable record the geometry agent's learning loop ingests.
# Owns: the export envelope's shape, which rows it is built from, and the delegation flag on every answer.
# Boundaries: a reader. It writes no platform row, runs no model, measures no geometry and judges nothing;
#             the verdicts and the split are the learning loop's (geometry_agent.learn.ingest_platform).
#             It selects no adapter: the object store is injected, and only the CLI entry point composes one.
# Collaborates with: persistence models for the rows, an injected ObjectStore for the delivered payload,
#                    agents.intake.recommendation for the one predicate that says a quote was a delegation.
"""THE SIGNALS ONLY A REAL JOB PRODUCES, written down before the job is forgotten.

`geometry_agent.learn` already ingests the corpus export and a recorded live evaluation run. Neither produces
the signals that are worth the most and cost nothing, because both are us talking to ourselves:

  - whether the CUSTOMER CORRECTED the proposal. The survey row already carries both halves - the question
    finder's per-place proposal in `asking.record` (`geometry_agent.ask.record.record_for`) and the customer's
    own choice in `answers` - and `answer: null` sits in every stored record row because nobody ever joined
    them. That join is this module's first job.
  - the REVIEWER's verdict and the quality gate's measured numbers.
  - the cells the builder DELIVERED against the cells the agent forecast.
  - what the LOOK said, beside the representation the job actually settled on.

WHY THE ENVELOPE IS PLATFORM VOCABULARY AND NOT AN OutcomeRecord. `geometry_agent` is a standalone package and
must never import `meshpipeline` (tests/unit/hygiene/test_architecture_boundaries.py). The dependency only runs
one way, so this module could import the agent's record schema - and deliberately does not. Two reasons, both
measured:

  1. THE RECORD NEEDS THINGS THIS PROCESS DOES NOT HAVE. A record's `split`, its labels and its `facts_sha256`
     come from the agent's own eval artefacts (`eval/split/export_split.json`, `eval/labels/export_labels.json`)
     and from `learn.store.facts_sha256`, which hashes the canonical measurement JSON. None of those is in the
     API image. Composing a record here would mean shipping half of one and calling it whole.
  2. THE TWO PACKAGES SPELL `facts_sha256` DIFFERENTLY AND IT WOULD HAVE BEEN SILENT. On this platform
     `geometry_surveys.facts_sha256` is the FILE's digest - `hexera.py:4563` reads it straight out of
     `facts["sha256"]` - and every row in the local database has `sha256 == facts_sha256`, verified on 8 rows.
     In `learn` the same name means the sha256 of the measurement's canonical JSON with `timings` removed
     (`learn/store.facts_sha256`), which is a different number and is what a record's file name and job key are
     built from. An exporter that filled the agent's field from the platform's column would key every record on
     the file instead of on the measurement, and a re-measurement that changed the answer would be invisible -
     the exact failure `ask.record.LEDGER_REQUIREMENTS` names for that column. So the envelope carries the
     platform's own two digests under the platform's own names (`source_sha256`, `facts_sha256_platform`) and
     the ingester derives the learn-side one itself, from the measurement document it is handed.

WHAT THIS MODULE DOES DECIDE, because only this side can: whether an answer's quote was a STANDING DELEGATION.
`geometry_survey.said_by_customer` accepts a quote from an earlier message only when
`agents.intake.recommendation.choice_deferred` says it is a deferral, so that function is the authority on
whether a stored answer rests on a choice or on "you decide". The value stored for a delegated answer is this
platform's own pick wearing the customer's authority; counting it as a confirmation would report agreement on
exactly the jobs where the customer said nothing about the substance. The flag is written here, beside the
answer, and the ingester refuses to score a row that does not carry it.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EXPORT_SCHEMA = "meshpipeline.learning_export.v1"

#: Only a job that reached a terminal state has an outcome. `pending` and `running` are refused by name rather
#: than exported with empty halves: a record whose reviewer block is empty because the review has not happened
#: is indistinguishable from one whose review failed, and the ingester would count both as "no verdict".
TERMINAL_STATUSES: tuple[str, ...] = ("succeeded", "failed")

#: The logical artifact that carries the delivered mesh's own numbers. The quality block is written by the
#: pipeline from the engine's manifest (`render.mesh_facts.quality`, `application.viewer_payload`) and is the
#: only durable place the DELIVERED cell count exists - `simulation_jobs.final_result` carries the verdict and
#: not a single metric, verified on job 489fa1ab.
VIEWER_DATA_KEY = "viewer_data"

_SLUG = re.compile(r"[^a-z0-9]+")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds")
    return str(value)


def _enum(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def case_name(original_filename: str, source_sha256: str) -> str:
    """The case name a platform job carries into the learning loop.

    PREFIXED `platform__` ON PURPOSE. `learn.store.record_paths` picks a record's side of the corpus split by
    matching its case name against the split file's two lists, so a platform case must be a name that is on
    neither. The prefix guarantees it whatever the customer called their file, including a file whose stem is
    exactly a corpus case name. The sha's first eight characters keep two different parts with the same
    filename apart.
    """
    stem = _SLUG.sub("_", Path(str(original_filename or "part")).stem.lower()).strip("_") or "part"
    return f"platform__{stem}__{str(source_sha256 or '')[:8]}"


def _delegated(words: str) -> bool:
    from meshpipeline.agents.intake.recommendation import choice_deferred
    return bool(choice_deferred(str(words or "")))


def answer_rows(answers: Any) -> list[dict[str, Any]]:
    """The survey's stored answers with one field added: whether each rests on a standing delegation.

    Nothing else is changed, nothing is dropped and no row is reordered - a retired row travels too, because
    the record still has to say what the customer said FIRST and what replaced it.
    """
    out: list[dict[str, Any]] = []
    for a in answers if isinstance(answers, list) else []:
        if not isinstance(a, dict):
            continue
        out.append({**a, "delegated": _delegated(a.get("words") or "")})
    return out


def quality_from_payload(payload: Any) -> dict[str, Any]:
    """The delivered mesh's own numbers, as the pipeline wrote them, copied and not recomputed.

    `criteria` is flattened to `{key: {measured, ok_when, passed, gating}}` because that is the only place
    several of the metrics exist: on job 489fa1ab the top-level block carries `cell_count` 258540, `engine`,
    `mesh_units` and `production_grade`, and `max_non_ortho` 64.9193, `layer_coverage_pct` 94.5776,
    `wall_faces` 8846, `skew_fraction` 0.0 and `fatal` [] are criteria rows.
    """
    if not isinstance(payload, dict):
        return {}
    q = payload.get("quality")
    q = q if isinstance(q, dict) else {}
    criteria: dict[str, Any] = {}
    for c in q.get("criteria") or []:
        if isinstance(c, dict) and c.get("key"):
            criteria[str(c["key"])] = {"measured": c.get("measured"), "ok_when": c.get("ok_when"),
                                       "passed": c.get("passed"), "gating": c.get("gating")}
    review = payload.get("review") if isinstance(payload.get("review"), dict) else {}
    return {"cell_count": q.get("cell_count"), "engine": q.get("engine"), "mesh_units": q.get("mesh_units"),
            "production_grade": q.get("production_grade"), "criteria": criteria,
            "review_verdict": review.get("verdict"), "attempt_reviewed": review.get("attempt_reviewed"),
            "rebuild_required": review.get("rebuild_required"),
            "mesh_available": payload.get("mesh_available")}


def envelope(*, job: Any, source: Any, interpretation: Any, measurement: Any, survey: Any,
             viewer_payload: Any = None, quality_unavailable: str = "") -> dict[str, Any]:
    """One finished job as the learning loop's input document. A pure function of the rows it is handed.

    EVERY HALF THAT IS MISSING SAYS SO BY NAME. A job with no survey row, no measurement, no interpretation or
    no delivered payload is still exported, with that half null and the reason in `absent`, because a job that
    ran without a survey is a real population and dropping it would make the batch describe only the jobs that
    went well. `absent` is what the ingester reports and what the runbook counts.
    """
    fr = job.final_result if isinstance(getattr(job, "final_result", None), dict) else {}
    doc = measurement.document if measurement is not None and isinstance(measurement.document, dict) else {}
    stamp = doc.get("stamp") if isinstance(doc.get("stamp"), dict) else {}
    step = survey.geometry_step if survey is not None and isinstance(survey.geometry_step, dict) else None
    absent: list[str] = []
    for name, row in (("geometry_source", source), ("interpretation", interpretation),
                      ("measurement", measurement), ("survey", survey)):
        if row is None:
            absent.append(f"no {name} row for this job")
    if survey is not None and step is None:
        absent.append("the geometry agent's step never ran on this job (geometry_step is null)")
    if viewer_payload is None:
        absent.append(quality_unavailable or f"the {VIEWER_DATA_KEY} payload was not read")

    src_sha = str(getattr(source, "sha256", "") or "")
    return {
        "schema": EXPORT_SCHEMA,
        "exported_at": _now(),
        "case": case_name(getattr(source, "original_filename", "") or "", src_sha),
        "absent": absent,
        "job": {
            "job_id": str(job.id),
            "status": _enum(job.status),
            "created_at": _iso(job.created_at),
            "started_at": _iso(job.started_at),
            "ended_at": _iso(job.ended_at),
            "current_attempt": job.current_attempt,
            "failed_reason": _enum(job.failed_reason),
            # THE VERDICT AND THE METRICS COME FROM TWO PLACES AND THAT IS NOT AN OVERSIGHT.
            # `final_result` is the durable terminal verdict and carries no metric at all; the metrics are in
            # the delivered payload. A record that reported one as the other would be a quality claim with no
            # measurement behind it.
            "final_result": dict(fr),
        },
        "source": None if source is None else {
            "geometry_source_id": str(source.id),
            "original_filename": source.original_filename,
            "suffix_hint": source.suffix_hint,
            "source_sha256": src_sha,
            "size_bytes": source.size_bytes,
            "bytes_available": bool(getattr(source, "bytes_available", False)),
        },
        "interpretation": None if interpretation is None else {
            "unit": interpretation.unit, "scale_to_metres": interpretation.scale_to_metres,
            "basis": interpretation.basis, "evidence": interpretation.evidence,
        },
        "measurement": None if measurement is None else {
            "status": measurement.status, "reason": measurement.reason, "purpose": measurement.purpose,
            #: THE PLATFORM'S OWN NAME FOR THE FILE DIGEST. See the module docstring: this is not the
            #: learning loop's `facts_sha256` and must never be copied into that field.
            "facts_sha256_platform": measurement.sha256,
            "facts_schema_version": measurement.facts_schema_version,
            "agent_git_sha": measurement.agent_git_sha,
            "measure_seconds": measurement.measure_seconds,
            "platform_sha": stamp.get("platform_sha"),
            "measurement_revision": stamp.get("measurement_revision"),
            #: the whole `hexera.report_measured` document, including its `facts` block. The ingester rebuilds
            #: GeometryFacts from it and derives the learn-side measurement digest itself.
            "document": doc,
        },
        "survey": None if survey is None else {
            "survey_id": str(survey.id),
            "session_id": None if survey.session_id is None else str(survey.session_id),
            "stage": survey.stage,
            "state_schema": survey.state_schema,
            "source_sha256": survey.sha256,
            "facts_sha256_platform": survey.facts_sha256,
            "agent_git_sha": survey.agent_git_sha,
            "look_queued": survey.look_queued,
            "composed_for": dict(survey.composed_for or {}),
            "asking": dict(survey.asking or {}),
            "asked": list(survey.asked or []),
            "answers": answer_rows(survey.answers),
            "late": dict(survey.late) if isinstance(survey.late, dict) else None,
            "planner_block": dict(survey.planner_block) if isinstance(survey.planner_block, dict) else None,
            "created_at": _iso(survey.created_at),
            "updated_at": _iso(survey.updated_at),
        },
        # The step, minus its ledger's events: the events are a median 24 kB per plan and every fact the
        # learning loop reads off them is already in `plan`, `envelope` and the answers.
        "step": None if step is None else {k: v for k, v in step.items() if k != "ledger"},
        "quality": quality_from_payload(viewer_payload),
    }


# -------------------------------------------------------------------------------------------------
# reading the rows
# -------------------------------------------------------------------------------------------------

async def _rows_for(db: Any, job: Any) -> tuple[Any, Any, Any, Any]:
    from sqlalchemy import select

    from meshpipeline.persistence.models import (
        GeometryInterpretationRow,
        GeometryMeasurement,
        GeometrySurvey,
    )
    source = job.geometry_source
    survey = measurement = interp = None
    if job.geometry_source_id is not None:
        survey = (await db.execute(select(GeometrySurvey).where(
            GeometrySurvey.geometry_source_id == job.geometry_source_id))).scalars().first()
        measurement = (await db.execute(select(GeometryMeasurement).where(
            GeometryMeasurement.geometry_source_id == job.geometry_source_id))).scalars().first()
    if job.geometry_interpretation_id is not None:
        interp = (await db.execute(select(GeometryInterpretationRow).where(
            GeometryInterpretationRow.id == job.geometry_interpretation_id))).scalars().first()
    return source, interp, measurement, survey


async def _viewer_payload(db: Any, job_id: Any, store: Any) -> tuple[Any, str]:
    """The delivered payload, or None and the reason. Never raises: a job whose bytes are gone is still a job."""
    from sqlalchemy import select

    from meshpipeline.persistence.models import Artifact
    if store is None:
        return None, "no object store was given to the exporter"
    row = (await db.execute(select(Artifact).where(
        Artifact.job_id == job_id, Artifact.logical_key == VIEWER_DATA_KEY))).scalars().first()
    if row is None:
        return None, f"the job delivered no {VIEWER_DATA_KEY} artifact"
    try:
        return json.loads(store.get_bytes(object_key=row.storage_key)), ""
    except Exception as exc:                       # noqa: BLE001 - a missing object is a fact, not a failure
        return None, f"the {VIEWER_DATA_KEY} object could not be read ({type(exc).__name__})"


async def export_job(db: Any, job_id: str, *, store: Any = None) -> dict[str, Any]:
    """One job's envelope. Raises ValueError when the job does not exist or has not finished."""
    from sqlalchemy import select

    from meshpipeline.persistence.models import SimulationJob
    job = (await db.execute(select(SimulationJob).where(SimulationJob.id == job_id))).scalars().first()
    if job is None:
        raise ValueError(f"no job {job_id}")
    status = _enum(job.status)
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"job {job_id} is {status}, not finished; the terminal statuses are "
                         f"{list(TERMINAL_STATUSES)}")
    source, interp, measurement, survey = await _rows_for(db, job)
    payload, why = await _viewer_payload(db, job.id, store)
    return envelope(job=job, source=source, interpretation=interp, measurement=measurement, survey=survey,
                    viewer_payload=payload, quality_unavailable=why)


def write_envelope(env: dict[str, Any], out_dir: Path) -> Path:
    """`<out_dir>/<job id>.json`, keyed on the job so a second export of one job replaces its own file."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{env['job']['job_id']}.json"
    path.write_text(json.dumps(env, indent=1, default=str, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


async def export_finished(db: Any, out_dir: Path, *, store: Any = None, limit: int = 50,
                          statuses: tuple[str, ...] = TERMINAL_STATUSES,
                          job_ids: list[str] | None = None, log=logger.info) -> dict[str, Any]:
    """Every finished job, newest first, as one envelope each. Idempotent: re-running replaces the same files."""
    from sqlalchemy import select

    from meshpipeline.persistence.models import JobStatus, SimulationJob
    if job_ids:
        ids = list(job_ids)
    else:
        wanted = [JobStatus(s) for s in statuses]
        rows = (await db.execute(select(SimulationJob.id).where(SimulationJob.status.in_(wanted))
                                 .order_by(SimulationJob.created_at.desc()).limit(limit))).scalars().all()
        ids = [str(i) for i in rows]
    report: dict[str, Any] = {"schema": EXPORT_SCHEMA, "out_dir": str(out_dir), "jobs": len(ids),
                              "written": [], "refused": {}, "absent": {}}
    for jid in ids:
        try:
            env = await export_job(db, jid, store=store)
        except ValueError as exc:
            report["refused"][jid] = str(exc)
            continue
        path = write_envelope(env, out_dir)
        report["written"].append(str(path))
        if env["absent"]:
            report["absent"][jid] = env["absent"]
        log("learning export: %s -> %s%s", jid, path,
            f" ({len(env['absent'])} halves absent)" if env["absent"] else "")
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(prog="python -m meshpipeline.application.learning_export",
                                 description="Export finished jobs for geometry_agent.learn ingest "
                                             "--source platform.")
    ap.add_argument("--out", required=True, help="the directory the envelopes are written to")
    ap.add_argument("--job", action="append", default=None, help="one job id; repeatable")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--no-quality", action="store_true",
                    help="do not read the delivered payload from object storage")
    a = ap.parse_args(argv)

    async def go() -> dict[str, Any]:
        # THE COMPOSITION HAPPENS HERE AND NOWHERE ELSE IN THIS FILE. `application/` may not select a concrete
        # adapter (tests/unit/hygiene/test_architecture_boundaries.test_product_and_application_import_no_
        # concrete_adapters, which caught the first version of this module importing `build_object_store`), so
        # the exporter's own functions take the store by injection and only this `__main__` entry point asks
        # runtime composition for one, then reads it back through the neutral accessor.
        from meshpipeline.contracts.object_storage import get_object_store
        from meshpipeline.persistence.session import get_db
        from meshpipeline.runtime.composition import install_adapters
        store = None
        if not a.no_quality:
            install_adapters()
            store = get_object_store()
        async with get_db() as db:
            return await export_finished(db, Path(a.out), store=store, limit=a.limit, job_ids=a.job,
                                         log=lambda *p: print(p[0] % p[1:]))

    report = asyncio.run(go())
    print(json.dumps({k: v for k, v in report.items() if k != "written"}, indent=1))
    print(f"{len(report['written'])} envelopes in {report['out_dir']}")
    return 0 if report["written"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
