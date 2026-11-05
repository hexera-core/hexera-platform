# Responsibility: Write the Mesh report - one self-contained HTML page that holds everything the result screen leaves out: every limit with its measured number, the places set aside (cusps, micro details) with their coordinates, the plan in words, the full hot-spot list, the per-solver checks, every joined pair, the faces accounted for and the units.
# Boundaries: pure. It reads the stored viewer payload (application/viewer_payload + engines/fea_results) and returns text; it reads no workspace, measures nothing and decides no verdict. No clock, no paths: the same payload always gives the same bytes, so the Everything bundle stays deterministic.
# Collaborates with: application/artifact_uploader.py (writes it into the Everything bundle), api/v1/simulation.py (serves it as its own download), ui/js/viewer/result_panel.js (the short result screen that points here).
"""THE MESH REPORT.

The result screen is short on purpose: a verdict, four numbers, the downloads and two folded
lists. Everything else a careful engineer may want lives here, in a page that opens in any
browser with no network and prints cleanly to PDF (light page, tables that do not split a row).
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from html import escape
from typing import Any

#: The file name inside the Everything bundle, and the name the download is saved under.
REPORT_FILE = "mesh_report.html"

_OP = {"<=": "at most", ">=": "at least", "==": "exactly", "<": "under", ">": "over", "empty": "none"}
_OUTCOME = {"pass": "Passed", "concerns": "Passed with notes", "unmeasured": "Not fully checked",
            "fail": "Failed"}
_SOLVER_OUT = {"pass": "Pass", "warnings": "Warnings", "fail": "Fail", "unmeasured": "Not fully checked"}


def _m(v: Any) -> Mapping:
    return v if isinstance(v, Mapping) else {}


def _l(v: Any) -> list:
    return list(v) if isinstance(v, (list, tuple)) else []


def _e(v: Any) -> str:
    return escape(str(v), quote=True)


def _num(v: Any) -> str:
    """A figure as an engineer prints it: counts with separators, measures to 3 significant figures."""
    if v is None or isinstance(v, bool) or v == "":
        return "not measured"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        if not math.isfinite(v):
            return "not measured"
        return f"{v:,.0f}" if abs(v) >= 1e5 else f"{v:.3g}"
    return str(v)


def _limit(op: Any, limit: Any) -> str:
    if op == "empty":
        return "none"
    if isinstance(limit, (list, tuple)):
        return ", ".join(str(x) for x in limit) or "-"
    return f"{_OP.get(str(op), str(op))} {_num(limit)}".strip()


def _ok(v: Any) -> str:
    return {True: '<span class="ok">Met</span>', False: '<span class="no">Not met</span>'}.get(
        v, '<span class="na">Not measured</span>')


def _internal(text: Any) -> bool:
    """A code path or a rule file - never printed in a customer's report."""
    t = str(text or "")
    return ".py" in t or "RULES.md" in t or "engines/" in t or "cad/" in t


