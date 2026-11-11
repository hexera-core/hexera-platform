# Responsibility: SHELL MESHING ON THE FEA STAGE, ONLY WHEN THE USER SAID IT (SHSTAGE-2..5). Words the user wrote
# (application/mesh_as, purpose_map: "shell", "mid-surface", "plate elements", "CQUAD4", "S4", "SHELL181" ...) are
# the only way to a shell: plain "FEA" is a solid mesh, exactly as before shell existed. When they asked for a
# shell, this runs the shell check of the part in the geometry-check worker (cad/shell_preview in a process of its
# own, up to SHELL_CHECK_LIMIT_S), keeps a short summary of it, and says what the stage shows: the mid-surface,
# what is not in the shell and where plates do not meet - and settles the choice when the user presses Proceed, so
# the run never waits on the user.
# Boundaries: never guesses or suggests a shell - no recommendation, no pick on the stage. A shell asked for on a
# part nothing can be made of (a triangle file, a part with no thin wall) is meshed solid, said in one sentence;
# never a refusal. The view functions are pure.
# Collaborates with: cad/shell_preview.py (the extraction and the drawing), application/geometry_check.py (the
# store, the upload's reading, the FEA naming that starts this), application/fea_check.py (the FEA view this
# joins), api/v1/geometry.py (serves the view and the drawing, starts a check asked for later, settles it),
# application/pipeline_run.py (reads the settled choice into the run), engines/fea_driver.py (meshes the shell),
# contracts/geometry_check.py (the worker seam), ui/js/render/fea_form.js and ui/js/viewer/geometry_stage.js.
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from meshpipeline.application import purpose_map as pm

logger = logging.getLogger(__name__)

#: The stored shell check, under the session's geometry check (geometry_check.check_object_key).
STATUS_FILE = "shell/status.json"
RECORD_FILE = "shell/shell.json"
SURFACE_FILE = "shell/midsurface.brep"
OVERLAY_FILE = "shell/overlay.json"
#: Its states.
MEASURING, READY, UNAVAILABLE = "measuring", "ready", "unavailable"
#: SHSTAGE-2: the shell check's own time limit, seconds. The extraction takes up to about ten minutes on the
#: biggest parts of the training corpus (sh0020/21: 528 s); past this the stage says the mid-surface could not be
#: drawn, and the run still makes the shell itself.
SHELL_CHECK_LIMIT_S = 900
#: ... and a check still "measuring" this long after it started is read as stopped (a worker that died).
STALE_AFTER_S = SHELL_CHECK_LIMIT_S + 300
#: The CAD formats a mid-surface is extracted from (a triangle file has no faces to pair).
CAD_FORMATS = ("step", "stp", "iges", "igs", "brep")

QUESTION = "Mesh as"
SHELL_LABEL = "Shell: the mid-surface, meshed with plate elements"
SOLID_LABEL = "Solid"
MEASURING_WORDS = "Making the mid-surface. This can take a few minutes; you can proceed now."
SAID_SHELL = "You asked for a shell mesh."
#: A shell asked for that cannot be made: one plain sentence each, and the part is meshed solid (never a refusal)
TRIANGLES_WHY = ("You asked for a shell, but this file is triangles with no faces to find the mid-surface on, so it "
                 "is meshed as a solid.")
NOTHING_TO_SHELL = "You asked for a shell, but this part has no plate or thin wall to make one from, so it is meshed as a solid."
NOTHING_TO_SHELL_DETAIL = ("You asked for a shell, but this part has no plate or thin wall to make one from ({detail}), "
                           "so it is meshed as a solid.")
#: The check could not draw the mid-surface: the run makes the shell itself (engines/fea_driver.shell_pass)
NOT_DRAWN = "The mid-surface could not be drawn here ({reason}); the run still meshes the part as a shell."


def key(session_id: str, name: str) -> str:
    from meshpipeline.application.geometry_check import check_object_key

    return check_object_key(session_id, name)


def _store_json(object_key: str, payload: dict) -> None:
    from meshpipeline.application.geometry_check import _store_json as store

    store(object_key, payload)


def read_status(session_id: str) -> dict | None:
    """The stored shell check's status (with its summary once ready), or None before it was started."""
    from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

    try:
        got = json.loads(get_object_store().get_bytes(object_key=key(session_id, STATUS_FILE)))
    except ObjectNotFound:
        return None
    return got if isinstance(got, dict) else None


