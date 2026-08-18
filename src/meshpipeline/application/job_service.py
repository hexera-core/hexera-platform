# Responsibility: Serve the job operations the API exposes: read a job, terminalize it, seed a dispute.
# Owns: the job read model and dispute seeding; post_terminal owns what a finished run records.
# Boundaries: it coordinates persistence and storage for the API; it runs no pipeline.
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.providers as provcfg
import meshpipeline.settings.runtime as rtcfg
from meshpipeline.contracts.pipeline_state import PipelineState
from meshpipeline.persistence.repositories.artifact_repository import ArtifactRepository
from meshpipeline.persistence.repositories.job_repository import JobRepository

logger = logging.getLogger(__name__)

job_repo      = JobRepository()
artifact_repo = ArtifactRepository()

_signed_url_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
_SIGNED_URL_CACHE_TTL_MARGIN = 60
_SIGNED_URL_CACHE_MAX = 2048 #: bound the per-process cache


def final_attempt_workspace(job_id: str):
    from pathlib import Path
    root = Path(rtcfg.WORKSPACE_BASE) / str(job_id)
    if not root.is_dir():
        return None

    def _trailing_int(name: str, default: int = -1) -> int:
        tail = name.rsplit("_", 1)[-1]
        return int(tail) if tail.isdigit() else default

    def _rank(d):
        return (_trailing_int(d.parent.name), _trailing_int(d.name))

    cands = [d for d in root.glob("generation_*/attempt_*")
             if (d / "mesh_manifest.json").exists()]
    return sorted(cands, key=_rank)[-1] if cands else None


class JobService:

    async def check_quotas(self, db: AsyncSession, owner_id: str) -> None:
        # Serialize the count-then-create per owner. Without this, two
        # concurrent submissions both pass the count and exceed the quota (the
        # count and the subsequent job/session insert happen in this same
        # transaction in every caller). A transaction-scoped Postgres advisory
        # lock keyed on the owner forces concurrent submitters to queue, so the
        # second one sees the first one's row. No-op on non-Postgres (tests).
        try:
            if db.bind and db.bind.dialect.name == "postgresql":
                from sqlalchemy import text
                await db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:o))"),
                    {"o": owner_id or ""},
                )
        except Exception as _exc:
            logger.warning("check_quotas: advisory lock unavailable (%s) - proceeding", _exc)

        user_active = await job_repo.count_active_for_owner(db, owner_id)
        if user_active >= polcfg.MAX_JOBS_PER_OWNER:
            raise ValueError(
                f"You already have {user_active} active job(s). "
                f"Wait for one to complete before submitting a new one "
                f"(limit: {polcfg.MAX_JOBS_PER_OWNER} per user)."
            )
        total_active = await job_repo.count_total_active(db)
        if total_active >= polcfg.MAX_CONCURRENT_JOBS:
            raise ValueError(
                f"The system is at capacity ({total_active} active jobs). "
                f"Please try again in a few minutes."
            )

    async def create_session(self, db: AsyncSession, owner_id: str) -> uuid.UUID:
        from meshpipeline.persistence.repositories.session_repository import SessionRepository
        session_repo = SessionRepository()
        session = await session_repo.create(db, owner_id)
        await db.flush()
        return session.id

    async def get_job(self, db: AsyncSession, job_id: uuid.UUID, owner_id: str):
        # Scoped in SQL: a foreign job id and a missing one are indistinguishable here, and no
        # other tenant's row is ever materialised inside this service.
        job = await job_repo.get_for_owner(db, job_id, owner_id)
        if not job:
            return None
        return job


    async def signed_url(self, storage_key: str) -> str:
        now = time.monotonic()
        cached_url, expiry = _signed_url_cache.get(storage_key, ("", 0.0))
        if cached_url and now < expiry:
            return cached_url

        from meshpipeline.contracts.object_storage import get_object_store
        ttl_s = provcfg.MINIO_SIGNED_URL_TTL
        store = get_object_store()
        url = await asyncio.to_thread(
            lambda: store.create_download_url(object_key=storage_key,
                                              expires_in=timedelta(seconds=ttl_s)))
        effective_ttl = max(0, ttl_s - _SIGNED_URL_CACHE_TTL_MARGIN)
        _signed_url_cache[storage_key] = (url, now + effective_ttl)
        _signed_url_cache.move_to_end(storage_key)
        while len(_signed_url_cache) > _SIGNED_URL_CACHE_MAX:
            _signed_url_cache.popitem(last=False)  # evict oldest
        return url


