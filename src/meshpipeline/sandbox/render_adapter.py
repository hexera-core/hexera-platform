# Responsibility: Turn an engine's declared review requirements into a render session over resolved artifacts.
# Owns: target expansion, identifier normalisation and the session the reviewer drives.
# Boundaries: it receives resolved artifacts and resolves none itself, which keeps the confinement fence meaningful.
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from meshpipeline.contracts.review_evidence import (
    CommandKind,
    EvidenceItem,
    InspectionTarget,
    PatchLegendEntry,
    PatchView,
    RenderCapabilities,
    RenderCommand,
    RenderContext,
    ResolvedArtifact,
    ReviewEvidenceFailure,
    ReviewRenderError,
    SceneBounds,
    SceneContext,
    SessionUpdateResult,
    TargetKind,
    ViewSpec,
)
from meshpipeline.sandbox.render_inputs import (
    RenderMetadata,
    ResolvedRenderInputs,
    resolve_regions,
)
from meshpipeline.sandbox.review_session import FRAME_WHOLE

logger = logging.getLogger(__name__)

SURFACE_KEY = "mesh_paths.surface"
VOLUME_KEY = "mesh_paths.volume"

# A mesh with more boundary patches than this is not a review problem, it is a build problem - and
# expanding an unbounded patch list into targets would let a malformed mesh exhaust the session.
MAX_PATCHES = 512