def is_cad(facts: dict | None) -> bool:
    """Whether the upload is a CAD file a mid-surface can be extracted from (not a triangle file)."""
    f = facts or {}
    return (str(f.get("source_format") or "").lower() in CAD_FORMATS
            and (f.get("validity") or {}).get("kind") != "triangles")


# ------------------------------------------------------------------------------------------------ the worker


def start(session_id: str, owner_id: str, *, facts: dict | None, interpretation: dict | None) -> bool:
    """SHSTAGE-2: start the shell check of an FEA part the user asked to mesh as a shell (wants_shell): the
    status says "measuring" at once (the stage shows it), and the worker runs it. A triangle file is not
    checked (no faces to pair). Only ever called for a said shell: a solid FEA part costs no worker time."""
    if not is_cad(facts):
        _store_json(key(session_id, STATUS_FILE), {"status": UNAVAILABLE, "reason": "triangles",
                                                   "written_at": time.time()})
        return False
    from meshpipeline.contracts import geometry_check as seam

    _store_json(key(session_id, STATUS_FILE), {"status": MEASURING, "started_at": time.time(),
                                               "written_at": time.time()})
    if not seam.enqueue_shell_check(session_id=session_id, owner_id=owner_id, interpretation=interpretation):
        _store_json(key(session_id, STATUS_FILE), {"status": UNAVAILABLE, "reason": "the shell check is not set up "
                                                   "on this deployment", "written_at": time.time()})
        return False
    return True


def run_shell_check(*, session_id: str, owner_id: str = "", interpretation: dict | None = None) -> dict:
    """The worker task body: read the upload as the FEA measurement reads it (the same coordinates, so the
    drawing lands on the stage's part), extract and draw it in a child process, store what it wrote and a
    short summary. Never raises: a check that cannot run says why (the run still makes the shell)."""
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.contracts.object_storage import get_object_store

    started = time.time()
    try:
        stored = gc.read_check(session_id) or {}
        with tempfile.TemporaryDirectory(prefix="shell_check_") as tmp:
            work = Path(tmp)
            cad, mm_per_unit = _cad_of(stored, interpretation, work)
            out = work / "out"
            proc = subprocess.run([sys.executable, "-m", "meshpipeline.cad.shell_preview", str(cad), str(out),
                                   repr(float(mm_per_unit))], capture_output=True, text=True,
                                  timeout=SHELL_CHECK_LIMIT_S, check=False)
            if proc.returncode != 0 or not (out / "shell.json").exists():
                raise RuntimeError(f"the shell check stopped (exit {proc.returncode}): {proc.stderr[-300:]}")
            record = json.loads((out / "shell.json").read_text())
            drawn = json.loads((out / "overlay.json").read_text())
            store = get_object_store()
            store.upload_file(local_path=out / "shell.json", object_key=key(session_id, RECORD_FILE))
            store.upload_file(local_path=out / "midsurface.brep", object_key=key(session_id, SURFACE_FILE))
            store.upload_file(local_path=out / "overlay.json", object_key=key(session_id, OVERLAY_FILE))
        status = {"status": READY, "written_at": time.time(), "seconds": round(time.time() - started, 1),
                  # the unit it was read in (the naming's): a unit corrected later re-reads its lengths
                  "scale_to_m": float(mm_per_unit) / 1000.0, "mm_per_unit": float(mm_per_unit),
                  "summary": summarise(record, drawn)}
    except subprocess.TimeoutExpired:
        status = {"status": UNAVAILABLE, "reason": "it took longer than the time allowed",
                  "written_at": time.time(), "seconds": round(time.time() - started, 1)}
    except Exception as exc:  # noqa: BLE001 - said on the stage; the run still makes the shell
        logger.warning("shell check failed - session=%s (%s: %s)", session_id, type(exc).__name__, str(exc)[:300])
        status = {"status": UNAVAILABLE, "reason": f"the part could not be read for it ({type(exc).__name__})",
                  "written_at": time.time(), "seconds": round(time.time() - started, 1)}
    _store_json(key(session_id, STATUS_FILE), status)
    logger.info("shell check %s - session=%s in %.1fs", status["status"], session_id, time.time() - started)
    return {"status": status["status"]}