def _table(head: Sequence[str], rows: Sequence[Sequence[str]], cls: str = "") -> str:
    if not rows:
        return ""
    th = "".join(f"<th>{_e(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>'


def _section(title: str, body: str, sid: str) -> str:
    return f'<section id="{sid}"><h2>{_e(title)}</h2>{body}</section>' if body else ""


def _list(items: Sequence[Any]) -> str:
    items = [i for i in items if str(i or "").strip()]
    return "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>" if items else ""


def _verdict(payload: Mapping) -> tuple[str, str]:
    """(class, words) for the report's one-line verdict, from the payload's own checks."""
    surface = _m(payload.get("surface"))
    fea = _m(surface.get("fea"))
    if fea:
        outcome = str(_m(fea.get("check")).get("outcome") or "")
        return outcome or "unmeasured", _OUTCOME.get(outcome, "Checked")
    q = _m(payload.get("quality"))
    rows = [_m(r) for r in _l(q.get("criteria"))]
    if q.get("production_grade") is False or any(r.get("gating") and r.get("passed") is False for r in rows):
        return "fail", "Failed"
    if any(r.get("passed") is False for r in rows):
        return "concerns", "Passed with notes"
    if rows:
        return "pass", "Passed"
    return "unmeasured", "Checked"


def _key_numbers(payload: Mapping) -> list[tuple[str, str]]:
    surface = _m(payload.get("surface"))
    fea = _m(surface.get("fea"))
    q = _m(payload.get("quality"))
    sq = _m(surface.get("quality"))
    out: list[tuple[str, str]] = []
    cells = fea.get("elements") or q.get("cell_count") or surface.get("cell_count") or sq.get("cells")
    if cells:
        out.append(("Elements" if fea else "Cells", _num(int(cells))))
    if fea:
        rows = {str(_m(r).get("key")): _m(r) for r in _l(_m(fea.get("check")).get("rows"))}
        sj = rows.get("fea_min_scaled_jacobian")
        if sj and sj.get("measured") is not None:
            out.append(("Worst scaled Jacobian", f"{_num(sj.get('measured'))} (needs {_limit(sj.get('op'), sj.get('limit'))})"))
        tw = _m(fea.get("through_wall"))
        if tw:
            out.append(("Walls", "every wall at least 2 elements thick" if tw.get("ok")
                        else f"{len(_l(tw.get('short')))} wall(s) thinner than 2 elements" if tw.get("short")
                        else "not measured"))
    else:
        if sq.get("max_non_ortho") is not None:
            out.append(("Worst non-orthogonality", f"{_num(sq.get('max_non_ortho'))}°"))
        if sq.get("max_skewness") is not None:
            out.append(("Worst skewness", _num(sq.get("max_skewness"))))
    return out


def _units(payload: Mapping) -> str:
    # the FEA block's own sentence (engines/fea_results), else the one authority's unit (contracts/mesh_units):
    # every delivered mesh is written in it
    from meshpipeline.contracts.mesh_units import export_units_words
    fea = _m(_m(payload.get("surface")).get("fea"))
    words = str(_m(fea["units"] if "units" in fea else None).get("words") or "")
    if words:
        return words
    return export_units_words()


def _fea_limits(fea: Mapping) -> str:
    rows = []
    for r in (_m(x) for x in _l(_m(fea.get("check")).get("rows"))):
        if isinstance(r.get("measured"), list):
            continue                       # a list of places: its own section below
        rows.append([_e(r.get("label") or r.get("key")), _e(_num(r.get("measured"))),
                     _e(_limit(r.get("op"), r.get("limit"))), _ok(r.get("ok"))])
    note = _m(fea.get("check")).get("solvers")
    return _table(("Check", "Measured", "Limit", "Result"), rows, "limits") + (f"<p class=\"hint\">{_e(note)}</p>" if note else "")


def _cfd_limits(payload: Mapping) -> str:
    q = _m(payload.get("quality"))
    rows = [[_e(r.get("label") or r.get("key")), _e(_num(r.get("measured"))), _e(r.get("ok_when") or ""),
             _ok(r.get("passed")) + (" <small>(decides the run)</small>" if r.get("gating") else "")]
            for r in (_m(x) for x in _l(q.get("criteria")))]
    figures = [[_e(str(k).replace("_", " ").capitalize()), _e(_num(v))]
               for k, v in _m(_m(payload.get("surface")).get("quality")).items()
               if not isinstance(v, (dict, list))]
    return (_table(("Check", "Measured", "Limit", "Result"), rows, "limits")
            + (_table(("Mesh figure", "Value"), figures, "figures") if figures else ""))


def _places(fea: Mapping) -> str:
    """Every row whose measure is a list of places: cusps, micro details, zones short of the rules."""
    out = []
    for r in (_m(x) for x in _l(_m(fea.get("check")).get("rows"))):
        places = r.get("measured")
        if not isinstance(places, list) or not places:
            continue
        out.append(f'<div class="place"><h3>{_e(r.get("label") or r.get("key"))}</h3>'
                   f'<ol class="coords">{"".join(f"<li>{_e(p)}</li>" for p in places)}</ol></div>')
    return "".join(out)


def _through_wall(fea: Mapping) -> str:
    tw = _m(fea.get("through_wall"))
    if not tw:
        return ""
    short = [[_e(s.get("face")), _e(_num(s.get("thickness_mm"))), _e(_num(s.get("elements")))]
             for s in (_m(x) for x in _l(tw.get("short")))]
    return f"<p>{_e(tw.get('words') or '')}</p>" + _table(("Face", "Wall (mm)", "Elements through it"), short)


def _hot_spots(fea: Mapping) -> str:
    spots = [_m(x) for x in _l(fea.get("hot_spots"))]
    if not spots:
        return ""
    kinds: dict[str, int] = {}
    for h in spots:
        k = str(h.get("shape") or h.get("type") or "hot spot")
        kinds[k] = kinds.get(k, 0) + 1
    summary = _table(("Kind", "Count"), [[_e(k.capitalize()), _e(f"{n:,}")] for k, n in
                                         sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))], "kinds")

    def at(h: Mapping) -> str:
        p = h.get("at_m")
        if isinstance(p, (list, tuple)) and len(p) == 3 and all(isinstance(v, (int, float)) for v in p):
            return "(" + ", ".join(f"{float(v) * 1000:.4g}" for v in p) + ") mm"
        return "-"

    rows = [[_e(h.get("id") or ""), _e(h.get("words") or h.get("shape") or ""),
             _e(", ".join(str(f) for f in _l(h.get("faces")))), _e(at(h)),
             ('<span class="no">short of its rule</span>' if h.get("short") else "")
             + (" capped" if h.get("capped") else "")]
            for h in spots]
    return summary + _table(("Spot", "What", "Faces", "Where", "Note"), rows, "spots")


