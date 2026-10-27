# Responsibility: Say what the FEA stage shows for a part - its size, its unit, how many bodies the
# file has and what the clean check found - and, for an assembly, its parts, where they touch or
# pass into each other, and the one question how they are connected. Also say which stage a
# session's check opens on, from what the user said the mesh is for.
# Boundaries: pure over the stored check's facts and the conversation; it stores nothing and
# meshes nothing. The unit box and its suggestion stay the generic check's (api/v1/geometry).
# It also serves what the measurement found (cad/fea_measure, FEA build step 3) - the thinnest wall
# and the stress hot spots - for the stage to show, and re-reads that record in a confirmed unit.
# Collaborates with: application/purpose_map.py (the route), api/v1/geometry.py (serves the view,
# takes the connection answer), ui/js/render/fea_form.js (draws it), cad/fea_measure.py and
# cad/fea_assembly.py (the record), application/geometry_check.py (measures and stores it),
# application/pipeline_run.py (reads the stored answer into the run).
from __future__ import annotations

#: The question the stage asks (design 3.1, Areen's words).
QUESTION = "Is this your part and are the units right?"
#: An assembly's one question (design 14), and its answers: bonded is the default.
CONNECTION_QUESTION = "How are the parts connected?"
CONNECTION_OPTIONS = (
    {"key": "bonded", "label": "Bonded", "words": "welded or glued: the parts move as one where they touch"},
    {"key": "contact", "label": "Contact", "words": "they can slide or separate where they touch"},
)
DEFAULT_CONNECTION = "bonded"
#: PLAN-fea-18: THE SMALL-GAP JOIN as the stage proposes it - parts that face each other across a small
#: gap are joined there (a weld or a thread the CAD does not draw), unless the user switches it off.
GAP_JOIN_QUESTION = "Join parts across small gaps (welds and threads the CAD does not draw)"
DEFAULT_GAP_JOINS = True
#: The check modes a stage opens in. "thermal" is the ECXML model's own (contracts/intake_formats).
STAGE_FLOW, STAGE_FEA, STAGE_THERMAL = "flow", "fea", "thermal"


def stage_route(stored: dict | None, messages) -> str:
    """Which stage the check opens on: "thermal" for a whole thermal model (its file decides),
    "fea" or "flow" once the user's words settle the purpose (application/purpose_map), and ""
    while they do not - no answer yet, or the one purpose question still open. A stage opened
    before the purpose is known would show an FEA part the flow form and its stickers."""
    from meshpipeline.application import purpose_map as pm
    from meshpipeline.contracts.intake_formats import THERMAL_CHECK

    if (stored or {}).get("check_mode") == THERMAL_CHECK:
        return STAGE_THERMAL
    reading = pm.reading_for(messages)
    if not reading.settled:
        return ""
    return STAGE_FEA if reading.route == pm.ROUTE_FEA else STAGE_FLOW


def body_count(facts: dict | None) -> int | None:
    """How many solid bodies the scout found (separate shells, for a triangle file); None unknown."""
    try:
        n = (facts or {}).get("solids")
        return int(n) if n is not None else None
    except (TypeError, ValueError):
        return None


def piece_count(facts: dict | None) -> int | None:
    """How many separate pieces the clean check found (cad/validity: its connected bodies) - more
    than the solids when one solid is made of pieces that pass into each other, as a turbine blade
    and its root drawn as two overlapping lumps of one solid. None when it did not count them."""
    try:
        n = (((facts or {}).get("validity") or {}).get("measured") or {}).get("bodies")
        return int(n) if n is not None else None
    except (TypeError, ValueError):
        return None


def clean_words(facts: dict | None) -> tuple[bool | None, str]:
    """The clean check (cad/validity, #224) in plain words: (clean yes/no/unknown, one sentence)."""
    v = (facts or {}).get("validity") or {}
    clean = v.get("clean")
    summary = str(v.get("summary") or "").strip()
    if v.get("kind") in ("unchecked", "check_failed") or clean not in (True, False):
        return None, summary or "The clean check did not run on this file."
    if clean is True:
        return True, summary or "The geometry is clean."
    return False, summary or "The geometry is not clean."