def _cad_of(stored: dict, interpretation: dict | None, work: Path) -> tuple[Path, float]:
    """The upload as a CAD file and its millimetres per unit, read as the FEA measurement reads it."""
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.cad.ingest import canonicalise
    from meshpipeline.cad.normalise import occ_scale_transform
    from meshpipeline.contracts.geometry_source import GeometryInterpretationRef, GeometrySourceRef
    from meshpipeline.contracts.intake_formats import GeometryKind

    ref = GeometrySourceRef.from_payload(stored["source"])
    interp_ref = GeometryInterpretationRef.from_payload(interpretation) if interpretation else None
    fetched = gc._fetch(ref, work)
    canonical = canonicalise(fetched, work, stem="geometry", ecxml_form="placed")
    if canonical.kind is not GeometryKind.cad:
        raise ValueError("not a CAD file")
    prepared, _note = gc._prepared_coordinates(canonical.path, interp_ref, ref)
    return canonical.path, 1000.0 * float(occ_scale_transform(prepared).ScaleFactor())


def _r(v, n: int = 3):
    return None if v is None else round(float(v), n)


def summarise(record: dict, drawn: dict | None = None) -> dict:
    """What the stage needs of the shell record, kept small (the record itself is stored apart): the bodies'
    verdicts, the thin-wall share by area, the walls, every region that stays solid with its reason and place,
    and where the plates do not meet. Lengths in the record's millimetres."""
    drawn = drawn or {}
    places = drawn.get("solid_at_mm") or {}
    pairs = {str(p.get("id")): p for p in record.get("pairs") or []}
    faces = {str(f.get("face")): f for f in record.get("faces") or []}
    walls = [float(p["thickness_mm"]) for p in pairs.values() if p.get("shell") and p.get("thickness_mm")]
    bodies, area, thin = [], 0.0, 0.0
    regions: list[dict] = []
    for row in record.get("bodies") or []:
        body, verdict = str(row.get("body")), str(row.get("verdict") or "solid")
        a = float(row.get("area_mm2") or 0.0)
        area += a
        thin += a * float(row.get("shell_area_share") or 0.0)
        bodies.append({"body": body, "verdict": verdict})
        if verdict == "solid":
            regions.append({"body": body, "whole": True, "why": str(row.get("why") or ""),
                            "at_mm": places.get(body)})
        for k, reg in enumerate(row.get("solid_regions") or [], start=1):
            names = [str(n) for n in reg.get("faces") or []]
            # a "thickness" only when the two faces are a wall: as wide as they are apart at least. Two small
            # faces far apart (a bar's two end caps) are a length, not a thickness - no number is said then
            thick = sorted({float(pairs[p]["thickness_mm"]) for n in names for p in (faces.get(n) or {}).get("pairs") or []
                            if p in pairs and _is_wall(pairs[p])})
            regions.append({"body": body, "whole": False, "faces": names[:12], "n_faces": len(names),
                            "thick_mm": _r(thick[0]) if thick else None, "at_mm": places.get(f"{body}:{k}")})
    opened = [q for row in record.get("sewn") or [] for q in row.get("open_at_mm") or []]
    groups = [{"body": g.get("body"), "at_mm": (g.get("at_mm") or [None])[0]} for g in record.get("fallback_regions") or []]
    return {"bodies": bodies, "share": _r(thin / area) if area > 0 else 0.0,
            "t_min_mm": _r(min(walls)) if walls else None, "t_max_mm": _r(max(walls)) if walls else None,
            "solid_regions": regions[:50], "solid_more": max(0, len(regions) - 50),
            "open_edges": len(opened), "open_places": groups[:50] or [{"at_mm": q} for q in opened[:50]],
            "open_at_mm": opened[:400], "connected": bool((record.get("checks") or {}).get("connected", True)),
            "complete": bool(record.get("complete", False)), "notes": list(record.get("notes") or [])[:6],
            # SHELL-23: the walls that are a shell only because the user asked (short for their thickness): the
            # stage says one line of advice from these rows, in the unit it shows
            "short_walls": [{k: r.get(k) for k in ("body", "sheet", "thickness_mm", "span_mm", "width_mm", "span_walls",
                                                   "width_walls")} for r in (record.get("short_walls") or [])[:200]],
            "overlay": bool(drawn.get("triangles")), "t_min_drawn_mm": drawn.get("t_min"),
            "t_max_drawn_mm": drawn.get("t_max")}


