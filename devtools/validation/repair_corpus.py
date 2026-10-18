#!/usr/bin/env python3
# Responsibility: Run the whole repair loop over a directory of real CAD files and report what it did to each.
# Owns: the run, the per-file record, and the summary. It asserts nothing and gates nothing.
# Boundaries: it reads CAD and writes repairs into a scratch directory. It touches no database, no
#             object store and no job; this is the pipeline's repair path exercised directly.

# WHY THIS EXISTS.
#
# The tests prove the repair behaves correctly on geometry authored to provoke one defect each.
# They cannot tell you what it does to a real part, because every fixture is a part somebody wrote
# to make a point. This runs inspect -> locate -> triage -> repair -> promote-check -> re-inspect
# over whatever files you point it at and prints the outcome for each, so the answer to "what does
# it actually repair" is evidence rather than an argument.
#
# WHAT A ROW MEANS. `before` is what the inspection located in the file as delivered; `after` is
# what the inspection locates in the file the repair produced. A repair is only credited when the
# second is emptier than the first, measured by re-reading the written file - never by the repair
# reporting its own success.
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

# The switches this tool is here to exercise. Set before the settings module is imported, because
# these are read once at import.
os.environ.setdefault("CAD_REPAIR_ENABLED", "true")
os.environ.setdefault("CAD_REPAIR_AUTONOMOUS", "true")

# Imported at the top so that a broken import fails before any output, not after the summaries and
# before the --json write. It must follow the environment defaults above: settings read them once.
from meshpipeline.cad.repair.operations import OPERATIONS  # noqa: E402

_SUFFIXES = {".step", ".stp"}


def _inspect(path: Path) -> dict:
    from meshpipeline.cad.repair.brep import inspect_brep_file

    report = inspect_brep_file(path)
    measured = {m.name: m.value for m in report.measurements}
    return {
        "report": report,
        "defects": [d.code.value for d in report.defects],
        "entities": [dict(e) for e in report.entities],
        "valid": measured.get("is_valid"),
        "counts": {k: measured.get(k) for k in
                   ("solids", "shells", "faces", "edges", "vertices")},
        "located": measured.get("located", {}),
    }


# THE LLM PLANNER INSTRUMENT. Everything from here to `_one` is the planner side of this tool:
# a reply cache wrapped AROUND the model seam (llm_plan is untouched), and the per-file pre-pass.
# This tool owns the async edge, so `asyncio.run` is called here, once per file.
def _supported_keys() -> tuple[str, ...]:
    """The credential names the routing layer actually reads - one table, so this tool cannot
    disagree with the router about which providers exist."""
    from meshpipeline.settings.providers import LLM_PROVIDER_KEY_ENV
    return tuple(LLM_PROVIDER_KEY_ENV.values())


_PROVIDER_KEYS = _supported_keys()
PLANNER_SOURCE = "llm-planner"


