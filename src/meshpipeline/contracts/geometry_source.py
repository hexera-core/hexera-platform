# Responsibility: Identify an uploaded geometry by its CONTENT and carry it, with its interpretation, through a run.
# Owns: content hashing, suffix handling, object-key derivation, and the materialized-geometry value.
# Boundaries: it derives identity and location.
# Collaborates with: contracts/geometry_units.py, application/geometry_materializer.py and the object-storage adapters.
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict

# Uploaded geometry lives in its own object namespace. It is NOT a delivered artifact, NOT a mesh
# exchange workspace and NOT a user-facing download: those have their own prefixes and lifecycles,
# and mixing them would let "delete the outputs" destroy the input.
SOURCE_PREFIX = "sources"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
# Materialisation writes a generated basename, never the user's filename. Only the suffix carries
# forward, because parser dispatch still keys off it - and even that is sanitised rather than
# trusted, since it arrived inside a client-supplied name.
_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,15}$")
_READ_CHUNK = 1024 * 1024


class GeometrySourceError(RuntimeError):

    def __init__(self, *args, failure_class=None, dependency="geometry_source"):
        super().__init__(*args)
        self.failure_class = failure_class
        # WHICH dependency failed, for the operator record. The execution entry prepares two
        # things behind one handler - the durable checkpoint and the source bytes - so a raise
        # that does not name itself is reported as the other one. A checkpoint store that could
        # not be read was being dead-lettered against `geometry_source` and returned as
        # `geometry_unavailable`, which sends an operator to the object store to investigate a
        # Postgres problem.
        self.dependency = dependency