#: NOT A CLOSED SOLID (Areen's Camaro STL, 2026-10-23: a car body of 395 open, overlapping surface
#: pieces was told "395 separate bodies - export each part on its own"). Version 1 meshes a closed
#: solid; what the clean check found that rules one out, in plain words, worst first. Every mesh
#: format (STL, OBJ, PLY, 3MF, GLB) and a CAD file with no solid in it are read the same way.
NOT_SOLID_REASONS = (
    ("open_shell", "its faces do not close around a volume"),
    ("open_boundary", "its surface has open edges - holes in the skin"),
    ("zero_volume", "it encloses no volume"),
    ("flat", "it is flat"),
    ("bodies_overlap", "separate pieces of it pass into each other"),
    ("non_manifold_edges", "some edges join more than two faces"),
    ("non_manifold_vertices", "some surfaces touch only at a point"),
)
NOT_SOLID = ("This is a surface, not a closed solid: {why}. FEA version 1 meshes closed solids only; "
             "shell meshes for surfaces like this are coming.")
#: The way on from a part version 1 cannot mesh.
WAY_OUT_SOLID = ("To go on, upload the part as one closed solid (a STEP from your CAD, or a watertight "
                 "STL). If you meant a fluid flowing around or through it (CFD), upload it again and "
                 "say so.")
_BREP_FORMATS = ("step", "stp", "iges", "igs", "brep")


def has_solid(facts: dict | None) -> bool:
    """A CLOSED SOLID CAN BE READ from the file: a CAD file with at least one solid body, or a triangle
    file whose surface closes. Then nothing the clean check says stops the stage (MESH-1: mesh first,
    warn - a good file is never blocked by our own broken-CAD checks)."""
    f = facts or {}
    v = f.get("validity") or {}
    n = body_count(f)
    brep = str(f.get("source_format") or "").lower() in _BREP_FORMATS and v.get("kind") != "triangles"
    if brep and n and n >= 1:
        return True
    if v.get("kind") in ("unchecked", "check_failed") or v.get("clean") is None:
        # the clean check could not say: never a stop on a file we could not judge
        return True
    errors = {str(r.get("code")) for r in v.get("reasons") or []
              if isinstance(r, dict) and str(r.get("severity") or "error") == "error"}
    # a triangle surface closes into a solid when it has no open edge and no edge shared by more than
    # two faces (the Camaro STL: 395 overlapping skins with non-manifold edges is a surface)
    # a CAD file of faces with no solid entity is still a solid when its faces close (the FEA staging
    # makes the closed shells solids - engines/fea_driver.solids_from_shells)
    if brep:
        return not (errors & {"open_shell", "open_boundary", "zero_volume", "flat"})
    return not (errors & {"open_shell", "open_boundary", "zero_volume", "flat", "non_manifold_edges"})


def not_solid_message(facts: dict | None) -> str:
    """The plain message for a file that is not one closed solid, "" when nothing says so - or when a
    closed solid can still be read from it (then what the clean check found is a warning: warnings())."""
    if has_solid(facts):
        return ""
    f = facts or {}
    v = f.get("validity") or {}
    errors = {str(r.get("code")) for r in v.get("reasons") or []
              if isinstance(r, dict) and str(r.get("severity") or "error") == "error"}
    n = body_count(f)
    brep = str(f.get("source_format") or "").lower() in _BREP_FORMATS and v.get("kind") != "triangles"
    if brep and n is not None and n > 1:
        # CLOSED SOLIDS PASSING INTO EACH OTHER are an assembly's interference: a meshing choice
        # (bonded: one piece there; contact: a pair), never a reason to stop (design 14)
        errors.discard("bodies_overlap")
    said = [words for code, words in NOT_SOLID_REASONS if code in errors]
    if brep and n == 0 and not said:
        said = ["the file holds faces but no solid"]
    if not said:
        return ""
    why = " and ".join(said[:2])
    if n is not None and n > 1 and v.get("kind") == "triangles":
        why = f"it is made of {n} separate surface pieces, and {why}"
    return NOT_SOLID.format(why=why)