def prompt_key(messages) -> str:
    """sha256 over the FULL prompt: every message's role and content, in order.

    The planner prompt carries the catalogue text, the defects and the story, so a change to any of
    them changes the key; a stale reply can never answer a different question.
    """
    canon = json.dumps([[str(m.get("role", "")), str(m.get("content", ""))] for m in messages],
                       ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _usable(text: str) -> bool:
    from meshpipeline.agents.repair_review._wire import parse_object
    reply = parse_object(text or "")
    return reply is not None and isinstance(reply.get("proposals"), list)


class CacheError(Exception):
    """The reply cache file cannot be used; the message names the path."""


class CachingRouter:
    """A model router that answers from a JSON file of recorded replies before asking `inner`.

    Wraps the seam (`call_repair_review_model`); a hit makes no call, a miss delegates to `inner`
    and records the raw reply. With no `inner`, a miss is a failed call (replay-only).
    """

    def __init__(self, path: Path | None, inner=None):
        self.path, self.inner = (Path(path) if path else None), inner
        self.hits = self.misses = 0
        self.table: dict[str, str] = {}
        if self.path is not None and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text() or "{}")
                if not isinstance(loaded, dict):
                    raise ValueError("not a JSON object")
                self.table = {str(k): str(v) for k, v in loaded.items()}
            except (ValueError, OSError) as exc:
                raise CacheError(f"reply cache {self.path} is unreadable: {exc}") from exc

    async def call_repair_review_model(self, messages, job_id="", name="RepairReview"):
        key = prompt_key(messages)
        if key in self.table:
            self.hits += 1
            return SimpleNamespace(ok=True, assistant_text=self.table[key])
        self.misses += 1
        if self.inner is None:
            return SimpleNamespace(ok=False, assistant_text="")
        result = await self.inner.call_repair_review_model(messages, job_id=job_id, name=name)
        text = getattr(result, "assistant_text", "") or ""
        # ONLY A USABLE REPLY IS RECORDED. The planner retries an unusable one with the identical
        # prompt, so caching it would answer the retry with the same bad reply - in memory even
        # without --reply-cache. Usable is what the planner itself reads: a JSON object carrying
        # a `proposals` list.
        if getattr(result, "ok", True) and _usable(text):
            self.table[key] = text
            if self.path is not None:
                # Not atomic on purpose: this is a scratch cache; a torn write is reported by
                # CacheError on the next load and the file is simply deleted.
                self.path.write_text(json.dumps(self.table, indent=2, sort_keys=True))
        return result

    def __getattr__(self, name):  # every other seam method goes straight to the real router
        if self.inner is None:
            raise AttributeError(name)
        return getattr(self.inner, name)


def install_planner_router(cache_path: str = "") -> tuple[CachingRouter | None, str]:
    """Install the model router the planner will use; return (cache, honest note).

    No secrets are fetched: a real model is used only when a provider key is already in the
    environment. Without one the planner has no model (or replays the cache alone when
    --reply-cache names a populated one), and the note says so.
    """
    from meshpipeline.contracts import model_inference

    inner = None
    if any(os.environ.get(k) for k in _PROVIDER_KEYS):
        from meshpipeline.adapters.model_inference import router as real
        inner = real
    cache = CachingRouter(Path(cache_path) if cache_path else None, inner)
    if inner is None and not cache.table:
        return None, ("no provider key in the environment and no replies cached: the planner has "
                      "no model, so every file reports no proposals")
    model_inference.set_model_router(cache)
    note = "model: real router" if inner is not None else "model: cache replay only"
    return cache, note


def plan_for(entities: list[dict], *, job_id: str = "corpus"):
    """The LLM planner's plan for one file's located defects (the async edge lives here)."""
    from meshpipeline.agents.repair_review import _wire
    from meshpipeline.agents.repair_review.planner import llm_plan

    story = _wire.story_of([{"defect_code": e.get("code")} for e in entities])
    return asyncio.run(llm_plan({"entities": entities}, story=story, job_id=job_id))


def _plan_row(plan) -> dict:
    return {
        "proposals": [{"op": d["op"], "targets": list(d["targets"]),
                       "defect_code": d["defect_code"], "confidence": d["planner_confidence"],
                       "directive_id": d["directive_id"]} for d in plan.directives],
        "declines": len(plan.declines), "call_failures": len(plan.call_failures),
        "called": plan.called,
    }


def agreement(proposals: list[dict], operations: list[dict]) -> bool | None:
    """Model's FIRST proposal == the rules planner's first attempted op and target; None if no
    proposal or no attempt exists to compare."""
    if not proposals or not operations:
        return None
    a, b = proposals[0], operations[0]
    return a["op"] == b.get("op") and sorted(a["targets"]) == sorted(b.get("targets") or [])


