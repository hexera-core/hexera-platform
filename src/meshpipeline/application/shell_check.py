# Responsibility: "MESH AS: Auto / Shell / Solid" on the FEA stage (SHSTAGE-2..5). Runs the shell check of an
# FEA part in the geometry-check worker (cad/shell_preview in a process of its own, up to SHELL_CHECK_LIMIT_S)
# after the part is measured, keeps a short summary of it, and says what the stage shows: the recommendation
# with its reason in plain words, what stays solid and why, where plates do not meet - and settles the choice
# when the user presses Proceed, so the run never waits on the user.
# Boundaries: the choice is the user's. Words they said (application/mesh_as) or a click are never overridden;
# only a shell nothing can be made of (a triangle file, a part with no thin wall) is meshed solid instead, said
# plainly. Auto recommends; it never decides past the measurement. The view functions are pure.
# Collaborates with: cad/shell_preview.py (the extraction and the drawing), application/geometry_check.py (the
# store, the upload's reading, the FEA naming that starts this), application/fea_check.py (the FEA view this
# joins), api/v1/geometry.py (serves the view and the drawing, takes the answer), application/pipeline_run.py
# (reads the settled choice into the run), engines/fea_driver.py (meshes the shell), contracts/geometry_check.py
# (the worker seam), ui/js/render/fea_form.js and ui/js/viewer/geometry_stage.js (draw it).
from __future__ import annotations

import json
import logging
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
#: biggest parts of the training corpus (sh0020/21: 528 s); past this the stage says it took too long, and Auto
#: meshes the part solid.
SHELL_CHECK_LIMIT_S = 900
#: ... and a check still "measuring" this long after it started is read as stopped (a worker that died).
STALE_AFTER_S = SHELL_CHECK_LIMIT_S + 300
#: SHSTAGE-3: AUTO RECOMMENDS SHELL when every body is a shell (cad/midsurface 1.4: every face paired, a rim or a
#: blend), its plates meet everywhere (no open edge), and the thin walls are at least this share of the part's
#: surface. Mixed shell-and-solid parts are recommended Solid until the shell-solid joints land (design PR 3).
AUTO_SHELL_SHARE = 0.9
#: The CAD formats a mid-surface is extracted from (a triangle file has no faces to pair).
CAD_FORMATS = ("step", "stp", "iges", "igs", "brep")

QUESTION = "Mesh as"
OPTIONS = (
    {"key": pm.MESH_AUTO, "label": "Auto (recommended)"},
    {"key": pm.MESH_SHELL, "label": "Shell", "words": "the mid-surface, meshed with plate elements"},
    {"key": pm.MESH_SOLID, "label": "Solid", "words": "the whole volume, meshed with tetrahedra"},
)
MEASURING_WORDS = "Measuring the walls to see if a shell mesh fits this part. This can take a few minutes."
EARLY_PROCEED = "If you proceed before this is done, Auto meshes the part as a solid. Shell and Solid work any time."
SHELL_WHY = ("{share}% of this part is plate {thin}, so a shell mesh needs far fewer elements than a solid one, "
             "and it is what engineers use for sheet metal and plate structures.")
MIXED_WHY = ("{share}% of this part is plate {thin}, but it also has thick regions that must stay solid. A mesh "
             "that joins shell and solid is coming; for now Auto meshes the whole part as a solid.")
OPEN_WHY = ("This part is plate {thin}, but its mid-surfaces do not meet at {n} place{s} yet, so a shell mesh "
            "would have gaps there. Auto meshes it as a solid.")
SOLID_WHY = "No wall of this part is thin against its size, so it is meshed as a solid."
#: SHSTAGE-3b: the same with the measured reason the shell check gave (cad/midsurface SHELL-2c)
SOLID_DETAIL_WHY = "No wall of this part is thin against its size: {detail}. So it is meshed as a solid."
LOW_SHARE_WHY = ("Only {share}% of this part is thin plate, so it is meshed as a solid.")
TRIANGLES_WHY = ("Shell meshes need a CAD file (STEP or IGES): this file is triangles, with no faces to find the "
                 "mid-surface on. It is meshed as a solid.")
NOT_RUN_WHY = "The walls could not be measured ({reason}), so Auto meshes the part as a solid."
EARLY_WHY = "The walls were still being measured when you proceeded, so Auto meshes the part as a solid."
NOTHING_TO_SHELL = ("You asked for a shell, but no wall of this part is thin enough to be one. It is meshed as a "
                    "solid instead.")
