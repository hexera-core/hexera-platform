# Responsibility: Move a finished run's artifacts into durable storage and bind them to the job.
# Owns: the contract checks, upload, the delivery record, conflict detection and orphan reporting.
# Boundaries: it delivers what the engine declared.
# Collaborates with: contracts/object_storage.py, application/execution_fence.py and the artifact repository.
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import re
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
    #: every object this call stored AND registered, as an orphan descriptor. If the registration
    #: transaction is then refused (the worker was fenced) these objects belong to no committed
    #: row, and the reconciler - which adopts an object a ready row owns and deletes one nobody
    #: owns - is told about each of them.
    stored: list[OrphanObject] = field(default_factory=list)

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
    # A GLOB HAS NO LITERAL TARGET. `processor*` never `exists()`, so checking it as a path
    # would report every decomposed delivery as missing its own mesh. A required glob is
    # satisfied by at least one match, and each match is then held to the same rules.
    if getattr(member, "is_glob", False):
        hits = sorted(workspace.glob(rel))
        if not hits:
            return f"{rel}: nothing matches"
        root = workspace.resolve()
        for hit in hits:
            if not str(hit.resolve()).startswith(str(root)):
                return f"{hit.name}: resolves outside the workspace (symlink escape)"
        return ""
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
    missing = [m.path for m in d.members if m.required and not m.is_glob
               and f"{d.prefix}/{m.path}" not in present]
    # A REQUIRED GLOB NEEDS A MATCH, not an exact name: `processor*` is satisfied by
    # processor0..N and by nothing else, and the count is per-run.
    missing += [m.path for m in d.members if m.required and m.is_glob
                and not fnmatch.filter(present, f"{d.prefix}/{m.path}")]
    if missing:
        raise BundleContractError(
            f"{spec.name}: archive is missing required member(s): {', '.join(missing)}")
    # ...AND WHAT IT HAS TO HAVE INSIDE IT. The member list is about paths and says nothing
    # about content: job b8f09ae9 shipped an "OpenFOAM case" whose `constant` and `system`
    # both existed and which contained no points, faces, owner or neighbour for either
    # region - 1.99 GB of viewer sidecars and a decimated surface. This is the engine's own
    # statement of what its format must carry (Deliverable.must_contain).
    hollow = [pat for pat in d.must_contain
              if not fnmatch.filter(present, f"{d.prefix}/{pat}")]
    if hollow:
        raise BundleContractError(
            f"{spec.name}: the archive carries no {', '.join(hollow)} - it has the shape of "
            f"a {d.label} and no mesh inside it")
    if f"{d.prefix}/{d.marker}" not in present:
        raise BundleContractError(f"{spec.name}: archive does not carry the declared marker")


def is_fea_workspace(workspace: Path) -> bool:
    """Whether the delivered mesh is an FEA plan's - read as the verdict reads it (the manifest's
    quality.fea.plan), not by a file a workspace may merely carry."""
    try:
        q = (json.loads((Path(workspace) / "mesh_manifest.json").read_text()) or {}).get("quality") or {}
    except (OSError, ValueError, AttributeError):
        return False
    if not isinstance(q, dict):
        return False
    fea = q.get("fea")
    return bool(isinstance(fea, dict) and fea.get("plan")) or str(q.get("quality_bar") or "") == "fea"


def fea_viewer_fallback(workspace: Path, why: str) -> dict:
    """The viewer payload of an FEA mesh whose surface could not be rendered: no surface to draw, the
    FEA result block (checks, solvers, downloads) and the note - the result screen still opens."""
    from meshpipeline.engines.fea_results import FEA_VIEW_MISSING

    logger.warning("FEA viewer: the 3D view was not built (%s); the mesh is delivered without it", why[:300])
    fea = None
    try:
        from meshpipeline.engines.fea_results import results
        fea = results(workspace)
    except Exception:  # noqa: BLE001 - the note still stands
        logger.warning("FEA viewer: the result block could not be built either", exc_info=True)
    surface: dict = {"kind": "stl", "mesh_units": "m", "patches": [], "view_note": FEA_VIEW_MISSING}
    if fea:
        surface["fea"] = {**fea, "view_note": FEA_VIEW_MISSING}
    return {"engine": "gmsh", "mesh_available": True, "surface": surface, "quality": {}, "review": None,
            "view_note": FEA_VIEW_MISSING}


