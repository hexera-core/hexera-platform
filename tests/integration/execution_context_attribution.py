# Responsibility: Attribute each real Redis event to the exact production call site that published it.
# Boundaries: observation only - it wraps nothing's behaviour and publishes nothing itself.
from __future__ import annotations

import importlib
import json
import pathlib
import sys

#: The production package, asked of the import system rather than guessed from a layout: the
#: frames this attributes are the ones actually imported, wherever they were installed from.
SRC = pathlib.Path(importlib.import_module("meshpipeline").__file__).resolve().parent

#: The two things this harness cannot derive: the canonical target set, and the ordinals that
#: tell repeated call sites in one definition apart. Both belong to the authority scanner, which
#: is a development tool - a runtime image that ships only `tests/` carries neither.
_QUALITY = pathlib.Path(__file__).resolve().parents[2] / "devtools" / "quality"
MANIFEST = _QUALITY / "publication_authority_manifest.json"

#: The gate and the adapter are how an event travels, never where it was decided.
_TRANSPARENT = {"execution_publisher.py", "redis.py"}


def unavailable_reason() -> str:
    # Absent, this harness could still record events - it just could not say which canonical
    # context they came from. It says so instead of measuring something weaker.
    if not MANIFEST.exists():
        return f"the publication-authority manifest is not present at {MANIFEST}"
    if not (_QUALITY / "publication_authority.py").exists():
        return f"the publication-authority scanner is not present at {_QUALITY}"
    return ""


def targets() -> dict[str, dict]:
    # THE target set, derived from the committed manifest. There is no second hand-written
    # list: if the manifest changes, this changes with it and the comparison below fails.
    rows = json.loads(MANIFEST.read_text())
    out = {}
    for r in rows:
        if r["lifecycle"] != "execution-owned":
            continue
        ident = f"{r['path']}::{r['qualname']}::{r['semantic']}#{r['ordinal']}"
        assert ident not in out, f"the manifest holds a duplicate identity: {ident}"
        out[ident] = r
    return out


def _line_index() -> dict:
    # (path, qualname, semantic, lineno) -> ordinal, taken from the SAME AST order the scanner
    # assigns, so six `check` calls in one function are six distinguishable sites at runtime.
    if str(_QUALITY) not in sys.path:
        sys.path.insert(0, str(_QUALITY))
    import publication_authority as PA

    index: dict = {}
    for site in PA.scan():
        if site.lifecycle != PA.L_EXECUTION:
            continue
        index[(site.path, site.qualname, site.semantic, site.lineno)] = int(
            site.canonical.rsplit("#", 1)[1])
    return index


_LINES: dict | None = None


def lines() -> dict:
    # Built once, on first use - importing this module must not require the scanner to exist.
    global _LINES
    if _LINES is None:
        _LINES = _line_index()
    return _LINES

#: what the adapter is asked to do -> the semantic event
SEMANTIC = {"note": "note", "warn": "warn", "error": "error", "stage": "stage",
            "attempt": "attempt", "check": "check", "action": "action", "search": "search",
            "screenshot": "screenshot", "file": "file", "reasoning": "reasoning",
            "rationale": "rationale", "tool_call": "tool_call", "tool_result": "tool_result",
            "meshing": "meshing", "meshed": "meshed", "verdict": "verdict", "closing": "closing"}


def _production_frame(naive: bool = False):
    frame = sys._getframe(2)
    if naive:                      # the earlier attribution error, kept as a negative control
        return frame
    # The first frame that is PRODUCTION. The gate and the adapter are how an event travels,
    # and a test observer - this one or another suite's - is never where it was decided.
    while frame is not None:
        f = pathlib.Path(frame.f_code.co_filename).resolve()
        if f.is_relative_to(SRC) and f.name not in _TRANSPARENT:
            return frame
        frame = frame.f_back
    return sys._getframe(2)


def _qualname(frame) -> str:
    # the scanner names a nested function `outer.inner`; the interpreter says
    # `outer.<locals>.inner`
    return frame.f_code.co_qualname.replace(".<locals>", "")