#: A BODY THAT CROSSES ITSELF is a CAD defect (Areen, 23 Oct): found, named and stopped at the stage,
#: never healed here - the CAD repair stage owns healing.
SELF_OVERLAP = "{who} overlaps itself – it needs CAD repair before it can be meshed."
WAY_OUT_REPAIR = "Fix it in your CAD and upload it again."
WAY_OUT_REPAIR_PART = "Fix it in your CAD and upload it again, or remove that part and upload the rest."


def _part_label(pid: str, name: str) -> str:
    return f"Part {name} ({pid})" if name else pid


def self_overlap_message(facts: dict | None) -> tuple[str, str]:
    """(the plain message, the way on) for a body whose surface passes through itself - ("", "") when
    none does. An assembly's part is named (its CAD name and Part_k); a single part is "This part"."""
    from meshpipeline.cad.fea_assembly import FACT_KEY as ASM_KEY
    from meshpipeline.cad.fea_measure import FACT_KEY

    f = facts or {}
    v = f.get("validity") or {}
    hit = next((r for r in v.get("reasons") or [] if isinstance(r, dict)
                and str(r.get("code")) == "self_intersection" and str(r.get("severity") or "error") == "error"),
               None)
    if hit is None:
        return "", ""
    n = body_count(f)
    if n is None or n <= 1:
        return SELF_OVERLAP.format(who="This part"), WAY_OUT_REPAIR
    measured = f.get(FACT_KEY)
    rec = measured.get(ASM_KEY) if isinstance(measured, dict) else None
    names = {str(p.get("id")): str(p.get("name") or "") for p in (rec or {}).get("parts") or []
             if isinstance(p, dict)}
    bodies = [int(b) for b in ((hit.get("details") or {}).get("bodies") or []) if 1 <= int(b) <= n]
    if not bodies:
        return SELF_OVERLAP.format(who="A part"), WAY_OUT_REPAIR_PART
    said = [SELF_OVERLAP.format(who=_part_label(f"Part_{b}", names.get(f"Part_{b}", ""))) for b in bodies[:3]]
    if len(bodies) > 3:
        said.append(f"So do {len(bodies) - 3} more part(s).")
    return " ".join(said), WAY_OUT_REPAIR_PART


#: A SURFACE THAT FOLDS BACK ON ITSELF (cad/fold_back.py; car_vs_pole, 24 Oct): two faces lying on each
#: other along an edge - a knife edge of almost no angle or a crack of almost no width - which no mesher
#: can fill. A CAD defect like a self-overlap: found, named and stopped at the stage, never healed here.
FOLD_BACK = ("{who} has a paper-thin fold: {faces} lie on top of each other along an edge {length} long. "
             "It needs CAD repair before it can be meshed.")


def _length_words(mm: float) -> str:
    return f"{mm / 1000.0:.2g} m" if mm >= 1000.0 else f"{mm:.3g} mm"


def fold_back_message(facts: dict | None) -> tuple[str, str]:
    """(the plain message, the way on) for a part whose surface folds back on itself - ("", "") when
    none does. The longest fold is named, with its part (an assembly's) and its two faces."""
    from meshpipeline.cad.fea_assembly import FACT_KEY as ASM_KEY
    from meshpipeline.cad.fea_measure import FACT_KEY

    measured = (facts or {}).get(FACT_KEY)
    folds = [f for f in (measured or {}).get("fold_back") or [] if isinstance(f, dict)] \
        if isinstance(measured, dict) else []
    if not folds:
        return "", ""
    worst = max(folds, key=lambda f: float(f.get("length_mm") or 0.0))
    faces = " and ".join(str(x) for x in worst.get("faces") or []) or "two faces"
    length = _length_words(float(worst.get("length_mm") or 0.0))
    n = body_count(facts)
    if n is None or n <= 1:
        return FOLD_BACK.format(who="This part", faces=faces, length=length), WAY_OUT_REPAIR
    rec = measured.get(ASM_KEY) if isinstance(measured, dict) else None
    names = {str(p.get("id")): str(p.get("name") or "") for p in (rec or {}).get("parts") or []
             if isinstance(p, dict)}
    pid = f"Part_{int(worst.get('body') or 0)}"
    who = _part_label(pid, names.get(pid, "")) if worst.get("body") else "A part"
    parts = sorted({int(f.get("body") or 0) for f in folds})
    more = f" Folds are found in {len(parts)} parts." if len(parts) > 1 else ""
    return FOLD_BACK.format(who=who, faces=faces, length=length) + more, WAY_OUT_REPAIR_PART