#: A VOLUME FILE LARGER THAN THIS IS NOT TAKEN AS A VOLUME AT ALL.
#:
#: `backend._load_volume` reads the whole file through `pv.read` into one process, and the Celery
#: worker a review runs in is capped at 16 GiB. The conjugate plate's fluid interior is
#: 10,366,611,525 bytes: handing that to this process does not produce a worse review, it
#: produces an OOM KILL of the worker, which loses the job and the mesh delivery with it - a
#: strictly worse outcome than the honest "evidence missing" the same job returns today. That is
#: reachable the moment the transfer ceiling that pruned the file is corrected.
#:
#: 1.2 GiB, AND THE NUMBER COMES OUT OF A MEASUREMENT RATHER THAN AN ASSUMPTION. This was 4 GiB
#: and the reasoning under it was a 1:1 read, which is not what a .vtu costs.
#:
#: MEASURED IN THIS WORKER'S OWN CONTAINER, 2026-11-17, one fresh process per file so no read
#: pollutes the next one's peak:
#:
#:     file                              bytes    peak RSS    ratio
#:     fluid_to_solid.vtp        1,238,969,100    1.037 GB     0.84x
#:     a 1,000,000-cell .vtu        19,774,549    0.276 GB     7.4x marginal over the smaller one
#:
#: THE TWO FORMATS ARE NOT THE SAME SHAPE and that is the whole correction. A .vtp of a surface
#: reads at about its own size. A .vtu carries an unstructured interior, whose connectivity is
#: written compactly and held explicitly - eight labels a hexahedron, plus points, plus VTK's own
#: overhead - so it inflates by something close to an order of magnitude. An independently
#: measured 93,924,028 B .vtu came back at 9.27x, and native.py's own table in this tree already
#: recorded 0.80 GB for a ~100 MB .vtu, which is the same ratio from a third direction.
#:
#: `mesh_paths.volume` is internal.vtu. It is the .vtu shape. So the old 4 GiB ceiling admitted a
#: file that would have wanted about 37 GB of RSS inside a 16 GiB cap - it did not bound the OOM
#: kill it was written to prevent, it permitted it with a number that looked careful.
#:
#: 12 GiB of headroom divided by 9.27 is 1.29 GiB, rounded DOWN to 1.2 GiB because the ratio is
#: the thing least well known here and the error that matters is the generous one.
#:
#: THE COST OF BEING WRONG IN THE OTHER DIRECTION IS BOUNDED AND VISIBLE: a volume between this
#: and what the worker could really have opened is declined rather than cut, which costs a review
#: its live interior evidence and logs why. A cut prepared where the mesh was built covers the
#: same ground. The cost of being wrong in this direction is the worker dying, which loses the
#: job and the mesh delivery with it.
#:
#: AND FOR FOUR OF THE SEVEN ENGINES THERE IS NO PREPARED CUT TO FALL BACK ON. THAT IS A NAMED
#: REGRESSION OF LOWERING THIS NUMBER FROM 4 GiB, NOT AN OVERSIGHT.
#:
#: MEASURED off the registry, 2026-11-17, not read off a report. Seven engines render for review
#: and all seven reach the interior through THIS function - the blast radius is not the modules
#: that import this constant (one file uses it; `backend.py` and
#: `engines/cfmesh_conjugate/native.py` only mention it in comments) but the single `build_backend`
#: every review renderer shares:
#:
#:     engine               declares mesh_paths.volume   publishes prepared frames
#:     cfmesh                        yes (optional)              NO   <- regresses
#:     cfmesh_conjugate              yes (optional)              yes
#:     snapgrid                      yes (optional)              NO   <- regresses
#:     snappy                        yes (optional)              NO   <- regresses
#:     snappy_multiregion            yes (optional)              NO   <- regresses
#:     gmsh                          no                          n/a
#:     vmtk                          no                          n/a
#:
#: So for cfmesh, snapgrid, snappy and snappy_multiregion a volume between 1.2 GiB and the old
#: 4 GiB now has NO live cut and NO replacement, and every `region:*` target those engines declare
#: goes unanswered - which is `reviewer_evidence_missing` where the same job used to get a verdict.
#: `tests/unit/sandbox/test_a_frame_is_measured_before_it_discharges` pins exactly that, for an
#: engine with frames and for one without, so the regression cannot be rediscovered as a surprise.
#:
#: IT IS STILL NOT SCOPED PER ENGINE, and that is the decision rather than the leftover. The thing
#: being bounded is what THIS PROCESS survives reading, which does not vary by engine: the file is
#: opened by the same `backend._load_volume` in the same 16 GiB worker whatever wrote it. Scoping
#: the lower ceiling to the engines that have a fallback would hand the other four a read measured
#: at about 9.5x their file size, so it would trade "this review has no interior evidence" for
#: "this job and its mesh delivery are lost to an OOM kill" - strictly the worse of the two, and
#: the exact trade the 4 GiB number made while looking careful.
#:
#: NOTE THAT vmtk's INTERIOR IS NOT BOUNDED AT ALL. Its lumen volume rides under
#: `mesh_paths.lumen`, not VOLUME_KEY, so this check never sees it. That is an existing gap in a
#: different engine's declaration, named here because this comment is where someone will look.
#:
#: RAISE IT ONLY WITH A MEASUREMENT of peak RSS against file size for a real internal.vtu at
#: delivery size. Nothing in this tree has that yet; everything above is either a smaller file or
#: a different format.
VOLUME_READ_CEILING_BYTES = 1229 * 1024 ** 2

# The nine rendering operations, as the reviewer's tools map onto the typed vocabulary.
_CAMERA_PRESETS = ("front", "rear", "left", "right", "top", "bottom", "iso")

# WHERE EACH OPERATION'S IMAGE COMES FROM. Enumerated because assuming it is how a crop command
# came to return a full-view screenshot: `zoom_to_region` PRODUCES an image (it magnifies a region
# and returns that crop), but it was routed through the generic "mutate, then screenshot" helper,
# so the crop was written to disk and then overwritten in the evidence by an unrelated later
# full view. The reviewer would have been handed the whole model labelled "4x magnification".
#   OWN_IMAGE - the backend call returns the image it just made; use exactly that.
#   THEN_SHOT - the call mutates the scene and returns None; a screenshot follows.
# Verified against backend.py: the three OWN_IMAGE operations are the three that call
# `_save_and_encode` themselves. A contract test re-derives this from the source, so an
# operation that changes category fails rather than silently misreporting.
OWN_IMAGE = "own_image"
THEN_SHOT = "then_shot"

