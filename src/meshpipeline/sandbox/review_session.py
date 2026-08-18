# Responsibility: Resolve every declared review artifact inside the job workspace, and pass on only what qualified.
# Owns: the confinement fence, format sniffing by content, and cancellation-safe session teardown.
# Boundaries: a path escaping the workspace is refused here, and diagnostics name the manifest key, not the path.
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meshpipeline.contracts.review_evidence import (
    NON_RENDERING_COMMANDS,
    ArtifactFormat,
    EvidenceItem,
    RenderArtifactRequirement,
    RenderCommand,
    RenderContext,
    ResolvedArtifact,
    ReviewEvidenceFailure,
    ReviewRenderError,
    SessionUpdateResult,
)

logger = logging.getLogger(__name__)

# The sandbox is AUTHORITATIVE on limits. An engine declaration may ask for less; it can never
# raise these. (RenderContext carries the request; these are the ceilings.)
MAX_IMAGES_PER_SESSION = 64
MAX_IMAGE_BYTES = 12 * 1024 * 1024

# THERE IS DELIBERATELY NO TOTAL SESSION DEADLINE. A `SESSION_DEADLINE_S = 600.0` sat here,
# unenforced and unread - a trap, because activating it would have looked like turning on a
# limit that already existed. It never did.
# A review runs up to REVIEWER_MAX_ROUNDS (30) rounds against a provider with a 1800s
# timeout, and the session stays open across all of them. A 600s wall-clock deadline would
# expire a HEALTHY session while it sat idle waiting for the model to think, and the failure
# would surface as a renderer fault. Renderer timing must keep session-open, per-operation,
# provider-wait and reviewer-node budgets distinct; a future total or idle deadline needs its own
# design, starting with whether model-wait counts.

# The magic bytes a declared format must actually start with. Extensions are attacker-controlled
# (the manifest is untrusted), so a declared format is checked against CONTENT where content
# says anything at all - a .msh that is really a zip is refused rather than handed to a loader.
_FORMAT_SNIFF: dict[ArtifactFormat, tuple[bytes, ...]] = {
    ArtifactFormat.VTK_VTP: (b"<?xml", b"<VTKFile"),
    ArtifactFormat.VTK_VTU: (b"<?xml", b"<VTKFile"),
    ArtifactFormat.GMSH_MSH: (b"$MeshFormat",),
    ArtifactFormat.JSON: (b"{", b"["),
}


class ArtifactResolver:

    def __init__(self, workspace: Path, manifest: dict[str, Any]) -> None:
        # realpath the workspace ONCE: every later comparison is real-to-real, so a symlinked
        # workspace root cannot make an escape look like containment.
        self._root = Path(workspace).resolve(strict=False)
        self._manifest = manifest or {}

    def _lookup(self, key: str) -> str | None:
        node: Any = self._manifest
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node if isinstance(node, str) and node.strip() else None

    def confine(self, key: str, raw: str) -> Path:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        # Resolve BEFORE comparing. `ws/link -> /etc` is inside the workspace by name and
        # outside it in fact; only the resolved path tells the truth.
        real = candidate.resolve(strict=False)
        try:
            real.relative_to(self._root)
        except ValueError:
            raise ReviewRenderError(
                ReviewEvidenceFailure.EVIDENCE_MISSING,
                # The key, never the path: an operator-visible error must not leak the box's
                # filesystem layout.
                f"artifact {key!r} resolves outside the job workspace") from None
        return real

    def resolve(self, req: RenderArtifactRequirement) -> ResolvedArtifact | None:
        return self.resolve_raw(req.artifact_key, self._lookup(req.artifact_key),
                                req.allowed_formats, required=req.required)

    def resolve_raw(self, key: str, raw: Any, allowed_formats: tuple[ArtifactFormat, ...],
                    *, required: bool) -> ResolvedArtifact | None:
        if not isinstance(raw, str) or not raw.strip():
            if required:
                raise ReviewRenderError(
                    ReviewEvidenceFailure.EVIDENCE_MISSING,
                    f"required artifact {key!r} is not in the manifest")
            return None

        # Unsafe raises out of here for required AND optional alike.
        path = self.confine(key, raw)

        # polyMesh is a DIRECTORY; every other declared format is a file.
        want_dir = ArtifactFormat.OPENFOAM_POLYMESH in allowed_formats
        if not (path.exists() if want_dir else path.is_file()):
            if required:
                raise ReviewRenderError(
                    ReviewEvidenceFailure.EVIDENCE_MISSING,
                    f"required artifact {key!r} does not exist")
            logger.info("artifact %s declared but not present - dependent views unavailable", key)
            return None

        if path.is_dir():
            return ResolvedArtifact(key, path, ArtifactFormat.OPENFOAM_POLYMESH)

        # Unusable content raises for required AND optional alike: an artifact that is present
        # but not what it claims is refused before any loader sees it, never quietly treated as
        # absent because it happened to be optional.
        fmt = self._sniff(key, path, allowed_formats)
        return ResolvedArtifact(artifact_key=key, path=path, fmt=fmt)

    def _sniff(self, key: str, path: Path,
               allowed_formats: tuple[ArtifactFormat, ...]) -> ArtifactFormat:
        try:
            head = path.open("rb").read(512)
        except OSError as exc:
            raise ReviewRenderError(
                ReviewEvidenceFailure.EVIDENCE_MISSING,
                f"artifact {key!r} could not be read") from exc

        for fmt in allowed_formats:
            sigs = _FORMAT_SNIFF.get(fmt)
            if sigs is None:
                return fmt          # a format with no signature (e.g. polyMesh dirs, STL text)
            if any(head.lstrip()[:len(s)] == s for s in sigs):
                return fmt
        raise ReviewRenderError(
            ReviewEvidenceFailure.EVIDENCE_MISSING,
            f"artifact {key!r} does not match any declared format "
            f"({', '.join(f.value for f in allowed_formats)})")


