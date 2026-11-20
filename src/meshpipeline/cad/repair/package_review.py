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
from meshpipeline.cad.repair import caps

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
    "samples and faces it rests on, deviation_basis saying which faces were sampled - edited_faces, "
    "changed_faces, adjacent_to_removed (the faces now covering a face a removal took away) or "
    "whole_part (a sparse sample of the whole part, when a removal left no neighbour) - and "
    "deviation_status saying whether and why not it was measured), removed (present only when "
    "kept operations removed faces: how many, their declared area_mm2, and area_fraction of the "
    "part's area_mm2_before), the mesh test's failure kinds before and after where the probe ran, the defect "
    "codes cleared, revealed and remaining in the region, and review_rejects: how many earlier "
    "repairs of this region were rejected by review, and why. a value \"unmeasured\" is a "
    "measurement that could not be taken - never read it as zero or as nothing moved. "
    "`author_claim` is what the repair's author SAID it was for: it is a claim, not evidence - "
    "judge the measurements, never the claim. reject a repair that deletes or collapses REAL "
    "geometry - a feature, a wall, a body, or area or volume beyond what the removed debris "
    "itself carries - to get past the mesher. removing sliver or degenerate DEBRIS faces is "
    "acceptable when ALL of these hold: the removal is declared (removed), its area_fraction is "
    "negligible, the measured deviation_mm is negligible relative to diagonal_mm (deviation_basis "
    "says which faces it rests on), and solids after equal solids before, so no body or feature "
    "was lost. reject a repair that fuses bodies the evidence does not justify fusing, or "
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


# THE MEASURED OBJECTION - what a REJECT returns to the author, and the only thing it returns.
#
# The reviewer stays SEALED from the author. The author's attempt log says "rejected by review"
# (session.REVIEW_REJECTED_REASON) and, under the live posture, WHICH MEASURED FIELD was objected
# to and WHAT THE SESSION MEASURED there. The reviewer's sentence goes to the report and to the
# reviewer's own next evidence; it never crosses to the author, because an author handed the
# grader's reasoning learns to word its way past the grader (controller ruling, P3 Task 4).

#: The ONE field name for "the reviewer named nothing this module recognises". Every unknown name,
#: extra key or malformed objection collapses to it - a name the reviewer cannot write into.
OBJECTION_UNSPECIFIED = "unspecified"

#: THE CLOSED SET OF FIELDS A REJECTION MAY OBJECT TO - each a MEASURED key of `evidence_of`'s
#: output: declared vs measured area and volume, the face and solid counts, the deviation fields
#: and why they are or are not there, the mesh test's failure kinds before and after, and the
#: defect codes cleared, revealed and remaining.
#:
#: IT CONTAINS NO FREE-TEXT FIELD, on purpose. `ops` (each operation's reason), `author_claim` and
#: `review_rejects` (earlier reviewers' prose) are evidence keys and are deliberately absent: the
#: objection is the one thing that crosses to the author, so the only strings it can name are ones
#: this tuple already spells out.
OBJECTION_FIELDS = (
    "area_mm2",
    "volume_mm3",
    "faces",
    "solids",
    "deviation_mm",
    "deviation_samples",
    "deviation_faces",
    "deviation_faces_skipped",
    "deviation_status",
    "deviation_basis",
    "removed",
    "mesh_before",
    "mesh_after",
    "defects_cleared",
    "defects_revealed",
    "defects_remaining",
)

#: Bounds on the measured value an objection carries, so one oversized evidence field cannot
#: become the author's whole next observation.
OBJECTION_ITEMS_MAX = 24
OBJECTION_TEXT_MAX = 60


#: APPENDED to REVIEW_SYSTEM under the live posture only, so with the posture off the reviewer is
#: sent exactly the bytes it is sent today. It asks for a FIELD NAME, never an explanation: the
#: author is shown the field and the session's own measurement of it, so a number or a sentence
#: written here would be discarded anyway.
OBJECTION_RULE = (
    " if you reject, ALSO name the one measured field your objection rests on, as "
    '{"objection": {"field": "<field>"}} beside verdict and reason, where <field> is exactly one '
    "of: " + ", ".join(OBJECTION_FIELDS) + ". name the field only - no value and no explanation; "
    "the repair's author is shown the field name and the measurement we took for it, never your "
    "words. omit the key if no single field carries your objection.")