def planner_summary(rows: list[dict], mode: str) -> dict:
    """Aggregate planner evidence from the rows (pure; the printer and the tests share it)."""
    out: dict = {"mode": mode}
    planned = [r for r in rows if r.get("plan") is not None]
    if mode == "shadow":
        checked = [agreement(r["plan"]["proposals"], r.get("operations") or []) for r in planned]
        checked = [c for c in checked if c is not None]
        out.update(files_planned=len(planned),
                   coverage=sum(1 for r in planned if r["plan"]["proposals"]),
                   agreed=sum(1 for c in checked if c), compared=len(checked),
                   declines=sum(r["plan"]["declines"] for r in planned),
                   call_failures=sum(r["plan"]["call_failures"] for r in planned))
    if mode == "live":
        def fixed(rs):
            total: dict[str, int] = {}
            for r in rs:
                for code, n in (r.get("repaired_defects") or {}).items():
                    total[code] = total.get(code, 0) + n
            return total
        rules = [r["rules_only"] for r in rows if r.get("rules_only")]
        out.update(rules_only_repaired=fixed(rules), live_repaired=fixed(rows),
                   rules_only_total=sum(fixed(rules).values()),
                   live_total=sum(fixed(rows).values()))
        per_op: dict[str, dict[str, int]] = {}
        for r in rows:
            for op in r.get("operations") or []:
                if op.get("source") != PLANNER_SOURCE:
                    continue
                e = per_op.setdefault(op.get("op", "?"), {"kept": 0, "rolled_back": 0,
                                                          "declined": 0, "failed": 0})
                key = op.get("status", "failed")
                if key == "rolled_back" and str(op.get("reason", "")).startswith("declined:"):
                    key = "declined"
                if key == "kept" and r.get("result_discarded"):
                    key = "rolled_back"   # the after-check threw the whole result away
                if key not in e:
                    key = "failed"
                e[key] += 1
        out["llm_ops"] = per_op
    return out


def _operation_rows(operations) -> list[dict]:
    return [{"op": op.get("name", ""), "status": op.get("status", ""),
             "reason": op.get("reason", ""), "basis": op.get("basis", ""),
             "defect_code": op.get("defect_code", ""),
             "targets": op.get("targets", []),
             **({"source": op["source"]} if op.get("source") else {})}
            for op in operations if isinstance(op, dict)]