#: MESH-1: what the stage says beside a possible defect (Areen, 24 Oct: mesh first).
MESH_RULE = "MESH-1"
TRY_ANYWAY = ("The mesh will be tried anyway: if it fails at that spot, the part needs CAD repair there; "
              "if it succeeds, the result notes the spot.")


def warnings(facts: dict | None) -> list[str]:
    """MESH-1: what the broken-CAD checks found on a part a closed solid can be read from - each a
    WARNING the user can go on past, never a stop: a face crossing another of its body, a paper-thin
    fold, and what the clean check calls not closed or not valid. Confirmed only when two independent
    measures agree (cad/suspect_defects)."""
    from meshpipeline.cad import suspect_defects as SD
    from meshpipeline.cad.fea_assembly import FACT_KEY as ASM_KEY
    from meshpipeline.cad.fea_measure import FACT_KEY

    f = facts or {}
    if not has_solid(f):
        return []
    raw = f.get(FACT_KEY)
    measured: dict = raw if isinstance(raw, dict) else {}
    rec = measured.get(ASM_KEY) if isinstance(measured, dict) else None
    names = {str(p.get("id")): str(p.get("name") or "") for p in (rec or {}).get("parts") or [] if isinstance(p, dict)}
    many = (body_count(f) or 0) > 1
    out: list[str] = []
    seen: set = set()
    for sus in (measured or {}).get("suspects") or []:
        if not isinstance(sus, dict):
            continue
        key = (sus.get("kind"), int(sus.get("body") or 0), tuple(sorted(sus.get("faces") or [])))
        if key in seen:
            continue
        seen.add(key)
        who = (_part_label(f"Part_{int(sus.get('body') or 0)}", names.get(f"Part_{int(sus.get('body') or 0)}", ""))
               if many and sus.get("body") else "The part")
        out.append(SD.words(sus, who))
    v = f.get("validity") or {}
    found = {str(r.get("code")) for r in v.get("reasons") or []
             if isinstance(r, dict) and str(r.get("severity") or "error") == "error"}
    said = [words for code, words in NOT_SOLID_REASONS if code in found
            and not (code == "bodies_overlap" and many)]
    if said:
        out.append(f"The clean check says {' and '.join(said[:2])}, though a solid can be read from the file.")
    if out:
        out.append(TRY_ANYWAY)
    return out


def blocker(facts: dict | None) -> tuple[str, str, str]:
    """(what stops version 1, its kind - "surface" | "self_overlap" | "", the way on) for the stage
    and the confirmation alike. Several bodies are no stop: an assembly is meshed (design 14); a body
    that crosses itself is (it needs CAD repair; detected only, never healed)."""
    # MESH-1 (Areen, 24 Oct): only a file no closed solid can be read from stops here - a surface, or
    # nothing at all. A face crossing another, a fold, an open or invalid patch on a readable solid are
    # warnings (warnings()): the mesh is tried, and only a mesh that fails at that spot says
    # "needs CAD repair" (engines/fea_driver)
    surface = not_solid_message(facts)
    if surface:
        return surface, "surface", WAY_OUT_SOLID
    return "", "", ""


def fea_view(facts: dict | None) -> dict:
    """What the FEA stage shows beside the part (the size and unit come with the proposal); for an
    assembly also its parts, their interfaces and the connection question."""
    clean, said = clean_words(facts)
    blocked, kind, way_out = blocker(facts)
    found = [] if blocked else warnings(facts)
    if found:
        said = f"{said} {' '.join(found)}".strip()
    out = {"question": QUESTION, "bodies": body_count(facts), "pieces": piece_count(facts),
           "clean": clean, "clean_summary": said, "single_part_only": blocked, "blocked_kind": kind, "way_out": way_out,
           "can_proceed": not blocked, "warnings": found, "measure": measure_view(facts)}
    # several closed solids: an assembly, with its one question (a surface that is no solid stops first)
    asm = None if blocked else assembly_view(facts)
    if asm is not None:
        out["assembly"] = asm
    plan = None if blocked else plan_view(facts)
    if plan is not None:
        out["plan"] = plan
    return out