# ------------------------------------------------------------------------------------------------ the stage


def _mm(v) -> str:
    v = float(v)
    return f"{v:.2g} mm" if v < 1 else f"{v:.3g} mm"


def _solid_detail(summary: dict | None, body: str | None = None) -> str:
    """SHSTAGE-3b: the measured reason a whole body stays solid ("its most plate-like wall is 8 mm thick and spans
    about 42 mm, ..."), read from the shell check's words after its colon; "" when there is none. With no body
    named, the part's: its one body's, or the reason every solid body shares."""
    whys = {str(r.get("why") or "") for r in (summary or {}).get("solid_regions") or []
            if r.get("whole") and (body is None or r.get("body") == body)}
    details = {w.split(": ", 1)[1].strip() for w in whys if ": " in w}
    return details.pop() if len(details) == 1 else ""


def _is_wall(pair: dict) -> bool:
    """A face pair that reads as a wall: it has a thickness, and its faces are at least as wide as they are
    apart (no width measured: taken as a wall, as before)."""
    t = pair.get("thickness_mm")
    if not t:
        return False
    w = pair.get("width_mm")
    return w is None or float(w) >= float(t)


#: The measured reason a body stays solid (cad/midsurface._solid_words), in a few words for the stage's one line.
_SPAN_WHY = re.compile(r"is ([\d.]+) mm thick and spans about [\d.]+ mm, only ([\d.]+) times")
_WIDTH_WHY = re.compile(r"is ([\d.]+) mm thick but only [\d.]+ mm wide, ([\d.]+) times")


def solid_short(detail: str) -> str:
    """A whole body's reason to stay solid in a few words: "too thick (4 mm wall, 9.7× span)", "too narrow (6 mm
    wall, 2× width)", "no thin wall"; "" when the words are none of these."""
    d = str(detail or "")
    m = _SPAN_WHY.search(d)
    if m:
        return f"too thick ({m.group(1)} mm wall, {m.group(2)}× span)"
    m = _WIDTH_WHY.search(d)
    if m:
        return f"too narrow ({m.group(1)} mm wall, {m.group(2)}× width)"
    if "round a tight bore" in d:
        return "a thick ring"
    if "no two of its faces run side by side" in d or "across a gap" in d:
        return "no plate"
    if "belong to a sheet with thicker walls" in d:
        return "part of a thicker sheet"
    return ""


def not_shelled(summary: dict | None) -> list[dict]:
    """What stays solid if the part is meshed as a shell, and where the plates do not meet, each in plain
    words with a tag and a place to mark: S1, S2 ... for solid regions, O1, O2 ... for open places. Each also
    carries `short`, the same in a few words for the stage's one line (the full words stay for its tooltip)."""
    s = summary or {}
    many = len(s.get("bodies") or []) > 1
    out = []
    for k, reg in enumerate(s.get("solid_regions") or [], start=1):
        who = reg.get("body") if many else "The part"
        name = reg.get("body") if many else "Whole part"
        if reg.get("whole"):
            words = (f"{reg.get('body')} stays solid: no wall of it is a plate." if many
                     else "The whole part stays solid: no wall of it is a plate.")
            detail = _solid_detail(s, reg.get("body"))
            if detail:
                words = f"{words[:-1]}: {detail}."
            short = f"{name} · {solid_short(detail) or 'no plate'}"
        else:
            n = int(reg.get("n_faces") or len(reg.get("faces") or []))
            where = f"A thick region of {who if many else 'the part'} ({n} face{'s' if n != 1 else ''})"
            words = (f"{where} stays solid: it is {_mm(reg['thick_mm'])} thick or more, too thick for a shell."
                     if reg.get("thick_mm") else f"{where} stays solid: it is not a thin wall.")
            faces = f"{n} face{'s' if n != 1 else ''}"
            short = (f"{reg.get('body') if many else 'Thick region'} · "
                     + (f"{_mm(reg['thick_mm'])}+ thick, {faces}" if reg.get("thick_mm") else f"not a thin wall, {faces}"))
        out.append({"id": f"S{k}", "kind": "solid", "words": words, "short": short, "at_mm": reg.get("at_mm"),
                    "faces": list(reg.get("faces") or [])})
    if s.get("solid_more"):
        out.append({"id": "", "kind": "solid", "words": f"And {s['solid_more']} more regions stay solid.",
                    "short": f"+{s['solid_more']} more stay solid", "at_mm": None, "faces": []})
    places = s.get("open_places") or []
    if s.get("open_edges"):
        n = len(places) or int(s["open_edges"])
        out.append({"id": "", "kind": "open_head", "at_mm": None, "faces": [],
                    "words": (f"The plates do not meet at {n} place{'s' if n != 1 else ''}: shown in red. A shell mesh "
                              "has a gap there."),
                    "short": f"Plates don't meet at {n} place{'s' if n != 1 else ''} (red)"})
        for k, p in enumerate(places, start=1):
            out.append({"id": f"O{k}", "kind": "open", "words": f"Plates do not meet here ({k} of {n}).",
                        "short": "plates don't meet", "at_mm": p.get("at_mm"), "faces": []})
    return out


