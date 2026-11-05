# Responsibility: Derive the object-storage keys a job's inputs, outputs and results live under.
# Boundaries: one place so a key is never spelled twice.
from __future__ import annotations

_PREFIX = "jobs"


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
