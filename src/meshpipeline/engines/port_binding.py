# Responsibility: Deterministically bind the user's declared patches to the openings the
# tessellation measured, or refuse pre-mesh with an error that says exactly what to fix.
# Boundaries: pure - no I/O, no randomness, no model calls, safe to unit-test exhaustively.
# The caller re-keys its artifacts with apply_binding; physically folding a surplus opening's
# STL into the wall is the driver's job (the binder only records which ones fold).
from __future__ import annotations

import math
from dataclasses import dataclass, field

# The symmetric ratio bound of the area test: measured/declared must land in [LO, HI]. A 25%
# band either way separates standard adjacent bores by a full band (40 vs 60 mm differs 2.25x
# in area) while surviving tessellation drift, chamfers and small counterbores.
AREA_RATIO_LO = 0.75
AREA_RATIO_HI = 1.25
# Two declared sizes closer than HI/LO cannot be told apart by the area test under worst-case
# drift - a counterbored 40 can measure like a reduced-bore 50 and bind SWAPPED with no
# ambiguity flag - so ports that close must be resolved by location hints instead (rule 3b).
MIN_CLASS_SEPARATION = AREA_RATIO_HI / AREA_RATIO_LO
# A hint binds only well inside the part and only if it is decisively closer to one opening
# than to the runner-up; otherwise a datum offset or wrong axis convention picks silently.
HINT_MAX_BBOX_FRACTION = 0.25
HINT_MARGIN = 0.5
_EPS = 1e-9  # so exact boundary ratios (0.75, 1.25) compare inclusively despite float noise


class BindError(ValueError):
    """A declaration that cannot be bound unambiguously. Raised before any meshing happens,
    so it costs seconds and carries everything the user needs to disambiguate."""


@dataclass(frozen=True)
class DeclaredPatch:
    name: str
    role: str  # "wall" | "inlet" | "outlet"
    diameter_mm: float | None = None
    area_mm2: float | None = None
    width_mm: float | None = None
    height_mm: float | None = None
    near_mm: tuple[float, float, float] | None = None
    interchangeable_with: tuple[str, ...] = ()

    @classmethod
    def from_intake(cls, entry: dict) -> "DeclaredPatch":
        near = entry.get("near_mm")
        return cls(
            name=entry["name"],
            role=entry["type"],
            diameter_mm=entry.get("diameter_mm"),
            area_mm2=entry.get("area_mm2"),
            width_mm=entry.get("width_mm"),
            height_mm=entry.get("height_mm"),
            near_mm=tuple(near) if near is not None else None,
            interchangeable_with=tuple(entry.get("interchangeable_with") or ()),
        )

    def declared_area_m2(self) -> float | None:
        if self.diameter_mm is not None:
            return math.pi * (self.diameter_mm / 2.0) ** 2 * 1e-6
        if self.area_mm2 is not None:
            return self.area_mm2 * 1e-6
        if self.width_mm is not None and self.height_mm is not None:
            return self.width_mm * self.height_mm * 1e-6
        return None

    def sizing_forms(self) -> int:
        return sum((self.diameter_mm is not None,
                    self.area_mm2 is not None,
                    self.width_mm is not None or self.height_mm is not None))


@dataclass(frozen=True)
class Binding:
    wall_name: str
    port_map: dict[str, str]           # user name -> engine opening key
    folded_into_wall: tuple[str, ...]  # engine opening keys that are blind plugs, not ports
    evidence: tuple[dict, ...]         # one row per bound port, for the disclosure note


def _dist(a, b) -> float:
    return math.dist(a, b)


def _equiv_d_mm(area_m2: float) -> float:
    return math.sqrt(4.0 * area_m2 / math.pi) * 1000.0


def _ratio_ok(measured: float, declared: float) -> bool:
    r = measured / declared
    return (AREA_RATIO_LO - _EPS) <= r <= (AREA_RATIO_HI + _EPS)


def _measures(area: float, opening: dict | None) -> list[float]:
    """Every size measure an opening offers: its face area and, when the tessellation
    reports the port face as annular (a ring), the area its inner wire encloses. A
    thin-walled duct's end ring is a few thousand mm² of metal rimming a half-metre
    bore - the declaration talks about the bore, so the ring's metal area alone must
    not be the only thing the declared size is held against."""
    out = [float(area)]
    if opening and opening.get("area") is not None:
        out.append(float(opening["area"]))
    return out


def _size_agrees(area: float, opening: dict | None, declared: float) -> bool:
    return any(_ratio_ok(m, declared) for m in _measures(area, opening))