def patch_words(summary: dict | None) -> str:
    """MIXED-9e on the stage: the shell check's regions that stay solid, said as the run makes them - meshed as
    solid and tied to the shell (engines/solid_patches.words); "" when nothing stays solid, or nothing is a shell."""
    from meshpipeline.engines.solid_patches import words

    s = summary or {}
    bodies = s.get("bodies") or []
    if not any(b.get("verdict") in ("shell", "mixed") for b in bodies):
        return ""
    rows: dict[str, dict] = {}
    for reg in s.get("solid_regions") or []:
        body = str(reg.get("body") or "")
        row = rows.setdefault(body, {"body": body, "whole": bool(reg.get("whole")), "count": 0})
        row["count"] += 1
    n_more = int(s.get("solid_more") or 0)
    patches = list(rows.values())
    if n_more and patches:
        patches[-1]["count"] += n_more
    return words(patches, bodies=len(bodies))


def _stale(status: dict | None, now: float) -> bool:
    if not status or status.get("status") != MEASURING:
        return False
    return now - float(status.get("started_at") or 0.0) > STALE_AFTER_S


def state_of(status: dict | None, *, cad: bool, now: float | None = None) -> tuple[str, str]:
    """(measuring | ready | unavailable, the reason it is unavailable)."""
    if not cad:
        return UNAVAILABLE, "triangles"
    if status is None:
        # the naming that starts it has not landed: the walls are about to be measured
        return MEASURING, ""
    if _stale(status, time.time() if now is None else now):
        return UNAVAILABLE, "it took longer than the time allowed"
    st = str(status.get("status") or "")
    if st in (MEASURING, READY):
        return st, ""
    return UNAVAILABLE, str(status.get("reason") or "the shell check did not run")


def shell_view(status: dict | None, facts: dict | None, said: dict | None = None, *,
               now: float | None = None) -> dict | None:
    """SHSTAGE-2..4: what the stage shows when the user asked for a shell (`said`: application/mesh_as.as_record);
    None otherwise - a solid FEA part's stage is the one from before shell existed. What it will be meshed as
    ("shell", or "solid" with the one-sentence reason when no shell can be made), and once the check is ready,
    what is not in the shell and the mid-surface's legend. Pure."""
    if pm.mesh_as_answer((said or {}).get("value")) != pm.MESH_SHELL:
        return None
    cad = is_cad(facts)
    state, reason = state_of(status, cad=cad, now=now)
    summary = (status or {}).get("summary") if state == READY else None
    if isinstance(summary, dict):
        summary = _in_unit(summary, (status or {}).get("scale_to_m"), (facts or {}).get("scale_to_m"))
    mesh_as, why = pm.MESH_SHELL, ""
    if not cad:
        mesh_as, why = pm.MESH_SOLID, TRIANGLES_WHY
    elif isinstance(summary, dict) and {b.get("verdict") for b in summary.get("bodies") or []} <= {"solid"}:
        detail = _solid_detail(summary)
        mesh_as = pm.MESH_SOLID
        why = NOTHING_TO_SHELL_DETAIL.format(detail=detail) if detail else NOTHING_TO_SHELL
    elif state == UNAVAILABLE:
        why = NOT_DRAWN.format(reason=reason or "the check did not run")
    elif isinstance(summary, dict) and summary.get("short_walls"):
        # SHELL-23: walls short for their thickness are shelled as asked, with one line of advice
        from meshpipeline.cad.shell_words import short_wall_words

        why = short_wall_words(list(summary["short_walls"]))
    if mesh_as == pm.MESH_SHELL and isinstance(summary, dict):
        # MIXED-9: what cannot be a shell is meshed as solid and tied to it - one line, none when nothing is solid
        patched = patch_words(summary)
        if patched:
            why = f"{why} {patched}".strip()
    out: dict = {"question": QUESTION, "state": state, "said": pm.MESH_SHELL, "said_words": SAID_SHELL,
           "mesh_as": mesh_as, "label": SHELL_LABEL if mesh_as == pm.MESH_SHELL else SOLID_LABEL, "reason": why,
           "measuring_words": MEASURING_WORDS if state == MEASURING and mesh_as == pm.MESH_SHELL else ""}
    if isinstance(summary, dict) and mesh_as == pm.MESH_SHELL:
        out.update(share=summary.get("share"), t_min_mm=summary.get("t_min_mm"), t_max_mm=summary.get("t_max_mm"),
                   bodies=summary.get("bodies"), not_shelled=not_shelled(summary),
                   open_edges=int(summary.get("open_edges") or 0),
                   open_at_mm=list(summary.get("open_at_mm") or []), overlay=bool(summary.get("overlay")),
                   legend_mm=[summary.get("t_min_drawn_mm"), summary.get("t_max_drawn_mm")],
                   scale=_k((status or {}).get("scale_to_m"), (facts or {}).get("scale_to_m")))
    return out