SAID_WORDS = {pm.MESH_SHELL: "You asked for a shell mesh in the chat.",
              pm.MESH_SOLID: "You asked for a solid mesh in the chat."}


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
    """SHSTAGE-2: start the shell check of an FEA part once it is measured: the status says "measuring" at
    once (the stage shows it), and the worker runs it. A triangle file is not checked (no faces to pair)."""
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
    short summary. Never raises: a check that cannot run says why, and Auto then meshes the part solid."""
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
    except Exception as exc:  # noqa: BLE001 - Auto then meshes it solid, with the reason
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
            thick = sorted({float(pairs[p]["thickness_mm"]) for n in names for p in (faces.get(n) or {}).get("pairs") or []
                            if p in pairs and pairs[p].get("thickness_mm")})
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
            "overlay": bool(drawn.get("triangles")), "t_min_drawn_mm": drawn.get("t_min"),
            "t_max_drawn_mm": drawn.get("t_max")}


# ------------------------------------------------------------------------------------------------ the stage


def _mm(v) -> str:
    v = float(v)
    return f"{v:.2g} mm" if v < 1 else f"{v:.3g} mm"


def _thin_words(s: dict) -> str:
    """"under 6 mm thick", "2 mm thick", "2 to 6 mm thick"."""
    lo, hi = s.get("t_min_mm"), s.get("t_max_mm")
    if hi is None:
        return "thin"
    if lo is None or abs(float(hi) - float(lo)) <= 0.02 * float(hi):
        return f"{_mm(hi)} thick"
    return f"{_mm(lo)} to {_mm(hi)} thick"


def _solid_detail(summary: dict | None, body: str | None = None) -> str:
    """SHSTAGE-3b: the measured reason a whole body stays solid ("its most plate-like wall is 8 mm thick and spans
    about 42 mm, ..."), read from the shell check's words after its colon; "" when there is none. With no body
    named, the part's: its one body's, or the reason every solid body shares."""
    whys = {str(r.get("why") or "") for r in (summary or {}).get("solid_regions") or []
            if r.get("whole") and (body is None or r.get("body") == body)}
    details = {w.split(": ", 1)[1].strip() for w in whys if ": " in w}
    return details.pop() if len(details) == 1 else ""


def recommendation(summary: dict | None, *, cad: bool = True, status: str = READY, reason: str = "") -> tuple[str, str]:
    """SHSTAGE-3: what Auto meshes the part as, and why, in plain words: ("shell" | "solid", the reason), or
    ("", "") while the walls are still being measured."""
    if not cad:
        return pm.MESH_SOLID, TRIANGLES_WHY
    if status == MEASURING:
        return "", ""
    if status != READY or not isinstance(summary, dict):
        return pm.MESH_SOLID, NOT_RUN_WHY.format(reason=reason or "the shell check did not run")
    verdicts = {b["verdict"] for b in summary.get("bodies") or []}
    share = float(summary.get("share") or 0.0)
    pct = max(0, min(100, int(round(100 * share))))
    if not verdicts or verdicts == {"solid"}:
        detail = _solid_detail(summary)
        return pm.MESH_SOLID, SOLID_DETAIL_WHY.format(detail=detail) if detail else SOLID_WHY
    if verdicts != {"shell"}:
        return pm.MESH_SOLID, MIXED_WHY.format(share=pct, thin=_thin_words(summary))
    if summary.get("open_edges"):
        n = len(summary.get("open_places") or []) or int(summary["open_edges"])
        return pm.MESH_SOLID, OPEN_WHY.format(thin=_thin_words(summary), n=n, s="" if n == 1 else "s")
    if share < AUTO_SHELL_SHARE:
        return pm.MESH_SOLID, LOW_SHARE_WHY.format(share=pct)
    return pm.MESH_SHELL, SHELL_WHY.format(share=pct, thin=_thin_words(summary))


def not_shelled(summary: dict | None) -> list[dict]:
    """What stays solid if the part is meshed as a shell, and where the plates do not meet, each in plain
    words with a tag and a place to mark: S1, S2 ... for solid regions, O1, O2 ... for open places."""
    s = summary or {}
    many = len(s.get("bodies") or []) > 1
    out = []
    for k, reg in enumerate(s.get("solid_regions") or [], start=1):
        who = reg.get("body") if many else "The part"
        if reg.get("whole"):
            words = (f"{reg.get('body')} stays solid: no wall of it is thin against its size." if many
                     else "The whole part stays solid: no wall is thin against its size.")
            detail = _solid_detail(s, reg.get("body"))
            if detail:
                words = f"{words[:-1]}: {detail}."
        else:
            n = int(reg.get("n_faces") or len(reg.get("faces") or []))
            where = f"A thick region of {who if many else 'the part'} ({n} face{'s' if n != 1 else ''})"
            words = (f"{where} stays solid: it is {_mm(reg['thick_mm'])} thick or more, too thick for a shell."
                     if reg.get("thick_mm") else f"{where} stays solid: it is not a thin wall.")
        out.append({"id": f"S{k}", "kind": "solid", "words": words, "at_mm": reg.get("at_mm"),
                    "faces": list(reg.get("faces") or [])})
    if s.get("solid_more"):
        out.append({"id": "", "kind": "solid", "words": f"And {s['solid_more']} more regions stay solid.",
                    "at_mm": None, "faces": []})
    places = s.get("open_places") or []
    if s.get("open_edges"):
        n = len(places) or int(s["open_edges"])
        out.append({"id": "", "kind": "open_head", "at_mm": None, "faces": [],
                    "words": (f"The plates do not meet at {n} place{'s' if n != 1 else ''}: shown in red. A shell mesh "
                              "has a gap there.")})
        for k, p in enumerate(places, start=1):
            out.append({"id": f"O{k}", "kind": "open", "words": f"Plates do not meet here ({k} of {n}).",
                        "at_mm": p.get("at_mm"), "faces": []})
    return out


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
               now: float | None = None) -> dict:
    """SHSTAGE-2..4: what the stage shows under "Mesh as". `said` is the words' reading
    (application/mesh_as.as_record) - the stage opens on it, and it is never overridden. Pure."""
    cad = is_cad(facts)
    state, reason = state_of(status, cad=cad, now=now)
    summary = (status or {}).get("summary") if state == READY else None
    if isinstance(summary, dict):
        summary = _in_unit(summary, (status or {}).get("scale_to_m"), (facts or {}).get("scale_to_m"))
    rec, why = recommendation(summary, cad=cad, status=state, reason=reason)
    said_value = pm.mesh_as_answer((said or {}).get("value"))
    if said_value == pm.MESH_SHELL and not cad:
        said_value = ""
    out = {"question": QUESTION, "state": state, "options": [dict(o) for o in OPTIONS],
           "default": said_value or pm.MESH_AUTO, "said": said_value, "said_words": SAID_WORDS.get(said_value, ""),
           "recommend": rec, "reason": why, "shell_ok": cad, "shell_off": "" if cad else TRIANGLES_WHY,
           "measuring_words": MEASURING_WORDS if state == MEASURING else "",
           "early_proceed": EARLY_PROCEED if state == MEASURING else ""}
    if isinstance(summary, dict):
        out.update(share=summary.get("share"), t_min_mm=summary.get("t_min_mm"), t_max_mm=summary.get("t_max_mm"),
                   bodies=summary.get("bodies"), not_shelled=not_shelled(summary),
                   open_edges=int(summary.get("open_edges") or 0),
                   open_at_mm=list(summary.get("open_at_mm") or []), overlay=bool(summary.get("overlay")),
                   legend_mm=[summary.get("t_min_drawn_mm"), summary.get("t_max_drawn_mm")],
                   scale=_k((status or {}).get("scale_to_m"), (facts or {}).get("scale_to_m")))
    return out


def said_of(facts: dict | None, messages=None) -> dict:
    """What the user's words said about shell or solid: the FEA naming's stored reading (the table, and the
    model for a paraphrase); for a check named before that reading existed, the word table over the
    user's messages."""
    from meshpipeline.application.geometry_check import MESH_AS_KEY

    got = (facts or {}).get(MESH_AS_KEY)
    if isinstance(got, dict):
        return got
    texts = [str(m.get("content") or "") for m in messages or []
             if isinstance(m, dict) and m.get("role") == "user"]
    reading = pm.mesh_as_of("\n".join(texts))
    return {"value": reading.value, "by": reading.by, "why": reading.why}


def view_for(session_id: str, facts: dict | None, messages=None) -> dict:
    """The stage's "Mesh as" view for a session: its stored shell check and what the words said."""
    try:
        status = read_status(session_id)
    except Exception as exc:  # noqa: BLE001 - an unreadable store is "not measured", never a broken stage
        logger.warning("shell check: status unreadable (%s)", type(exc).__name__)
        status = {"status": UNAVAILABLE, "reason": "it could not be read"}
    return shell_view(status, facts, said_of(facts, messages))


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


def settle(view: dict | None, answer: str | None) -> dict:
    """SHSTAGE-5: THE CHOICE, SETTLED WHEN THE USER PRESSES PROCEED - the run never waits on the user. The
    user's pick (the form's, else the words') stands; Auto takes the recommendation, or Solid while the walls
    are still being measured. A shell nothing can be made of is meshed solid, said plainly.
    {"mesh_as": "shell" | "solid", "picked": "auto" | "shell" | "solid", "by": "user" | "words" | "auto",
     "why": plain words}"""
    v = view or {}
    picked = str(answer or "").strip().lower()
    by = "user"
    if picked not in (pm.MESH_AUTO, pm.MESH_SHELL, pm.MESH_SOLID):
        picked, by = (str(v["said"]), "words") if v.get("said") else (pm.MESH_AUTO, "auto")
    elif picked == pm.MESH_AUTO:
        by = "auto"
    elif picked == v.get("said"):
        by = "words"
    if picked == pm.MESH_SOLID:
        return {"mesh_as": pm.MESH_SOLID, "picked": picked, "by": by, "why": "You picked a solid mesh."
                if by == "user" else SAID_WORDS[pm.MESH_SOLID]}
    if picked == pm.MESH_SHELL:
        if not v.get("shell_ok", True):
            return {"mesh_as": pm.MESH_SOLID, "picked": picked, "by": by, "why": TRIANGLES_WHY}
        if v.get("state") == READY and {b.get("verdict") for b in v.get("bodies") or []} <= {"solid"}:
            return {"mesh_as": pm.MESH_SOLID, "picked": picked, "by": by, "why": NOTHING_TO_SHELL}
        return {"mesh_as": pm.MESH_SHELL, "picked": picked, "by": by,
                "why": "You picked a shell mesh." if by == "user" else SAID_WORDS[pm.MESH_SHELL]}
    rec = v.get("recommend") or ""
    if rec in (pm.MESH_SHELL, pm.MESH_SOLID):
        return {"mesh_as": rec, "picked": pm.MESH_AUTO, "by": "auto", "why": str(v.get("reason") or "")}
    return {"mesh_as": pm.MESH_SOLID, "picked": pm.MESH_AUTO, "by": "auto", "why": EARLY_WHY}


def declaration_words(settled: dict | None) -> str:
    """The declaration's line for the settled choice: "Meshed as: a shell (the mid-surface ...). <why>"."""
    s = settled or {}
    if s.get("mesh_as") == pm.MESH_SHELL:
        head = ("Meshed as: a shell - the mid-surface with plate elements, in Nastran, CalculiX, Abaqus and Ansys "
                "files. The solid mesh is made beside it, as before.")
    elif s.get("mesh_as") == pm.MESH_SOLID:
        head = "Meshed as: a solid."
    else:
        return ""
    why = str(s.get("why") or "").strip()
    return f"{head} {why}".strip()


def confirmed_mesh_as(session_id: str) -> str:
    """The settled choice as confirmed on the stage (the stored confirmation's fea_mesh_as): "shell" or
    "solid"; "solid" when nothing says (no confirmation, an unreadable store) - today's path."""
    if not session_id:
        return pm.MESH_SOLID
    from meshpipeline.contracts.object_storage import get_object_store

    try:
        record = json.loads(get_object_store().get_bytes(object_key=key(session_id, "confirmed.json")))
    except Exception:  # noqa: BLE001 - unknown is today's solid path, never a refusal
        return pm.MESH_SOLID
    got = record.get("fea_mesh_as") if isinstance(record, dict) else None
    return pm.MESH_SHELL if got == pm.MESH_SHELL else pm.MESH_SOLID


__all__ = ["AUTO_SHELL_SHARE", "MEASURING", "OPTIONS", "QUESTION", "READY", "SHELL_CHECK_LIMIT_S", "UNAVAILABLE",
           "confirmed_mesh_as", "declaration_words", "is_cad", "not_shelled", "read_status", "recommendation",
           "run_shell_check", "settle", "shell_view", "start", "state_of", "summarise"]