def fea_export_plan(job_id: uuid.UUID, workspace: Path) -> list[_Planned]:
    """AN FEA MESH'S SOLVER FILES, one download each (FEA build step 6: mesh once, export many - the
    user picks the solver at download). Each is an optional `mesh` row under its own logical key;
    the bundle still carries them all. Nothing for any other run."""
    from meshpipeline.engines.fea_writers import EXPORT_ARTIFACTS
    if not (workspace / "fea_plan.json").exists():
        return []
    from meshpipeline.artifact_keys import export_key
    out = []
    for logical, file_name, _label in EXPORT_ARTIFACTS:
        src = workspace / file_name
        if src.exists() and src.stat().st_size > 0:
            out.append(_Planned(ArtifactType.mesh, logical, export_key(str(job_id), file_name),
                                "text/plain", src, required=False))
    return out


#: HOW THE DELIVERED BUNDLE IS COMPRESSED. Six, which is zlib's own default and the number
#: gcs_exchange.RESULT_ARCHIVE_LEVEL reached independently on the return archive - not Python's
#: `gzip` default of nine, which is what this used and what _build_plan's own note now records the
#: cost of.
#:
#: WHY SIX AND NOT THE FASTEST. On one thread the measured table in _build_plan would argue for
#: two: 98.0 MB/s against level 6's 26.8, for 4.27x against 5.82x. But the archive is no longer
#: written on one thread - `parallel_gzip` spreads it over every core the job has - so the lever
#: that was costing hours is the THREAD COUNT, and once it is pulled, level 6 buys 36% smaller
#: bundles for time the cores now absorb. 66.6 GB at 26.8 MB/s a core is about five minutes on
#: eight. The customer downloads this file and we store it; the smaller one is the better one as
#: soon as writing it is no longer the thing that loses the run.
#:
#: NOT the same name as gcs_exchange's, and not imported from it: that constant is for the archive
#: a MESH VM sends home and this one is for the bundle a CUSTOMER is handed. They agree today, and
#: a reason to move one is not automatically a reason to move the other.
BUNDLE_ARCHIVE_LEVEL = 6


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

    plan.extend(fea_export_plan(job_id, workspace))

    # REQUIRED: the engine-declared deliverable bundle (spec.deliverable) - what the user is promised.
    # requiredness is decided by the ONE artifact policy, keyed on the engine whose
    # deliverable this is - never by a literal here.
    from meshpipeline.application.artifact_policy import is_required_class
    spec = resolve_engine(workspace, engine)
    verify_workspace_contract(workspace, spec)
    _recipe = spec.deliverable

    # THE ONE THE MESHER ALREADY PACKED, IF IT PACKED ONE. `engines/bundled_delivery` builds this
    # bundle on the machine that has the mesh, for the reason its header measures: of the 124 GB job
    # 606af55e sent home, 123 GB was the per-rank trees that exist in this workspace only so that
    # this loop can pack them, and compressing them here means doing it through a 4.0-CPU quota -
    # about nineteen minutes a run now that the cells are in the bundle at all.
    #
    # IT IS STILL VERIFIED HERE, below, by exactly the same `verify_archive` call: a file found in a
    # workspace is evidence and not authority, and this is the side that answers to the customer.
    # `bundled_file` checks the record names this engine's own bundle and that the file is there at
    # the size that was recorded, so another engine's archive cannot be delivered under this label.
    _ready = None
    try:
        from meshpipeline.engines.bundled_delivery import bundled_file
        _ready = bundled_file(workspace, spec)
    except Exception:  # noqa: BLE001 - a missing or unreadable record means "build it here"
        _ready = None
    if _ready is not None:
        verify_archive(_ready, spec)
        logger.info("artifact_uploader: delivering the bundle the mesher packed (%d bytes) - %s",
                    _ready.stat().st_size, _ready.name)
        # THE SAME ORDER AS THE OTHER PATH - viewer, then bundle - because the plan is delivered in
        # order and the two paths must produce the same delivery, not merely the same artifacts.
        plan.extend(_viewer_plan(job_id, workspace, spec, scratch))
        plan.append(_Planned(ArtifactType.mesh_bundle, ArtifactType.mesh_bundle.value,
                             f"jobs/{job_id}/{_recipe.bundle}",
                             "application/gzip", _ready,
                             required=is_required_class(spec.name,
                                                        ArtifactType.mesh_bundle.value)))
        return plan

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

    # NOT `gzip.GzipFile`, WHOSE DEFAULT IS LEVEL 9 ON ONE CORE, which is what this was and what
    # cost job 57b613d7 its delivery. That run meshed a 283,410,955-cell cold plate in 3 h 53 m,
    # passed every gate, and then sat in THIS loop: at 17:48 UTC, 63 minutes after the graph
    # returned, it had consumed 12.57 GB of the recipe's 66.6 GB `constant` tree and was still
    # going. The whole bundle would have taken about eight and a half hours, during which
    # `_invoke_with_heartbeat` has already stopped both the in-process beat and the lease keeper
    # (pipeline_run.py, its `finally`), so the lease lapsed after 900 s, the stalled-job reaper
    # claimed a new generation, and a second 64-vCPU VM started on a mesh that was already built.
    # The row never reached `terminal_finalize`'s CAS, so it still read `running` with no
    # `ended_at` - which is what made it look stalled in the first place.
    #
    # MEASURED, 2026-11-15, on that mesh's own fluid `faces` file - a 419,430,400-byte slice taken
    # one third in, one thread, each level compressing the same bytes:
    #
    #     level   MB/s     ratio     the recipe's 66.6 GB at that rate
    #        1    87.0     4.07x     12m 44s
    #        2    98.0     4.27x     11m 18s
    #        5    51.6     5.71x     21m 27s
    #        6    26.8     5.82x     41m 20s
    #        9     3.7     5.89x     4h 59m 59s
    #
    # Level 9 is 23 times slower than level 2 to save a seventh of the bytes. And `faces` is the
    # KIND half: gcs_exchange.RESULT_ARCHIVE_LEVEL has the same measurement on the other files -
    # "0.7 against 16.9 on `owner`" - and `owner` and `neighbour` are 15.85 GB of this recipe,
    # which is why the 5 h above is a floor rather than an estimate.
    #
    # THE LEVEL AND THE THREADING ARE BOTH THAT MODULE'S ANSWER, NOT A NEW ONE. gcs_exchange
    # learned this twice already - its own note says "measuring against a tarfile default instead
    # of against this has been wrong twice - first its level-9 compresslevel, then its
    # single-threaded deflate stream" - and `parallel_gzip` is what it reached for: one gzip
    # member, cut on fixed CHUNK_BYTES boundaries and primed with the previous window, spread over
    # every core THE JOB has. This was simply the third place to make the same mistake, and it is
    # the place where the mesh is actually handed to the customer.
    #
    # STILL DETERMINISTIC, which this bundle's checksum depends on (see _norm above): the cut
    # points are fixed by `chunk_bytes` and the pieces are concatenated in order, so `worker_count`
    # changes how fast it is written and not one byte of what is written. The gzip header
    # parallel_gzip emits carries no mtime, for the same reason `mtime=0` was passed here.
    #
    # `mode="w|"` AND NOT `"w"`: ParallelGzipWriter is a write-only, non-seekable sink, which is
    # the streaming tar mode's contract. The nesting order matters too - the tar's end-of-archive
    # blocks have to be written before the gzip trailer that covers them, which is what the two
    # `with`s in this order do.
    from meshpipeline.engines.determinism import job_cpus
    from meshpipeline.parallel_gzip import ParallelGzipWriter, workers

    with _tmp.open("wb") as _fh, \
            ParallelGzipWriter(_fh, level=BUNDLE_ARCHIVE_LEVEL,
                               worker_count=workers(job_cpus())) as _gz, \
            tarfile.open(fileobj=_gz, mode="w|") as _ctar:
        # `expand` resolves glob members against the workspace, which is how a decomposed
        # mesh's `processor*` trees get in: the rank count is chosen per run, so no literal
        # member list can name them. Non-glob members are unchanged, present-or-skipped.
        for _rel in _recipe.expand(workspace):
            _cp = workspace / _rel
            if _cp.exists():              # optional members ship when present
                _ctar.add(_cp, arcname=f"{_recipe.prefix}/{_rel}", filter=_norm)
    verify_archive(_tmp, spec)
    scratch.append(_tmp)

    plan.extend(_viewer_plan(job_id, workspace, spec, scratch))
    plan.append(_Planned(ArtifactType.mesh_bundle, ArtifactType.mesh_bundle.value,
                         f"jobs/{job_id}/{_recipe.bundle}",
                         "application/gzip", _tmp,
                         required=is_required_class(spec.name,
                                                    ArtifactType.mesh_bundle.value)))
    return plan


