"""Try the team's meshing standard (Settings > Workspace > Meshing, STD-1..5) against the REAL model: every line of
devtools/settings_bank.json is read the way the product reads a saved standard (engines/meshing_standard.read_at_save:
the notes' reader's own prompt and tool, the builder route, the same wait), then checked and merged for each route the
line names exactly as a run does (meshing_standard.checked with the route, then merge with no notes), and one row is
printed per line and route: what was read (field = value, strictness), what the user is told (applied, nearest, not
applied and why, put it in a run's notes, delivered as a solver file, not for this kind of mesh, kept as guidance, or
nothing at all), and OK or MISS against the bank's expectation. Totals per category and per route close the run.
Nothing is stored; no database, no worker, no workspace.

The model settings are read the way the app reads them: from the environment (meshpipeline.settings, the builder's
route in agents/builder/settings). No setting value is printed. With no model configured it says so and exits 0.

Run from the worktree, with the environment the app runs with:
    PYTHONPATH=src python devtools/try_meshing_standard.py                     # the whole bank
    PYTHONPATH=src python devtools/try_meshing_standard.py --only D01,SHEET1   # some lines
    PYTHONPATH=src python devtools/try_meshing_standard.py --category design_intent --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

BANK = Path(__file__).with_name("settings_bank.json")
#: the bank's routes -> the route a run reads the standard with (engines/fea_driver.read_requirements: "fea";
#: engines/cfd_requirements.read_for_attempt: "flow")
ROUTES = {"solid": "fea", "shell": "fea", "cfd": "flow"}
GUIDANCE_KEY = "guidance"           # the merged set's standard.guidance: the lines handed to the AI steps (STD-6)


# #
# what the product made of one line, on one route
# #

def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def observed(merged: dict, settings: dict) -> dict:
    """What the merged section holds and tells, in the bank's outcome words: {"fields": {"global": 5.0, ...},
    "strict": {"aspect_ratio": "hard"}, "classes": {"told", "notes", ...}, "lines": [...], "unit": "mm"}."""
    from meshpipeline.engines import meshing_standard as MS

    got = merged.get("requirements")
    req: dict = got if isinstance(got, dict) else {}
    fields: dict[str, object] = {}
    strict: dict[str, str] = {}
    limits = {str(e.get("id")): e for e in req.get("limits") or []}
    local = {str(e.get("id")): e for e in req.get("local") or []}
    sizes, kind = req.get("sizes") or {}, req.get("kind") or {}
    layers, cells, controls = req.get("layers") or {}, req.get("cells") or {}, req.get("controls") or {}
    layer_key = {"count": "count", "first_height": "first_mm", "total_thickness": "total_mm", "growth": "growth",
                 "y_plus": "y_plus"}
    for r in req.get("rows") or []:
        if r.get("from", "settings") != "settings":
            continue
        f, rid = str(r.get("field") or ""), str(r.get("id") or "")
        if f in ("global", "min_edge", "max_edge", "thickness", "near_wall"):
            fields[f] = _num(sizes.get(f"{f}_mm"))
        elif f == "growth":
            fields[f] = _num(sizes.get("growth"))
        elif f.startswith("limit:"):
            lim = limits.get(rid, {})
            op = "<=" if str(lim.get("op")) in ("<=", "<") else ">="
            key = f"{f}{op}{_num(lim.get('target'))}"
            share = _num(lim.get("share")) or 0.0
            if share:
                key += f"/{share * 100:g}%" + (f"/{_num(lim.get('outer'))}" if lim.get("outer") is not None else "")
            fields[key] = True
        elif f in ("quads_only", "triangles_only", "linear", "quadratic", "free_mesh", "no_washers", "structured"):
            fields[f"kind:{f}"] = True
        elif f == "max_triangle_percent":
            fields[f"kind:tria%<={_num(kind.get('tria_percent_max'))}"] = True
        elif f.startswith("cells:"):
            fields[f] = _num(cells.get(f.split(":", 1)[1]))
        elif f.startswith("layers:"):
            fields[f] = _num(layers.get(layer_key.get(f.split(":", 1)[1], "")))
        elif f.startswith("control:"):
            c = controls.get(f.split(":", 1)[1]) or {}
            fields[f] = c.get("choice") or _num(c.get("value"))
        elif f == "local":
            e = local.get(rid, {})
            fields[f"local:{' '.join(str(e.get('where') or '').split()).casefold()}"] = _num(e.get("mm")) or 0.0
        if r.get("strictness"):
            strict[f.split(":", 1)[-1]] = str(r["strictness"])
    lines = [str(w) for w in merged.get("lines") or []]
    off = str(merged.get("standard_not_applied") or "")
    if off and off not in lines:
        lines.insert(0, off)
    classes: set[str] = set()
    for w in lines:
        if w.startswith("Not applied"):
            classes.add("told")
            if MS.ELSEWHERE_WORDS in w:
                classes.add("notes")
            else:
                classes.add("not_possible")
        if "every FEA run delivers them" in w:
            classes.add("solver_file")
        if w.startswith("Not a mesh setting"):
            classes.update(("told", "not_possible"))
        if w.startswith(MS.GUIDANCE_LEAD):
            # STD-6: handed to the run's AI steps (meshing_standard.guidance_notes) and told so
            classes.update(("told", "guidance"))
    if off:
        classes.add("not_here")
    if (merged.get("standard") or {}).get(GUIDANCE_KEY) if isinstance(merged.get("standard"), dict) else False:
        classes.add("guidance")
    unit = (merged.get("standard") or {}).get("unit") if isinstance(merged.get("standard"), dict) else None
    return {"fields": fields, "strict": strict, "classes": classes, "lines": lines, "unit": unit,
            "status": merged.get("status")}


_FIELD = re.compile(r"^(?P<name>[a-z_:%<>=.]+?[a-z_%])(?P<op><=|>=|=)(?P<rest>.+)$")


def _close(a, b) -> bool:
    fa, fb = _num(a), _num(b)
    if fa is None or fb is None:
        return str(a).strip().casefold() == str(b).strip().casefold()
    return math.isclose(fa, fb, rel_tol=1e-3, abs_tol=1e-12)


def _canon(key: str, *, base: bool = False) -> str:
    """A limit or kind key with its numbers written one way ("limit:warpage<=15.0/1%/30.0"); `base`: without the
    allowance (an expectation that names none matches a limit with or without one)."""
    m = re.match(r"^((?:limit:[a-z_]+|kind:tria%))(<=|>=)([0-9.eE+-]+)(?:/([0-9.]+)%(?:/([0-9.eE+-]+))?)?$", key)
    if not m:
        return key
    out = f"{m.group(1)}{m.group(2)}{float(m.group(3)):g}"
    if m.group(4) and not base:
        out += f"/{float(m.group(4)):g}%" + (f"/{float(m.group(5)):g}" if m.group(5) else "")
    return out


def met(outcome: str, seen: dict) -> bool:
    """Whether one expected outcome (the bank's words, alternatives split by |) is what the product made."""
    for alt in str(outcome).split("|"):
        alt = alt.strip()
        if alt in ("guidance", "notes", "not_possible", "solver_file", "not_here", "told"):
            if alt in seen["classes"]:
                return True
            continue
        if alt.startswith("unit="):
            if str(seen["unit"] or "") == alt.split("=", 1)[1]:
                return True
            continue
        if alt.startswith("strict:"):
            target, level = alt[len("strict:"):].split("=", 1)
            got = seen["strict"]
            if target in got and got[target] == level:
                return True
            if target in ("quality", "all", "minimums", "maximums", "sizes", "angles", "counts", "count") and got \
                    and all(v == level for v in got.values()):
                return True
            continue
        if alt.startswith(("limit:", "kind:")):
            if _canon(alt) in {_canon(k, base=("/" not in alt)) for k in seen["fields"]}:
                return True
            continue
        fm = _FIELD.match(alt)
        if fm and fm.group("name").startswith("local:"):
            where, value = alt[len("local:"):].rsplit("=", 1)
            got = seen["fields"].get(f"local:{where.casefold()}")
            if got is not None and _close(got, value):
                return True
            continue
        if fm:
            got = seen["fields"].get(fm.group("name"))
            if got is not None and _close(got, fm.group("rest")):
                return True
    return False


def told_as(seen: dict) -> str:
    """The plainest summary of what the user is told for the line."""
    c = seen["classes"]
    parts = []
    if seen["fields"]:
        parts.append("applied")
    for k in ("solver_file", "notes", "not_here", "guidance"):
        if k in c:
            parts.append({"solver_file": "delivered (solver file)", "notes": "elsewhere (a run's notes)",
                          "not_here": "not for this kind of mesh", "guidance": "kept as guidance"}[k])
    if "not_possible" in c:
        parts.append("not applied")
    return ", ".join(parts) or "NOTHING TOLD"


def judge(entry: dict, route: str, arguments: str | None) -> dict:
    """One row: the line on one route, read from `arguments` (the model's answer kept at save time) as a run reads
    it - checked with the route, merged with no notes."""
    from meshpipeline.engines import mesh_requirements as MR
    from meshpipeline.engines import meshing_standard as MS

    text = str(entry["text"])
    expect = list((entry.get("expect") or {}).get(route) or (entry.get("expect") or {}).get("*") or [])
    if arguments is None:
        settings = MR.section("failed", source="rules (nothing saved)", note=MS.UNREAD_WORDS)
    else:
        settings = MS.checked(arguments, text, route=ROUTES[route], key=MS.key_of(text), source="kept")
    merged = MS.merge(MS.switched(settings, MS.QUADS), MR.section("none"), route=ROUTES[route])
    seen = observed(merged, settings)
    results = [(o, met(o, seen)) for o in expect]
    read = [f"{k}={v}" if v is not True else k for k, v in seen["fields"].items()]
    read += [f"strict:{k}={v}" for k, v in seen["strict"].items()]
    if seen["unit"]:
        read.append(f"unit={seen['unit']}")
    return {"id": entry["id"], "category": entry.get("category", ""), "route": route, "text": text,
            "read": read, "told": told_as(seen), "lines": seen["lines"],
            "expect": expect, "missed": [o for o, ok in results if not ok],
            "ok": bool(results) and all(ok for _o, ok in results), "answered": arguments is not None}


# #
# the reading, as the product makes it when the standard is saved
# #

async def reading(text: str, *, wait_s: float) -> tuple[str | None, float]:
    """The model's answer for these words, from the product's own save-time call; (None, seconds) when it gave none."""
    from meshpipeline.engines import meshing_standard as MS

    t0 = time.monotonic()
    got = await MS.read_at_save(text, user_id="try-meshing-standard", wait_s=wait_s)
    return (got or {}).get("arguments"), time.monotonic() - t0


async def run_bank(entries: list[dict], *, wait_s: float, out=print) -> list[dict]:
    rows: list[dict] = []
    for e in entries:
        args, took = await reading(str(e["text"]), wait_s=wait_s)
        for route in e.get("routes") or []:
            row = judge(e, route, args)
            row["seconds"] = round(took, 2)
            rows.append(row)
            first = " / ".join(str(e["text"]).splitlines())
            out(f"{'OK  ' if row['ok'] else 'MISS'} {e['id']:7} {route:5} {took:5.1f}s  {first[:70]!r}\n"
                f"       read: {', '.join(row['read']) or '-'}\n"
                f"       told: {row['told']}" + ("" if row["answered"] else "  (the model gave no reading)")
                + (f"\n       want: {', '.join(row['expect'])}; missed: {', '.join(row['missed'])}"
                   if not row["ok"] else ""))
            for w in row["lines"]:
                if not w.startswith("From your settings"):
                    out(f"         | {w}")
    return rows


def totals(rows: list[dict], out=print) -> dict:
    by_cat: dict[str, Counter] = defaultdict(Counter)
    by_route: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        for agg, key in ((by_cat, r["category"]), (by_route, r["route"])):
            agg[key]["ok" if r["ok"] else "miss"] += 1
    out("=" * 100)
    for title, agg in (("per category", by_cat), ("per route", by_route)):
        out(f"{title}:")
        for k in sorted(agg):
            out(f"  {k:16} {agg[k]['ok']:4} OK  {agg[k]['miss']:4} MISS")
    ok = sum(1 for r in rows if r["ok"])
    out(f"all: {ok}/{len(rows)} OK")
    return {"category": {k: dict(v) for k, v in by_cat.items()}, "route": {k: dict(v) for k, v in by_route.items()},
            "ok": ok, "rows": len(rows)}


# #
# the model, as the app reaches it
# #

def _configured() -> str:
    """"" when the builder's model can be called; else why not (no value printed)."""
    from meshpipeline.agents.builder import settings as bcfg
    from meshpipeline.settings import providers as provcfg
    provider = str(getattr(bcfg.BUILDER_ROUTE.primary, "provider", "") or "")
    if not provider:
        return "the builder route names no provider"
    if "deepseek" in provider.casefold():
        return "the builder route names DeepSeek, which is never used here"
    if not getattr(provcfg, f"{provider.upper()}_API_KEY", ""):
        return f"no key is set in the environment for the builder route's provider ({provider})"
    return ""


def _install() -> None:
    """The real router and an in-process capacity controller (what runtime/composition binds, minus Redis and the
    decision cache, so every call really goes to the model)."""
    from meshpipeline.adapters.model_capacity.local import LocalCapacityController
    from meshpipeline.adapters.model_inference import router as real
    from meshpipeline.contracts import model_capacity, model_inference
    model_capacity.set_capacity_controller(LocalCapacityController())
    model_inference.set_model_router(real)


def load(path: Path = BANK, *, only: str = "", category: str = "", route: str = "") -> list[dict]:
    entries = json.loads(Path(path).read_text(encoding="utf-8"))["lines"]
    if only:
        keep = {x.strip() for x in only.split(",") if x.strip()}
        entries = [e for e in entries if e["id"] in keep]
    if category:
        entries = [e for e in entries if e.get("category") == category]
    if route:
        entries = [{**e, "routes": [r for r in e["routes"] if r == route]} for e in entries if route in e["routes"]]
    return entries


def main(argv: list[str] | None = None) -> int:
    from meshpipeline.engines import meshing_standard as MS

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bank", type=Path, default=BANK)
    ap.add_argument("--only", default="", help="comma-separated line ids")
    ap.add_argument("--category", default="")
    ap.add_argument("--route", default="", choices=["", *ROUTES])
    ap.add_argument("--wait", type=float, default=MS.SAVE_WAIT_S, help="seconds per reading (the product's save wait)")
    ap.add_argument("--json", type=Path, default=None, help="also write every row here")
    args = ap.parse_args(argv)
    why = _configured()
    if why:
        print(f"No model is configured for the builder route ({why}). Nothing was called.")
        return 0
    _install()
    entries = load(args.bank, only=args.only, category=args.category, route=args.route)
    rows = asyncio.run(run_bank(entries, wait_s=args.wait))
    summary = totals(rows)
    if args.json:
        args.json.write_text(json.dumps({"rows": rows, "totals": summary}, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
