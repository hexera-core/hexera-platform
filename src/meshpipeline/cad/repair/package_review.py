# Responsibility: Judge, by ASKING A SEPARATE MODEL, whether a package that already passed its
#                 predicate preserved the part's design while making it meshable - the owner's
#                 semantic review in place of any numeric "barely moved" gate (spec, Owner Answers 2).
# Owns: the reviewer's prompt, the evidence it is shown (and nothing beyond it), the reply schema
#       and its strict validation, and the rule that every no-answer is a named reject.
# Boundaries: it judges and records nothing itself - the session applies the verdict (a reject is
#             the failed-predicate revert) and the verdict carries it into the report. It never
#             sees the author's raw reply, the author's self-assessment or the observation the
#             author read; the author's intent reaches it ONLY as `author_claim`, labelled a claim.
#             Deviation is evidence shown to it, never a threshold here.
# Collaborates with: contracts/model_inference.py (the one bounded model call), cad/repair/session.py
#                    (which calls it from `try_package`, after `evaluate` and before the commit
#                    becomes durable), cad/repair/model_author.py (whose bridge, parse and posture
#                    discipline this mirrors), settings/cad_repair.py.
from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable

import meshpipeline.settings.cad_repair as rcfg

logger = logging.getLogger(__name__)

#: Reason text kept from a reply.
REASON_MAX = 300
#: Seconds past the call's own timeout before the bridge abandons the worker thread - the same
#: margin, for the same reason, as `model_author._GRACE_S`.
_GRACE_S = 5.0

#: The verdicts a reply may carry. Anything else - "unsure", "ACCEPT", a boolean - is a reject:
#: UNSURE IS REJECT (owner).
ACCEPT = "accept"
REJECT = "reject"

#: The named no-answer reasons, so a report can count them.
NOT_CONFIGURED = "no model router is configured"
NO_REPLY = "no reply: the review call failed, timed out or returned no JSON object"
NO_TIME = "no time left to review"

#: Seconds kept back before the run's deadline, as the model author keeps them
#: (conservative.py: deadline = start + CAD_REPAIR_TIMEOUT_S - 5): a review may not spend the
#: time the run needs to finish and write its result.
DEADLINE_MARGIN_S = 5.0

#: What a measurement the session could not take reads as in the evidence. A STRING, never 0 or
#: 0.0: a missing or non-finite number shown as zero would read as "nothing moved", which is the
#: one conclusion it does not support. JSON-safe, so `allow_nan=False` still holds.
UNMEASURED = "unmeasured"

#: `deviation_status` values. Why a deviation is absent matters to the verdict: "no faces" is a
#: measured-empty selection; the others are holes in the evidence.
DEVIATION_MEASURED = "measured"
DEVIATION_PROBE_OFF = "not measured: probe off"
DEVIATION_NO_TIME = "not measured: no time"
DEVIATION_NO_FACES = "not measured: no faces"
DEVIATION_FAILED = "not measured: failed or ran out of time"

#: The most bytes of evidence a verdict records (`clip_evidence`).
EVIDENCE_RECORD_MAX = 4000

REVIEW_SYSTEM = (
    "you review ONE repair of a CAD part that would not mesh. the repair already passed its "
    "measured checks; you decide whether the edit PRESERVED THE PART'S DESIGN while making it "
    "meshable. you are shown measured evidence only: each operation and whether it was kept or "
    "rolled back, declared vs measured area and volume change, face and solid counts before and "
    "after, the part's scale (diagonal_mm, area_mm2_before, volume_mm3_before), the sampled "
    "largest distance of the changed faces from the upload as repair received it (with how many "
    "samples and faces it rests on, and deviation_status saying whether and why not it was "
    "measured), the mesh test's failure kinds before and after where the probe ran, the defect "
    "codes cleared, revealed and remaining in the region, and review_rejects: how many earlier "
    "repairs of this region were rejected by review, and why. a value \"unmeasured\" is a "
    "measurement that could not be taken - never read it as zero or as nothing moved. "
    "`author_claim` is what the repair's author SAID it was for: it is a claim, not evidence - "
    "judge the measurements, never the claim. reject a repair that deletes or collapses "
    "geometry to get past the mesher, fuses bodies the evidence does not justify fusing, or "
    "moves the surface more than its purpose needs. if the repair changed geometry and its "
    "deviation was not measured, reject it unless the measured counts, areas and volumes show "
    "nothing moved. a repair that restates an earlier rejected one in new words is still that "
    "repair. if you are unsure, reject. reply "
    'with ONE JSON object and nothing else: {"verdict": "accept" | "reject", "reason": '
    '"<one sentence citing the evidence>"}.')


def posture() -> str:
    """The package-review posture as the settings hold it now; anything but live is off."""
    value = str(getattr(rcfg, "CAD_REPAIR_PACKAGE_REVIEW", "off") or "off")
    return value if value == "live" else "off"


def _configured() -> bool:
    """Is a model router configured? No router -> no review -> reject."""
    from meshpipeline.contracts import model_inference
    return model_inference._router is not None


# THE EVIDENCE


