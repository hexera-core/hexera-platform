# Responsibility: Render the REPAIR REPORT as one self-contained HTML page (CRS v5 beta, Decision 2):
#                 the key numbers, then every problem -> what we did -> result, and what was left.
# Boundaries: a pure function of the job's `repair` section (pipeline/repair_result.section). It
#             reads no storage and says nothing the section does not; every string is escaped and
#             every free string has already passed contracts/repair_plain's guard.
# Collaborates with: api/v1/simulation.py (`/repair/report.html`), render/mesh_report.py (its mesh
#                    twin, whose plain style this follows).
from __future__ import annotations

import html
from collections.abc import Mapping

from meshpipeline.contracts.repair_plain import STATUS_WORDS, leaks, type_words
from meshpipeline.pipeline.repair_result import _sub

_CHOICE_WORDS = {"meshing": "Repair + mesh", "standalone": "Repair CAD only", "off": "Mesh only"}
_OUTCOME_WORDS = {"repaired": "Repaired", "partly_repaired": "Partly repaired",
                  "unchanged": "Unchanged", "unrepairable": "Could not be repaired"}

_STYLE = ("body{font:14px/1.5 system-ui,sans-serif;max-width:900px;margin:2em auto;color:#1d2430}"
          "h1{font-size:22px}h2{font-size:16px;margin-top:2em}table{border-collapse:collapse;"
          "width:100%}td,th{border-bottom:1px solid #e3e6eb;padding:6px 8px;text-align:left;"
          "vertical-align:top}th{color:#5b6472;font-weight:600}.fixed{color:#137333}"
          ".not_fixed,.left_as_is{color:#a05a00}.muted{color:#5b6472}")


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _mm(value: object) -> str:
    try:
        return f"{float(value):.3g} mm"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "not measured"


def _pct(ratio: object) -> str:
    try:
        return f"{(float(ratio) - 1.0) * 100:+.2f}%"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "not measured"


def key_rows(section: Mapping) -> list[tuple[str, str]]:
    counts = _sub(section, "counts")
    rows = [("What you asked for", _CHOICE_WORDS.get(str(section.get("choice")), "Repair + mesh")),
            ("Result", _OUTCOME_WORDS.get(str(section.get("outcome")), "Finished"))]
    if counts:
        rows.append(("Problems found", str(counts.get("found", 0))))
        rows.append(("Problems fixed", str(counts.get("fixed", 0))))
    if section.get("meshers_total"):
        rows.append(("Meshers passing", f"{section.get('meshers_passing') or 0} of "
                                         f"{section.get('meshers_total')}"))
    if section.get("max_change_mm") is not None:
        rows.append(("Largest change", f"{_mm(section.get('max_change_mm'))} (allowed "
                                       f"{_mm(section.get('allowed_change_mm'))})"))
    if section.get("volume_ratio") is not None:
        rows.append(("Volume change", _pct(section.get("volume_ratio"))))
    if section.get("size_ratio") is not None:
        rows.append(("Size change", _pct(section.get("size_ratio"))))
    if section.get("seconds") is not None:
        rows.append(("Time", f"{float(section['seconds']):.0f} s"))
    if section.get("waiting_for_review"):
        rows.append(("Review", "Waiting for your approval"))
    elif section.get("auto_accepted"):
        rows.append(("Review", "Accepted automatically (review skipped for this run)"))
    elif section.get("accepted_by_person"):
        rows.append(("Review", "Approved by you"))
    elif section.get("needs_review") and not section.get("applied", True):
        rows.append(("Review", "Not applied: it needed your approval, and review isn't "
                               "available here"))
    return rows


def repair_report_html(section: Mapping, *, job_id: str = "") -> str:
    """The page. Only plain words: a string that fails the guard is replaced, never shown."""
    rows = "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in key_rows(section))
    trace = []
    for p in section.get("problems") or ():
        if not isinstance(p, Mapping):
            continue
        status = str(p.get("status") or "not_fixed")
        said = str(p.get("reason_text") or "")
        said = said if said and not leaks(said) else STATUS_WORDS.get(status, "")
        trace.append(f"<tr><td>{_e(type_words(p.get('type')).capitalize())}</td>"
                     f"<td>{_e(said)}</td>"
                     f"<td class=\"{_e(status)}\">{_e(STATUS_WORDS.get(status, ''))}</td>"
                     f"<td>{_e(_mm(p.get('change_mm')) if p.get('change_mm') is not None else '')}"
                     f"</td></tr>")
    flags = [f for f in section.get("flags") or () if isinstance(f, str) and not leaks(f)]
    parts = [f"<!doctype html><html><head><meta charset=\"utf-8\"><title>Repair report</title>"
             f"<style>{_STYLE}</style></head><body><h1>Repair report</h1>"]
    if job_id:
        parts.append(f"<p class=\"muted\">Job {_e(job_id)}</p>")
    parts.append(f"<h2>Key numbers</h2><table>{rows}</table>")
    if trace:
        parts.append("<h2>What we found and what we did</h2><table><tr><th>Problem</th>"
                     "<th>What we did</th><th>Result</th><th>Change</th></tr>"
                     + "".join(trace) + "</table>")
    if flags:
        parts.append("<h2>Notes</h2><ul>" + "".join(f"<li>{_e(f)}</li>" for f in flags) + "</ul>")
    parts.append("</body></html>")
    return "".join(parts)


__all__ = ["key_rows", "repair_report_html"]