def objection_posture() -> str:
    """The measured-objection posture as the settings hold it now; anything but live is off.

    READ DEFENSIVELY (`getattr`) AND FAIL CLOSED: with no such setting present at all - which is
    the state until the flag is plumbed - this is "off", and every path below behaves exactly as
    it did before the objection existed.
    """
    value = str(getattr(rcfg, "CAD_REPAIR_REVIEW_OBJECTION", "off") or "off")
    return value if value == "live" else "off"


def _measured(value: object) -> object:
    """One evidence value as an objection may carry it: JSON scalars, and bounded lists and dicts
    of them; anything else UNMEASURED. It never parses or reformats - the value stays the
    session's own measurement - it only bounds it. PURE."""
    if isinstance(value, bool):
        return UNMEASURED
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return value[:OBJECTION_TEXT_MAX]
    if isinstance(value, (list, tuple)):
        return [_measured(v) for v in list(value)[:OBJECTION_ITEMS_MAX]]
    if isinstance(value, dict):
        return {str(k)[:OBJECTION_TEXT_MAX]: _measured(v)
                for k, v in list(value.items())[:OBJECTION_ITEMS_MAX]}
    return UNMEASURED


def _objection_of(reply: dict, evidence: dict) -> dict:
    """The measured objection a rejection returns: `{"field": f, "measured": m}`. PURE.

    FROM THE REPLY THIS TAKES ONE THING: a field NAME, which must be a member of
    `OBJECTION_FIELDS` - a closed tuple of this module's own string literals. A non-string, an
    unknown name, an extra key, a missing or malformed `objection`: all collapse to
    `OBJECTION_UNSPECIFIED`. So the only strings that can leave here are ones this module wrote.

    `measured` is read from the SESSION'S `evidence` under that field, NEVER from the reply: a
    reviewer citing a number the session did not measure hands the author the session's number,
    not its own. A field the evidence does not carry reads UNMEASURED, never 0.
    """
    field = OBJECTION_UNSPECIFIED
    raw = reply.get("objection")
    if isinstance(raw, dict) and set(raw) == {"field"}:
        named = raw["field"]
        if isinstance(named, str) and named in OBJECTION_FIELDS:
            # NAMED, NOT COPIED: our own tuple's member, so not even an equal string object from
            # the reply is what travels on.
            field = OBJECTION_FIELDS[OBJECTION_FIELDS.index(named)]
    if field == OBJECTION_UNSPECIFIED or field not in evidence:
        return {"field": field, "measured": UNMEASURED}
    return {"field": field, "measured": _measured(evidence[field])}


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


def net_faces_removed(evidence) -> int:
    """Faces an operation's declarations say it REMOVED and did not replace: `faces_removed` less
    `faces_added`, never below 0. A swap (`RebuildFace`: one out, one in) removes no surface; the
    sliver sweep (`FixSmallFaces`) declares removals with nothing added. 0 for anything unreadable
    - a removal is never INVENTED here; the tier's veto reads `removed_fingerprints` as well."""
    def count(key: str) -> int:
        value = (evidence or {}).get(key, 0)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0
    return max(0, count("faces_removed") - count("faces_added"))


def removes_surface(evidence) -> bool:
    """Did this operation take surface away (not merely replace it)? The tier's removal veto."""
    return net_faces_removed(evidence) > 0 or bool((evidence or {}).get("removed_fingerprints"))