def observe(monkeypatch, seen: list, *, naive: bool = False) -> None:
    # Wraps the inner adapter's methods to RECORD who called them. The real method always runs,
    # the publisher object is never replaced, and nothing is injected into the payload.
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    from meshpipeline.application import execution_fence as fence

    index = lines()

    def wrap(method: str):
        original = getattr(JobPublisher, method)

        def w(self, *a, **k):
            frame = _production_frame(naive)
            try:
                rel = pathlib.Path(frame.f_code.co_filename).resolve().relative_to(
                    SRC).as_posix()
            except ValueError:
                rel = frame.f_code.co_filename
            qual = _qualname(frame)
            semantic = SEMANTIC[method]
            ordinal = index.get((rel, qual, semantic, frame.f_lineno), 0)
            own = fence.current_ownership()
            seen.append({
                "context": f"{rel}::{qual}::{semantic}#{ordinal}",
                "path": rel, "qualname": qual, "semantic": semantic, "method": method,
                "lineno": frame.f_lineno, "op_id": k.get("op_id", ""),
                "args": [str(x)[:60] for x in a],
                "publisher": id(self), "job_id": str(getattr(self, "job_id", "")),
                "generation": getattr(own, "execution_generation", None),
                "token_fingerprint": (
                    __import__("hashlib").sha256(str(own.worker_token).encode()).hexdigest()[:12]
                    if own is not None else ""),
            })
            return original(self, *a, **k)

        monkeypatch.setattr(JobPublisher, method, w)

    for method in SEMANTIC:
        wrap(method)


def executed(seen: list) -> set[str]:
    return {r["context"] for r in seen}


def per_context(seen: list) -> dict:
    out: dict = {}
    for r in seen:
        out.setdefault(r["context"], []).append(r)
    return out


# MEASUREMENT and CERTIFICATION are deliberately two contracts. Measurement may legitimately
# observe a subset - what it may never do is misattribute. Certification is the equality that
# only the completed campaign can satisfy.


def measure(seen: list) -> dict:
    target = targets()
    observed = executed(seen)
    ambiguous = sorted({r["context"] for r in seen if r["context"].endswith("#0")})
    return {
        "targets": sorted(target),
        "observed": sorted(observed),
        "missing": sorted(set(target) - observed),
        "unexpected": sorted(observed - set(target)),
        "ambiguous": ambiguous,
        "by_path": {p: sum(1 for c in observed if c.split("::")[0] == p)
                    for p in sorted({c.split("::")[0] for c in observed})},
        "target_total": len(target),
        "observed_total": len(observed & set(target)),
    }


class AttributionUnsound(AssertionError):
    pass


def assert_measurement_sound(result: dict) -> None:
    # Green means the INSTRUMENT is trustworthy, not that coverage is complete.
    problems = []
    if result["unexpected"]:
        problems.append(f"observed identities outside the manifest: {result['unexpected']}")
    if result["ambiguous"]:
        problems.append(
            f"calls that could not be attributed to a manifest site: {result['ambiguous']}")
    # A LITERAL on purpose, like the scanner's ordinals: the denominator must not follow the
    # manifest silently, so growth fails closed here until the campaign re-certifies. 61 was the
    # reconciled set at the certification campaign (1 relocated + 17 renamed + 43 unchanged);
    # four sites were added since, each regenerated into the manifest and proven in the
    # certification ledger by a named committed scenario (the fourth is the infra-retry node's
    # note, scenario infra-retry:transient-replay), and the combined measurement reaches
    # 65/65 - so 65 is the calibrated denominator.
    if result["target_total"] != 65:
        problems.append(f"the manifest holds {result['target_total']} execution contexts, not 65")
    if problems:
        raise AttributionUnsound("; ".join(problems))


def assert_every_context_certified(result: dict) -> None:
    # NOT wired into check-fast: it is expected to fail until every production root has a
    # behavioural scenario.
    missing = result["missing"]
    if missing:
        raise AssertionError(
            f"{len(missing)} of {result['target_total']} execution contexts were never "
            f"behaviourally executed:\n  " + "\n  ".join(missing))