def _solvers(fea: Mapping) -> str:
    out = []
    for sv in (_m(x) for x in _l(fea.get("solvers"))):
        rows = []
        for r in (_m(x) for x in _l(sv.get("rows"))):
            op = str(r.get("op") or "")
            side = "over" if op == "<=" else "under"
            rows.append([_e(r.get("label") or r.get("key")), _e(_num(r.get("measured"))),
                         _e(f"{side} {_num(r.get('warn'))}" if r.get("warn") is not None else "-"),
                         _e(f"{side} {_num(r.get('error'))}" if r.get("error") is not None else "-"),
                         _e(_SOLVER_OUT.get(str(r.get("status")), str(r.get("status") or ""))),
                         _e("" if _internal(r.get("source")) else (r.get("source") or ""))])
        ready = "" if sv.get("file_ready") is not False else " · the file is not available"
        out.append(f'<div class="solver"><h3>{_e(sv.get("label"))} '
                   f'<span class="tag {_e(sv.get("outcome") or "")}">{_e(_SOLVER_OUT.get(str(sv.get("outcome")), ""))}</span></h3>'
                   f'<p class="hint">{_e(sv.get("file") or "")} · {_e(sv.get("element") or "")}{ready} · '
                   f'{_e(sv.get("tested_words") or "")}</p><p>{_e(sv.get("words") or "")}</p>'
                   + _table(("Check", "Measured", "Warns", "Fails", "Result", "Basis"), rows) + "</div>")
    return "".join(out)


def _joints(fea: Mapping) -> str:
    a = _m(fea.get("assembly"))
    if not a:
        return ""
    out = [f"<p>{_e(a.get('words') or '')}</p>"]
    opened = [_m(o) for o in _l(a.get("open_bonded"))]
    if opened:
        out.append('<div class="alert"><b>' + (_e("A bonded pair is open") if len(opened) == 1
                                                else _e(f"{len(opened)} bonded pairs are open"))
                   + "</b>: the solver will not join these faces." + _list([o.get("words") for o in opened]) + "</div>")
    pairs = [[_e(" and ".join(str(p) for p in _l(c.get("parts")))), _e(str(c.get("type") or "").capitalize()),
              _e(str(c.get("kind") or "")), _e("you, on the stage" if c.get("source") == "user" else "Hexera"),
              _e(c.get("words") or "")] for c in (_m(x) for x in _l(a.get("pairs")))]
    out.append(_table(("Parts", "Joined as", "How they meet", "Set by", "In words"), pairs, "pairs"))
    joints = [[_e(j.get("name") or ""), _e(str(j.get("kind") or "")), _e(j.get("words") or "")]
              for j in (_m(x) for x in _l(a.get("joints")))]
    if joints:
        out.append("<h3>Joints in the solver files</h3>" + _table(("Name", "Kind", "What it ties"), joints))
    parts = [[_e(p.get("id") or ""), _e(p.get("name") or ""), _e(_num(p.get("elements")))]
             for p in (_m(x) for x in _l(a.get("parts")))]
    if parts:
        out.append("<h3>Parts</h3>" + _table(("Part", "Name", "Elements"), parts))
    out.append(_list(_l(a.get("notes"))))
    return "".join(out)


