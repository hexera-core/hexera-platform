# Responsibility: FIRST WHAT THE PART IS, THEN WHICH OPENING IS WHICH. One "what is this" result
# (understand: the object in plain words, its class, how sure, who said so and why) made from the
# user's own words, the measured openings (how many, how big, which are equal, which face each other
# on one line, which stand square to the rest), the inside-space map and the flow kind - and the
# picture model's name only where the words name nothing. Then one role for every opening (assign:
# inlet or outlet, with a sureness, a one-line reason and whether it is a guess) by general
# engineering reasoning from that understanding: a pump draws in at its eye, a diffuser is fed at its
# narrow end, an exhaust manifold collects its runners into one pipe; where nothing can know the way
# the fluid goes (a straight pipe, a bend, a tee that may split or mix) the role is a best guess,
# marked as one, for the user to confirm or flip on the stage.
# Boundaries: pure - the check's facts, the proposal's openings, the user's words and the sanitized
# picture answer in; plain dicts out. Never a file or part name, never a model call; never raises.
from __future__ import annotations

import math
import re

from meshpipeline.application.flow_kind import EXTERNAL, INTERNAL

#: The rules (docs/RULES.md): what the part is, the roles from it, and no sticker on a non-opening.
RULE_UNDERSTAND, RULE_ROLES, RULE_STICKERS = "FLOW-6", "FLOW-7", "FLOW-8"
#: Two openings are EQUAL when their sizes differ by under this share of the larger.
EQUAL = 0.1
#: Two mouths face each other on ONE LINE when their normals' dot is under -LINE and the line between
#: them leaves the normal by under 1 - LINE (as a cosine); they face the SAME WAY over +LINE.
LINE = 0.95
#: A third opening stands SQUARE to a run when its normal's dot with the run's axis is under this.
SQUARE = 0.35
#: ...and their centres lie off each other's axis by under this share of the wider mouth.
OFF_LINE = 0.25
#: A WAIST: a fluid body between two equal ends holding under this share of a straight cylinder.
WAIST = 0.9
#: A COLLECTOR: the largest opening is at least this much wider than the next.
LARGER = 1.25
#: How sure each shape class is (the share of the labelled corpus it names right is in docs/RULES.md).
SURE = {"straight": 0.7, "venturi": 0.65, "reducer": 0.65, "diffuser": 0.75, "bend": 0.75, "return_bend": 0.7, "offset": 0.65,
        "chamber": 0.55, "volute": 0.6, "tee": 0.75, "wye": 0.65, "three_way": 0.45, "manifold": 0.7,
        "header": 0.6, "circuits": 0.6, "branching": 0.4, "vessel": 0.4, "closed": 0.3, "body": 0.6}


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(parts), re.IGNORECASE)