def _frame(t: dict) -> str:
    return (f"engine frame (metres): bbox_min={t['bbox_min']} bbox_max={t['bbox_max']} - "
            f"state locations in these coordinates")


def _listing(openings: list[tuple]) -> str:
    rows = []
    for key, a, c, opening in openings:
        row = (f"  {key}: centroid=({c[0]:.4f}, {c[1]:.4f}, {c[2]:.4f}) m, "
               f"area={a * 1e6:.1f} mm2, equivalent diameter={_equiv_d_mm(a):.1f} mm")
        if opening and opening.get("area") is not None:
            oa = float(opening["area"])
            row += (f" (ring face; inner opening {oa * 1e6:.1f} mm2, "
                    f"equivalent diameter {_equiv_d_mm(oa):.1f} mm)")
        rows.append(row)
    return "\n".join(rows)


def bind_ports(declared: list[DeclaredPatch], t: dict) -> Binding:
    walls = [d for d in declared if d.role == "wall"]
    ports = [d for d in declared if d.role in ("inlet", "outlet")]
    if len(walls) != 1:
        raise BindError(f"the declaration must carry exactly one wall patch, found "
                        f"{len(walls)}: {[w.name for w in walls]}")
    unknown = [d.name for d in declared if d.role not in ("wall", "inlet", "outlet")]
    if unknown:
        raise BindError(f"unknown roles for patches {unknown}; expected wall, inlet or outlet")

    openings = [(key, rec["area"], tuple(rec["centroid"]), rec.get("opening"))
                for key, rec in sorted(t["openings"].items())]
    frame = _frame(t)

    # Rule 0: every port must be bindable by SOMETHING - one sizing form, a location, or both.
    for p in ports:
        if p.sizing_forms() > 1:
            raise BindError(f"port '{p.name}' declares more than one size form "
                            f"(diameter/area/width x height) - state exactly one")
        if p.declared_area_m2() is None and p.near_mm is None:
            raise BindError(f"port '{p.name}' has no size and no location - state its "
                            f"diameter (or area, or width x height) or roughly where it is.\n"
                            f"Detected openings:\n{_listing(openings)}\n{frame}")

    # Rule 1, missing side: fewer openings than declared ports is unfixable here - either the
    # opening's planar face is encoded as a B-spline (the detector misses it) or the declaration
    # names a port the part does not have. Both need the user, not a retry.
    if len(openings) < len(ports):
        raise BindError(
            f"declared {len(ports)} ports but only {len(openings)} openings were detected.\n"
            f"Detected openings:\n{_listing(openings)}\n{frame}\n"
            f"If a real opening is missing its face may be modelled as a B-spline rather than "
            f"a plane - pass opening_faces to identify the opening faces explicitly.")

    pool: dict[str, tuple] = {k: (a, c, o) for k, a, c, o in openings}
    bound: dict[str, str] = {}
    diag = _dist(t["bbox_min"], t["bbox_max"])

    # Rule 2: location hints bind first, in name order (deterministic). A hint must be inside
    # the part's neighbourhood, decisively nearer its opening than the runner-up, and - when the
    # port also declares a size - agree with the measured area, so a wrong-frame hint cannot
    # override the size evidence silently.
    for p in sorted((q for q in ports if q.near_mm is not None), key=lambda q: q.name):
        hint = p.near_mm
        assert hint is not None  # the filter above guarantees it; mypy cannot see through sorted
        hint_m = tuple(v / 1000.0 for v in hint)
        ranked = sorted(pool.items(), key=lambda kv: _dist(hint_m, kv[1][1]))
        key, (area, centroid, opening) = ranked[0]
        d1 = _dist(hint_m, centroid)
        d2 = _dist(hint_m, ranked[1][1][1]) if len(ranked) > 1 else math.inf
        if d1 > HINT_MAX_BBOX_FRACTION * diag:
            raise BindError(
                f"port '{p.name}' states a location {p.near_mm} mm but the nearest detected "
                f"opening is {d1:.4f} m away (limit {HINT_MAX_BBOX_FRACTION * diag:.4f} m) - "
                f"the hint does not plausibly refer to any opening.\n"
                f"Detected openings:\n{_listing(openings)}\n{frame}")
        if d1 > HINT_MARGIN * d2:
            k2, (a2, c2, _o2) = ranked[1]
            raise BindError(
                f"port '{p.name}' states a location {p.near_mm} mm that does not decisively "
                f"pick one opening: {key} at ({centroid[0]:.4f}, {centroid[1]:.4f}, "
                f"{centroid[2]:.4f}) m is {d1:.4f} m away and {k2} at ({c2[0]:.4f}, "
                f"{c2[1]:.4f}, {c2[2]:.4f}) m is {d2:.4f} m away - state a location nearer "
                f"the intended opening.\n{frame}")
        declared_area = p.declared_area_m2()
        if declared_area is not None and not _size_agrees(area, opening, declared_area):
            raise BindError(
                f"port '{p.name}' points at opening {key} by location, but that opening "
                f"measures {area * 1e6:.1f} mm2 (equivalent diameter "
                f"{_equiv_d_mm(area):.1f} mm) while the port declares "
                f"{declared_area * 1e6:.1f} mm2 - the location and the size disagree, so "
                f"one of them is wrong.\nDetected openings:\n{_listing(openings)}\n{frame}")
        bound[p.name] = key
        del pool[key]

    # Rule 3: size classes for the remaining ports. Rule 0 guarantees each has a size.
    remaining = [p for p in ports if p.name not in bound]
    classes: dict[float, list[DeclaredPatch]] = {}
    for p in remaining:
        declared_class_area = p.declared_area_m2()
        # rule 0 guaranteed a size or a hint; hinted ports were bound (or refused) in rule 2,
        # so every remaining port has a size
        assert declared_class_area is not None
        classes.setdefault(declared_class_area, []).append(p)

    # Rule 3b (binder backstop; intake also refuses earlier): size classes the area test cannot
    # separate at worst-case drift may not coexist without hints - this is exactly how a
    # counterbored 40 binds swapped against a reduced-bore 50 with no ambiguity raised.
    areas = sorted(classes)
    for lo, hi in zip(areas, areas[1:]):
        if hi / lo < MIN_CLASS_SEPARATION - _EPS:
            lo_names = [p.name for p in classes[lo]]
            hi_names = [p.name for p in classes[hi]]
            raise BindError(
                f"declared port sizes are too close to tell apart reliably: {lo_names} "
                f"({lo * 1e6:.1f} mm2) vs {hi_names} ({hi * 1e6:.1f} mm2) differ by less "
                f"than the area test can separate under normal manufacturing drift. State "
                f"rough locations (near_mm) for these ports.\n"
                f"Detected openings:\n{_listing(openings)}\n{frame}")

    # Band-uniqueness: an opening the area test cannot assign to ONE class is fatal.
    matches: dict[str, list[float]] = {}
    for key, (area, _c, opening) in pool.items():
        matches[key] = [a for a in classes if _size_agrees(area, opening, a)]
        if len(matches[key]) > 1:
            raise BindError(
                f"detected opening {key} ({area * 1e6:.1f} mm2, equivalent diameter "
                f"{_equiv_d_mm(area):.1f} mm) matches more than one declared size - the "
                f"declaration cannot be bound by size alone. State rough locations "
                f"(near_mm).\nDetected openings:\n{_listing(openings)}\n{frame}")

    for class_area, class_ports in sorted(classes.items()):
        candidates = sorted((k for k, m in matches.items() if m == [class_area]),
                            key=lambda k: pool[k][1])
        names = sorted(p.name for p in class_ports)
        if len(candidates) < len(class_ports):
            raise BindError(
                f"declared ports {names} ({class_area * 1e6:.1f} mm2) match only "
                f"{len(candidates)} detected opening(s) of that size - a declared port has "
                f"no opening.\nDetected openings:\n{_listing(openings)}\n{frame}\n"
                f"If a real opening is missing its face may be modelled as a B-spline rather "
                f"than a plane - pass opening_faces to identify the opening faces explicitly.")
        if len(candidates) > len(class_ports):
            raise BindError(
                f"{len(candidates)} detected openings match the declared size of ports "
                f"{names} ({class_area * 1e6:.1f} mm2) but only {len(class_ports)} such "
                f"port(s) are declared - a same-size surplus opening cannot be folded into "
                f"the wall safely. If it is a real port, declare it; if it is a blind plug, "
                f"identify the true openings with opening_faces or state locations "
                f"(near_mm).\nDetected openings:\n{_listing(openings)}\n{frame}")
        if len(class_ports) > 1:
            # Rule 4: same size, same class - binding is arbitrary, which is only acceptable
            # when the user SAID the ports are interchangeable. Twin feeds carrying hot and
            # cold streams look identical here, and no gate downstream could catch a swap.
            roles = {p.role for p in class_ports}
            confirmed = all(
                q.name in p.interchangeable_with and p.name in q.interchangeable_with
                for p in class_ports for q in class_ports if p.name != q.name)
            if len(roles) > 1 or not confirmed:
                raise BindError(
                    f"ports {names} declare the same size ({class_area * 1e6:.1f} mm2) and "
                    f"cannot be told apart. If they truly carry no distinct streams, mark "
                    f"them interchangeable; otherwise state rough locations (near_mm).\n"
                    f"Detected openings:\n{_listing(openings)}\n{frame}")
            # Deterministic tie-break, stable across attempt re-tessellation: names in
            # lexicographic order onto candidates in centroid (x, y, z) order.
        for name, key in zip(names, candidates):
            bound[name] = key
        for key in candidates:
            del pool[key]

    # Whatever remains matched no declared size: blind plugs and machining faces fold into the
    # wall. (Anything that DID match a declared size was refused above, so this is safe.)
    folded = tuple(sorted(pool))

    by_name = {p.name: p for p in ports}
    evidence = tuple(
        {"name": name, "role": by_name[name].role, "engine_key": key,
         "centroid": list(t["openings"][key]["centroid"]),
         "area_m2": t["openings"][key]["area"],
         # only ring (annular) port faces carry it: the area the inner wire encloses -
         # the size the user's declaration matched when the metal ring's own area could
         # not (a thin-walled duct's end ring around a large bore)
         **({"opening_area_m2": t["openings"][key]["opening"]["area"]}
            if t["openings"][key].get("opening") else {})}
        for name, key in sorted(bound.items()))
    return Binding(wall_name=walls[0].name, port_map=dict(sorted(bound.items())),
                   folded_into_wall=folded, evidence=evidence)


