# Responsibility: Make a filesystem scan that finds nothing fail loudly instead of passing vacuously.
# Boundaries: the guard every source-walking test wraps its subject in; it asserts nothing about what it finds.
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


def scanned(paths: Iterable[T], what: str, *, at_least: int = 1) -> list[T]:
    found = sorted(paths)  # type: ignore[type-var]
    if len(found) < at_least:
        raise AssertionError(
            f"scanned {len(found)} of an expected {at_least}+ for {what!r} - the scan found "
            "nothing to inspect, so whatever this test asserts about them is vacuous. The "
            "subject has moved, been renamed or been emptied; point the scan at its new home."
        )
    return found


def scanned_dir(directory: Path, pattern: str, what: str, *, at_least: int = 1) -> list[Path]:
    if not directory.is_dir():
        raise AssertionError(
            f"{directory} is not a directory, so the scan for {what!r} inspected nothing")
    return scanned(directory.glob(pattern), what, at_least=at_least)


__all__ = ["scanned", "scanned_dir"]