def _one(path: Path, scratch: Path, *, mode: str = "off") -> dict:
    from meshpipeline.cad.repair.conservative import (
        RepairRefused,
        RepairUnavailable,
        repair_step_file,
    )
    from meshpipeline.cad.repair.contracts import RepairStatus, status_from_defects
    from meshpipeline.cad.repair.triage import ROUTE_CONSERVATIVE_REPAIR, recommend

    row: dict = {"file": path.name, "size_bytes": path.stat().st_size}
    started = time.monotonic()
    try:
        before = _inspect(path)
    except Exception as exc:  # noqa: BLE001 - a file the kernel will not read is a real outcome
        row.update(outcome="unreadable", error=f"{type(exc).__name__}: {exc}")
        return row

    status = status_from_defects(before["report"].defects)
    row.update(
        valid_before=before["valid"],
        counts_before=before["counts"],
        defects_before=sorted(set(before["defects"])),
        located_before=before["located"].get("by_code", {}),
        regions_before=before["located"].get("by_region", {}),
        status=status.value,
    )

    advice = recommend(repair_status=status.value, report=before["report"].to_dict(),
                       target_engine="gmsh")
    row.update(route=advice.route, confidence=advice.confidence,
               targets=[{"entity": t["entity"], "code": t["code"], "region": t["region"]}
                        for t in advice.targets],
               abstain_reason=advice.abstain_reason)

    if status is RepairStatus.clean and not before["entities"]:
        row.update(outcome="nothing_to_repair", seconds=round(time.monotonic() - started, 2))
        return row
    if advice.route != ROUTE_CONSERVATIVE_REPAIR:
        row.update(outcome=f"not_attempted:{advice.route}",
                   seconds=round(time.monotonic() - started, 2))
        return row

    destination = scratch / f"{path.stem}-repaired.step"
    extra: dict = {}
    if mode in ("shadow", "live"):
        plan = plan_for(before["entities"], job_id=path.stem)
        row["plan"] = _plan_row(plan)
        if mode == "live" and plan.directives:  # shadow hands the kernel nothing
            extra["directives"] = tuple(plan.directives)
    try:
        repaired = repair_step_file(path, destination, **extra)
    except RepairUnavailable as exc:
        row.update(outcome="unavailable", error=str(exc))
        return row
    except RepairRefused as exc:
        # THE ATTEMPTS GO WITH THE REFUSAL (conservative.py puts them in its measurements), so a
        # model operation that rolled back and left nothing kept is still counted. A result the
        # after-check discarded kept nothing either: its `kept` operations count as rolled back.
        measured = dict(getattr(exc, "measurements", None) or {})
        row.update(outcome="refused", error=str(exc),
                   operations=_operation_rows(measured.get("operations") or ()),
                   result_discarded=bool(measured.get("result_discarded")),
                   seconds=round(time.monotonic() - started, 2))
        return row
    except Exception as exc:  # noqa: BLE001
        row.update(outcome="repair_failed", error=f"{type(exc).__name__}: {exc}")
        return row

    measured = {m.name: m.value for m in repaired.measurements}
    row.update(
        # EVERY ATTEMPT, KEPT OR NOT. The rolled-back ones are the interesting half: they are
        # where the rules planner ran out of ideas, which is exactly what a model later has to
        # beat, and a corpus run that only listed successes could not show it.
        operations=_operation_rows(repaired.operations),
        operations_attempted=int(measured.get("operations_attempted", 0) or 0),
        operations_kept=int(measured.get("operations_kept", 0) or 0),
        # FINISHED OR OUT OF ROOM. The single most useful number from a corpus run: a corpus full
        # of `op_budget` says raise the budget, one full of `stalled` says the catalogue is missing
        # an operation, and one full of `no_candidate` says the planner cannot aim at what real
        # files actually carry.
        stopped_because=str(measured.get("stopped_because", "")),
        resolved=bool(measured.get("resolved", False)),
        complete_scan=bool(measured.get("complete_scan", True)),
        planner=str(measured.get("planner", "")),
        refusals=list(measured.get("refusals", []) or []),
        declared_area_mm2=float(measured.get("declared_area_mm2", 0.0) or 0.0),
    )

    after = _inspect(destination)
    row.update(valid_after=after["valid"], counts_after=after["counts"],
               defects_after=sorted(set(after["defects"])),
               located_after=after["located"].get("by_code", {}))

    # THE CREDIT IS GIVEN BY THE RE-INSPECTION, not by the repair. A repair that reports success
    # over a file whose defects are still there has not repaired anything.
    fixed = {code: count for code, count in row["located_before"].items()
             if count > row["located_after"].get(code, 0)}
    row["repaired_defects"] = fixed
    row["outcome"] = ("repaired" if fixed else
                      "no_change" if row["located_after"] == row["located_before"] else
                      "changed_without_fixing")
    row["seconds"] = round(time.monotonic() - started, 2)
    return row