def _viewer_plan(job_id: uuid.UUID, workspace: Path, spec, scratch: list[Path]) -> list[_Planned]:
    """The VIEWER payload, rendered here because this process owns the engine workspace. The API has
    no workspace in a hosted deployment, so anything it needs to show must become durable before
    this one is cleaned up.

    ITS OWN FUNCTION so that both bundle paths produce it identically. `_build_plan` either packs
    the bundle here or delivers the one the mesher packed (engines/bundled_delivery), and a viewer
    payload built on only one of those two paths is a mesh delivered without a view.
    """
    from meshpipeline.application.artifact_policy import is_required_class
    from meshpipeline.application.viewer_payload import (
        VIEWER_LOGICAL_KEY,
        ViewerPayloadError,
        build_viewer_payload,
    )
    # AN FEA MESH is delivered whether or not its 3D view can be built: the deck and the solver
    # files are the deliverable, and the result screen says the view is missing (Areen, 24 Oct)
    _fea = is_fea_workspace(workspace)
    try:
        _payload = build_viewer_payload(workspace)
    except ViewerPayloadError as exc:
        if not _fea:
            raise BundleContractError(
                f"{spec.name}: the delivered mesh produces no viewer payload ({exc})") from exc
        _payload = fea_viewer_fallback(workspace, str(exc))
    except Exception as exc:  # noqa: BLE001 - only for FEA: a view that cannot be built is a note
        if not _fea:
            raise
        _payload = fea_viewer_fallback(workspace, f"{type(exc).__name__}: {exc}")
    _vfd, _vname = tempfile.mkstemp(prefix="viewer_", suffix=".json", dir=str(workspace))
    os.close(_vfd)
    _vpath = Path(_vname)
    _vpath.write_text(json.dumps(_payload, sort_keys=True, default=str))
    scratch.append(_vpath)
    return [_Planned(ArtifactType.viewer_data, VIEWER_LOGICAL_KEY,
                     f"jobs/{job_id}/{VIEWER_LOGICAL_KEY}.json",
                     "application/json", _vpath,
                     required=is_required_class(spec.name, VIEWER_LOGICAL_KEY,
                                                "structural" if _fea else ""))]


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
            report.stored.append(_orphan_of)
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