def _faces(fea: Mapping) -> str:
    out = []
    if fea.get("faces_short"):
        out.append(f"<p>{_e(fea['faces_short'])}</p>")
    skins = [[_e(s.get("face") or ""), _e(s.get("on") or ""), _e(s.get("words") or "")]
             for s in (_m(x) for x in _l(fea.get("skins_left_out")))]
    if skins:
        out.append("<h3>Skins of no thickness, left out</h3>" + _table(("Face", "Lies on", "Why"), skins))
    merged = [[_e(m.get("face") or ""), _e(m.get("into") or ""), _e(_num(m.get("width_mm")))]
              for m in (_m(x) for x in _l(fea.get("merged_faces")))]
    if merged:
        out.append("<h3>Tiny faces meshed with a neighbour</h3>" + _table(("Face", "Meshed as part of", "Width (mm)"), merged))
    faces = [[_e(f.get("name") or ""), _e(f.get("cad_name") or ""), _e(f.get("kind") or ""), _e(_num(f.get("area_mm2")))]
             for f in (_m(x) for x in _l(fea.get("faces")))]
    if faces:
        out.append("<h3>Named faces</h3>" + _table(("Face", "CAD name", "Kind", "Area (mm²)"), faces))
    return "".join(out)


def _download(fea: Mapping) -> str:
    dl = _m(fea.get("download"))
    rows = [[_e(c.get("file") or ""), _e(c.get("words") or "")] for c in (_m(x) for x in _l(dl.get("contains")))]
    return (_table(("File", "What it is"), rows) + (f"<p class=\"hint\">{_e(dl['later'])}</p>" if dl.get("later") else ""))