def _user_texts(messages) -> list[str]:
    return [str(m.get("content") or "") for m in messages or []
            if isinstance(m, dict) and m.get("role") == "user"]


def said_of(facts: dict | None, messages=None) -> dict:
    """What the user's words said about shell or solid. THE LATEST WORD STANDS: the newest message of the user's
    the word table reads as shell or solid (a shell asked for in a note after the check, or taken back), else the
    FEA naming's stored reading (the table, and the model for a paraphrase), else nothing said."""
    from meshpipeline.application.geometry_check import MESH_AS_KEY

    for text in reversed(_user_texts(messages)):
        reading = pm.mesh_as_of(text)
        if reading.value:
            return {"value": reading.value, "by": reading.by, "why": reading.why}
    got = (facts or {}).get(MESH_AS_KEY)
    if isinstance(got, dict):
        return got
    return {"value": "", "by": "", "why": "nothing said"}


def wants_shell(facts: dict | None, messages=None) -> bool:
    """Whether the user's words asked for a shell mesh - the only way to one."""
    return pm.mesh_as_answer(said_of(facts, messages).get("value")) == pm.MESH_SHELL


def _status_of(session_id: str) -> dict | None:
    try:
        return read_status(session_id)
    except Exception as exc:  # noqa: BLE001 - an unreadable store is "not drawn", never a broken stage
        logger.warning("shell check: status unreadable (%s)", type(exc).__name__)
        return {"status": UNAVAILABLE, "reason": "it could not be read"}


def view_for(session_id: str, facts: dict | None, messages=None) -> dict | None:
    """The stage's shell view for a session (None unless the user asked for a shell): its stored shell check and
    what the words said."""
    said = said_of(facts, messages)
    if pm.mesh_as_answer(said.get("value")) != pm.MESH_SHELL:
        return None
    return shell_view(_status_of(session_id), facts, said)


def start_if_asked(session_id: str, owner_id: str, *, facts: dict | None, messages=None,
                   interpretation: dict | None = None) -> bool:
    """A SHELL ASKED FOR AFTER THE FEA NAMING (a later message, a note): the shell check starts now, once - only for a
    measured FEA part (the naming's reading stored), and only when no check was started yet. True when started."""
    from meshpipeline.application.geometry_check import MESH_AS_KEY

    if not isinstance((facts or {}).get(MESH_AS_KEY), dict) or not wants_shell(facts, messages):
        return False
    if _status_of(session_id) is not None:
        return False
    return start(session_id, owner_id, facts=facts, interpretation=interpretation)


def _k(measured_under, now_in) -> float:
    try:
        a, b = float(measured_under or 0.0), float(now_in or 0.0)
    except (TypeError, ValueError):
        return 1.0
    return b / a if a > 0 and b > 0 else 1.0


def _in_unit(summary: dict, measured_under, now_in) -> dict:
    """The summary re-read in the unit the stage shows now (the user may have corrected it since)."""
    k = _k(measured_under, now_in)
    if abs(k - 1.0) <= 1e-12:
        return summary
    from meshpipeline.application.geometry_check import rescaled_lengths

    return rescaled_lengths(summary, k)