# A DATABASE REFUSAL A RETRY CANNOT FIX.
# Measured on job 0ff6f9e5 (the 283,410,955-cell conjugate plate): the OpenFOAM case bundle was
# 10,616,027,998 bytes and artifacts.size_bytes was int4, so the row write was refused for the
# VALUE, which is the same on every attempt. The loop below retried the whole delivery three
# times - 21:37, 21:46, 21:56 - and each attempt first re-ran exact_surface on both regions
# (16,584,838 faces for the fluid, 485,526 for the solid) and re-uploaded mesh.msh and
# viewer_data.json, ~29 minutes spent to arrive at the identical error.
# SQLSTATE CLASS 22 (data exception) is the predicate, and it is the smallest one this incident
# supports: class 22 says the value and the column disagree, and neither of them changes while
# the worker sleeps. Both shapes this incident takes are class 22 - against an int4 column
# asyncpg refuses the BIND before the statement is sent (22000, "invalid input for query
# argument $8: 10616027998 (value out of int32 range)"), and with the model on BigInteger but
# the migration not yet applied the SERVER refuses it (22003, "integer out of range").
# NOT class 42 (undefined_column/undefined_table). That is what a half-rolled deploy looks like
# to the worker that reaches the new code first, and the retry is exactly what carries it across
# to the instance that has the migration; calling it terminal would convert a deploy window into
# failed jobs.
# NOT an exception class either. ArtifactDeliveryConflict is returned from two branches of
# deliver_artifact and the second is an explicit race ("a concurrent delete between the
# INSERT-conflict and this read"), which a retry is what resolves. The loop cannot tell those two
# branches apart; only deliver_artifact can.
_TERMINAL_SQLSTATE_CLASS = "22"

