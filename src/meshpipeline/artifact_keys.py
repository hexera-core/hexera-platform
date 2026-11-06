# Responsibility: Derive the object-storage keys a job's inputs, outputs and results live under.
# Boundaries: one place so a key is never spelled twice.
from __future__ import annotations

_PREFIX = "jobs"

#: The mesh phases a partial may be published for, stated HERE because these names become object
#: paths and this module is the one place a key is spelled. `engines/snappy/progressive` holds the
#: same vocabulary for the mesher's side; a test keeps the two from drifting.
PARTIAL_STAGES = ("castellated",)


def input_key(job: str) -> str:
    return f"{_PREFIX}/{job}/input.tar.gz"


def output_key(job: str) -> str:
    return f"{_PREFIX}/{job}/output.tar.gz"


def result_key(job: str) -> str:
    return f"{_PREFIX}/{job}/result.json"


def mesh_artifact_key(job: str) -> str:
    return f"{_PREFIX}/{job}/mesh.msh"


def export_key(job: str, file_name: str) -> str:
    """One solver file of an FEA mesh (mesh once, export many): jobs/<job>/exports/<file>."""
    return f"{_PREFIX}/{job}/exports/{file_name}"


def repair_report_key(job: str, digest: str = "") -> str:
    """Where an inspection report's bytes live.

    KEYED BY CONTENT when a digest is given, because two writers can race for one job: a retry, or
    a worker whose lease was taken over. Sharing one key let the loser overwrite bytes a COMMITTED
    artifact row already pointed at, so a customer could download a report that did not match the
    record. Content-addressed, an identical re-delivery writes identical bytes to the same place
    and a differing one cannot reach the committed object at all.
    """
    return f"{_PREFIX}/{job}/repair_report-{digest[:16]}.json" if digest else \
        f"{_PREFIX}/{job}/repair_report.json"


def repaired_cad_key(job: str, digest: str, suffix: str = ".step") -> str:
    """Where repaired geometry's bytes live. Content-addressed, for the same reason."""
    safe = suffix if suffix.startswith(".") else f".{suffix}"
    return f"{_PREFIX}/{job}/repaired-{digest[:16]}{safe}"


def result_key_beside(output_object_key: str) -> str:
    return output_object_key.rsplit("/", 1)[0] + "/result.json"


def progress_key_beside(output_object_key: str) -> str:
    """Where a running remote mesher says which stage it is at (runtime/mesh_runner's beacon)."""
    return output_object_key.rsplit("/", 1)[0] + "/progress.json"


def partial_key_beside(output_object_key: str, stage: str) -> str:
    """Where a running remote mesher leaves a boundary surface of the mesh-so-far.

    A SIBLING of the beacon and of the result, derived the same way, and deliberately NOT inside
    the beacon: the beacon is a tiny blob written on a 30-second timeout as a courtesy, and a few
    megabytes of geometry through it would make a mesh wait on a viewer frame.

    `stage` names the phase, so a second frame later cannot overwrite the first. It is restricted
    to the phase names this repo publishes rather than interpolated as given: this becomes an
    object path, and the keys beside it are the ones a run's own exchange is made of.
    """
    if stage not in PARTIAL_STAGES:
        raise ValueError(f"{stage!r} is not a published mesh phase; one of {PARTIAL_STAGES}")
    return f"{output_object_key.rsplit('/', 1)[0]}/partial-{stage}.json"


def is_partial_key(object_key: str) -> bool:
    """Whether `object_key` is a key THIS module could have derived for a partial.

    Checked again at the point of reading, even though the key comes from the run's own event log
    rather than from a request: a key is an object path, and one place deriving it is not the same
    as one place trusting it.
    """
    import re
    return bool(re.fullmatch(rf"{_PREFIX}/[0-9a-f]{{64}}/partial-"
                             rf"(?:{'|'.join(PARTIAL_STAGES)})\.json", str(object_key or "")))
