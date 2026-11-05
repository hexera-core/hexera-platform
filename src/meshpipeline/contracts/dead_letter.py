# Responsibility: Declare the sink that receives work no consumer could complete.
# Boundaries: a Protocol and its process-wide binding only; the durable implementation lives in adapters/dead_letter/.
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class DeadLetterError(RuntimeError):
    pass


@runtime_checkable
class DeadLetterSink(Protocol):
    def append(self, record: dict[str, Any]) -> None:
        ...

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        ...


_sink: DeadLetterSink | None = None


def set_dead_letter_sink(sink: DeadLetterSink | None) -> None:
    global _sink
    _sink = sink


def append(record: dict[str, Any]) -> None:
    if _sink is None:
        raise DeadLetterError(
            "no dead-letter sink configured - runtime composition must call set_dead_letter_sink()")
    _sink.append(record)


def recent(limit: int = 100) -> list[dict[str, Any]]:
    if _sink is None:
        raise DeadLetterError(
            "no dead-letter sink configured - runtime composition must call set_dead_letter_sink()")
    return _sink.recent(limit)