class ReviewSession:

    def __init__(self, inner: Any, root: Path, limits: _Limits) -> None:
        self._inner = inner
        self._root = root
        self._limits = limits
        self._closed = False
        self._images = 0
        self._opening: tuple[EvidenceItem, ...] | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_open(self) -> None:
        if self._closed:
            raise ReviewRenderError(
                ReviewEvidenceFailure.RENDERER_UNAVAILABLE,
                "the review session is closed")

    def capabilities(self):
        self._require_open()
        return self._inner.capabilities()

    def scene_context(self):
        self._require_open()
        return self._inner.scene_context()

    def opening_evidence(self) -> tuple[EvidenceItem, ...]:
        self._require_open()
        if self._opening is None:
            self._opening = tuple(self._validate(i) for i in self._inner.opening_evidence())
        return self._opening

    def execute(self, command: RenderCommand) -> EvidenceItem | SessionUpdateResult:
        self._require_open()
        caps = self._inner.capabilities()
        if not caps.supports(command.kind):
            raise ReviewRenderError(
                ReviewEvidenceFailure.RENDERER_UNAVAILABLE,
                f"this engine's renderer does not support {command.kind.value}")

        result = self._inner.execute(command)

        if command.kind in NON_RENDERING_COMMANDS:
            if not isinstance(result, SessionUpdateResult):
                raise ReviewRenderError(
                    ReviewEvidenceFailure.RENDERER_UNAVAILABLE,
                    f"{command.kind.value} returned evidence - a session update renders "
                    "nothing and must never claim inspection")
            return result

        if isinstance(result, SessionUpdateResult):
            raise ReviewRenderError(
                ReviewEvidenceFailure.RENDERER_UNAVAILABLE,
                f"{command.kind.value} is a rendering command but returned a session update - "
                "a failed render must be reported as failed evidence, not hidden")
        return self._validate(result)

    def _validate(self, item: EvidenceItem) -> EvidenceItem:
        if not item.image_ref:
            return item                                   # already an honest failure
        path = Path(item.image_ref)
        real = (path if path.is_absolute() else self._root / path).resolve(strict=False)
        try:
            real.relative_to(self._root)
        except ValueError:
            return self._demote(item, "image escaped the sandbox output area")
        if not real.is_file():
            return self._demote(item, "image file does not exist")
        size = real.stat().st_size
        if size == 0 or size > self._limits.max_image_bytes:
            return self._demote(item, f"image size {size}B is outside the permitted range")
        self._images += 1
        if self._images > self._limits.max_images:
            raise ReviewRenderError(
                ReviewEvidenceFailure.RENDERER_UNAVAILABLE,
                f"renderer exceeded the session image limit ({self._limits.max_images})")
        return item

    def _demote(self, item: EvidenceItem, why: str) -> EvidenceItem:
        logger.warning("review session: rejected evidence %s - %s", item.evidence_id, why)
        return EvidenceItem(**{**item.__dict__, "image_ref": "",
                               "diagnostics": (*item.diagnostics, f"rejected: {why}")})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._inner.close()
        except Exception:  # noqa: BLE001
            logger.warning("review session: renderer close failed", exc_info=True)


@dataclass(frozen=True)
class _Limits:
    max_images: int
    max_image_bytes: int


async def _inline_dispatch(fn, *args, **kwargs):
    return fn(*args, **kwargs)


@asynccontextmanager
async def open_review_session(spec, context: RenderContext, native_dispatch=None):
    renderer = spec.review_renderer
    if renderer is None:
        raise ReviewRenderError(
            ReviewEvidenceFailure.RENDERER_UNAVAILABLE,
            "this engine declares no review renderer")

    root = Path(context.workspace).resolve(strict=False)
    resolver = ArtifactResolver(root, context.manifest)

    # Resolve EVERYTHING before the renderer runs: a missing required artifact must fail before
    # a render process exists, not halfway through one.
    resolved: dict[str, ResolvedArtifact] = {}
    for req in spec.render_artifacts:
        art = resolver.resolve(req)
        if art is not None:
            resolved[req.artifact_key] = art
        elif not req.required:
            logger.info("review session: optional artifact %s absent - views needing it will "
                        "not be offered", req.artifact_key)

    limits = _Limits(
        max_images=min(context.max_images or MAX_IMAGES_PER_SESSION, MAX_IMAGES_PER_SESSION),
        max_image_bytes=MAX_IMAGE_BYTES,
    )

    # NATIVE WORK STARTS HERE. Artifact resolution above is pure pathlib and stays on the
    # event loop; from this line on we are touching gmsh and VTK, which are thread-affine -
    # so construction goes through the same lane every later call will use.
    # The resolved set is HANDED OVER. Resolving it and then leaving the renderer to find its
    # own paths would have made the confinement above decorative.
    dispatch = native_dispatch or _inline_dispatch
    inner = await dispatch(renderer.open, context, resolved)
    session = ReviewSession(inner, root, limits)
    try:
        yield session
    finally:
        # EVERY exit: success, renderer failure, caller exception, timeout, cancellation.
        # Cancellation propagates AFTER cleanup - never swallowed, and never turned into a
        # quality verdict.
        # Close goes through the SAME lane, so it queues behind any in-flight native call
        # instead of tearing down VTK from a second thread mid-render. Shielded because the
        # caller's cancellation must not interrupt native cleanup halfway.
        _final = getattr(native_dispatch, "__self__", None)
        _close = _final.run_final if _final is not None else dispatch
        await asyncio.shield(asyncio.ensure_future(_close(session.close)))