#: THE OBJECT THE WORDS NAME: (class, mode, pattern). `mode` says which way the fluid goes where the
#: object itself says it: "collect" (many in, one out), "distribute" (one in, many out), "mix", "split".
_NAMED: tuple[tuple[str, str, re.Pattern], ...] = (
    ("manifold", "collect", _rx(r"\bexhaust (?:manifold|header|collector)s?\b", r"\bcollectors?\b",
                                r"\b(?:collecting|merging|combining) manifolds?\b")),
    ("manifold", "distribute", _rx(r"\b(?:intake|inlet|induction|distribution|supply) manifolds?\b",
                                   r"\bdistributors?\b", r"\bdistribution (?:header|block)\b", r"\bsplitter manifold")),
    ("manifold", "", _rx(r"\bmanifolds?\b", r"\bheaders?\b")),
    ("tee", "mix", _rx(r"\bmixing (?:tee|t|junction|wye)\b", r"\bt-?mixer\b", r"\bmixing\b")),
    ("tee", "split", _rx(r"\b(?:splitting|dividing|diverting) (?:tee|t|junction|wye)\b", r"\bsplitter\b", r"\bdivider\b")),
    ("tee", "", _rx(r"\btee\b", r"\bt[- ](?:piece|junction|joint)\b")),
    ("wye", "", _rx(r"\bwye\b", r"\by[- ](?:piece|junction|branch)\b", r"\bbifurcation\b")),
    ("circuits", "", _rx(r"\bheat exchangers?\b", r"\bshell[- ]and[- ]tube\b", r"\bcounter[- ]?flow\b", r"\bradiators?\b",
                         r"\bcold plates?\b")),
    ("volute", "", _rx(r"\bvolutes?\b", r"\bscroll casing\b", r"\bpump (?:casing|housing|volute)\b",
                       r"\bcentrifugal (?:pump|fan|blower|compressor)\b", r"\bblower housing\b")),
    ("diffuser", "", _rx(r"\bdiffusers?\b")),
    ("reducer", "", _rx(r"\breducers?\b", r"\breducing\b", r"\bnozzles?\b", r"\bcontractions?\b", r"\bventuri\b",
                        r"\bconfusers?\b", r"\btransitions?\b", r"\bexpanders?\b")),
    ("chamber", "", _rx(r"\bvalves?\b", r"\bplenums?\b", r"\bhousings?\b", r"\bfilter housings?\b", r"\bmufflers?\b",
                        r"\bsilencers?\b", r"\bchambers?\b")),
    ("bend", "", _rx(r"\belbows?\b", r"\bbends?\b", r"\bu[- ]?bends?\b", r"\breturn bends?\b", r"\bs[- ]?ducts?\b")),
    ("straight", "", _rx(r"\bpipes?\b", r"\btubes?\b", r"\bducts?\b", r"\bchannels?\b")),
    ("vessel", "", _rx(r"\btanks?\b", r"\bvessels?\b", r"\breactors?\b", r"\bcyclones?\b")),
)

_WHAT = {"straight": "a straight pipe or duct: two equal ends on one line",
         "reducer": "a reducer: a wide end and a narrow end on one line",
         "diffuser": "a diffuser: a passage that widens gradually from its narrow end",
         "bend": "a bend: two ends at an angle", "return_bend": "a return bend: two ends side by side, facing the same way",
         "offset": "an offset (S) duct: two ends facing apart, off one line",
         "chamber": "a chamber between its ends (a valve body, a housing or a plenum)",
         "volute": "a volute or pump casing: an eye on its axis and an outlet off to the side",
         "tee": "a tee: a straight run with a branch square to it", "wye": "a wye: a run with a branch at an angle",
         "venturi": "a venturi or orifice: a throat between two equal ends",
         "three_way": "a three-way junction", "manifold": "a manifold: {k} branches off one larger pipe",
         "header": "a header with {n} equal branches", "circuits": "{p} separate passages (a heat exchanger or a cold plate)",
         "branching": "a branching part with {n} openings", "vessel": "a vessel with one opening",
         "closed": "a closed part with no openings found", "body": "a body in a flow"}