def declaration_targets(intake_patches: list) -> list:
    """The declaration reduced to what face selection needs: name, target area in m², and the
    location hint in metres. Ports only; [] when nothing is declared."""
    out = []
    for p in (intake_patches or []):
        if not isinstance(p, dict) or (p.get("type") or "").strip() not in ("inlet", "outlet"):
            continue
        dp = DeclaredPatch.from_intake(p)
        out.append({"name": dp.name, "area_m2": dp.declared_area_m2(),
                    "near_m": (tuple(v / 1000.0 for v in dp.near_mm) if dp.near_mm else None),
                    # the declared SHAPE, for candidates whose opening is an inner wire:
                    # circles are matched by bore diameter, rectangles by W x H
                    "d_m": (dp.diameter_mm / 1000.0 if dp.diameter_mm is not None else None),
                    "wh_m": ((dp.width_mm / 1000.0, dp.height_mm / 1000.0)
                             if dp.width_mm is not None and dp.height_mm is not None
                             else None)})
    return out


def bind_intake(t: dict, intake_patches: list) -> tuple[dict, str, str]:
    """Bind intake-declared patches onto the measured openings: (t re-keyed to user names,
    wall key, binding-evidence note). No declaration -> t untouched, engine-canonical keys and
    an empty note, so programmatic submits keep today's behaviour exactly. A BindError
    propagates to the caller - a pre-mesh refusal, not a meshing failure. Shared by every
    engine that carves internal flow (snappy, cfmesh), so a combiner binds identically
    whichever engine meshes it."""
    declared = [p for p in (intake_patches or []) if isinstance(p, dict)]
    if not declared:
        return t, "wall", ""
    b = bind_ports([DeclaredPatch.from_intake(p) for p in declared], t)
    out = apply_binding(t, b)
    rows = "; ".join(
        f"{p['name']} ({p['role']}) at ({', '.join(f'{v:.3f}' for v in p['centroid'])}) m, "
        f"{float(p['area_m2']) * 1e6:.0f} mm²"
        + (f" (ring face; opening {float(p['opening_area_m2']) * 1e6:.0f} mm²)"
           if p.get("opening_area_m2") is not None else "")
        for p in out["binding"]["ports"])
    note = (f"bound to your declared ports: {rows}; wall = {b.wall_name}"
            + (f"; {len(b.folded_into_wall)} blind face(s) folded into the wall"
               if b.folded_into_wall else ""))
    return out, b.wall_name, note


def apply_binding(t: dict, b: Binding) -> dict:
    """Re-key the tessellation output so the user's names are the only names downstream.
    Folded openings leave 'openings' and their STLs move to 'folded_stls' for the driver to
    merge into the wall surface."""
    inverse = {engine: user for user, engine in b.port_map.items()}
    out = dict(t)
    out["stls"] = {b.wall_name: t["stls"]["wall"],
                   **{inverse[k]: v for k, v in t["stls"].items()
                      if k != "wall" and k in inverse}}
    out["folded_stls"] = {k: t["stls"][k] for k in b.folded_into_wall}
    out["openings"] = {inverse[k]: v for k, v in t["openings"].items() if k in inverse}
    out["binding"] = {"wall_name": b.wall_name,
                      "ports": [dict(row) for row in b.evidence],
                      "folded_into_wall": list(b.folded_into_wall)}
    return out