IMAGE_SOURCE: dict[CommandKind, str] = {
    CommandKind.SELECT_VIEW: THEN_SHOT,
    CommandKind.CAMERA_OP: THEN_SHOT,
    CommandKind.GO_TO_COORDINATES: THEN_SHOT,
    CommandKind.TOGGLE_ENTITY: THEN_SHOT,
    CommandKind.CAPTURE: THEN_SHOT,
    CommandKind.APPLY_CLIP: OWN_IMAGE,        # zoom_to_region returns its crop
    CommandKind.INSPECT_TARGET: OWN_IMAGE,    # inspect_region returns its slice
}


def _view_specs() -> tuple[ViewSpec, ...]:
    return tuple(
        ViewSpec(view_id=p, label=f"{p} view",
                 purpose="orient the camera to a standard direction")
        for p in _CAMERA_PRESETS
    )


def normalize_patch_id(name: str) -> str:
    return f"patch:{name}"


def normalize_region_id(name: str) -> str:
    return f"region:{name}"


def expand_patch_targets(
    patch_names: Sequence[str],
    *,
    required: bool,
    purpose: str,
) -> tuple[InspectionTarget, ...]:
    seen: dict[str, str] = {}
    out: list[InspectionTarget] = []
    for name in patch_names:
        if not isinstance(name, str) or not name.strip():
            continue
        target_id = normalize_patch_id(name)
        if target_id in seen:
            # Two distinct patches normalising onto one id would silently merge coverage: one
            # toggle would mark both looked-at. Refuse the collision rather than absorb it.
            logger.warning("render adapter: patch id collision %r vs %r -> %s",
                           seen[target_id], name, target_id)
            continue
        seen[target_id] = name
        out.append(InspectionTarget(target_id=target_id, kind=TargetKind.PATCH,
                                    label=name, purpose=purpose, required=required))
        if len(out) >= MAX_PATCHES:
            logger.warning("render adapter: patch list truncated at %d", MAX_PATCHES)
            break
    return tuple(out)


def expand_region_targets(
    regions: Mapping[str, Any],
    *,
    required: bool,
    purpose: str,
    volume_available: bool,
    cuts: Mapping[str, Any] | None = None,
) -> tuple[InspectionTarget, ...]:
    """The regions this session can actually show, as targets.

    WHAT DECIDES IS NO LONGER ONE BOOL FOR THE WHOLE JOB. `volume_available` asked "is there an
    interior to cut", and on a 283 M-cell plate the answer is permanently no: the interior is
    10.4 GB and the worker that would slice it is capped at 16 GiB. The cut is taken where the
    mesh is instead, so a region can be showable because its frames came home even though the
    volume did not - and the decision is per region, because frames are published per region and
    a job with two of three is two of three, not all or nothing.

    `cuts` is optional and empty by default: the four other engines that call this pass a volume
    and nothing else, and their behaviour is unchanged to the line.
    """
    cuts = cuts or {}
    out: list[InspectionTarget] = []
    unshowable: list[str] = []
    for r in regions.values():
        # A FRAME THAT CANNOT ANSWER THE QUESTION DOES NOT MAKE A REGION SHOWABLE. The coverage
        # floor is satisfied by the frame a cell is countable in (see `_prepared_cut`), so a
        # region whose only frame is sub-pixel can be LOOKED at and never INSPECTED - and
        # offering it would put the review in a loop: eligibility tells the reviewer the region
        # "IS available - inspect it now", the only frame there cannot discharge it, and the
        # rounds run out. Not offering it makes visual.py's expected-target pre-check fire
        # instead, which is the honest decline, reached before the model is asked anything.
        if volume_available or any(getattr(c, "counts_cells", False)
                                   for c in cuts.get(r.region_id) or ()):
            out.append(InspectionTarget(
                target_id=normalize_region_id(r.region_id), kind=TargetKind.REGION,
                label=r.region_id, purpose=purpose, required=required))
        else:
            unshowable.append(r.region_id)
    if unshowable:
        # THE REASON IS NAMED, because an operator reading this needs to tell "no cut came home"
        # from "a cut came home that nothing can be counted in", and the fix is different.
        logger.info("render adapter: %d region(s) not offered - no volume artifact, and no "
                    "prepared frame a cell is countable in, for %s",
                    len(unshowable), ", ".join(sorted(unshowable)))
    return tuple(out)