# ------------------------------------------------------------------------------ measuring ----
def _num(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _vec(v) -> list[float]:
    try:
        out = [float(x) for x in v][:3]
        return out if len(out) == 3 else [0.0, 0.0, 0.0]
    except (TypeError, ValueError):
        return [0.0, 0.0, 0.0]


def size_of(o: dict) -> float:
    """An opening's size: its diameter, or the diameter of a circle of its area."""
    d = _num(o.get("diameter_mm"))
    if d > 0:
        return d
    a = _num(o.get("area_mm2"))
    return 2.0 * math.sqrt(a / math.pi) if a > 0 else 0.0


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def on_one_line(a: dict, b: dict) -> bool:
    """Two mouths that face apart along one line: the two ends of something straight - their centres
    off each other's axis by under OFF_LINE of the wider one."""
    na, nb = _vec(a.get("normal")), _vec(b.get("normal"))
    ca, cb = _vec(a.get("centroid_mm")), _vec(b.get("centroid_mm"))
    d = [y - x for x, y in zip(ca, cb)]
    along = _dot(d, nb)
    lateral = math.sqrt(max(_dot(d, d) - along * along, 0.0))
    return _dot(na, nb) < -LINE and lateral <= OFF_LINE * max(size_of(a), size_of(b), 1e-9)


def _opens(facts: dict) -> bool:
    """Whether the passage behind any mouth opens at once into a chamber (cad/flow_reading's profile
    reaches OPENS: twice the mouth's radius)."""
    prof = ((facts.get("reading") or {}).get("roles") or {}).get("profiles") or {}
    return any(_num(v) >= 2.0 for p in prof.values() for v in (p or []))


def _waist(facts: dict, a: dict, b: dict) -> bool:
    """A fluid body narrower in its middle than at its equal ends (a venturi's throat, an orifice): its
    volume is well under a straight cylinder of its ends' size between them."""
    if str(facts.get("input_kind_scout") or facts.get("input_kind") or "") != "fluid-domain":
        return False
    vol = _num((facts.get("inside") or {}).get("enclosed_mm3"))
    ca, cb = _vec(a.get("centroid_mm")), _vec(b.get("centroid_mm"))
    length = math.sqrt(sum((y - x) ** 2 for x, y in zip(ca, cb)))
    area = max(_num(a.get("area_mm2")), _num(b.get("area_mm2"))) or math.pi / 4.0 * max(size_of(a), size_of(b)) ** 2
    return vol > 0 and length > 0 and vol < WAIST * area * length


def equal(a: dict, b: dict) -> bool:
    sa, sb = size_of(a), size_of(b)
    return max(sa, sb) > 0 and abs(sa - sb) <= EQUAL * max(sa, sb)


def ports(openings) -> list[dict]:
    return [o for o in openings or [] if isinstance(o, dict) and o.get("role") in ("inlet", "outlet")]


# ---------------------------------------------------------------------------- understand ----
def words_object(text: str) -> tuple[str, str, str]:
    """(class, mode, the words) the user's words name, or ("", "", "")."""
    said = " ".join(str(text or "").split())
    for cls, mode, rx in _NAMED:
        m = rx.search(said)
        if m:
            return cls, mode, m.group(0).strip()
    return "", "", ""


def shape_class(facts: dict, openings, flow: str) -> tuple[str, dict]:
    """The class the measured openings make, and the measured details the roles are assigned from."""
    if flow == EXTERNAL:
        return "body", {}
    ps = ports(openings)
    n = len(ps)
    if n == 0:
        return "closed", {}
    if n == 1:
        return "vessel", {}
    inside = facts["inside"] if isinstance(facts.get("inside"), dict) else {}
    circuits = [int(_num(m)) for m in inside.get("mouths_per_passage") or [] if int(_num(m)) >= 2]
    if len(circuits) >= 2 and n >= 4:
        return "circuits", {"passages": len(circuits)}
    sizes = sorted((size_of(o) for o in ps), reverse=True)
    if n == 2:
        a, b = ps
        eye = _volute(facts, a, b)
        if eye is not None:
            return "volute", {"eye": eye.get("id")}
        if _opens(facts):
            return "chamber", {}
        if on_one_line(a, b):
            if equal(a, b):
                return ("venturi", {}) if _waist(facts, a, b) else ("straight", {})
            roles = (facts.get("reading") or {}).get("roles") or {}
            if roles.get("inlet") is not None and _num(roles.get("confidence")) >= 0.7:
                return "diffuser", {"inlet": roles.get("inlet")}
            return "reducer", {}
        dot = _dot(_vec(a.get("normal")), _vec(b.get("normal")))
        if dot > LINE:
            return "return_bend", {}
        if dot < -LINE:
            return "offset", {}
        return "bend", {}
    run = _run(ps)
    if n == 3:
        if run is not None:
            k = next(o for o in ps if o.get("id") not in run)
            axis = _vec(by_id(ps, run[0]).get("normal"))
            cls = "tee" if abs(_dot(axis, _vec(k.get("normal")))) < SQUARE else "wye"
            return cls, {"run": list(run), "branch": k.get("id")}
        for stem in ps:
            arms = [o for o in ps if o is not stem]
            mean = [x + y for x, y in zip(_vec(arms[0].get("normal")), _vec(arms[1].get("normal")))]
            norm = math.sqrt(_dot(mean, mean)) or 1.0
            if equal(arms[0], arms[1]) and _dot(_vec(stem.get("normal")), mean) / norm < -0.8:
                return "wye", {"stem": stem.get("id"), "arms": [o.get("id") for o in arms]}
        return "three_way", {}
    if run is not None:
        width = max(size_of(by_id(ps, i)) for i in run)
        branches = [o for o in ps if o.get("id") not in run]
        if all(size_of(o) < width for o in branches):
            return "manifold", {"run": list(run), "branches": [o.get("id") for o in branches], "kind": "header"}
    big = max(ps, key=size_of)
    rest = [o for o in ps if o is not big]
    if sizes[0] >= LARGER * sizes[1] and all(equal(rest[0], o) for o in rest[1:]):
        return "manifold", {"collector": big.get("id"), "runners": [o.get("id") for o in rest]}
    if all(equal(ps[0], o) for o in ps[1:]):
        return "header", {}
    return "branching", {}


def by_id(ps: list[dict], i) -> dict:
    return next(o for o in ps if o.get("id") == i)


def _run(ps: list[dict]):
    """The RUN: the widest pair of openings that face apart on one line (a tee's or a header's two
    ends), as their ids, or None."""
    pairs = [(a, b) for k, a in enumerate(ps) for b in ps[k + 1:] if on_one_line(a, b)]
    if not pairs:
        return None
    a, b = max(pairs, key=lambda p: size_of(p[0]) + size_of(p[1]))
    return (a.get("id"), b.get("id"))


def _volute(facts: dict, a: dict, b: dict):
    """The EYE of a casing round an axis - an opening on the part's thin axis, near its middle, with
    the other square to that axis, off to the side - or None."""
    size = [_num(v) for v in facts.get("size_mm") or [0, 0, 0]]
    lo_m = facts.get("bbox_min_m")
    if not any(size) or not lo_m:
        return None
    lo = [1000.0 * v for v in _vec(lo_m)]
    mid = [lo[i] + 0.5 * size[i] for i in range(3)]
    for eye, out in ((a, b), (b, a)):
        ne, no = _vec(eye.get("normal")), _vec(out.get("normal"))
        k = max(range(3), key=lambda i: abs(ne[i]))
        ce, co = _vec(eye.get("centroid_mm")), _vec(out.get("centroid_mm"))
        others = [i for i in range(3) if i != k]
        centred = all(abs(ce[i] - mid[i]) <= 0.2 * size[i] for i in others)
        aside = any(abs(co[i] - mid[i]) >= 0.3 * size[i] for i in others)
        # TANGENTIAL: the outlet's axis passes the eye's axis well off it (a bend's end points at it)
        i, j = others
        rel = [co[i] - ce[i], co[j] - ce[j]]
        tangent = abs(rel[0] * no[j] - rel[1] * no[i]) / (math.hypot(no[i], no[j]) or 1.0)
        if abs(ne[k]) > 0.9 and abs(no[k]) < SQUARE and centred and aside and tangent >= 0.4 * size_of(out):
            return eye
    return None


def understand(facts: dict, openings, *, flow: str = "", words: str = "", vision: dict | None = None) -> dict:
    """WHAT THE PART IS: {object, class, mode, confidence, by, reasons, measured}. The user's words name
    it first (they win over the pictures); the measured openings give the class whatever is named, and
    say so when the two disagree; the picture model's name is used only when the words name nothing."""
    try:
        out = _understand(facts if isinstance(facts, dict) else {}, openings, flow, words, vision)
    except Exception:  # noqa: BLE001 - a part that cannot be read is said to be unknown, never an error
        out = {"object": "a part the check could not read", "class": "", "mode": "", "confidence": 0.0,
               "by": "shape", "reasons": [], "measured": {}}
    out["rule"] = RULE_UNDERSTAND
    return out


def _understand(facts: dict, openings, flow: str, words: str, vision: dict | None) -> dict:
    flow = flow or str(facts.get("flow") or INTERNAL)
    cls, measured = shape_class(facts, openings, flow)
    n = len(ports(openings))
    k = len(measured.get("branches") or measured.get("runners") or []) or max(n - 1, 0)
    what = _WHAT.get(cls, "a part").format(n=n, k=k, p=measured.get("passages", 0))
    reasons = [f"measured: {what}"] if cls else []
    named, mode, said = words_object(words)
    if named:
        reasons.insert(0, f'you said "{said}"')
        if cls and cls not in (named, "body") and not (named in ("bend",) and cls in ("return_bend", "offset")):
            reasons.append("the measured openings read differently; your words win")
        return {"object": f"{said} ({what})" if named == cls else said, "class": named, "mode": mode,
                "confidence": 0.9, "by": "words", "reasons": reasons, "measured": {"class": cls, **measured}}
    part = str((vision or {}).get("part") or "") if isinstance(vision, dict) and "error" not in vision else ""
    pnamed, pmode, _ = words_object(part)
    if pnamed and pnamed == cls:
        return {"object": f"{part} ({what})", "class": cls, "mode": pmode, "confidence": max(SURE.get(cls, 0.4), 0.75),
                "by": "shape", "reasons": [*reasons, f'the pictures agree: "{part}"'], "measured": {"class": cls, **measured}}
    if pnamed and SURE.get(cls, 0.0) < 0.6:
        return {"object": part, "class": pnamed, "mode": pmode, "confidence": 0.6, "by": "pictures",
                "reasons": [f'the pictures show "{part}"', *reasons], "measured": {"class": cls, **measured}}
    return {"object": what, "class": cls, "mode": "", "confidence": SURE.get(cls, 0.4), "by": "shape",
            "reasons": reasons + ([f'the pictures suggest "{part}"'] if part else []), "measured": {"class": cls, **measured}}


# -------------------------------------------------------------------------------- assign ----
def _role(o: dict, role: str, confidence: float, reason: str, guess: bool) -> None:
    o["role"] = role
    o["role_reason"] = {"confidence": round(float(confidence), 2), "reason": reason, "guess": bool(guess),
                        "rule": RULE_ROLES}


def _rename(ps: list[dict]) -> None:
    """inlet / outlet, numbered when there are several, in the order the openings stand."""
    for role in ("inlet", "outlet"):
        same = [o for o in ps if o.get("role") == role]
        for k, o in enumerate(same, start=1):
            o["name"] = role if len(same) == 1 else f"{role}_{k}"


def assign(openings, understanding: dict, facts: dict | None = None) -> list[dict]:
    """EVERY OPENING'S ROLE FROM WHAT THE PART IS: a copy of the openings, each port with its role, a
    `role_reason` {confidence, reason, guess} and a name that goes with it. Where nothing can know the
    way the fluid goes, the measuring step's order stands and is marked a guess. An opening the file
    declares (declared_by) or that is not an opening is left as it is. Never raises."""
    out = [dict(o) for o in openings or [] if isinstance(o, dict)]
    try:
        _assign(out, understanding if isinstance(understanding, dict) else {}, facts if isinstance(facts, dict) else {})
    except Exception:  # noqa: BLE001 - the measuring step's roles stand
        return [dict(o) for o in openings or [] if isinstance(o, dict)]
    return out


def _assign(out: list[dict], und: dict, facts: dict) -> None:
    ps = [o for o in ports(out) if not o.get("declared_by")]
    if len(ps) < 2:
        return
    cls, mode = str(und.get("class") or ""), str(und.get("mode") or "")
    m = und.get("measured") or {}
    by_id = {o.get("id"): o for o in ps}
    first_in = next((o for o in ps if o.get("role") == "inlet"), ps[0])

    def keep(reason: str, conf: float) -> None:
        for o in ps:
            _role(o, str(o.get("role")), conf, reason, True)

    def roles(inlets: set, reason: str, conf: float, guess: bool) -> None:
        for o in ps:
            _role(o, "inlet" if o.get("id") in inlets else "outlet", conf, reason, guess)
        _rename(ps)

    if cls == "volute" and m.get("eye") in by_id:
        roles({m["eye"]}, "a pump or fan draws the fluid in at the eye on its axis and delivers it through "
                          "the volute: flip both if this one runs backwards", 0.75, False)
    elif cls == "diffuser" and m.get("inlet") in by_id:
        roles({m["inlet"]}, "a diffuser is fed at its narrow end and widens to the other", 0.75, False)
    elif cls == "manifold" and m.get("run") and all(i in by_id for i in m["run"]):
        run = list(m["run"])
        first = first_in.get("id") if first_in.get("id") in run else run[0]
        branches = set(m.get("branches") or [])
        if mode == "collect":
            roles(branches | {first}, "a collecting manifold takes the flow in at its branches and out at one end "
                                      "of its pipe; which end is the way out is a guess", 0.6, True)
        else:
            roles({first}, "the flow is taken in at one end of the main pipe and out through the branches and the "
                           "other end (a distributing manifold); a collector runs the other way, and either end may "
                           "be the feed - a guess", 0.45 if not mode else 0.6, True)
    elif cls in ("tee", "wye") and m.get("stem") in by_id:
        arms = set(m.get("arms") or [])
        if mode == "mix":
            roles(arms, "a mixing wye takes two flows in at its arms and joins them into the stem", 0.8, False)
        elif mode == "split":
            roles({m["stem"]}, "a dividing wye takes the flow in at its stem and splits it to the two arms", 0.8, False)
        else:
            roles({m["stem"]}, "a wye may split one flow into its arms or join two into its stem: the stem is taken "
                               "as the inlet - a guess", 0.45, True)
    elif cls == "manifold" and m.get("collector") in by_id:
        runners = set(m.get("runners") or [])
        if mode == "collect":
            roles(runners, "a collecting manifold takes the flow in at its equal runners and out through the "
                           "larger collector", 0.85, False)
        elif mode == "distribute":
            roles({m["collector"]}, "a distributing manifold takes the flow in at the larger opening and shares "
                                    "it out through the equal runners", 0.85, False)
        else:
            roles({m["collector"]}, "the larger opening is taken as the feed and the equal runners as the outlets; "
                                    "a collector (an exhaust manifold) runs the other way - a guess", 0.45, True)
    elif cls in ("tee", "wye") and m.get("run") and m.get("branch") in by_id:
        run = [i for i in m["run"] if i in by_id]
        first = first_in.get("id") if first_in.get("id") in run else run[0]
        other = next(i for i in run if i != first)
        if mode == "mix":
            roles({first, m["branch"]}, "a mixing tee takes two flows in - one run end and the branch - and sends "
                                        "them out the other run end; which run end is the way out is a guess",
                  0.55, True)
        elif mode == "split":
            roles({first}, "a dividing tee takes the flow in at one run end and splits it to the other run end "
                           "and the branch; which run end is the feed is a guess", 0.55, True)
        else:
            roles({first}, "a tee may split one flow or mix two, and either run end may be the feed: one run end is "
                           f"taken as the inlet - a guess (opening {other} and the branch can be inlets too)", 0.4, True)
    elif cls in ("straight", "bend", "return_bend", "offset", "chamber", "venturi"):
        keep("its ends look alike, so which one the fluid enters is a guess: flip them if it flows the other way", 0.5)
    elif cls == "reducer":
        keep("a reducer is passed either way (a nozzle from its wide end, a diffuser from its narrow end): "
             "which end is the inlet is a guess", 0.5)
    else:
        keep("nothing measured says which way the fluid goes through these openings: the roles are a guess", 0.4)