def settle(view: dict | None) -> dict:
    """SHSTAGE-5: THE CHOICE, SETTLED WHEN THE USER PRESSES PROCEED - the run never waits on the user. A shell only
    when the user's words asked for one (the view is None otherwise: a solid, as before shell existed); a shell
    asked for that cannot be made is a solid, with its one-sentence reason. Pressed while the check still measures,
    a said shell is a shell: the run makes it itself.
    {"mesh_as": "shell" | "solid", "picked": "shell" | "", "by": "words" | "", "why": plain words}"""
    v = view or {}
    if v.get("said") != pm.MESH_SHELL:
        return {"mesh_as": pm.MESH_SOLID, "picked": "", "by": "", "why": ""}
    if v.get("mesh_as") == pm.MESH_SOLID:
        return {"mesh_as": pm.MESH_SOLID, "picked": pm.MESH_SHELL, "by": "words", "why": str(v.get("reason") or "")}
    # (a surface file's view carries its own reason: it is a shell by what the file is, SURF-6)
    return {"mesh_as": pm.MESH_SHELL, "picked": pm.MESH_SHELL, "by": str(v.get("by") or "words"),
            "why": str(v.get("settle_why") or SAID_SHELL)}


#: The declaration's heads, one per settled choice (declaration_words); the request card reads them back
SHELL_HEAD = ("Meshed as: a shell - the mid-surface with plate elements, each with its wall thickness, in Nastran, "
              "CalculiX, Abaqus and Ansys files.")
MIXED_HEAD = ("Meshed as: shell where thin, solid where thick - plate elements on the thin walls joined to tetrahedra in "
              "the thick regions, in Nastran, CalculiX, Abaqus and Ansys files. The solid mesh is made beside it, as before.")
#: SURF-6: the head for a file that is a surface (application/surface_check): the plates lie on the surface itself
SURFACE_HEAD = ("Meshed as: a shell - plate elements on the surface as given, in Nastran, CalculiX, Abaqus and Ansys "
                "files.")
#: The request card's "Mesh as" row for a settled shell or shell + solid (a solid needs no row: it is the default)
CARD_LABELS = {pm.MESH_SHELL: "Shell (mid-surface, plate elements)",
               pm.MESH_MIXED: "Shell + solid (thin walls shell, thick regions solid)"}


def declaration_words(settled: dict | None) -> str:
    """The declaration's line for the settled choice: "Meshed as: a shell (the mid-surface ...). <why>"."""
    s = settled or {}
    if s.get("mesh_as") == pm.MESH_SHELL:
        head = SURFACE_HEAD if s.get("by") == "surface file" else SHELL_HEAD
    elif s.get("mesh_as") == pm.MESH_MIXED:
        head = MIXED_HEAD
    elif s.get("mesh_as") == pm.MESH_SOLID and s.get("picked") == pm.MESH_SHELL:
        # a shell asked for that could not be made: said; a plain solid says nothing, as before shell existed
        head = "Meshed as: a solid."
    else:
        return ""
    why = str(s.get("why") or "").strip()
    return f"{head} {why}".strip()


def declared_shell(messages) -> bool:
    """Whether the conversation's latest geometry declaration settled a SHELL (plates only, no tetrahedra): a part
    meshed as a shell or a surface file (SURF-6) - never shell + solid, which has tetrahedra in its thick regions."""
    from meshpipeline.application.geometry_confirmation import _is_declaration

    for m in reversed(list(messages or [])):
        if isinstance(m, dict) and _is_declaration(m):
            text = str(m.get("content") or "")
            return SHELL_HEAD in text or SURFACE_HEAD in text
    return False


#: The words of a solid tetrahedral mesh, which a shell run's summary must never promise (the intake's FEA path wrote
#: "Generate a quadratic tetrahedral structural mesh ..." for a surface file meshed as a shell)
TET_WORDS = re.compile(r"\b(?:tetrahedr\w*|tets?|10-node|ten-node|c3d10|solid187|ctetra|quadratic)\b", re.I)
#: How a shell run's summary says its mesh instead
SHELL_SUMMARY = ("a shell mesh - plate elements (linear 4-node quads, a few 3-node triangles) on the part's "
                 "mid-surface, or on the surface itself for a surface file, each carrying its wall thickness, every "
                 "face kept as a named group")


