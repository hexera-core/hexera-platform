# Responsibility: Stand in for the Redis execution fence so a service-free test can claim delivery.
# Boundaries: the external mirror only - ownership checks, claim state and transactions stay real.
from __future__ import annotations


class RecordingFence:
    # The real contract, in memory: install writes, refresh and revoke are compare-and-swap, and
    # a mismatch changes nothing. A permissive stand-in that said yes to everything would let a
    # broken lifecycle pass, which is the one thing this boundary exists to catch.
    def __init__(self, *, install_succeeds: bool = True) -> None:
        self.install_succeeds = install_succeeds
        self.held: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.calls: list[tuple] = []

    def fingerprint(self, job_id: str, generation: int, worker_token: object) -> str:
        from meshpipeline.contracts.event_stream import fence_fingerprint
        return fence_fingerprint(job_id, generation, worker_token)

    def install(self, job_id: str, value: str, ttl_seconds: int) -> bool:
        self.calls.append(("install", job_id, value, int(ttl_seconds)))
        if not self.install_succeeds:
            return False
        self.held[job_id] = value
        self.ttl[job_id] = int(ttl_seconds)
        return True

    def refresh(self, job_id: str, value: str, ttl_seconds: int) -> bool:
        self.calls.append(("refresh", job_id, value, int(ttl_seconds)))
        if self.held.get(job_id) != value:      # never creates, never replaces another fence
            return False
        self.ttl[job_id] = int(ttl_seconds)
        return True

    def revoke(self, job_id: str, value: str) -> bool:
        self.calls.append(("revoke", job_id, value))
        if self.held.get(job_id) != value:      # a mismatch is someone else's fence
            return False
        self.held.pop(job_id, None)
        self.ttl.pop(job_id, None)
        return True

    def current(self, job_id: str) -> str:
        return self.held.get(job_id, "")

    def operations(self) -> list[str]:
        return [c[0] for c in self.calls]


def install(monkeypatch, *, install_succeeds: bool = True) -> RecordingFence:
    # TWO seams, both the ones production actually resolves: the application's module-level
    # installer and the claim authority's revoke/refresh lookup. Neither the repository, the
    # ownership check nor the transaction boundary is touched, so a suite may supply whatever
    # LeaseRepository double it likes.
    import meshpipeline.application.execution_fence as _app
    import meshpipeline.persistence.lease as _lease

    double = RecordingFence(install_succeeds=install_succeeds)
    monkeypatch.setattr(_lease, "_fence_ops", lambda: double)
    monkeypatch.setattr(_app, "install_execution_fence",
                        lambda job_id, fingerprint, ttl: double.install(job_id, fingerprint, ttl))
    return double