#: PLAN-fea-22: THE SIZE CHOICE as the stage asks it - the full-rules mesh or a quicker one (application facts
#: key, engines/fea_plan_options); the full-rules mesh is made when nothing was picked.
PLAN_FACT_KEY = "fea_plan"
PLAN_QUESTION = "Which mesh?"
PLAN_FULL, PLAN_QUICKER = "full", "quicker"


def _elements_words(n: int, at_least: bool = False) -> str:
    said = f"{n / 1e6:.1f} million" if n >= 1_000_000 else f"{n:,}"
    return f"{'at least' if at_least else 'about'} {said} elements"


def _time_words(minutes) -> str:
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return ""
    return f"up to about {max(1, m)} minutes" if m < 90 else f"up to about {m / 60.0:.1f} hours"


def _machine_words(row: dict) -> str:
    if row.get("standard"):
        return "the standard machine"
    gib = row.get("memory_gib")
    return f"a {gib:g} GiB machine" if isinstance(gib, (int, float)) and gib else "a bigger machine"


def _option(row: dict, key: str, recommended: str) -> dict:
    label = "Full rules" if key == PLAN_FULL else "Quicker"
    bits = [_elements_words(int(row.get("elements") or 0), bool(row.get("at_least"))), _machine_words(row),
            _time_words(row.get("minutes"))]
    words = " · ".join(b for b in bits if b)
    if key == PLAN_QUICKER and row.get("short_zones"):
        words += f" · {int(row['short_zones'])} zones coarser than their rules"
    return {"key": key, "label": label, "words": words, "recommended": key == recommended,
            "short": list(row.get("short") or [])[:6], "short_kinds": list(row.get("short_kinds") or [])}


def plan_view(facts: dict | None) -> dict | None:
    """PLAN-fea-22, the size choice as the stage shows it: "sizing" while the check plans the mesh, then the two
    meshes with the one the evidence recommends (preselected) and why - or, when the full-rules mesh fits the
    standard machine, that there is nothing to choose; "not sized" when the check could not plan it in time
    (the run makes the full-rules mesh). None before the check started sizing it."""
    got = (facts or {}).get(PLAN_FACT_KEY)
    if not isinstance(got, dict):
        return None
    if got.get("sizing"):
        return {"state": "sizing", "words": "Sizing the mesh..."}
    if not got.get("sized"):
        return {"state": "not_sized", "words": (got.get("why") or "The mesh is sized when it runs") + ": "
                "the full-rules mesh is made, on the machine it needs."}
    full = got.get("full") or {}
    rec = PLAN_QUICKER if got.get("recommended") == PLAN_QUICKER else PLAN_FULL
    options = [_option(full, PLAN_FULL, rec)]
    if got.get("choice") and isinstance(got.get("quicker"), dict):
        options.append(_option(got["quicker"], PLAN_QUICKER, rec))
    out: dict = {"state": "sized", "choice": len(options) > 1, "recommended": rec, "why": str(got.get("why") or ""),
                 "question": PLAN_QUESTION, "options": options}
    if len(options) == 1:
        out["words"] = (f"{_elements_words(int(full.get('elements') or 0), bool(full.get('at_least')))} on "
                        f"{_machine_words(full)}, {_time_words(full.get('minutes'))}.")
    return out


def plan_answer(value, facts: dict | None) -> str:
    """The size choice the stage sent, checked against what the check offered: "full" or "quicker" when there
    was a choice (the recommended one when the stage sent neither), "" when there was none to make."""
    view = plan_view(facts)
    if not view or not view.get("choice"):
        return ""
    v = str(value or "").strip().lower()
    return v if v in (PLAN_FULL, PLAN_QUICKER) else str(view.get("recommended") or PLAN_FULL)


