# Responsibility: Compose the sentence explaining what the application concluded and why the next step follows.
# Boundaries: presentation of decisions already made; it decides nothing itself and reads no state beyond its arguments.
# Collaborates with: the agents and application stages whose conclusions it renders.
from __future__ import annotations

from typing import Any

from meshpipeline.contracts.event_stream import EventStreamError, ExecutionEventPublisher

_SEEN_ATTR = "_rationale_seen"


# The guard and the once-per-run bookkeeping, shared by both publication paths. It emits
# nothing: it only answers whether this conclusion has already been said on this publisher.
def _should_say(publish: Any, conclusion: str, because: str) -> bool:
    if publish is None or not conclusion:
        return False
    try:
        seen = getattr(publish, _SEEN_ATTR, None)
        if seen is None:
            seen = set()
            try:
                setattr(publish, _SEEN_ATTR, seen)
            except Exception:
                seen = None
        key = (conclusion, because)
        if seen is not None:
            if key in seen:
                return False
            seen.add(key)
    except Exception:
        return False
    return True


def _say(publish: Any, conclusion: str, because: str = "") -> None:
    if not _should_say(publish, conclusion, because):
        return
    try:
        publish.rationale(conclusion, because)
    except Exception:
        pass


# The execution-owned counterpart. Same sentence, same once-per-run rule, but published through
# the ownership-checked contract, so a worker that no longer owns the run cannot narrate it.
# A refusal is NOT an observability blip and is never swallowed: the caller must learn that its
# claim is gone, exactly as it would at any other fenced boundary.
async def _asay(publish: ExecutionEventPublisher, conclusion: str, because: str = "") -> None:
    if not _should_say(publish, conclusion, because):
        return
    try:
        await publish.arationale(conclusion, because)
    except EventStreamError:
        raise
    except Exception:
        pass


# intake


def intake_compatibility(publish: Any, *, engine: str, purpose: str,
                         input_kind: str, supported: bool, explanation: str = "") -> None:
    subject = f"{engine} for {purpose} from a {input_kind} input"
    if supported:
        _say(publish, f"Your selected setup is workable: {subject}.",
             "the engine admits this purpose and this input kind")
    else:
        _say(publish, f"Your selected setup cannot produce this mesh: {subject}.",
             explanation or "the engine does not admit this purpose from this input kind")


def intake_requirements_finalized(publish: Any, *, patches: int, dimensionality: str) -> None:
    _say(publish, "Requirements are complete and validated.",
         f"{patches} boundary assignment(s) and a {dimensionality} domain were "
         "checked against the geometry and the selected engine")


def intake_submission(publish: Any, *, authorized: bool, reason: str = "") -> None:
    if authorized:
        _say(publish, "The brief is authorized for meshing.",
             "the submitted values match the configuration you confirmed")
    else:
        _say(publish, "The brief was not authorized for meshing.",
             reason or "the submission did not match the confirmed configuration")


# geometry admission


def geometry_admission(publish: Any, *, accepted: bool, detail: str = "") -> None:
    if accepted:
        _say(publish, "The geometry was accepted for meshing.",
             detail or "the input parsed as a closed, watertight surface")
    else:
        _say(publish, "The geometry was not accepted.",
             detail or "the input could not be used as a meshing surface")


# builder


async def ageometry_admission(publish: ExecutionEventPublisher, *, accepted: bool,
                              detail: str = "") -> None:
    if accepted:
        await _asay(publish, "The geometry was accepted for meshing.",
                    detail or "the input parsed as a closed, watertight surface")
    else:
        await _asay(publish, "The geometry was not accepted.",
                    detail or "the input could not be used as a meshing surface")


def builder_configuration(publish: Any, *, ready: bool, detail: str = "") -> None:
    if ready:
        _say(publish, "The mesh configuration is complete and ready to run.",
             detail or "every value the engine requires is present and self-consistent")
    else:
        _say(publish, "The mesh configuration needs a correction before it can run.",
             detail or "a required value is missing or inconsistent")


async def abuilder_configuration(publish: ExecutionEventPublisher, *, ready: bool,
                                 detail: str = "") -> None:
    if ready:
        await _asay(publish, "The mesh configuration is complete and ready to run.",
                    detail or "every value the engine requires is present and self-consistent")
    else:
        await _asay(publish, "The mesh configuration needs a correction before it can run.",
                    detail or "a required value is missing or inconsistent")


def builder_mesh_ready(publish: Any, *, cells: int | None = None) -> None:
    _say(publish, "The mesh was generated and is ready for review.",
         f"the mesher produced {cells:,} cells and reported no fatal errors"
         if cells else "the mesher completed without fatal errors")


async def abuilder_mesh_ready(publish: ExecutionEventPublisher, *,
                              cells: int | None = None) -> None:
    await _asay(publish, "The mesh was generated and is ready for review.",
                f"the mesher produced {cells:,} cells and reported no fatal errors"
                if cells else "the mesher completed without fatal errors")


# reviewer


def reviewer_evidence_incomplete(publish: Any, *, detail: str = "") -> None:
    _say(publish, "The review could not be completed, so no verdict was reached.",
         detail or "the reviewer did not gather enough evidence to judge the mesh")


async def areviewer_evidence_incomplete(publish: ExecutionEventPublisher, *,
                                        detail: str = "") -> None:
    await _asay(publish, "The review could not be completed, so no verdict was reached.",
                detail or "the reviewer did not gather enough evidence to judge the mesh")


def reviewer_verdict(publish: Any, *, passed: bool, failed_axes: tuple[str, ...] = ()) -> None:
    if passed:
        _say(publish, "The mesh meets your brief.",
             "every acceptance criterion the brief set was checked and met")
    else:
        axes = ", ".join(a.replace("_", " ") for a in failed_axes if a)
        _say(publish, "The mesh does not meet your brief yet.",
             f"these criteria were not met: {axes}" if axes
             else "at least one acceptance criterion was not met")


# pipeline


def terminal_failure(publish: Any, *, classification: str = "") -> None:
    _say(publish, "The run ended without a delivered mesh.",
         f"the pipeline classified this as: {classification}" if classification
         else "the pipeline could not complete the requested mesh")


async def areviewer_verdict(publish: ExecutionEventPublisher, *, passed: bool,
                            failed_axes: tuple[str, ...] = ()) -> None:
    if passed:
        await _asay(publish, "The mesh meets your brief.",
                    "every acceptance criterion the brief set was checked and met")
    else:
        axes = ", ".join(a.replace("_", " ") for a in failed_axes if a)
        await _asay(publish, "The mesh does not meet your brief yet.",
                    f"these criteria were not met: {axes}" if axes
                    else "at least one acceptance criterion was not met")
