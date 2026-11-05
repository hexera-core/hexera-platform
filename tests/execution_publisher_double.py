# Responsibility: Stand in for the execution publication port so a service-free test needs no database.
# Boundaries: it records what was published; it proves nothing about ownership or durability.
from __future__ import annotations


class RecordingExecutionPublisher:
    # Implements the real async protocol and nothing else: no synchronous emitter, no fallback,
    # and no PostgreSQL or Redis access. Each call completes only when awaited.
    def __init__(self, job_id: str, agent: str = "") -> None:
        self.job_id, self.agent = job_id, agent
        self.calls: list[dict] = []

    def _record(self, method: str, **fields) -> None:
        self.calls.append({"method": method, "agent": self.agent, **fields})

    async def anote(self, text: str, tone: str = "info", op_id: str = "") -> None:
        self._record("anote", text=text, tone=tone, op_id=op_id)

    async def awarn(self, text: str, op_id: str = "") -> None:
        self._record("awarn", text=text, op_id=op_id)

    async def aerror(self, text: str, op_id: str = "") -> None:
        self._record("aerror", text=text, op_id=op_id)

    async def astage(self, op_id: str = "") -> None:
        self._record("astage", op_id=op_id)

    async def aattempt(self, n: int, of: int, op_id: str = "") -> None:
        self._record("aattempt", n=n, of=of, op_id=op_id)

    async def acheck(self, statement: str, *, ok: bool) -> None:
        self._record("acheck", statement=statement, ok=ok)

    async def aaction(self, actions: list[str]) -> None:
        self._record("aaction", actions=list(actions))

    async def asearch(self, query: str) -> None:
        self._record("asearch", query=query)

    async def ascreenshot(self, image_b64: str, op_id: str = "") -> None:
        self._record("ascreenshot", op_id=op_id)

    async def afile(self, display_path: str, byte_count: int,
                    operation: str = "created") -> None:
        self._record("afile", display_path=display_path, byte_count=byte_count,
                     operation=operation)

    async def areasoning(self, rid: str, phase: str, *, duration_ms: int | None = None,
                         token_count: int | None = None, content: str | None = None,
                         status: str = "active") -> None:
        self._record("areasoning", rid=rid, phase=phase, status=status, content=content)

    async def arationale(self, conclusion: str, because: str = "") -> None:
        self._record("arationale", conclusion=conclusion, because=because)

    async def atool_call(self, cid: str, tool_name: str, arguments: object = None,
                         status: str = "started", op_id: str = "") -> None:
        self._record("atool_call", cid=cid, tool_name=tool_name, status=status, op_id=op_id)

    async def atool_result(self, rid: str, call_id: str, tool_name: str, result: object = None,
                           status: str = "success", duration_ms: int | None = None,
                           op_id: str = "") -> None:
        self._record("atool_result", rid=rid, call_id=call_id, tool_name=tool_name,
                     status=status, op_id=op_id)

    async def ameshing(self, engine: str, budget_s: int, history: dict | None = None) -> None:
        self._record("ameshing", engine=engine, budget_s=budget_s)

    async def ameshed(self, cells: int | None = None) -> None:
        self._record("ameshed", cells=cells)

    async def averdict(self, verdict: str, summary: str = "") -> None:
        self._record("averdict", verdict=verdict, summary=summary)

    async def aclosing(self, text: str, event_id: str = "") -> None:
        self._record("aclosing", text=text, event_id=event_id)


def install(monkeypatch, module) -> list[RecordingExecutionPublisher]:
    # Patches the symbol the module actually resolves, never the ownership authority.
    made: list[RecordingExecutionPublisher] = []

    def _factory(job_id, agent=""):
        pub = RecordingExecutionPublisher(str(job_id), agent)
        made.append(pub)
        return pub

    monkeypatch.setattr(module, "execution_publisher", _factory)
    return made