def plan_words(answer: str, facts: dict | None) -> str:
    """The size choice in the declaration's words."""
    view = plan_view(facts) or {}
    opt = next((o for o in view.get("options") or [] if o["key"] == answer), None)
    if not opt:
        return ""
    return f"Mesh: {opt['label'].lower()} ({opt['words']})."


def confirmed_plan_choice(session_id: str) -> str:
    """PLAN-fea-22: the size choice confirmed on the stage (the stored confirmation's fea_plan): "quicker" or
    "full"; "" when nothing was picked (the run then makes the full-rules mesh)."""
    import json

    if not session_id:
        return ""
    from meshpipeline.application.geometry_check import check_object_key
    from meshpipeline.contracts.object_storage import get_object_store

    try:
        record = json.loads(get_object_store().get_bytes(object_key=check_object_key(session_id, "confirmed.json")))
    except Exception:  # noqa: BLE001 - unknown is the full-rules mesh, never a refusal
        return ""
    v = str((record or {}).get("fea_plan") or "").strip().lower() if isinstance(record, dict) else ""
    return v if v in (PLAN_FULL, PLAN_QUICKER) else ""


def assembly_view(facts: dict | None) -> dict | None:
    """An assembly as the stage shows it: its parts (Part_k and the CAD's name), the interfaces the
    measurement found - where parts touch (contact candidates) and where they pass into each other
    (interference) - each with a place to mark, and the one question, bonded the default. None for
    a single part. A file of several bodies not measured yet still gets the question, with no
    interfaces to show."""
    from meshpipeline.cad.fea_assembly import FACT_KEY as ASM_KEY
    from meshpipeline.cad.fea_measure import FACT_KEY

    n = body_count(facts)
    measured = (facts or {}).get(FACT_KEY)
    rec = measured.get(ASM_KEY) if isinstance(measured, dict) else None
    if not isinstance(rec, dict) and not (n is not None and n > 1):
        return None
    rec = rec if isinstance(rec, dict) else {}
    parts = [{"id": p.get("id"), "name": p.get("name") or "", "faces": p.get("faces"),
              "centroid_mm": p.get("centroid_mm"), "box_mm": p.get("box_mm")} for p in rec.get("parts") or []]
    interfaces = [{"id": i.get("id"), "kind": i.get("kind"), "parts": list(i.get("parts") or []),
                   "at_mm": i.get("at_mm"), "area_mm2": i.get("area_mm2"), "overlap_share": i.get("overlap_share"),
                   "faces": i.get("faces") or {}} for i in rec.get("interfaces") or []]
    for i, src in zip(interfaces, rec.get("interfaces") or [], strict=False):
        if src.get("kind") == "gap":
            i.update(gap_mm=src.get("gap_mm"), gap_max_mm=src.get("gap_max_mm"), gap_limit_mm=src.get("gap_limit_mm"))
    touching = sum(1 for i in interfaces if i["kind"] == "touching")
    over = sum(1 for i in interfaces if i["kind"] == "overlap")
    gaps = [i for i in interfaces if i["kind"] == "gap"]
    words = [f"{len(parts) or n} parts"]
    if touching:
        words.append(f"{touching} place{'s' if touching != 1 else ''} where parts touch")
    if gaps:
        words.append(f"{len(gaps)} across a small gap")
    if over:
        words.append(f"{over} where parts pass into each other")
    label = {p["id"]: (p["name"] or p["id"]) for p in parts}
    raw_joins = rec.get("joins")
    joins: dict = raw_joins if isinstance(raw_joins, dict) else {}
    return {"parts": parts, "interfaces": interfaces, "free_parts": list(rec.get("free_parts") or []),
            "notes": list(rec.get("notes") or []), "summary": ", ".join(words),
            # NOT MEASURED YET is not "nothing touches": the stage says which
            "measured": bool(rec),
            # PLAN-fea-18: each small-gap joint in words, and the one switch for them (on by default)
            "gap_joins": [{"id": i["id"], "parts": i["parts"], "gap_mm": i.get("gap_mm"),
                           "words": gap_words(label.get(i["parts"][0], i["parts"][0]),
                                              label.get(i["parts"][1], i["parts"][1]), i.get("gap_mm"))}
                          for i in gaps],
            "gap_question": GAP_JOIN_QUESTION if gaps else "", "gap_default": DEFAULT_GAP_JOINS,
            "join_notes": list(joins.get("notes") or []),
            "question": CONNECTION_QUESTION, "options": [dict(o) for o in CONNECTION_OPTIONS],
            "default": DEFAULT_CONNECTION,
            "overlap_note": ("Where parts pass into each other, a bonded mesh makes them one piece of material "
                             "there; with contact, the solver starts with them overlapping." if over else "")}