_CSS = """
:root{--ink:#1d1b18;--muted:#5d5a54;--faint:#8a867e;--line:#e3e0d8;--soft:#f6f5f1;--ok:#24804f;--warn:#9a6a12;--no:#b3261e;--accent:#e8611a}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:var(--ink);font:13px/1.5 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
main{max-width:900px;margin:0 auto;padding:32px 28px 48px}
header{border-bottom:2px solid var(--accent);padding-bottom:14px;margin-bottom:8px}
.brand{font:600 11px/1 inherit;letter-spacing:.14em;color:var(--muted);text-transform:uppercase}
h1{font-size:22px;margin:8px 0 4px;font-weight:600}
.verdict{font-size:15px;font-weight:600;margin:6px 0 0}
.verdict.pass{color:var(--ok)}.verdict.concerns,.verdict.unmeasured{color:var(--warn)}.verdict.fail{color:var(--no)}
dl.keys{display:flex;flex-wrap:wrap;gap:0;margin:14px 0 0}
dl.keys div{padding:0 18px 0 0;margin:0 18px 6px 0;border-right:1px solid var(--line)}
dl.keys div:last-child{border:0}
dl.keys dt{font-size:11px;color:var(--faint)}dl.keys dd{margin:0;font-size:15px;font-weight:600}
nav{margin:14px 0 0;font-size:12px;color:var(--muted)}nav a{color:var(--muted);margin-right:12px}
section{margin-top:26px}
h2{font-size:15px;margin:0 0 8px;padding-bottom:4px;border-bottom:1px solid var(--line)}
h3{font-size:13px;margin:14px 0 6px}
table{width:100%;border-collapse:collapse;margin:6px 0 10px;font-size:12px}
th{text-align:left;font-weight:600;color:var(--muted);border-bottom:1px solid var(--line);padding:5px 8px 5px 0}
td{border-bottom:1px solid var(--line);padding:5px 8px 5px 0;vertical-align:top;font-variant-numeric:tabular-nums}
tr{break-inside:avoid;page-break-inside:avoid}
ul,ol{margin:4px 0 10px;padding-left:20px}li{margin:2px 0}
ol.coords{columns:2;column-gap:28px;font-variant-numeric:tabular-nums}
.ok{color:var(--ok)}.no{color:var(--no);font-weight:600}.na{color:var(--warn)}
.hint{color:var(--faint);font-size:12px}
.tag{font-size:11px;font-weight:600;border:1px solid var(--line);border-radius:10px;padding:1px 7px;margin-left:6px;color:var(--muted)}
.tag.pass{color:var(--ok)}.tag.warnings,.tag.unmeasured{color:var(--warn)}.tag.fail{color:var(--no)}
.alert{border:1px solid var(--no);background:#fdf3f2;border-radius:6px;padding:8px 12px;margin:8px 0;color:var(--no)}
.alert ul{color:var(--ink)}
.units{background:var(--soft);border-radius:6px;padding:8px 12px}
footer{margin-top:32px;color:var(--faint);font-size:11px;border-top:1px solid var(--line);padding-top:10px}
@media print{main{padding:0;max-width:none}nav{display:none}section{break-inside:auto}
  h2,h3{break-after:avoid;page-break-after:avoid}@page{margin:16mm 14mm}}
"""


def mesh_report_html(payload: Mapping[str, Any] | None) -> str:
    """The Mesh report for one delivered mesh, from its stored viewer payload. Pure."""
    p = _m(payload)
    surface = _m(p.get("surface"))
    fea = _m(surface.get("fea"))
    thermal = _m(surface.get("thermal"))
    cls, said = _verdict(p)
    title = "FEA mesh" if fea else ("Thermal mesh" if thermal else "CFD mesh")
    keys = "".join(f"<div><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>" for k, v in _key_numbers(p))
    summary = str(fea.get("summary") or thermal.get("summary") or "")
    sections = [
        ("units", "Units", f'<p class="units">{_e(_units(p))}</p>'),
        ("limits", "Every limit", _fea_limits(fea) if fea else _cfd_limits(p)),
        ("places", "Places set aside, with coordinates", _places(fea) if fea else ""),
        ("walls", "Through the walls", _through_wall(fea) if fea else ""),
        ("joints", "Parts and joints", _joints(fea) if fea else ""),
        ("plan", "The plan, in words", _list(_l(fea.get("plan_words"))) if fea else ""),
        ("hot-spots", "Hot spots", _hot_spots(fea) if fea else ""),
        ("solvers", "Per solver", _solvers(fea) if fea else ""),
        ("faces", "Faces", _faces(fea) if fea else ""),
        ("files", "What the download holds", _download(fea) if fea else ""),
    ]
    body = "".join(_section(t, b, sid) for sid, t, b in sections)
    nav = "".join(f'<a href="#{sid}">{_e(t)}</a>' for sid, t, b in sections if b)
    return ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>Mesh report · {_e(title)}</title><style>{_CSS}</style></head><body><main>"
            f"<header><div class=\"brand\">Hexera · Mesh report</div><h1>{_e(title)}</h1>"
            f"<p class=\"verdict {_e(cls)}\">{_e(said)}</p>"
            + (f"<p>{_e(summary)}</p>" if summary else "")
            + (f"<dl class=\"keys\">{keys}</dl>" if keys else "")
            + (f"<nav>{nav}</nav>" if nav else "") + "</header>"
            + body
            + "<footer>Made by Hexera from the delivered mesh. Every number here was measured on the files "
              "in your download.</footer></main></body></html>\n")


__all__ = ["REPORT_FILE", "mesh_report_html"]