# #
# DISPUTE SEEDING - start a dispute run from the mesh the user actually flagged.
# This resolves the PARENT run's final attempt, reads its manifest and seeds the dispute run's
# state from it. It lives beside `final_attempt_workspace`, the parent-resolution it depends on.
# Nothing is COPIED or staged: `openfoam_workspace` is pointed at the parent's final attempt
# directory and two files are read from it. So there is no partial-copy state to clean up, and a
# failure raises before any state key is written, which is the property the tests below pin.
# WHY THE PARENT'S ENGINE WINS: a dispute rebuild must use the mesher the delivered mesh came from.
# The user-selected pin is applied BEFORE this, so this overwrite is deliberate and ordered.
# #


@dataclass(frozen=True)
class DisputeSeed:

    parent_job_id: str
    parent_workspace: Path
    parent_manifest: dict
    parent_engine: str
    flags: int

    def apply_to(self, state: PipelineState) -> None:
        from meshpipeline.engines.registry import resolve_engine_params

        state["openfoam_workspace"] = str(self.parent_workspace)
        state["mesh_manifest"] = self.parent_manifest
        state["engine"] = self.parent_engine
        state["executor_success"] = True
        state["engine_params"] = resolve_engine_params(
            self.parent_engine, self.parent_manifest.get("engine_params") or {})
        state["flow_topology"] = str(
            self.parent_manifest.get("flow_topology") or "").strip().lower()
        if not state.get("request_txt"):
            request = self.parent_workspace / "request.txt"
            if request.exists():
                state["request_txt"] = request.read_text(errors="replace")


def resolve_dispute_seed(user_dispute: Mapping, *, jlog) -> DisputeSeed:
    from meshpipeline.errors import FailureClass, SystemFailure

    # DEFENCE IN DEPTH. The wire schema refuses accept-with-flags, but a payload committed before
    # that rule, or one constructed by hand, reaches the graph through dispatch_payload rather than
    # through the schema. Entering the review graph with both would compose the human-flag axis and
    # promise a rebuild that accept mode never performs, so it is refused here too - classified,
    # not silently repaired, because neither dropping the flags nor forcing a rebuild is what the
    # engineer asked for.
    if str(user_dispute.get("mode", "")) == "accept" and (user_dispute.get("flags") or []):
        raise SystemFailure(
            "dispute_mode", FailureClass.INTERNAL,
            "dispute payload carries mode=accept with flagged regions; accept never rebuilds, "
            "so the two cannot be honoured together")

    parent_id = str(user_dispute.get("of_job_id", "")).strip()
    workspace = final_attempt_workspace(parent_id) if parent_id else None
    if workspace is None:
        raise SystemFailure(
            "dispute_workspace", FailureClass.INTERNAL,
            f"dispute of job {parent_id}: no reviewable workspace found "
            f"(purged or never delivered)")
    try:
        manifest = json.loads((workspace / "mesh_manifest.json").read_text())
    except Exception as exc:
        raise SystemFailure(
            "dispute_workspace", FailureClass.INTERNAL,
            f"dispute of job {parent_id}: manifest unreadable: {exc}") from exc
    engine = str(manifest.get("mesh_mode", "") or "")
    if not manifest.get("engine_params"):
        jlog.warning("dispute parent manifest has no engine_params - rebuild uses %s spec defaults",
                     engine)
    return DisputeSeed(parent_id, workspace, manifest, engine,
                       len(user_dispute.get("flags", []) or []))


def capture_dispute_context(job_id: str, seed: DisputeSeed, user_dispute: Mapping, *, jlog) -> None:
    try:
        from meshpipeline.capture.logger import TrainingLogger

        TrainingLogger(job_id).log("dispute_context", op_id="dispute-context", payload={
            "of_job_id": seed.parent_job_id,
            "n_flags": seed.flags,
            "comment": str(user_dispute.get("comment", ""))[:2000],
            "parent_engine": seed.parent_engine,
        })
    except Exception as exc:                       # noqa: BLE001 - capture never fails a run
        jlog.warning("dispute_context training event failed: %s", exc)


async def seed_dispute_run(state: PipelineState, user_dispute: Mapping, *,
                           job_id: str, jlog,
                           publish: Callable[[int], Awaitable[None]]) -> DisputeSeed:
    seed = resolve_dispute_seed(user_dispute, jlog=jlog)
    capture_dispute_context(job_id, seed, user_dispute, jlog=jlog)
    state["user_dispute"] = dict(user_dispute)
    seed.apply_to(state)
    jlog.info("Dispute run: parent=%s workspace=%s engine=%s flags=%d",
              seed.parent_job_id, seed.parent_workspace.name, seed.parent_engine, seed.flags)
    await publish(seed.flags)
    return seed