#: asyncpg names the PARAMETER it refused, never the column; SQLAlchemy carries the statement and
#: the bound row on the same error, and the two together give the column a name.
_BIND_ARGUMENT = re.compile(r"argument \$(\d+)")
_INSERT_COLUMNS = re.compile(r"INSERT INTO (\w+) \(([^)]*)\)", re.IGNORECASE)


def _cause_chain(exc: BaseException) -> list[BaseException]:
    # The loop never sees the DBAPI error itself: upload_job_artifacts raises
    # RequiredArtifactDeliveryError "from exc", and SQLAlchemy keeps the driver's own error on
    # `.orig` (one more hop: its `__cause__` is the asyncpg exception). Follow those two links and
    # no others - __context__ would drag in whatever merely happened to be in flight.
    out: list[BaseException] = []
    seen: set[int] = set()
    queue: list[BaseException | None] = [exc]
    while queue:
        cur = queue.pop(0)
        if not isinstance(cur, BaseException) or id(cur) in seen:
            continue
        seen.add(id(cur))
        out.append(cur)
        queue.extend([getattr(cur, "orig", None), cur.__cause__])
    return out


def _refused_binding(statement, params, driver_message: str) -> str:
    # EVERY EXIT IS A STRING, never an exception. This runs inside the delivery loop's
    # `except Exception`, so a raise here does not degrade the message - it escapes
    # deliver_succeeded_run entirely, past the downgrade, the dead letter and the publish that
    # tells the customer we could not store it. A missing column name costs a worse log line;
    # a raise costs the handling.
    if not isinstance(statement, str):
        return ""
    _m = _INSERT_COLUMNS.search(statement)
    # executemany binds a LIST OF ROWS, and then no single parameter is "the" value - $8 would
    # index a row, not a column - so say nothing rather than point at the wrong thing.
    if not _m or not isinstance(params, (list, tuple)) or any(
            isinstance(p, (list, tuple, dict)) for p in params):
        return ""
    table, columns = _m.group(1), [c.strip() for c in _m.group(2).split(",")]
    _arg = _BIND_ARGUMENT.search(driver_message)
    if _arg:
        i = int(_arg.group(1))
        if 1 <= i <= len(columns) and i <= len(params):
            try:
                return f"{table}.{columns[i - 1]}={params[i - 1]!r}"
            except Exception:
                # a bound value whose __repr__ raises; the column is still worth naming
                return f"{table}.{columns[i - 1]}"
    # A server-side refusal names neither a parameter nor a column, so print the row that was
    # bound: it is the evidence, and it is what a dead letter would have had to be read for.
    return f"{table}(" + ", ".join(
        f"{c}={p!r}"[:56] for c, p in zip(columns, params)) + ")"