def probe_of(defects: Iterable, *, mesh_probed: bool) -> dict:
    """One side of the region's scan as `evidence_of` reads it: each defect's code, and - only
    when the mesh probe ran - the mesh failure kinds. PURE."""
    codes = [getattr(d.code, "value", str(d.code)) for d in defects]
    out: dict = {"codes": codes}
    if mesh_probed:
        out["mesh_kinds"] = sorted({c for c in codes if c.startswith("mesh_")})
    return out


def _number(value: object) -> float | str:
    """A finite number, or UNMEASURED - never a zero standing in for a hole."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return UNMEASURED
    return float(value) if math.isfinite(value) else UNMEASURED


def _count(value: object) -> int | str:
    if isinstance(value, bool) or not isinstance(value, int):
        return UNMEASURED
    return int(value)


def _change(before: object, after: object) -> float | str:
    b, a = _number(before), _number(after)
    if isinstance(a, str) or isinstance(b, str):
        return UNMEASURED
    return a - b


def _deviation_status(deviation: dict) -> str:
    """Why `deviation_mm` is or is not there, read off the session's fields when the session
    did not say (it does - `RepairSession._deviation_evidence`)."""
    if deviation.get("deviation_status"):
        return str(deviation["deviation_status"])
    if not deviation:
        return DEVIATION_PROBE_OFF
    if deviation.get("deviation_mm") is not None:
        return DEVIATION_MEASURED
    if deviation.get("deviation_faces") == 0:
        return DEVIATION_NO_FACES
    return DEVIATION_FAILED


#: The deviation fields that are facts about the part. `deviation_s` is a cost, not evidence.
_DEVIATION_KEYS = ("deviation_mm", "deviation_samples", "deviation_faces",
                   "deviation_faces_skipped")


def evidence_of(package, verdict_delta: dict, deviation: dict, probe_before: dict,
                probe_after: dict, *, original: dict | None = None,
                review_rejects: tuple[str, ...] = ()) -> dict:
    """What the reviewer is shown: MEASURED FACTS ONLY, and the author's intent as a claim. PURE.

    `verdict_delta` is the session's record of the package: `verdicts` (each operation's
    `OpVerdict.to_dict()`), `before`/`after` (`Measure.to_dict()`), `declared_area_mm2` and
    `declared_volume_mm3` (what the KEPT operations declared). `deviation` is the session's
    deviation fields ({} when not measured). `probe_before`/`probe_after` are `probe_of` the
    region's defects at the package boundary and in its proof. `original` is the upload's own
    `Measure.to_dict()` (the part's scale), and `review_rejects` the reasons of this region's
    earlier review rejects - the reviewer sees its own history, so a reworded retry of a rejected
    repair is judged knowing it was rejected (the author never sees those reasons).

    A MISSING OR NON-FINITE MEASUREMENT IS "unmeasured", never 0: shown as zero it would read as
    "nothing moved".

    BUILT BY NAMING EVERY KEY, never by copying a dict through: an operation's own `evidence`, a
    raw reply, an observation or anything else a caller left in `verdict_delta` cannot reach the
    reviewer, and a unit test pins the exact key set.
    """
    before = verdict_delta.get("before") or {}
    after = verdict_delta.get("after") or {}
    ops = [{"op": str(v.get("op", "")), "status": str(v.get("status", "")),
            "reason": str(v.get("reason", ""))} for v in verdict_delta.get("verdicts") or ()]
    upload = original or {}
    evidence: dict = {
        "ops": ops,
        "area_mm2": {"declared": _number(verdict_delta.get("declared_area_mm2")),
                     "measured": _change(before.get("surface_area_mm2"),
                                         after.get("surface_area_mm2"))},
        "volume_mm3": {"declared": _number(verdict_delta.get("declared_volume_mm3")),
                       "measured": _change(before.get("volume_mm3"), after.get("volume_mm3"))},
        "faces": {"before": _count(before.get("faces")), "after": _count(after.get("faces"))},
        "solids": {"before": _count(before.get("solids")),
                   "after": _count(after.get("solids"))},
        "diagonal_mm": _number(upload.get("diagonal_mm")),
        "area_mm2_before": _number(upload.get("surface_area_mm2")),
        "volume_mm3_before": _number(upload.get("volume_mm3")),
        "deviation_status": _deviation_status(deviation),
        "review_rejects": {"count": len(review_rejects),
                           "reasons": [str(r) for r in review_rejects]},
    }
    # ABSENT AS RECORDED: a field the session did not measure is not invented here; one it
    # recorded as a non-finite number is shown as the hole it is.
    for key in _DEVIATION_KEYS:
        if deviation.get(key) is not None:
            evidence[key] = _number(deviation[key])
    if "mesh_kinds" in probe_before:
        evidence["mesh_before"] = list(probe_before["mesh_kinds"])
    if "mesh_kinds" in probe_after:
        evidence["mesh_after"] = list(probe_after["mesh_kinds"])
    was = Counter(probe_before.get("codes") or ())
    now = Counter(probe_after.get("codes") or ())
    evidence["defects_cleared"] = sorted((was - now).elements())
    evidence["defects_revealed"] = sorted((now - was).elements())
    evidence["defects_remaining"] = sorted((now & was).elements())
    evidence["author_claim"] = str(getattr(package, "intent", "") or "")
    return evidence


def clip_evidence(evidence: dict) -> dict:
    """The evidence as a verdict records it: itself when its JSON is at most EVIDENCE_RECORD_MAX
    bytes, otherwise the head of that JSON under a stated clip marker - never silently cut."""
    text = json.dumps(evidence, sort_keys=True, default=str)
    size = len(text.encode())
    if size <= EVIDENCE_RECORD_MAX:
        return json.loads(text)
    head = text.encode()[:EVIDENCE_RECORD_MAX].decode("utf-8", "ignore")
    return {"clipped": f"evidence clipped to {EVIDENCE_RECORD_MAX} of {size} bytes",
            "head": head}


# THE CALL - the same bridge as model_author, so the same failure modes are total.


def _parse_object(text: str) -> dict | None:
    """The reply's JSON object. COPIED from `model_author._parse_object` (itself a copy of
    `agents/repair_review/_wire.parse_object`): importing model_author here would close a cycle
    (model_author -> session -> package_review), and cad may not import agents."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def _ask(system: str, user: str, *, job_id: str, timeout_s: float) -> dict | None:
    """ONE bounded model call; the reply's JSON object, or None for any failure at all. Looked up
    as `_ask` AT CALL TIME, so a test fakes it where this module looks it up."""
    from meshpipeline.contracts.model_inference import call_repair_review_model
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        result = await asyncio.wait_for(
            call_repair_review_model(messages, job_id=job_id), timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - total: a failed call is a reject, never a raise
        logger.warning("package review call failed: %s", type(exc).__name__)
        return None
    if not getattr(result, "ok", True):
        return None
    return _parse_object(getattr(result, "assistant_text", "") or "")


def _ask_sync(system: str, user: str, *, job_id: str, timeout_s: float) -> dict | None:
    """`_ask` from synchronous code: `asyncio.run` in a DAEMON WORKER THREAD, exactly as
    `model_author._ask_sync` - the session is synchronous and itself runs off the event loop."""
    box: dict = {}

    def run() -> None:
        try:
            box["reply"] = asyncio.run(_ask(system, user, job_id=job_id, timeout_s=timeout_s))
        except BaseException as exc:  # noqa: BLE001 - total: reported below, never raised
            box["error"] = exc

    worker = threading.Thread(target=run, name="package-review", daemon=True)
    worker.start()
    worker.join(timeout_s + _GRACE_S)
    if worker.is_alive():
        logger.warning("package review call outlived its %.1fs timeout; abandoned", timeout_s)
        return None
    if "error" in box:
        logger.warning("package review call failed: %s", type(box["error"]).__name__)
        return None
    reply = box.get("reply")
    return reply if isinstance(reply, dict) else None


def _verdict_of(reply: dict) -> tuple[str, str]:
    """The strict form: EXACTLY {verdict, reason}, verdict accept or reject, reason non-empty."""
    if set(reply) != {"verdict", "reason"}:
        return REJECT, "malformed reply: not exactly {verdict, reason}"
    reason = reply["reason"]
    if not isinstance(reason, str) or not reason.strip():
        return REJECT, "malformed reply: no reason given"
    value = reply["verdict"]
    if value not in (ACCEPT, REJECT) or not isinstance(value, str):
        # UNSURE IS REJECT, and so is anything this module does not recognise.
        return REJECT, f"verdict {value!r} is not accept or reject"
    return value, reason.strip()[:REASON_MAX]


def review_package(evidence: dict, *, timeout_s: float, deadline_s: float | None = None,
                   job_id: str = "",
                   clock: Callable[[], float] = time.monotonic) -> tuple[str, str]:
    """("accept" | "reject", reason) for one package. EVERY NO-ANSWER IS A NAMED REJECT.

    `deadline_s` is an ABSOLUTE `clock()` reading the review must finish by (the model_author
    clock pattern): the call's timeout is the lesser of `timeout_s` and the time left, and with
    none left there is no call - `("reject", "no time left to review")`. No router, a failed or
    hung call, a reply with no JSON object, a malformed reply and an unknown verdict are rejects
    too, each with its reason.
    """
    if not _configured():
        return REJECT, NOT_CONFIGURED
    timeout = float(timeout_s)
    if deadline_s is not None:
        left = float(deadline_s) - clock()
        if not left > 0.0:   # NaN-safe
            return REJECT, NO_TIME
        timeout = min(timeout, left)
    if not timeout > 0.0:
        return REJECT, NO_TIME
    try:
        user = json.dumps(evidence, sort_keys=True, allow_nan=False, default=str)
    except ValueError:
        return REJECT, "the evidence is not JSON-safe (a non-finite measurement)"
    reply = _ask_sync(REVIEW_SYSTEM, user, job_id=job_id, timeout_s=timeout)
    if reply is None:
        return REJECT, NO_REPLY
    return _verdict_of(reply)