def card_mesh_as(declaration: str) -> str:
    """The request card's "Mesh as" row, read from the conversation's FEA declaration (its head, written by
    declaration_words at Proceed): a label for a shell or shell + solid, "" for a solid or none. Pure."""
    text = str(declaration or "")
    for kind, head in ((pm.MESH_MIXED, MIXED_HEAD), (pm.MESH_SHELL, SHELL_HEAD), (pm.MESH_SHELL, SURFACE_HEAD)):
        if head in text:
            return CARD_LABELS[kind]
    return ""


#: The request card's "Engine settings" for a shell run (engines/shell_writers: CQUAD4 / S4 / SHELL181 plates, linear,
#: a few 3-node triangles where quads do not fit) - no solid is made beside a shell (SHSTAGE-7), so an element order
#: is said only for a shell + solid run's thick regions (engines/mixed_mesh: C3D10)
SHELL_ELEMENTS = "Shell: linear plates (4-node quads, a few 3-node triangles)"
MIXED_ELEMENTS = "Thin walls: linear plates (4-node quads, a few 3-node triangles)"


def card_engine_settings(mesh_as_label: str, params: dict | None) -> str:
    """The request card's "Engine settings" row for a shell or shell + solid run, true to what is written: the plates
    are linear, and the element order applies to the solid made beside them (and to the thick regions kept solid).
    "" for a solid run (the card shows the settings as they are). Pure."""
    kind = next((k for k, label in CARD_LABELS.items() if label == mesh_as_label), "")
    if not kind:
        return ""
    p = dict(params or {})
    order = p.pop("element_order", None)
    parts = [SHELL_ELEMENTS if kind == pm.MESH_SHELL else MIXED_ELEMENTS]
    if order not in (None, "") and kind == pm.MESH_MIXED:
        parts.append(f"thick regions and the solid mesh made beside it: element order {order}")
    parts += [f"{str(k).replace('_', ' ')}: {v}" for k, v in p.items()]
    return "; ".join(parts)


def confirmed_mesh_as(session_id: str, notes=None) -> str:
    """The settled choice as confirmed on the stage (the stored confirmation's fea_mesh_as): "shell" (or "mixed",
    from a confirmation stored before shell + solid was taken off the stage) or "solid"; "solid" when nothing says
    (no confirmation, an unreadable store) - today's path. A SHELL ASKED FOR IN A NOTE AFTER PROCEED (the request
    card's notes, `notes`: contracts/user_notes) is a shell, unless the stage already found it could not be one."""
    if not session_id:
        return pm.MESH_SOLID
    from meshpipeline.contracts.object_storage import get_object_store

    try:
        record = json.loads(get_object_store().get_bytes(object_key=key(session_id, "confirmed.json")))
    except Exception:  # noqa: BLE001 - unknown is today's solid path, never a refusal
        return pm.MESH_SOLID
    record = record if isinstance(record, dict) else {}
    got = record.get("fea_mesh_as")
    if got in (pm.MESH_SHELL, pm.MESH_MIXED):
        return str(got)
    if str(record.get("fea_mesh_as_picked") or "").startswith(pm.MESH_SHELL):
        return pm.MESH_SOLID
    texts = [str(n.get("text") or "") for n in notes or [] if isinstance(n, dict)]
    for text in reversed(texts):
        reading = pm.mesh_as_of(text)
        if reading.value:
            return pm.MESH_SHELL if reading.value == pm.MESH_SHELL else pm.MESH_SOLID
    return pm.MESH_SOLID


__all__ = ["CARD_LABELS", "MEASURING", "MIXED_ELEMENTS", "NOTHING_TO_SHELL", "QUESTION", "READY", "SHELL_CHECK_LIMIT_S",
           "SHELL_ELEMENTS", "SHELL_SUMMARY", "TET_WORDS", "TRIANGLES_WHY", "UNAVAILABLE", "card_engine_settings",
           "card_mesh_as", "declared_shell", "confirmed_mesh_as",
           "declaration_words", "is_cad", "not_shelled", "solid_short", "read_status", "run_shell_check", "said_of", "settle",
           "shell_view", "start", "start_if_asked", "state_of", "summarise", "view_for", "wants_shell"]
