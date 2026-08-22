# Responsibility: Move a finished run's artifacts into durable storage and bind them to the job.
# Owns: the contract checks, upload, the delivery record, conflict detection and orphan reporting.
# Boundaries: it delivers what the engine declared.
# Collaborates with: contracts/object_storage.py, application/execution_fence.py and the artifact repository.
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import tarfile
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from meshpipeline.contracts.object_storage import get_object_store
from meshpipeline.persistence.models import ArtifactType, FailedReason
from meshpipeline.persistence.repositories.artifact_repository import (
    ArtifactRepository,
    DeliveryOutcome,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrphanObject:
    object_key: str
    checksum: str | None
    size_bytes: int
    logical_key: str
    artifact_type: ArtifactType
    delivery_attempt: int


class RequiredArtifactDeliveryError(RuntimeError):

    def __init__(self, message: str, *, orphans: list[OrphanObject] | None = None) -> None:
        super().__init__(message)
        self.orphans = list(orphans or [])

    @property
    def orphaned_objects(self) -> list[str]:
        return [o.object_key for o in self.orphans]


class ArtifactDeliveryConflict(RequiredArtifactDeliveryError):
    pass


@dataclass(frozen=True)
class _Planned:
    artifact_type: ArtifactType
    logical_key: str
    object_key: str
    content_type: str
    local_path: Path
    required: bool


@dataclass
class ArtifactDeliveryReport:
    planned: list[str] = field(default_factory=list)
    required: list[str] = field(default_factory=list)
    delivered: list[dict] = field(default_factory=list)      # {type, storage_key, size_bytes, checksum}
    optional_failures: list[str] = field(default_factory=list)
    orphans: list[OrphanObject] = field(default_factory=list)

    @property
    def orphaned_objects(self) -> list[str]:
        return [o.object_key for o in self.orphans]

    def required_all_delivered(self) -> bool:
        _done = {d["type"] for d in self.delivered}
        return all(r in _done for r in self.required)


def _md5_hex(path: Path) -> str:
    h = hashlib.md5()  # noqa: S324 - integrity check against the store's own md5/etag, not a secret
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_integrity(stored, local_path: Path) -> str | None:
    local_size = local_path.stat().st_size
    if stored.size_bytes != local_size:
        return f"size mismatch (local {local_size}, stored {stored.size_bytes})"
    ck = (stored.checksum or "").strip().strip('"').lower()
    if ck and "-" not in ck and len(ck) == 32:
        try:
            int(ck, 16)
        except ValueError:
            return None
        if ck != _md5_hex(local_path):
            return "checksum mismatch - the stored object does not match the delivered bytes"
    return None


class BundleContractError(RequiredArtifactDeliveryError):
    pass


def resolve_engine(workspace: Path, declared_engine: str):
    from meshpipeline.engines.registry import UnknownEngineError, get_spec

    if not declared_engine:
        raise BundleContractError(
            "no engine on the durable execution - the deliverable contract cannot be resolved")
    try:
        spec = get_spec(declared_engine)
    except (UnknownEngineError, KeyError) as exc:
        raise BundleContractError(f"unknown engine {declared_engine!r}") from exc
    if not spec.implemented or not spec.deliverable:
        raise BundleContractError(f"engine {declared_engine!r} declares no deliverable")

    manifest_path = workspace / "mesh_manifest.json"
    if not manifest_path.exists():
        raise BundleContractError(
            f"{declared_engine}: mesh_manifest.json is missing - a successful engine run writes it")
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as exc:  # noqa: BLE001
        raise BundleContractError(f"{declared_engine}: mesh_manifest.json is unreadable") from exc
    mode = str(manifest.get("mesh_mode", "") or "")
    if mode and mode != spec.name:
        # Disagreement is an integrity failure, not a tie to break: one of the two is describing
        # a different run, and shipping either answer would mislabel the user's download.
        raise BundleContractError(
            f"manifest engine {mode!r} disagrees with the execution's engine {spec.name!r}")

    if not (workspace / spec.deliverable.marker).exists():
        raise BundleContractError(
            f"{spec.name}: declared marker {spec.deliverable.marker} is absent")
    return spec


def _member_ok(workspace: Path, member) -> str:
    rel = member.path
    if rel.startswith("/") or ".." in Path(rel).parts:
        return f"{rel}: declared path is not workspace-relative"
    target = workspace / rel
    if not target.exists():
        return f"{rel}: missing"
    resolved, root = target.resolve(), workspace.resolve()
    if not str(resolved).startswith(str(root)):
        return f"{rel}: resolves outside the workspace (symlink escape)"
    if member.kind == "dir":
        if not target.is_dir():
            return f"{rel}: declared a directory but is not one"
        if not any(target.iterdir()):
            return f"{rel}: required directory is empty"
    else:
        if not target.is_file():
            return f"{rel}: declared a file but is not one"
        if target.stat().st_size == 0:
            return f"{rel}: required file is empty"
    return ""


def verify_workspace_contract(workspace: Path, spec) -> None:
    problems = [why for m in spec.deliverable.members if m.required
                and (why := _member_ok(workspace, m))]
    if problems:
        raise BundleContractError(
            f"{spec.name}: deliverable contract not satisfied - " + "; ".join(problems))


def verify_archive(path: Path, spec) -> None:
    d = spec.deliverable
    if not path.exists() or path.stat().st_size == 0:
        raise BundleContractError(f"{spec.name}: archive is empty")
    with tarfile.open(path) as tf:
        names = tf.getnames()
        for ti in tf.getmembers():
            if ti.name.startswith("/") or ".." in Path(ti.name).parts:
                raise BundleContractError(f"{spec.name}: archive member escapes root: {ti.name}")
            if ti.issym() or ti.islnk():
                raise BundleContractError(f"{spec.name}: archive contains a link: {ti.name}")
            if not ti.name.startswith(f"{d.prefix}/"):
                raise BundleContractError(
                    f"{spec.name}: archive member outside the declared prefix: {ti.name}")
    present = set(names)
    missing = [m.path for m in d.members if m.required
               and f"{d.prefix}/{m.path}" not in present]
    if missing:
        raise BundleContractError(
            f"{spec.name}: archive is missing required member(s): {', '.join(missing)}")
    if f"{d.prefix}/{d.marker}" not in present:
        raise BundleContractError(f"{spec.name}: archive does not carry the declared marker")


def _build_plan(job_id: uuid.UUID, workspace: Path, scratch: list[Path],
                engine: str) -> list[_Planned]:
    plan: list[_Planned] = []

    # OPTIONAL: the boundary surface for quick viewing (the solver mesh lives in the bundle).
    # surface_mesh.msh is the boundary this run PRODUCED. mesh.msh is the CAD the mesher snapped to,
    # written for the reviewer to navigate by and kept for it. Prefer the produced one: serving the
    # other handed a user their own upload back - for a 3.3M-cell NACA 0012, 232 facets against the
    # 170,511 the run generated - under a label that reads as "here is my mesh".
    from meshpipeline.engines.surface_deliverable import SURFACE_MSH
    mesh_path = workspace / SURFACE_MSH
    if not (mesh_path.exists() and mesh_path.stat().st_size > 0):
        mesh_path = workspace / "mesh.msh"
    if mesh_path.exists() and mesh_path.stat().st_size > 0:
        from meshpipeline.artifact_keys import mesh_artifact_key
        plan.append(_Planned(ArtifactType.mesh, ArtifactType.mesh.value,
                             mesh_artifact_key(str(job_id)),
                             "application/octet-stream", mesh_path, required=False))

    # REQUIRED: the engine-declared deliverable bundle (spec.deliverable) - what the user is promised.
    # requiredness is decided by the ONE artifact policy, keyed on the engine whose
    # deliverable this is - never by a literal here.
    from meshpipeline.application.artifact_policy import is_required_class
    spec = resolve_engine(workspace, engine)
    verify_workspace_contract(workspace, spec)
    _recipe = spec.deliverable

    _fd, _tmpname = tempfile.mkstemp(prefix="bundle_", suffix=".tar.gz", dir=str(workspace))
    os.close(_fd)
    _tmp = Path(_tmpname)
    # DETERMINISTIC bundle: gzip mtime=0 and normalised tar entries (mtime/uid/gid/names),
    # so the SAME content yields the SAME checksum across rebuilds. Otherwise a same-attempt
    # retry would look like different content and trip the conflict guard.
    def _norm(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        ti.mtime = 0
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti

    with gzip.GzipFile(filename="", fileobj=open(_tmp, "wb"), mode="wb", mtime=0) as _gz:  # noqa: SIM115
        with tarfile.open(fileobj=_gz, mode="w") as _ctar:
            for _rel in sorted(m.path for m in _recipe.members):
                _cp = workspace / _rel
                if _cp.exists():          # optional members ship when present
                    _ctar.add(_cp, arcname=f"{_recipe.prefix}/{_rel}", filter=_norm)
    verify_archive(_tmp, spec)
    scratch.append(_tmp)

    # The VIEWER payload, rendered here because this process owns the engine workspace. The API
    # has no workspace in a hosted deployment, so anything it needs to show must become durable
    # before this one is cleaned up.
    from meshpipeline.application.viewer_payload import (
        VIEWER_LOGICAL_KEY,
        ViewerPayloadError,
        build_viewer_payload,
    )
    try:
        _payload = build_viewer_payload(workspace)
    except ViewerPayloadError as exc:
        raise BundleContractError(
            f"{spec.name}: the delivered mesh produces no viewer payload ({exc})") from exc
    _vfd, _vname = tempfile.mkstemp(prefix="viewer_", suffix=".json", dir=str(workspace))
    os.close(_vfd)
    _vpath = Path(_vname)
    _vpath.write_text(json.dumps(_payload, sort_keys=True, default=str))
    scratch.append(_vpath)
    plan.append(_Planned(ArtifactType.viewer_data, VIEWER_LOGICAL_KEY,
                         f"jobs/{job_id}/{VIEWER_LOGICAL_KEY}.json",
                         "application/json", _vpath,
                         required=is_required_class(spec.name, VIEWER_LOGICAL_KEY)))
    plan.append(_Planned(ArtifactType.mesh_bundle, ArtifactType.mesh_bundle.value,
                         f"jobs/{job_id}/{_recipe.bundle}",
                         "application/gzip", _tmp,
                         required=is_required_class(spec.name,
                                                    ArtifactType.mesh_bundle.value)))
    return plan


def _orphan_descriptor(p: _Planned, stored, delivery_attempt: int) -> OrphanObject:
    return OrphanObject(object_key=p.object_key, checksum=stored.checksum,
                        size_bytes=stored.size_bytes, logical_key=p.logical_key,
                        artifact_type=p.artifact_type, delivery_attempt=delivery_attempt)


async def upload_job_artifacts(db, job_id: uuid.UUID, workspace: Path, *,
                               engine: str,
                               delivery_attempt: int = 0,
                               execution_generation: int = 0) -> ArtifactDeliveryReport:
    # delivery_attempt is the caller's retry_count (final_state["retry_count"]), NOT the
    # SimulationJob.current_attempt column, which can lag the in-flight attempt.
    workspace = Path(workspace)
    if not workspace.exists():
        # A success path with no workspace cannot have produced the promised deliverable.
        raise RequiredArtifactDeliveryError(
            f"workspace {workspace} does not exist - no deliverable to publish")

    store = get_object_store()
    artifact_repo = ArtifactRepository()
    scratch: list[Path] = []
    report = ArtifactDeliveryReport()

    try:
        plan = _build_plan(job_id, workspace, scratch, engine)
        report.planned = [p.artifact_type.value for p in plan]
        report.required = [p.artifact_type.value for p in plan if p.required]

        # FAIL CLOSED: a succeeded mesh run promises a deliverable. No required artifact in the plan
        # (e.g. the bundle marker never materialised) is a delivery failure, not an empty success.
        if not report.required:
            raise RequiredArtifactDeliveryError(
                f"no required deliverable was produced for job {job_id} - refusing to report success")

        for p in plan:
            # 1: local source must be a regular file (never follow a symlink out of the workspace).
            if not (p.local_path.is_file() and not p.local_path.is_symlink()):
                _msg = f"required source missing for {p.artifact_type.value}: {p.local_path}"
                if p.required:
                    raise RequiredArtifactDeliveryError(_msg)
                report.optional_failures.append(p.artifact_type.value)
                continue

            # 2: upload. A required upload failure aborts delivery (no orphan - nothing was stored).
            try:
                stored = store.upload_file(local_path=p.local_path, object_key=p.object_key,
                                           content_type=p.content_type)
            except Exception as exc:  # noqa: BLE001
                logger.warning("artifact_uploader: upload failed for %s key=%s: %s",
                               p.artifact_type.value, p.object_key, exc)
                if p.required:
                    raise RequiredArtifactDeliveryError(
                        f"required upload failed for {p.artifact_type.value}: {exc}") from exc
                report.optional_failures.append(p.artifact_type.value)
                continue

            _orphan_of = _orphan_descriptor(p, stored, delivery_attempt)

            # 3: integrity - the stored bytes must match the local file (size always; checksum when
            # the store reports an md5-shaped one). A corrupt/truncated object is NOT ready.
            _bad = _verify_integrity(stored, p.local_path)
            if _bad:
                logger.warning("artifact_uploader: integrity check failed for %s key=%s: %s",
                               p.artifact_type.value, p.object_key, _bad)
                _o = _reconcile_or_delete(store, _orphan_of, report)
                if p.required:
                    raise RequiredArtifactDeliveryError(
                        f"required artifact {p.artifact_type.value} failed integrity: {_bad}",
                        orphans=[_o] if _o else [])
                report.optional_failures.append(p.artifact_type.value)
                continue

            # 4: register the Artifact row ATOMICALLY (INSERT … ON CONFLICT with an attempt CAS): the
            # DB, not a prior read, resolves concurrency, so two workers cannot duplicate a row and a
            # stale attempt cannot overwrite the winner. A row failure after a successful upload is an
            # ORPHAN: best-effort delete, else a durable reconciliation record.
            try:
                outcome = await artifact_repo.deliver_artifact(
                    db, job_id=job_id, logical_key=p.logical_key, artifact_type=p.artifact_type,
                    storage_key=stored.object_key, size_bytes=stored.size_bytes,
                    checksum=stored.checksum, delivery_attempt=delivery_attempt,
                    execution_generation=execution_generation)
            except Exception as exc:  # noqa: BLE001
                logger.warning("artifact_uploader: row write failed for %s key=%s: %s "
                               "(object stored - reconciling)", p.artifact_type.value, p.object_key, exc)
                _o = _reconcile_or_delete(store, _orphan_of, report)
                if p.required:
                    raise RequiredArtifactDeliveryError(
                        f"required artifact {p.artifact_type.value} could not be registered: {exc}",
                        orphans=[_o] if _o else []) from exc
                report.optional_failures.append(p.artifact_type.value)
                continue

            if outcome == DeliveryOutcome.conflict:
                # A different-content ready row exists at the same attempt: never overwrite it.
                logger.error("artifact_uploader: delivery CONFLICT for %s key=%s - a different ready "
                             "artifact already exists", p.artifact_type.value, p.object_key)
                _o = _reconcile_or_delete(store, _orphan_of, report)
                if p.required:
                    raise ArtifactDeliveryConflict(
                        f"required artifact {p.artifact_type.value} conflicts with an existing ready "
                        "deliverable - refusing to overwrite", orphans=[_o] if _o else [])
                report.optional_failures.append(p.artifact_type.value)
                continue

            # created / updated / idempotent / superseded → the logical artifact IS ready (superseded
            # means a NEWER attempt already delivered it, which still satisfies the contract).
            report.delivered.append({"type": p.artifact_type.value, "storage_key": stored.object_key,
                                     "size_bytes": stored.size_bytes, "checksum": stored.checksum,
                                     "outcome": outcome.value})
            logger.info("artifact_uploader: delivered %s (%d bytes, %s) - key=%s",
                        p.artifact_type.value, stored.size_bytes, outcome.value, stored.object_key)
    finally:
        for _t in scratch:
            try:
                _t.unlink()
            except Exception:  # noqa: BLE001
                pass

    return report


def _reconcile_or_delete(store, orphan: OrphanObject,
                         report: ArtifactDeliveryReport) -> OrphanObject | None:
    try:
        store.delete_object(object_key=orphan.object_key)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("artifact_uploader: could not delete orphan object %s: %s - durable "
                     "reconciliation record required", orphan.object_key, exc)
        report.orphans.append(orphan)
        return orphan


# #
# DELIVERY OF A SUCCEEDED RUN - deliver the mesh, or downgrade the run.
# Extracted from application/pipeline_run._run_async, where the retry loop and its downgrade sat as
# ~91 inline lines. One change reason: a succeeded mesh run MUST deliver its required artifact, and
# a run whose mesh cannot reach the user did not succeed. Reporting `succeeded` for an undelivered
# mesh hands the user a result they cannot fetch, so the downgrade belongs with the delivery it
# judges - not in the orchestrator that merely sequences it.
# ORDERING CONTRACT, unchanged: deliver FIRST, commit status ONCE afterwards. A client must never
# see `succeeded` before the mesh is fetchable, and an upload failure must never require flipping an
# already-committed `succeeded` back to `failed`.
# #

_DELIVERY_ATTEMPTS = 3


class RunDelivery(NamedTuple):

    delivered: list                          #: artifact rows the run may report; EMPTY on failure
    succeeded: bool
    failed_reason: FailedReason | None       #: set only when the run must be downgraded
    error: Exception | None


async def deliver_succeeded_run(session_factory, *, job_id: str, owner_id: str, workspace: str,
                                engine: str, delivery_attempt: int, execution_generation: int,
                                jlog, publish, persist_orphans) -> RunDelivery:
    from pathlib import Path as _Path

    from meshpipeline.errors import (
        FailureClass,
        classify_exception,
        failed_reason_for,
        record_dead_letter,
    )
    upload_exc: Exception | None = None
    delivered: list = []

    # No workspace to deliver FROM is itself a delivery failure - never a silent success.
    if not workspace:
        upload_exc = RuntimeError("no workspace to deliver artifacts from")
    else:
        for attempt in range(_DELIVERY_ATTEMPTS):
            try:
                async with session_factory() as adb:
                    report = await upload_job_artifacts(
                        adb, uuid.UUID(job_id), _Path(workspace),
                        # the engine the EXECUTION resolved, not one guessed from the workspace:
                        # two engines share a deliverable marker
                        engine=engine,
                        delivery_attempt=delivery_attempt,
                        execution_generation=execution_generation,
                    )
                    await adb.commit()
                # Any orphan whose immediate delete failed gets a DURABLE reconciliation record
                # (survives restart; a bounded reconciler cleans/adopts it safely).
                await persist_orphans(owner_id, job_id, getattr(report, "orphans", []))
                # Defence in depth: a normal return already guarantees every required artifact was
                # delivered (the uploader raises otherwise), but assert it so a future change cannot
                # quietly reintroduce a partial success.
                if not report.required_all_delivered():
                    raise RuntimeError(
                        "delivery report missing a required artifact despite normal return")
                delivered = report.delivered
                # Delivery succeeds at most once per run, so the announcement is ONE operation
                # however many upload attempts preceded it.
                publish(job_id).note(
                    f"Packaged your mesh - {len(delivered)} file(s) ready to download",
                    op_id="delivered")
                return RunDelivery(delivered, True, None, None)
            except Exception as exc:                      # noqa: BLE001 - classified below
                upload_exc = exc
                # Persist any orphans the failed attempt reported, so an undeletable object is
                # durably reconcilable even on the failure path.
                await persist_orphans(owner_id, job_id, getattr(exc, "orphans", []))
                jlog.warning("Artifact upload attempt %d/%d failed: %s",
                             attempt + 1, _DELIVERY_ATTEMPTS, exc)
                if attempt < _DELIVERY_ATTEMPTS - 1:
                    await asyncio.sleep(2 ** attempt)

    # DELIVERY FAILED (store unreachable, a required deliverable that could not be uploaded AND
    # registered, or no workspace at all). Downgrade to a SYSTEM failure; the caller's single status
    # commit records it. Reporting `succeeded` would hand the user a result they cannot fetch.
    sf = classify_exception(upload_exc, "minio") if upload_exc else None
    fc = sf.failure_class if (sf and sf.failure_class.is_system) else FailureClass.DEPENDENCY_DOWN
    jlog.error("Artifact delivery FAILED - marking job FAILED (delivery failure, not success) - "
               "job_id=%s: %s", job_id, upload_exc)
    try:
        reason = FailedReason(failed_reason_for(fc))
    except ValueError:
        reason = FailedReason.unhandled
    record_dead_letter(job_id, fc, "minio", f"artifact delivery failed: {upload_exc}")
    try:
        from meshpipeline.metrics import failure as _mfail
        _mfail(fc.value, "minio")
    except Exception:                                     # noqa: BLE001 - metrics never fail a run
        pass
    # The terminal verdict is application-rendered by the caller (delivery_failed category); no
    # message is set here so a single renderer owns the closing.
    publish(job_id).error("Your mesh was built and reviewed, but we could not store it for "
                          "download - this is our fault, not your geometry's. Nothing has "
                          "been delivered.", op_id="delivery-failed")
    return RunDelivery([], False, reason, upload_exc)
