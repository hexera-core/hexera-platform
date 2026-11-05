# Responsibility: Detect a turn that changed nothing, so a loop cannot spin without progress.
# Boundaries: it measures authored state before and after; the response to a no-op is the policy's.
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import meshpipeline.agents.builder.settings as bcfg

#: Consecutive no-op retries after which the retry budget is exhausted.
NOOP_LIMIT = 2


def authored_digest(workspace: Path, engine: str) -> str:
    from meshpipeline.agents.builder.tools import get_spec_run_files

    digest = hashlib.md5()
    found = False
    for relative in get_spec_run_files(engine):
        path = Path(workspace) / relative
        if path.exists():
            digest.update(relative.encode())
            digest.update(path.read_bytes())
            found = True
    return digest.hexdigest() if found else ""


@dataclass(frozen=True)
class NoopVerdict:

    repeated: bool
    consecutive: int
    retry_count: int

    @property
    def budget_exhausted(self) -> bool:
        return self.consecutive >= NOOP_LIMIT


def assess(*, before: str, after: str, carried_count: int, retry_count: int) -> NoopVerdict:
    repeated = bool(before) and bool(after) and before == after
    consecutive = carried_count + 1 if repeated else carried_count
    effective = retry_count
    if consecutive >= NOOP_LIMIT:
        effective = bcfg.MAX_BUILDER_RETRIES + 1     # route to the failure sink
    return NoopVerdict(repeated=repeated, consecutive=consecutive, retry_count=effective)


__all__ = ["NOOP_LIMIT", "NoopVerdict", "assess", "authored_digest"]