def _removed(verdicts, area_before) -> dict:
    """What the KEPT operations declared they removed - face count, declared area, and that area
    as a fraction of the upload's - so a reviewer can tell sliver DEBRIS from real geometry.
    {} when nothing was removed. Read only from each kept verdict's `faces_removed` and
    `area_removed_mm2` declarations (`FixSmallFaces`), named here, never copied through."""
    faces = 0
    area: float | str = 0.0
    for verdict in verdicts:
        if verdict.get("status") != "kept":
            continue
        declared = verdict.get("evidence") or {}
        # NET OF WHAT WAS ADDED: a face replaced (RebuildFace's one-out-one-in) is not debris, and
        # its missing area must not turn a known sliver total into "unmeasured".
        count = net_faces_removed(declared)
        if count > 0:
            faces += count
            value = _number(declared.get("area_removed_mm2"))
            # One undeclared area makes the total a hole, never a smaller number.
            area = UNMEASURED if isinstance(area, str) or isinstance(value, str) \
                else area + value
    if not faces:
        return {}
    total = _number(area_before)
    fraction: float | str = UNMEASURED
    if not isinstance(area, str) and not isinstance(total, str) and total > 0:
        fraction = area / total
    return {"faces": faces, "area_mm2": area, "area_fraction": fraction}


def evidence_of(package, verdict_delta: dict, deviation: dict, probe_before: dict,
                probe_after: dict, *, original: dict | None = None,
                review_rejects: tuple[str, ...] = (),
                delivered: dict | None = None) -> dict:
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
    # WHICH BODIES CHANGED, not how many. On 0051_00513292 a committed `sew_faces` took solids
    # 7 -> 8 with the aggregate volume moving +0.942 mm3, while the real event was a 3601 mm3
    # body becoming closed: an open shell's faces already contributed a NUMBER to the volume
    # integral rather than a volume, so the aggregate understated it about 3800-fold. A reviewer
    # shown only the count and the aggregate cannot tell that from a body cut in two.
    bodies = caps.solid_change_of(before.get("solid_volumes") or (),
                                  after.get("solid_volumes") or ())
    ops = [{"op": str(v.get("op", "")), "status": str(v.get("status", "")),
            "reason": str(v.get("reason", ""))} for v in verdict_delta.get("verdicts") or ()]
    upload = original or {}
    evidence: dict = {
        **({"solids_appeared_mm3": _number(bodies["appeared_mm3"]),
            "solids_disappeared_mm3": _number(bodies["disappeared_mm3"]),
            # A COUNT, because what a reviewer does with it is notice that it is not zero.
            "solids_split": len(bodies["split"])} if bodies["known"] else {}),
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
        **({"deviation_basis": str(deviation["deviation_basis"])}
           if deviation.get("deviation_basis") else {}),
        "review_rejects": {"count": len(review_rejects),
                           "reasons": [str(r) for r in review_rejects]},
    }
    # ABSENT AS RECORDED: a field the session did not measure is not invented here; one it
    # recorded as a non-finite number is shown as the hole it is.
    for key in _DEVIATION_KEYS:
        if deviation.get(key) is not None:
            evidence[key] = _number(deviation[key])
    removed = _removed(verdict_delta.get("verdicts") or (), upload.get("surface_area_mm2"))
    if removed:
        evidence["removed"] = removed
    if "mesh_kinds" in probe_before:
        evidence["mesh_before"] = list(probe_before["mesh_kinds"])
    # `mesh_after` IS THE DELIVERED ARTEFACT'S READING when there is one. Measured in the pipeline
    # image on abc 0060_00608290, three probes each and every one identical: the repaired shape IN
    # MEMORY fails with 5 x mesh_edge_through_face, and the same geometry written to STEP and read
    # back MESHES. A reviewer shown the in-memory probe refuses repairs whose delivered file
    # meshes - correct on its evidence, wrong about the product. The owner's headline is a
    # repaired STEP that gmsh meshes, and the keep rule already judges cures on the delivered
    # STEP, so review may not judge a different object. The in-memory reading is KEPT BESIDE IT,
    # labelled, so the disagreement stays visible instead of being silently replaced.
    if delivered is not None and "mesh_kinds" in delivered:
        evidence["mesh_after"] = list(delivered["mesh_kinds"])
        evidence["mesh_after_source"] = "delivered"
        if "mesh_kinds" in probe_after:
            evidence["mesh_after_in_memory"] = list(probe_after["mesh_kinds"])
    elif "mesh_kinds" in probe_after:
        # NO DELIVERED READING: no time left, or an export that failed. Say which reading this is,
        # so an unmeasured delivered probe can never be mistaken for a passing one.
        evidence["mesh_after"] = list(probe_after["mesh_kinds"])
        evidence["mesh_after_source"] = "in_memory_only"
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
            # ITS OWN LABEL: the one-shot author asks as "RepairReview", and telemetry must tell a
            # reviewer's call from an author's.
            call_repair_review_model(messages, job_id=job_id, name="RepairPackageReview"),
            timeout=timeout_s)
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


