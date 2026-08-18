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


def result_key_beside(output_object_key: str) -> str:
    return output_object_key.rsplit("/", 1)[0] + "/result.json"