def gap_words(a: str, b: str, gap_mm) -> str:
    """One small-gap joint in plain words: "Bolt M8 sits 0.19 mm from Part_8 - joined across the gap"."""
    try:
        g = float(gap_mm)
        said = f"{g:.2g} mm" if g < 1 else f"{g:.3g} mm"
    except (TypeError, ValueError):
        said = "a small gap"
    return f"{a} sits {said} from {b} - joined across the gap"


def gap_joins_answer(value) -> bool:
    """The small-gap switch the stage sent: on unless it was plainly switched off."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "0", "no", "off")


def confirmed_gap_joins(session_id: str) -> bool:
    """Whether parts facing each other across a small gap are joined there (PLAN-fea-18), as confirmed
    on the stage (the stored confirmation's fea_gap_joins); on when nothing says."""
    import json

    if not session_id:
        return DEFAULT_GAP_JOINS
    from meshpipeline.application.geometry_check import check_object_key
    from meshpipeline.contracts.object_storage import get_object_store

    try:
        record = json.loads(get_object_store().get_bytes(object_key=check_object_key(session_id, "confirmed.json")))
    except Exception:  # noqa: BLE001 - unknown is the default, never a refusal
        return DEFAULT_GAP_JOINS
    if not isinstance(record, dict) or record.get("fea_gap_joins") is None:
        return DEFAULT_GAP_JOINS
    return gap_joins_answer(record.get("fea_gap_joins"))


def parts_words(asm: dict | None) -> str:
    """An assembly's parts in one plain line for the declaration: "3 parts: Part_1 (Valve body),
    Part_2 and Part_3"."""
    parts = list((asm or {}).get("parts") or [])
    if not parts:
        return ""
    named = [f"{p['id']} ({p['name']})" if p.get("name") else str(p.get("id")) for p in parts[:12]]
    more = f" and {len(parts) - 12} more" if len(parts) > 12 else ""
    if more or len(named) == 1:
        shown = ", ".join(named)
    else:
        shown = ", ".join(named[:-1]) + " and " + named[-1]
    return f"{len(parts)} parts: {shown}{more}"


def connection_answer(value: str | None) -> str:
    """The connection the stage sent, checked: bonded or contact ("" for anything else)."""
    v = str(value or "").strip().lower()
    return v if v in {o["key"] for o in CONNECTION_OPTIONS} else ""


def confirmed_connection(session_id: str) -> str:
    """How the parts of the session's FEA assembly are joined, as confirmed on the stage (the stored
    confirmation's fea_connection): "bonded" or "contact"; "" when nothing says (a single part, no
    confirmation, a store that cannot be read - the driver then bonds them, the default)."""
    import json

    if not session_id:
        return ""
    from meshpipeline.application.geometry_check import check_object_key
    from meshpipeline.contracts.object_storage import get_object_store

    try:
        record = json.loads(get_object_store().get_bytes(object_key=check_object_key(session_id, "confirmed.json")))
    except Exception:  # noqa: BLE001 - unknown, never a refusal
        return ""
    return connection_answer(record.get("fea_connection")) if isinstance(record, dict) else ""


#: The stage is sent EVERY hot spot the record holds (all of them, up to cad/fea_features.
#: MAX_HOT_SPOTS): a hot spot the stage leaves out looks the same as one never found (Areen's F1
#: car, 21 Oct: a rear-wing corner had no tag while its mirror twin had one). The stage draws a
#: long list as small marks and the panel groups it by type.
SHOWN_HOT_SPOTS = None
#: What the stage needs of a hot spot: what it is, how big, where, and whether it is capped.
_SHOWN_KEYS = ("id", "type", "shape", "radius_mm", "angle_deg", "length_mm", "depth_mm", "faces",
               "at_mm", "capped")
#: Hot-spot words, one and many, in the order they are said.
_NOUNS = {"hole": ("hole", "holes"), "fillet": ("fillet", "fillets"),
          "notch": ("notch or groove", "notches or grooves"),
          "sharp_corner": ("sharp inside corner", "sharp inside corners")}


def measure_view(facts: dict | None) -> dict | None:
    """The measurement as the stage shows it (V1: shown only, nothing to edit): the thinnest wall
    and where, and the hot spots the plan will refine. None before anything was measured."""
    from meshpipeline.cad.fea_measure import FACT_KEY

    rec = (facts or {}).get(FACT_KEY)
    if not isinstance(rec, dict):
        return None
    if not rec.get("measured"):
        return {"measured": False, "reason": str(rec.get("reason") or "the part was not measured")}
    spots = [{k: h[k] for k in _SHOWN_KEYS if k in h} for h in rec.get("hot_spots") or []]
    thick = rec.get("thickness") or {}
    counts = {str(k): int(v) for k, v in (rec.get("counts") or {}).items()}
    shown = spots if SHOWN_HOT_SPOTS is None else spots[:SHOWN_HOT_SPOTS]
    return {"measured": True, "basis": rec.get("basis"), "thickness_min_mm": thick.get("min_mm"),
            "thickness_face": thick.get("face"), "thickness_at_mm": thick.get("at_mm"),
            "hot_spots": shown, "counts": counts, "more": max(0, sum(counts.values()) - len(shown)),
            "summary": hot_spot_words(counts), "notes": list(rec.get("notes") or [])}


def hot_spot_words(counts: dict[str, int]) -> str:
    """"1 hole, 2 fillets and 1 sharp inside corner", or "no hot spots"."""
    parts = [f"{counts[k]} {_NOUNS[k][0 if counts[k] == 1 else 1]}" for k in _NOUNS if counts.get(k)]
    if not parts:
        return "no hot spots"
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def in_unit(record: dict | None, scale_to_m: float | None) -> dict | None:
    """The measurement re-read in the unit the user confirmed: every length scales by the ratio of
    the confirmed unit to the one it was measured under (a file read as millimetres and confirmed
    as inches has every length 25.4 times longer). For FEA build step 4, which plans in the
    confirmed unit."""
    if not isinstance(record, dict):
        return record
    measured_under = float(record.get("scale_to_m") or 0.0)
    if not scale_to_m or measured_under <= 0:
        return dict(record)
    k = float(scale_to_m) / measured_under
    if abs(k - 1.0) <= 1e-12:
        return dict(record)
    from meshpipeline.application.geometry_check import rescaled_lengths

    out = rescaled_lengths(record, k)
    out["scale_to_m"] = float(scale_to_m)
    return out


__all__ = ["TRY_ANYWAY", "has_solid", "warnings", "FOLD_BACK", "fold_back_message", "SELF_OVERLAP", "WAY_OUT_REPAIR", "WAY_OUT_REPAIR_PART", "self_overlap_message", "CONNECTION_OPTIONS", "CONNECTION_QUESTION", "DEFAULT_CONNECTION", "NOT_SOLID", "NOT_SOLID_REASONS",
           "QUESTION", "SHOWN_HOT_SPOTS", "GAP_JOIN_QUESTION", "DEFAULT_GAP_JOINS", "gap_words",
           "gap_joins_answer", "confirmed_gap_joins", "STAGE_FEA", "STAGE_FLOW", "STAGE_THERMAL", "WAY_OUT_SOLID", "assembly_view",
           "blocker", "body_count", "clean_words", "confirmed_connection", "connection_answer", "fea_view",
           "hot_spot_words", "in_unit", "measure_view", "not_solid_message", "parts_words", "stage_route"]