def _verdict_of(reply: dict, *, objection: bool = False) -> tuple[str, str]:
    """The strict form: EXACTLY {verdict, reason}, verdict accept or reject, reason non-empty.

    With `objection` - the live posture only - an `objection` key is ALSO allowed, and only that
    one extra key. With the posture off the schema is untouched, so a reply carrying an objection
    is the malformed reject it is today.
    """
    allowed = [{"verdict", "reason"}]
    if objection:
        allowed.append({"verdict", "reason", "objection"})
    if set(reply) not in allowed:
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
    """("accept" | "reject", reason) for one package - `review_package_objection` without the
    objection. The pre-objection signature, so every existing caller is unchanged."""
    verdict, reason, _ = review_package_objection(
        evidence, timeout_s=timeout_s, deadline_s=deadline_s, job_id=job_id, clock=clock)
    return verdict, reason


def review_package_objection(
        evidence: dict, *, timeout_s: float, deadline_s: float | None = None,
        job_id: str = "",
        clock: Callable[[], float] = time.monotonic) -> tuple[str, str, dict | None]:
    """("accept" | "reject", reason, objection) for one package. EVERY NO-ANSWER IS A NAMED REJECT.

    `objection` is the MEASURED objection (`_objection_of`), and is not None only for a REJECT
    under the live `CAD_REPAIR_REVIEW_OBJECTION` posture - the session hands it to the author and
    keeps `reason` for the report and the reviewer's own next evidence. An ACCEPT has nothing to
    object to (None). A no-answer reject named no field, so it carries the UNSPECIFIED objection
    rather than inventing one: the author is still owed the fact that review refused it.

    `deadline_s` is an ABSOLUTE `clock()` reading the review must finish by (the model_author
    clock pattern): the call's timeout is the lesser of `timeout_s` and the time left, and with
    none left there is no call - `("reject", "no time left to review")`. No router, a failed or
    hung call, a reply with no JSON object, a malformed reply and an unknown verdict are rejects
    too, each with its reason.
    """
    live = objection_posture() == "live"
    unnamed = {"field": OBJECTION_UNSPECIFIED, "measured": UNMEASURED} if live else None

    def rejected(reason: str) -> tuple[str, str, dict | None]:
        return REJECT, reason, unnamed

    if not _configured():
        return rejected(NOT_CONFIGURED)
    timeout = float(timeout_s)
    if deadline_s is not None:
        left = float(deadline_s) - clock()
        if not left > 0.0:   # NaN-safe
            return rejected(NO_TIME)
        timeout = min(timeout, left)
    if not timeout > 0.0:
        return rejected(NO_TIME)
    try:
        user = json.dumps(evidence, sort_keys=True, allow_nan=False, default=str)
    except ValueError:
        return rejected("the evidence is not JSON-safe (a non-finite measurement)")
    # The objection rule is APPENDED, never folded in: with the posture off the reviewer is sent
    # the same bytes it is sent today.
    system = REVIEW_SYSTEM + (OBJECTION_RULE if live else "")
    reply = _ask_sync(system, user, job_id=job_id, timeout_s=timeout)
    if reply is None:
        return rejected(NO_REPLY)
    verdict, reason = _verdict_of(reply, objection=live)
    if verdict == ACCEPT or not live:
        return verdict, reason, None
    return verdict, reason, _objection_of(reply, evidence)