def _db_refusal_detail(exc: BaseException) -> str | None:
    statement = params = None
    state = driver_message = ""
    for o in _cause_chain(exc):
        if statement is None:
            statement = getattr(o, "statement", None)
        if params is None:
            params = getattr(o, "params", None)
        # asyncpg sets both; SQLAlchemy's own `.code` is a documentation slug ("dbapi"), never a
        # SQLSTATE, so it is not consulted. The DEEPEST bearer is the driver's own message, the
        # one that carries the value.
        _code = getattr(o, "sqlstate", None) or getattr(o, "pgcode", None)
        if isinstance(_code, str) and len(_code) == 5:
            state, driver_message = _code, str(o)
    if not state.startswith(_TERMINAL_SQLSTATE_CLASS):
        return None
    _where = _refused_binding(statement, params, driver_message)
    return f"SQLSTATE {state}" + (f" on {_where}" if _where else "") + f" - {driver_message}"


_DELIVERY_ATTEMPTS = 3


class RunDelivery(NamedTuple):

    delivered: list                          #: artifact rows the run may report; EMPTY on failure
    succeeded: bool
    failed_reason: FailedReason | None       #: set only when the run must be downgraded
    error: Exception | None


async def deliver_succeeded_run(session_factory, *, job_id: str, owner_id: str, workspace: str,
                                engine: str, delivery_attempt: int, execution_generation: int,
                                jlog, publish, persist_orphans,
                                fence_commit=None) -> RunDelivery:
    # `fence_commit(db)` is the orchestrator's: awaited in the registration transaction just before
    # it commits, it holds the claim under the job row's lock or raises StaleWorkerFenced. This
    # module stays neutral about ownership (it never reads or binds it), so the check is handed in
    # the way `publish` and `persist_orphans` are.
    from pathlib import Path as _Path

    from meshpipeline.contracts.execution_guard import StaleWorkerFenced
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
            report = None
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
                    # THE REGISTRATION IS FENCED UNDER THE FINALIZE'S LOCK. The orchestrator's
                    # ownership check before delivery is read-only, so an owner's cancel that
                    # commits between that check and this commit would otherwise leave artifact
                    # rows - download links - on a cancelled job. Taking the job row's lock here,
                    # in the rows' own transaction, serialises with the cancel: a fenced worker's
                    # rows roll back with this session and never become visible.
                    if fence_commit is not None:
                        await fence_commit(adb)
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
            except StaleWorkerFenced:
                # NOT A DELIVERY FAILURE, and nothing to retry or downgrade: this worker no longer
                # owns the job (its owner cancelled it, or a newer generation took it over). The
                # rows rolled back with the session; the objects it stored are nobody's, and the
                # reconciler cleans them. Nothing is announced - the job's owner speaks for it now.
                await persist_orphans(owner_id, job_id, list(getattr(report, "stored", []) or []))
                raise
            except Exception as exc:                      # noqa: BLE001 - classified below
                upload_exc = exc
                # Persist any orphans the failed attempt reported, so an undeletable object is
                # durably reconcilable even on the failure path.
                await persist_orphans(owner_id, job_id, getattr(exc, "orphans", []))
                _refusal = _db_refusal_detail(exc)
                if _refusal is not None:
                    # Terminal on the FIRST attempt. Everything above this line has already been
                    # paid for - the surface rebuild, the upload - and the next attempt would pay
                    # it again to be refused for the same value. The detail names the value and
                    # the column so this is readable here, without opening the dead letter.
                    jlog.error("Artifact delivery REFUSED by the database on attempt %d/%d - the "
                               "value and the column cannot change on a retry, so delivery stops "
                               "here: %s", attempt + 1, _DELIVERY_ATTEMPTS, _refusal)
                    break
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