def sha256_of(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_READ_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


def safe_suffix(suffix_hint: str) -> str:
    s = str(suffix_hint or "").strip().lower()
    return s if _SAFE_SUFFIX.match(s) else ""


def source_object_key(source_id) -> str:
    ident = str(source_id).strip()
    if not ident or "/" in ident or ".." in ident or "\\" in ident:
        raise GeometrySourceError(f"unusable source id for an object key: {ident!r}")
    return f"{SOURCE_PREFIX}/{ident}"


_REF_FIELDS = ("source_id", "owner_id", "object_key", "sha256", "size_bytes",
               "original_filename", "suffix_hint")


@dataclass(frozen=True, slots=True)
class GeometrySourceRef:

    source_id: str
    owner_id: str
    object_key: str
    sha256: str
    size_bytes: int
    original_filename: str
    suffix_hint: str

    def __post_init__(self) -> None:
        if not _HEX64.match(self.sha256 or ""):
            raise GeometrySourceError("source sha256 must be 64 lowercase hex characters")
        if int(self.size_bytes) <= 0:
            raise GeometrySourceError("source size_bytes must be positive")
        for field in ("source_id", "owner_id", "object_key"):
            if not str(getattr(self, field) or "").strip():
                raise GeometrySourceError(f"source {field} is required")

    @classmethod
    def from_row(cls, row) -> GeometrySourceRef:
        return cls(source_id=str(row.id), owner_id=str(row.owner_id),
                   object_key=str(row.object_key), sha256=str(row.sha256),
                   size_bytes=int(row.size_bytes),
                   original_filename=str(row.original_filename or ""),
                   suffix_hint=str(row.suffix_hint or ""))

    def to_payload(self) -> dict:
        return {f: (int(getattr(self, f)) if f == "size_bytes" else str(getattr(self, f)))
                for f in _REF_FIELDS}

    @classmethod
    def from_payload(cls, payload) -> GeometrySourceRef:
        if not isinstance(payload, dict):
            raise GeometrySourceError("geometry source snapshot is missing or malformed")
        try:
            return cls(source_id=str(payload["source_id"]), owner_id=str(payload["owner_id"]),
                       object_key=str(payload["object_key"]), sha256=str(payload["sha256"]),
                       size_bytes=int(payload["size_bytes"]),
                       original_filename=str(payload.get("original_filename", "")),
                       suffix_hint=str(payload.get("suffix_hint", "")))
        except KeyError as exc:
            raise GeometrySourceError(
                f"geometry source snapshot is missing {exc.args[0]!r}") from exc
        except (TypeError, ValueError) as exc:
            raise GeometrySourceError(f"geometry source snapshot is malformed: {exc}") from exc

    def identity(self) -> dict:
        return {"source_id": self.source_id, "sha256": self.sha256,
                "size_bytes": int(self.size_bytes), "suffix_hint": self.suffix_hint}

    def disagreements_with(self, other: GeometrySourceRef) -> list[str]:
        return [f for f in _REF_FIELDS if str(getattr(self, f)) != str(getattr(other, f))]


_INTERP_FIELDS = ("interpretation_id", "geometry_source_id", "unit", "scale_to_metres", "basis")


@dataclass(frozen=True, slots=True)
class GeometryInterpretationRef:

    interpretation_id: str
    geometry_source_id: str
    unit: str
    scale_to_metres: float
    basis: str
    evidence: str = ""

    def __post_init__(self) -> None:
        from meshpipeline.contracts.geometry_units import (
            SCALE_TO_METRES,
            LengthUnit,
            ResolutionBasis,
        )
        for field in ("interpretation_id", "geometry_source_id"):
            if not str(getattr(self, field) or "").strip():
                raise GeometrySourceError(f"interpretation {field} is required")
        try:
            unit = LengthUnit(self.unit)
        except ValueError as exc:
            raise GeometrySourceError(f"unsupported geometry unit {self.unit!r}") from exc
        try:
            ResolutionBasis(self.basis)
        except ValueError as exc:
            raise GeometrySourceError(f"unsupported resolution basis {self.basis!r}") from exc
        # The factor is not taken on trust: it must be the one the unit defines, so a payload
        # cannot smuggle in a scale the vocabulary never sanctioned.
        expected = SCALE_TO_METRES[unit]
        if float(self.scale_to_metres) != expected:
            raise GeometrySourceError(
                f"scale {self.scale_to_metres} does not match {unit.value} ({expected})")

    @classmethod
    def from_domain(cls, interpretation) -> GeometryInterpretationRef:
        return cls(interpretation_id=str(interpretation.interpretation_id),
                   geometry_source_id=str(interpretation.geometry_source_id),
                   unit=interpretation.unit.value,
                   scale_to_metres=float(interpretation.scale_to_metres),
                   basis=interpretation.basis.value,
                   evidence=str(interpretation.evidence or ""))

    def to_payload(self) -> dict:
        return {"interpretation_id": self.interpretation_id,
                "geometry_source_id": self.geometry_source_id,
                "unit": self.unit, "scale_to_metres": float(self.scale_to_metres),
                "basis": self.basis, "evidence": self.evidence}

    @classmethod
    def from_payload(cls, payload) -> GeometryInterpretationRef:
        if not isinstance(payload, dict):
            raise GeometrySourceError("geometry interpretation snapshot is missing or malformed")
        try:
            return cls(interpretation_id=str(payload["interpretation_id"]),
                       geometry_source_id=str(payload["geometry_source_id"]),
                       unit=str(payload["unit"]),
                       scale_to_metres=float(payload["scale_to_metres"]),
                       basis=str(payload["basis"]),
                       evidence=str(payload.get("evidence", "")))
        except KeyError as exc:
            raise GeometrySourceError(
                f"geometry interpretation snapshot is missing {exc.args[0]!r}") from exc
        except (TypeError, ValueError) as exc:
            raise GeometrySourceError(
                f"geometry interpretation snapshot is malformed: {exc}") from exc

    def identity(self) -> dict:
        return {"interpretation_id": self.interpretation_id,
                "geometry_source_id": self.geometry_source_id,
                "unit": self.unit, "scale_to_metres": float(self.scale_to_metres),
                "basis": self.basis}

    def disagreements_with(self, other: GeometryInterpretationRef) -> list[str]:
        return [f for f in _INTERP_FIELDS
                if str(getattr(self, f)) != str(getattr(other, f))]


class GeometryState(TypedDict):

    ref: dict
    # The physical interpretation this run was approved under. Present whenever geometry is,
    # because bytes without a scale cannot be meshed.
    interpretation: NotRequired[dict]
    # ABSENT until something materialises. Identity exists from the moment of upload; a local file
    # exists only inside a process that downloaded one, so an API turn legitimately carries the
    # reference alone. Present-but-empty is not a third state - it is refused.
    local_path: NotRequired[str]


@dataclass(frozen=True, slots=True)
class MaterializedGeometry:

    ref: GeometrySourceRef
    #: The physical meaning these bytes carry. Required: a verified handle whose scale is unknown
    #: cannot be meshed, and leaving it optional is how the implicit-metres assumption survived.
    interpretation: GeometryInterpretationRef
    local_path: Path

    def __post_init__(self) -> None:
        # A handle with no path is not a weaker handle, it is a false one: every consumer reads
        # `path` and would receive "" as if it were a file. There is no caller for whom an empty
        # path is the right answer - a stage that has not materialised anything must carry no
        # geometry at all rather than a handle that claims to have one.
        if not str(self.local_path or "").strip():
            raise GeometrySourceError(
                "a materialized geometry must name a real local file; "
                "state with no local file must carry no geometry at all")

        if self.interpretation.geometry_source_id != self.ref.source_id:
            # The two snapshots describe different uploads; meshing either would be a guess.
            raise GeometrySourceError(
                "the geometry interpretation does not belong to this source")

    @property
    def path(self) -> str:
        return str(self.local_path)

    @property
    def prepared(self):
        from meshpipeline.contracts.coordinate_state import from_source_file
        from meshpipeline.contracts.geometry_units import (
            GeometryInterpretation,
            LengthUnit,
            ResolutionBasis,
        )
        return from_source_file(GeometryInterpretation(
            interpretation_id=self.interpretation.interpretation_id,
            owner_id=self.ref.owner_id,
            geometry_source_id=self.interpretation.geometry_source_id,
            unit=LengthUnit(self.interpretation.unit),
            scale_to_metres=float(self.interpretation.scale_to_metres),
            basis=ResolutionBasis(self.interpretation.basis),
            evidence=self.interpretation.evidence))

    def to_state(self) -> GeometryState:
        return GeometryState(ref=self.ref.to_payload(),
                             interpretation=self.interpretation.to_payload(),
                             local_path=str(self.local_path))

    @classmethod
    def from_state(cls, value) -> MaterializedGeometry | None:
        if not isinstance(value, dict) or not value.get("ref"):
            return None
        local = str(value.get("local_path", "") or "").strip()
        if not local:
            # The reference survived the checkpoint but no file did. Callers get "nothing is
            # materialised here", which is true, instead of a handle to the empty string.
            return None
        interp = value.get("interpretation")
        if not interp:
            # A checkpoint carrying bytes but no physical meaning cannot be resumed into meshing:
            # the scale would have to be assumed, which is the failure this contract removes.
            raise GeometrySourceError(
                "geometry state carries no interpretation - its physical scale is unknown")
        return cls(ref=GeometrySourceRef.from_payload(value["ref"]),
                   interpretation=GeometryInterpretationRef.from_payload(interp),
                   local_path=Path(local))