def _print_planner(summary: dict, cache: CachingRouter | None) -> None:
    print(f"\n== llm planner ({summary['mode']}) ==")
    if cache is None:
        print("  no model was available: the planner proposed nothing (declared off)")
    else:
        print(f"  reply cache: {cache.hits} hit(s), {cache.misses} miss(es)")
    if summary["mode"] == "shadow":
        print(f"  coverage: {summary['coverage']}/{summary['files_planned']} file(s) with a proposal")
        print(f"  agreement with the rules planner (first op+target): "
              f"{summary['agreed']}/{summary['compared']}")
        print(f"  declines={summary['declines']}  call_failures={summary['call_failures']}")
    else:
        print(f"  defects repaired, rules-only: {summary['rules_only_total']} "
              f"{summary['rules_only_repaired']}")
        print(f"  defects repaired, live:       {summary['live_total']} "
              f"{summary['live_repaired']}")
        for name, c in sorted(summary["llm_ops"].items()):
            print(f"  llm-planner {name:<20} kept={c['kept']:<4} "
                  f"rolled_back={c['rolled_back']:<4} declined={c['declined']:<4} "
                  f"failed={c['failed']}")
        if not summary["llm_ops"]:
            print("  no llm-planner directive was attempted")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+", help="directories holding .step/.stp files")
    parser.add_argument("--limit", type=int, default=0, help="stop after N files")
    parser.add_argument("--loop", action="store_true",
                        help="print each file's operation sequence, not just the summary")
    parser.add_argument("--planner", choices=("off", "shadow", "live"), default="off",
                        help="LLM planner: shadow records proposals only; live also runs a "
                             "rules-only pass first so the two can be compared")
    parser.add_argument("--reply-cache", default="",
                        help="JSON file of recorded planner replies keyed by prompt hash")
    parser.add_argument("--json", dest="json_out", default="", help="write the rows here")
    args = parser.parse_args()

    files: list[Path] = []
    for directory in args.directories:
        for path in sorted(Path(directory).rglob("*")):
            if path.suffix.lower() in _SUFFIXES and path.is_file():
                files.append(path)
    if args.limit:
        files = files[: args.limit]
    if not files:
        print("no .step/.stp files found", file=sys.stderr)
        return 2

    import meshpipeline.settings.cad_repair as rcfg
    print(f"CAD_REPAIR_ENABLED={rcfg.CAD_REPAIR_ENABLED}  "
          f"AUTONOMOUS={rcfg.CAD_REPAIR_AUTONOMOUS}  "
          f"FILL_PLANAR_HOLES={rcfg.CAD_REPAIR_FILL_PLANAR_HOLES}  "
          f"max_deviation={rcfg.CAD_REPAIR_MAX_DEVIATION_RATIO}  "
          f"max_tolerance_mm={rcfg.CAD_REPAIR_MAX_TOLERANCE_MM}")
    print(f"{len(files)} file(s)\n")

    # A real directory rather than a per-run temp one: the repaired files ARE the evidence, and
    # a reviewer wants to open them after the run rather than watch them be deleted.
    scratch = Path(tempfile.gettempdir()) / "repair_corpus_out"
    scratch.mkdir(parents=True, exist_ok=True)
    print(f"repaired files are written to {scratch}")

    cache = None
    if args.planner != "off":
        try:
            cache, note = install_planner_router(args.reply_cache)
        except CacheError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"planner={args.planner}  {note}")

    rows = []
    for path in files:
        if args.planner == "live":
            # rules-only FIRST, into its own directory, so the live pass cannot overwrite it
            rules_dir = scratch / "rules_only"
            rules_dir.mkdir(parents=True, exist_ok=True)
            rules_row = _one(path, rules_dir)
            row = _one(path, scratch, mode="live")
            row["rules_only"] = rules_row
        else:
            row = _one(path, scratch, mode=args.planner)
        rows.append(row)
        print(f"{row['outcome']:<28} {row['file'][:52]:<54} "
              f"before={row.get('defects_before', [])} "
              f"located={row.get('located_before', {})} "
              f"-> fixed={row.get('repaired_defects', {})}"
              + (f" [{row['error'][:90]}]" if row.get("error") else ""))
        if args.loop:
            for op in row.get("operations", []) or []:
                mark = "+" if op.get("status") == "kept" else "-"
                detail = f" ({op['reason']})" if op.get("reason") else ""
                print(f"      {mark} {op.get('op', '?'):<20} {op.get('defect_code', ''):<22}"
                      f"{op.get('status', '')}{detail}")
            for record in row.get("refusals") or []:
                print(f"      ! refused {record.get('reason', '?')}: "
                      f"{record.get('detail', '')}")
            if row.get("stopped_because"):
                print(f"      = stopped: {row['stopped_because']} "
                      f"(resolved={row.get('resolved')})")

    print("\n== summary ==")
    outcomes: dict[str, int] = {}
    for row in rows:
        key = row["outcome"].split(":")[0] if row["outcome"].startswith("not_attempted") \
            else row["outcome"]
        outcomes[key] = outcomes.get(key, 0) + 1
    for key, count in sorted(outcomes.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {key}")

    total_fixed: dict[str, int] = {}
    for row in rows:
        for code, count in (row.get("repaired_defects") or {}).items():
            total_fixed[code] = total_fixed.get(code, 0) + count
    if total_fixed:
        print("\n== defects actually repaired, by kind (credited by re-inspection) ==")
        for code, count in sorted(total_fixed.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>4}  {code}")
    else:
        print("\n  no defect was repaired in this corpus")

    routes: dict[str, int] = {}
    for row in rows:
        if row.get("route"):
            routes[row["route"]] = routes.get(row["route"], 0) + 1
    print("\n== what triage advised ==")
    for route, count in sorted(routes.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {route}")

    print("\n== why the loop stopped ==")
    stops: dict[str, int] = {}
    for row in rows:
        if row.get("stopped_because"):
            stops[row["stopped_because"]] = stops.get(row["stopped_because"], 0) + 1
    for reason, count in sorted(stops.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {reason}")
    if not stops:
        print("  no file reached the loop")

    print("\n== operations, kept vs rolled back ==")
    # THE PLANNER'S HIT RATE, PER OPERATION. An operation proposed often and kept rarely is
    # either aimed at the wrong defect or missing from the catalogue in its useful form - and it
    # is the baseline a model planner gets scored against.
    # A DECLINE IS NOT A FAILURE. An operation that said no, with a reason, is recorded as
    # rolled_back by the session; the `declined:` prefix is what tells the two apart here, so
    # `rolled_back` below counts only operations that ran and did not repair their target.
    tally: dict[str, dict[str, int]] = {}
    for row in rows:
        for op in row.get("operations", []) or []:
            entry = tally.setdefault(op.get("op", "?"),
                                     {"kept": 0, "rolled_back": 0, "declined": 0, "failed": 0})
            key = op.get("status", "failed")
            if key == "rolled_back" and str(op.get("reason", "")).startswith("declined:"):
                key = "declined"
            entry[key] = entry.get(key, 0) + 1
    for name, counts in sorted(tally.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"  {name:<22} kept={counts['kept']:<4} rolled_back={counts['rolled_back']:<4} "
              f"declined={counts['declined']:<4} failed={counts['failed']}")
    if not tally:
        print("  nothing was attempted")

    print("\n== what the planner refused to attempt ==")
    refused = [r for row in rows for r in (row.get("refusals") or [])]
    by_reason: dict[str, int] = {}
    for record in refused:
        key = str(record.get("reason", "?"))
        by_reason[key] = by_reason.get(key, 0) + 1
    for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {reason}")
    if not by_reason:
        print("  no refusals")

    print("\n== operations never exercised on this corpus ==")
    # Names the catalogue holds that no file gave the loop a reason to try. "Exercised" means the
    # operation actually ran: one that was proposed but only ever declined never touched a shape,
    # so it is listed separately below rather than counted as exercised.
    never = [op.name for op in OPERATIONS if op.name not in tally]
    for name in never:
        print(f"        {name}")
    if not never:
        print("  every operation was attempted at least once")
    only_declined = [name for name, c in tally.items()
                     if c["declined"] and not (c["kept"] or c["rolled_back"] or c["failed"])]
    if only_declined:
        print("  proposed but only ever declined (never ran):")
        for name in only_declined:
            print(f"        {name}")

    if args.planner != "off":
        _print_planner(planner_summary(rows, args.planner), cache)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rows, indent=2, default=str))
        print(f"\nrows written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