class BackendRenderSession:

    def __init__(
        self,
        backend: Any,
        *,
        inputs: ResolvedRenderInputs,
        regions: Mapping[str, Any],
        patch_targets: tuple[InspectionTarget, ...],
        region_targets: tuple[InspectionTarget, ...],
        opening_purpose: str,
    ) -> None:
        self._backend = backend
        self._inputs = inputs
        self._regions = dict(regions)
        self._patch_targets = patch_targets
        self._region_targets = region_targets
        self._opening_purpose = opening_purpose
        self._seq = 0
        self._closed = False
        self._opening: tuple[EvidenceItem, ...] | None = None

    def capabilities(self) -> RenderCapabilities:
        ops = {
            CommandKind.SELECT_VIEW, CommandKind.CAMERA_OP, CommandKind.GO_TO_COORDINATES,
            CommandKind.APPLY_CLIP, CommandKind.TOGGLE_ENTITY, CommandKind.CAPTURE,
            CommandKind.CONFIGURE_SESSION,
        }
        # INSPECT_TARGET is offered only when there is something to cut. A capability that cannot
        # succeed must not be advertised: the reviewer would spend calls on it and read the
        # resulting surface screenshot as an interior it inspected.
        #
        # A CUT ALREADY MADE COUNTS AS SOMETHING TO CUT, and leaving it out was the whole defect.
        # review_session.execute RAISES ReviewRenderError(RENDERER_UNAVAILABLE) for any command
        # the capabilities do not support, so on a job with frames and no volume the reviewer's
        # own inspect_region call became a RENDERER FAULT - the review was rerun and the mesh
        # delivered with no verdict - rather than returning the frame sitting in the workspace.
        if self._region_targets and (self._inputs.has_volume or self._inputs.has_cuts):
            ops.add(CommandKind.INSPECT_TARGET)
        return RenderCapabilities(
            views=_view_specs(),
            targets=self._patch_targets + self._region_targets,
            entities=tuple(t.label for t in self._patch_targets),
            operations=frozenset(ops),
        )

    def scene_context(self) -> SceneContext:
        self._require_open()
        facts = self._backend.scene_facts()
        b = facts.get("bounds")
        return SceneContext(
            has_geometry=bool(facts["has_geometry"]),
            mesh_units=facts["mesh_units"],
            pan_step_mm=facts["pan_step_mm"],
            zoom_step=facts["zoom_step"],
            bounds=SceneBounds(**b) if b else None,
            patch_legend=tuple(PatchLegendEntry(patch_id=n, label=n, color=c)
                               for n, c in facts["patch_legend"]),
            patch_views=tuple(
                PatchView(patch_id=n, x=float(v["x"]), y=float(v["y"]), z=float(v["z"]),
                          span=float(v["span"]), preset=str(v["preset"]),
                          isolate=bool(v.get("isolate", True)))
                for n, v in sorted((facts.get("patch_views") or {}).items())),
        )

    def opening_evidence(self) -> tuple[EvidenceItem, ...]:
        if self._opening is None:
            self._require_open()
            image = self._render(self._backend.take_screenshot)
            self._opening = (self._evidence(
                image=image, label="opening view", purpose=self._opening_purpose,
                command_kind="open", view_id="iso", covers_target=""),)
        return self._opening

 # execution
    def execute(self, command: RenderCommand) -> EvidenceItem | SessionUpdateResult:
        self._require_open()
        if command.kind is CommandKind.CONFIGURE_SESSION:
            return self._configure(command)
        handler = {
            CommandKind.SELECT_VIEW: self._select_view,
            CommandKind.CAMERA_OP: self._camera_op,
            CommandKind.GO_TO_COORDINATES: self._go_to_coordinates,
            CommandKind.APPLY_CLIP: self._zoom_to_region,
            CommandKind.INSPECT_TARGET: self._inspect_target,
            CommandKind.TOGGLE_ENTITY: self._toggle_entity,
            CommandKind.CAPTURE: self._capture,
        }.get(command.kind)
        if handler is None:
            raise ReviewRenderError(
                ReviewEvidenceFailure.RENDERER_UNAVAILABLE,
                f"this renderer does not support {command.kind.value}")
        return handler(command)

    def _configure(self, command: RenderCommand) -> SessionUpdateResult:
        applied = self._backend.set_navigation_defaults(
            command.pan_step_mm, command.zoom_step)
        return SessionUpdateResult(
            command_kind=command.kind.value,
            message=(f"Navigation defaults updated: "
                     f"pan_step={applied['pan_step_mm']:.0f} mm, "
                     f"zoom_step={applied['zoom_step']:.2f}×."),
            pan_step_mm=applied["pan_step_mm"], zoom_step=applied["zoom_step"])

    def _select_view(self, command: RenderCommand) -> EvidenceItem:
        if command.view_id not in _CAMERA_PRESETS:
            return self._failed(command, f"unknown camera preset {command.view_id!r}")
        self._backend.set_camera_preset(command.view_id)
        return self._shot(command, label=f"{command.view_id} view",
                          purpose="orient the camera", view_id=command.view_id)

    def _camera_op(self, command: RenderCommand) -> EvidenceItem:
        op = command.camera
        if op in ("up", "down", "left", "right", "forward", "back"):
            self._backend.move_camera(op, command.amount)
            label = f"camera moved {op}"
        elif op in ("yaw", "pitch", "roll"):
            if command.amount is None:
                return self._failed(command, f"{op} requires an angle")
            self._backend.rotate_camera(op, command.amount)
            label = f"camera rotated {op} {command.amount:g}°"
        elif op == "zoom":
            self._backend.zoom(command.amount)
            label = "camera zoomed"
        elif op == "reset":
            self._backend.reset_view()
            label = "view reset"
        else:
            return self._failed(command, f"unknown camera operation {op!r}")
        return self._shot(command, label=label, purpose="reposition the camera")

    def _go_to_coordinates(self, command: RenderCommand) -> EvidenceItem:
        c = command.coordinates
        if c is None or command.span is None:
            return self._failed(command, "coordinates and span are required")

        preset = command.view_id or None
        if preset is not None and preset not in _CAMERA_PRESETS:
            return self._failed(command, f"unknown camera preset {preset!r}")

        patch_name = command.entity_id or None
        if patch_name is not None and patch_name not in {t.label for t in self._patch_targets}:
            # An unknown patch must not reach the backend, and must not produce text claiming an
            # isolation of something that is not there.
            return self._failed(command, f"unknown patch {patch_name!r}")

        self._backend.go_to_coordinates(c[0], c[1], c[2], command.span,
                                        preset=preset, patch_name=patch_name)
        return self._shot(command, label=f"at ({c[0]:g}, {c[1]:g}, {c[2]:g})",
                          purpose="inspect a specific location",
                          view_id=preset or "")

    def _zoom_to_region(self, command: RenderCommand) -> EvidenceItem:
        c = command.coordinates
        if c is None:
            return self._failed(command, "a screen region is required")
        image = self._render(
            lambda: self._backend.zoom_to_region(c[0], c[1], command.span or 0.25))
        return self._evidence(image=image, label="zoomed to screen region",
                              purpose="magnify part of the current view",
                              command_kind=command.kind.value, artifact_key=SURFACE_KEY)

    def prepared_cuts(self) -> tuple[Any, ...]:
        """Every frame this session can serve, for the regions it actually offers.

        Filtered to the offered regions on purpose: a frame for a region the session does not
        advertise is a frame the reviewer cannot ask for, and listing it in the opening message
        would send it to ask for something that will be refused.
        """
        offered = {t.label for t in self._region_targets}
        return tuple(c for name in sorted(offered)
                     for c in self._inputs.cut_frames(name))

    def _inspect_target(self, command: RenderCommand) -> EvidenceItem:
        # `region:<name>` addresses the region; `region:<name>#<frame>` addresses one frame of
        # its prepared cut. The frame rides in the id rather than in a new RenderCommand field
        # because the command vocabulary is a contract five engines implement, and a frame is a
        # property of one engine's evidence, not a new kind of command.
        addressed, _, want_scope = command.target_id.removeprefix("region:").partition("#")
        name = addressed
        region = self._regions.get(name)
        if region is None:
            # Unknown names FAIL. They do not quietly render the surface and count.
            return self._failed(command, f"unknown inspection region {command.target_id!r}")
        # THE SAME CONDITION THAT USED TO FAIL HERE, now routed instead of refused. It keeps
        # `requires_volume` read rather than leaving it set-and-unread: a region that declares it
        # needs no volume must not be sent down the prepared-frame path either, because it is
        # saying it can be drawn from what is already loaded.
        if region.requires_volume and not self._inputs.has_volume:
            return self._prepared_cut(command, name, want_scope)
        if want_scope:
            # The volume is here, so the cut is taken live and there are no frames to choose
            # between. Saying so beats cutting the live volume and letting the reviewer believe
            # it received the frame it named.
            return self._failed(
                command, f"region {name!r} is cut live from the volume for this job, so it has "
                         f"no prepared {want_scope!r} frame - call it without a scope")
        # A FAILURE, NEVER A SCREENSHOT - the same rule _toggle_entity states below for a patch
        # named wrong, which this did not follow for a cut that shows nothing. The backend used
        # to hand back a picture in all three of the ways a cut can produce nothing (no readable
        # volume, a slice that raised, an empty slice) and the adapter could not tell them from a
        # real section. Measured on a live conjugate run: a volume that read as 0 cells was cut
        # twice, both blank frames were filed as usable target evidence for the two regions, and
        # the review returned verdict=passed concerns=[]. `_render` reads last_shot_path rather
        # than this return, so the return is checked FIRST - a stale path from an earlier
        # screenshot would otherwise be filed as this region's section.
        cut, why = self._backend.inspect_region(region)
        if not cut:
            return self._failed(
                command, why or f"the cut at {command.target_id!r} produced no section")
        shot = self._backend.last_shot_path
        return self._evidence(
            image=str(shot) if shot is not None else "",
            label=f"section at {name}", purpose="reveal internal structure",
            command_kind=command.kind.value, target_ref=command.target_id,
            regions=(name,), covers_target=normalize_region_id(name),
            artifact_key=VOLUME_KEY)

    def _prepared_cut(self, command: RenderCommand, name: str, want_scope: str) -> EvidenceItem:
        """Serve one frame of a cut taken where the mesh was, with what it shows stated.

        THE FRAME SAYS WHAT IT IS IN ITS OWN LABEL, and that is the point of this path rather
        than a detail of it. A section the reviewer cut itself can be re-aimed; a frame cannot,
        and anything outside its window is not in it. A reviewer handed a picture with no window
        will describe the part from one plane at one magnification and be right by accident or
        wrong without knowing - which is the failure every line of this change exists to avoid.
        """
        frames = self._inputs.cut_frames(name)
        if not frames:
            return self._failed(
                command, f"region {command.target_id!r} needs the volume artifact or a cut "
                         "prepared where the mesh was built, and this job provided neither")
        cut = self._inputs.cut_frame(name, want_scope)
        if cut is None:
            return self._failed(
                command, f"region {name!r} has no {want_scope!r} frame - it has "
                         f"{', '.join(repr(c.scope) for c in frames)}")
        # COVERAGE IS THE COUNTABLE FRAME'S, NOT ANY FRAME'S. `covers_target` is what the
        # coverage floor reads, and a frame in which a cell is under COUNTABLE_CELL_PX pixels
        # across cannot answer either question slices.py says the cut exists for. Filing it as an
        # inspection would let the review complete on frames where the channel is sub-pixel and
        # return a confident verdict - the exact shape of job 575f76e6. It is still served, still
        # labelled and still citable; it just does not discharge the obligation.
        covers = normalize_region_id(name) if cut.counts_cells else ""
        return self._evidence(
            image=str(cut.path),
            label=f"{cut.scope} frame of the cut at {name}",
            purpose=("show where this cut sits in the part" if cut.scope == FRAME_WHOLE
                     else "show the cells this cut passes through"),
            command_kind=command.kind.value,
            # The CANONICAL region id, whichever frame was named. unified.record_operation_evidence
            # compares the entity addressed against the entity covered and files a disagreement as
            # not covered, so a frame-suffixed target_ref would silently stop advancing the floor.
            target_ref=normalize_region_id(name),
            regions=(name,), covers_target=covers,
            artifact_key=cut.artifact_key)

    def _toggle_entity(self, command: RenderCommand) -> EvidenceItem:
        known = {t.label for t in self._patch_targets}
        if command.entity_id not in known:
            # A failure, never a screenshot: a returned image would let the coverage floor count
            # a patch as "inspected" because it was named wrong.
            return self._failed(command, f"unknown patch {command.entity_id!r}")
        self._backend.toggle_patch(command.entity_id, command.visible)
        state = "shown" if command.visible else "hidden"
        return self._evidence(
            image=self._render(self._backend.take_screenshot),
            label=f"{command.entity_id} {state}",
            purpose="isolate a patch to check it directly",
            command_kind=command.kind.value, target_ref=command.entity_id,
            patches=(command.entity_id,),
            covers_target=normalize_patch_id(command.entity_id),
            artifact_key=SURFACE_KEY)

    def _capture(self, command: RenderCommand) -> EvidenceItem:
        return self._shot(command, label="current view", purpose="record the current view")

 # evidence construction
    def _render(self, call) -> str:
        call()
        path = self._backend.last_shot_path
        return str(path) if path is not None else ""

    def _shot(self, command: RenderCommand, *, label: str, purpose: str,
              view_id: str = "") -> EvidenceItem:
        return self._evidence(image=self._render(self._backend.take_screenshot), label=label,
                              purpose=purpose, command_kind=command.kind.value,
                              view_id=view_id, artifact_key=SURFACE_KEY)

    def _evidence(self, *, image: str, label: str, purpose: str, command_kind: str,
                  view_id: str = "", target_ref: str = "", patches: tuple[str, ...] = (),
                  regions: tuple[str, ...] = (), covers_target: str = "",
                  artifact_key: str = SURFACE_KEY) -> EvidenceItem:
        self._seq += 1
        return EvidenceItem(
            evidence_id=f"{command_kind}-{self._seq:03d}", seq=self._seq,
            label=label, purpose=purpose, image_ref=image or "",
            view_id=view_id, command_kind=command_kind, target_ref=target_ref,
            patches=patches, regions=regions,
            # axis_ids stays EMPTY: evidence-to-axis binding arrives in commit 4, and inventing
            # a relationship now would be a claim nothing has established.
            axis_ids=(),
            artifact_key=artifact_key,        # provenance by KEY, never an absolute path
            covers_target=covers_target if image else "")

    def _failed(self, command: RenderCommand, why: str) -> EvidenceItem:
        self._seq += 1
        return EvidenceItem(
            evidence_id=f"{command.kind.value}-{self._seq:03d}", seq=self._seq,
            label="command refused", purpose="report a command that could not be honoured",
            image_ref="", command_kind=command.kind.value,
            target_ref=command.target_id or command.entity_id,
            diagnostics=(why,), covers_target="")

 # lifecycle
    def _require_open(self) -> None:
        if self._closed:
            raise ReviewRenderError(
                ReviewEvidenceFailure.RENDERER_UNAVAILABLE, "the review session is closed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._backend.close()
        except Exception:  # noqa: BLE001
            logger.warning("render adapter: backend close failed", exc_info=True)


def build_backend(
    context: RenderContext,
    artifacts: Mapping[str, ResolvedArtifact],
) -> tuple[Any, ResolvedRenderInputs, dict[str, Any]]:
    surface = artifacts.get(SURFACE_KEY)
    if surface is None:
        raise ReviewRenderError(
            ReviewEvidenceFailure.EVIDENCE_MISSING,
            f"required artifact {SURFACE_KEY!r} was not resolved for this session")

    volume = artifacts.get(VOLUME_KEY)
    regions = resolve_regions(context.manifest)

    # TOO BIG TO OPEN IS NOT A VOLUME. Checked HERE rather than in `backend._load_volume`, which
    # is where the read happens, because the capability gate is decided from `inputs.has_volume`:
    # a volume the process cannot survive opening must not get its regions advertised and then
    # fail per call. See VOLUME_READ_CEILING_BYTES for the number and what would justify raising
    # it. A size that cannot be read is treated as "no volume", so prepared frames take over if
    # there are any and the region is honestly unshowable if there are not.
    if volume is not None:
        try:
            size = volume.path.stat().st_size
        except OSError:
            size = 0                      # unreadable size is not evidence of a huge file
        if size > VOLUME_READ_CEILING_BYTES:
            logger.error(
                "render adapter: the volume is %.2f GiB, over the %.2f GiB this process will "
                "open, so it is not loaded: reading it whole would OOM the worker and lose the "
                "job, not just the review. The cut has to come from where the mesh was built.",
                size / 2 ** 30, VOLUME_READ_CEILING_BYTES / 2 ** 30)
            volume = None

    # THE PREPARED FRAMES RIDE ON THE RESOLVED MAPPING, put there by
    # review_session.open_review_session, which is the only place a path is turned into a
    # confined handle. Read with `getattr` because a caller that hands over a plain dict (every
    # unit test that builds the mapping itself) legitimately has none.
    cuts: dict[str, Any] = {}
    offered = getattr(artifacts, "cuts", None) or {}
    if offered and volume is not None:
        # THE VOLUME WINS WHEN IT IS HERE, and the frames are set aside rather than offered
        # alongside it. A live cut can be re-aimed and a frame cannot, so the live cut is
        # strictly better evidence - and two answers to one question is how a reviewer ends up
        # citing the one that happens to agree with it.
        logger.info("render adapter: %d prepared cut region(s) set aside - the volume came home "
                    "and is cut live", len(offered))
    elif offered:
        # Only for regions this manifest actually declares: a frame naming a region nothing else
        # knows about would be a target with no plane behind it.
        cuts = {k: v for k, v in offered.items() if k in regions}
        for name in sorted(set(offered) - set(cuts)):
            logger.warning("render adapter: prepared cut names region %r, which this manifest "
                           "does not declare - not offered", name)

    inputs = ResolvedRenderInputs(surface=surface, volume=volume, cuts=cuts)

    # Imported HERE, not at module scope: backend.py pulls in gmsh and pyvista, and the API
    # process imports engine specs through the registry and must stay light.
    from meshpipeline.sandbox.backend import MeshRenderBackend

    backend = MeshRenderBackend(
        inputs=inputs,
        metadata=RenderMetadata.from_manifest(context.manifest),
        save_dir=Path(context.save_dir) if context.save_dir else None,
        regions=regions,
    )
    return backend, inputs, regions
