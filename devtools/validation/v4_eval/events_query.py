#!/usr/bin/env python3
# Responsibility: Answer "what exactly failed, where, and why" from CRS v5's streamed event logs
#                 (`repair_v5/events.py`, one JSON line per event, `events.jsonl` per unit on the VMs).
# Owns: loading events from a local directory or a gs:// prefix (cached locally), and the standard questions:
#       failures by phase/class/op/reason, one file's timeline, wall time per phase, top exception types with example
#       tracebacks, mesher failures by member and kind, and files that never ended (killed: their last heartbeat).
# Boundaries: read-only; plain Python (DuckDB only for the optional `sql` command). No engine imports.
"""Query CRS v5 event logs.

    python devtools/validation/v4_eval/events_query.py SRC summary
    python devtools/validation/v4_eval/events_query.py SRC failures --by phase,defect_class,op,reason_key
    python devtools/validation/v4_eval/events_query.py SRC timeline --file <sha-prefix|name-substring>
    python devtools/validation/v4_eval/events_query.py SRC walltime
    python devtools/validation/v4_eval/events_query.py SRC exceptions --top 10
    python devtools/validation/v4_eval/events_query.py SRC mesher
    python devtools/validation/v4_eval/events_query.py SRC killed
    python devtools/validation/v4_eval/events_query.py SRC sql "select event, count(*) from ev group by 1"

SRC is a directory (every `*events*.jsonl` under it), one .jsonl file, or a gs:// prefix (copied with `gcloud storage`
into ~/.cache/crs-v5-events/<prefix> first; `--refresh` copies again).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

CACHE = Path(os.environ.get("CRS_V5_EVENTS_CACHE", Path.home() / ".cache" / "crs-v5-events"))
BAD = ("WARNING", "ERROR", "CRITICAL")
#: a heat's results that are not a failure
HEAT_OK = ("survived", "won")


# --------------------------------------------------------------------------- loading


def _fetch_gcs(prefix: str, refresh: bool) -> Path:
    dest = CACHE / prefix.removeprefix("gs://").rstrip("/")
    if dest.is_dir() and any(dest.rglob("*.jsonl")) and not refresh:
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    listed = subprocess.run(["gcloud", "storage", "ls", "-r", prefix.rstrip("/") + "/**"], capture_output=True,
                            text=True, check=False)
    urls = [u for u in listed.stdout.split() if u.endswith(".jsonl") and "events" in u.rsplit("/", 1)[-1]]
    if not urls:
        sys.exit(f"no *events*.jsonl under {prefix} ({listed.stderr.strip()[-300:]})")
    base = prefix.rstrip("/") + "/"
    for url in urls:
        local = dest / url.removeprefix(base)
        local.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["gcloud", "storage", "cp", "-q", url, str(local)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return dest


def files_of(src: str, refresh: bool = False) -> list[Path]:
    root = _fetch_gcs(src, refresh) if src.startswith("gs://") else Path(src)
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*.jsonl") if "events" in p.name)


def load(paths: Iterable[Path]) -> list[dict]:
    """Every event, in time order. A torn last line (a VM killed mid-write) is skipped, never fatal."""
    out: list[dict] = []
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(e, dict) and "event" in e:
                    out.append(e)
    out.sort(key=lambda e: (str(e.get("ts", "")), e.get("pid", 0), e.get("seq", 0)))
    return out


# --------------------------------------------------------------------------- helpers


def _short(v: Any, n: int = 90) -> str:
    s = "" if v is None else str(v).replace("\n", " | ")
    return s if len(s) <= n else s[: n - 3] + "..."


def _table(rows: Sequence[Sequence[Any]], head: Sequence[str]) -> str:
    rows = [[_short(c, 70) for c in r] for r in rows]
    w = [max(len(str(h)), *(len(r[i]) for r in rows)) if rows else len(str(h)) for i, h in enumerate(head)]
    line = lambda r: "  ".join(str(c).ljust(w[i]) for i, c in enumerate(r))  # noqa: E731
    return "\n".join([line(head), line(["-" * x for x in w]), *(line(r) for r in rows)])


def _file_key(e: dict) -> tuple:
    return (e.get("run_id"), e.get("vm"), e.get("shard"), e.get("file_sha256"), e.get("arm"))


def is_failure(e: dict) -> bool:
    if e.get("event") == "heat":
        return e.get("result") not in HEAT_OK
    if e.get("event") in ("heartbeat", "phase", "process_start", "process_end", "file_start"):
        return False
    return e.get("severity") in BAD


def reason_of(e: dict) -> str:
    return str(e.get("reason_key") or e.get("error_kind") or e.get("exc_type") or e.get("status")
               or e.get("result") or "")


# --------------------------------------------------------------------------- the questions


def failures(ev: list[dict], by: Sequence[str]) -> str:
    c: collections.Counter = collections.Counter()
    example: dict[tuple, dict] = {}
    for e in ev:
        if not is_failure(e):
            continue
        key = tuple(reason_of(e) if k == "reason_key" else e.get(k) for k in by)
        key = (e["event"], *key)
        c[key] += 1
        example.setdefault(key, e)
    rows = [[n, *k, _short(example[k].get("reason") or example[k].get("error") or example[k].get("exc_message")
                          or example[k].get("message"), 80)] for k, n in c.most_common()]
    return _table(rows, ["n", "event", *by, "example"]) if rows else "no failures"


def timeline(ev: list[dict], which: str, include_heartbeat: bool = True) -> str:
    mine = [e for e in ev if str(e.get("file_sha256") or "").startswith(which)
            or (which and which in str(e.get("file_name") or ""))]
    if not mine:
        return f"no events for {which!r}"
    rows = []
    for e in mine:
        if e["event"] == "heartbeat" and not include_heartbeat:
            continue
        what = (e.get("result") or e.get("status") or e.get("exc_type") or "")
        why = (e.get("reason") or e.get("error") or e.get("exc_message") or e.get("line") or e.get("message") or "")
        if e["event"] == "heartbeat":
            why = f"rss {e.get('rss_mb')} MB, children {e.get('children_rss_mb')} MB, wall left {e.get('file_wall_left_s')}"
        if e["event"] == "phase":
            why = f"<- {e.get('prev_phase')} {e.get('prev_wall_s')} s"
        defect = "/".join(str(x) for x in (e.get("defect_id"), e.get("defect_class")) if x)
        rows.append([f"{e.get('file_elapsed_s', '')}", e["event"], e.get("severity", ""), e.get("phase", ""),
                     e.get("round", ""), defect, e.get("op") or e.get("member") or e.get("kernel_call") or "",
                     what, _short(why, 100)])
    head = dict(mine[0])
    for e in mine:            # the part's size is known only once it is read, after file_start
        for k in ("part_faces", "part_solids"):
            if head.get(k) is None and e.get(k) is not None:
                head[k] = e[k]
    title = f"{head.get('file_name')} {head.get('file_sha256', '')[:12]} arm={head.get('arm')} vm={head.get('vm')} " \
            f"shard={head.get('shard')} faces={head.get('part_faces')} solids={head.get('part_solids')}"
    return title + "\n" + _table(rows, ["t_s", "event", "sev", "phase", "rnd", "defect", "op/member", "result", "why"])


def walltime(ev: list[dict]) -> str:
    per: collections.Counter = collections.Counter()
    files: dict[str, set] = collections.defaultdict(set)
    for e in ev:
        if e["event"] in ("phase", "file_end") and e.get("prev_phase") and e.get("prev_wall_s") is not None:
            per[e["prev_phase"]] += float(e["prev_wall_s"])
            files[e["prev_phase"]].add(_file_key(e))
    total = sum(per.values()) or 1.0
    rows = [[ph, f"{s:.0f}", f"{100 * s / total:.1f}%", len(files[ph]), f"{s / max(1, len(files[ph])):.0f}"]
            for ph, s in per.most_common()]
    ends = [e for e in ev if e["event"] == "file_end"]
    out = _table(rows, ["phase", "wall_s", "share", "files", "s/file"]) if rows else "no phase events"
    if ends:
        slow = sorted(ends, key=lambda e: -float(e.get("wall_s") or 0))[:5]
        out += "\n\nslowest files:\n" + _table(
            [[e.get("file_name"), e.get("arm"), e.get("wall_s"), e.get("result"), e.get("prev_phase"),
              _short(e.get("reason"), 60)] for e in slow], ["file", "arm", "wall_s", "result", "last phase", "reason"])
    return out


def exceptions(ev: list[dict], top: int = 10) -> str:
    c: collections.Counter = collections.Counter()
    ex: dict[str, dict] = {}
    for e in ev:
        if e.get("exc_type"):
            k = f"{e['exc_type']} ({e['event']})"
            c[k] += 1
            ex.setdefault(k, e)
    if not c:
        return "no exceptions"
    parts = []
    for k, n in c.most_common(top):
        e = ex[k]
        tb = "\n".join("      " + x for x in str(e.get("traceback") or "").splitlines()[-8:])
        parts.append(f"{n:5d}  {k}  e.g. {e.get('file_name')} phase={e.get('phase')} op={e.get('op')}\n"
                     f"       {_short(e.get('exc_message'), 160)}\n{tb}")
    return "\n".join(parts)


def mesher(ev: list[dict]) -> str:
    c: collections.Counter = collections.Counter()
    secs: collections.Counter = collections.Counter()
    ex: dict[tuple, dict] = {}
    for e in ev:
        if e["event"] == "panel_member" and e.get("status") != "pass":
            k = (e.get("member"), e.get("status"), e.get("error_kind"), e.get("cause"))
            c[k] += 1
            secs[k] += float(e.get("wall_s") or 0)
            ex.setdefault(k, e)
    rows = [[n, *k, f"{secs[k]:.0f}", _short(ex[k].get("message"), 70)] for k, n in c.most_common()]
    out = _table(rows, ["n", "member", "status", "error_kind", "cause", "wall_s", "example message"]) \
        if rows else "no failing panel members"
    kids = [e for e in ev if e["event"] == "mesher_child"]
    if kids:
        kc = collections.Counter((e.get("profile"), e.get("end"), e.get("child_exit_code")) for e in kids)
        kex = {}
        for e in kids:
            kex.setdefault((e.get("profile"), e.get("end"), e.get("child_exit_code")), e)
        out += "\n\ngmsh child processes that did not exit cleanly:\n" + _table(
            [[n, *k, _short(kex[k].get("stderr_excerpt", "")[-200:], 90)] for k, n in kc.most_common()],
            ["n", "profile", "end", "exit", "stderr tail"])
    return out


def killed(ev: list[dict]) -> str:
    """Files with a `file_start` and no `file_end`: the process died with the file open (OOM kill, VM loss,
    SIGKILL). Their last heartbeat and last event say where they were and how big."""
    started: dict[tuple, dict] = {}
    ended: set = set()
    last: dict[tuple, dict] = {}
    beat: dict[tuple, dict] = {}
    for e in ev:
        k = _file_key(e)
        if e["event"] == "file_start":
            started[k] = e
            ended.discard(k)
        elif e["event"] == "file_end":
            ended.add(k)
        if k[3]:
            last[k] = e
            if e["event"] == "heartbeat":
                beat[k] = e
    open_ = [k for k in started if k not in ended]
    out = _table(_killed_rows(open_, started, beat, last), ["file", "arm", "vm", "shard", "last_t_s", "phase",
                                                            "last event", "rss_mb", "children_mb", "biggest child"]) \
        if open_ else "every started file ended"
    ooms = oom_attribution(ev)
    if ooms:
        out += "\n\nkernel OOM kills (vm_run.sh's dmesg scrape), with the file each unit was on at that second:\n" + \
            _table(ooms, ["ts", "vm", "unit", "task", "anon_rss_mb", "file", "phase", "op"])
    return out


def oom_attribution(ev: list[dict]) -> list[list]:
    """Each `oom_kill` event with the file its unit was running at that second (the unit's last event before it)."""
    rows = []
    for o in (e for e in ev if e["event"] == "oom_kill"):
        at = str(o.get("ts") or "")[:19]
        unit = str(o.get("unit") or "")
        mine = [e for e in ev if e.get("file_sha256") and e.get("vm") == o.get("vm") and unit
                and str(e.get("shard") or "").startswith(unit + "/") and str(e.get("ts") or "")[:19] <= at]
        near = mine[-1] if mine else {}
        rows.append([o.get("ts"), o.get("vm"), unit or o.get("container_id"), o.get("task"), o.get("anon_rss_mb"),
                     near.get("file_name"), near.get("phase"), near.get("op") or near.get("kernel_call")])
    return rows


def _killed_rows(open_: list, started: dict, beat: dict, last: dict) -> list[list]:
    rows = []
    for k in open_:
        b, l = beat.get(k, {}), last.get(k, {})
        big = b.get("biggest_child") or {}
        rows.append([started[k].get("file_name"), k[4], k[1], k[2], l.get("file_elapsed_s"), l.get("phase"),
                     l.get("event"), b.get("rss_mb"), b.get("children_rss_mb"),
                     f"{big.get('rss_mb')} MB {_short(big.get('cmd'), 40)}" if big else ""])
    return rows


def summary(ev: list[dict]) -> str:
    c = collections.Counter(e["event"] for e in ev)
    ends = collections.Counter(e.get("result") for e in ev if e["event"] == "file_end")
    parts = [f"{len(ev)} events, {len({_file_key(e) for e in ev if e['event'] == 'file_start'})} files started; "
             f"file ends: {dict(ends)}",
             "events: " + ", ".join(f"{k}={n}" for k, n in c.most_common(25))]
    for title, body in (("FAILURES by phase/class/op/reason", failures(ev, ["phase", "defect_class", "op", "reason_key"])),
                        ("WALL TIME per phase", walltime(ev)), ("EXCEPTIONS", exceptions(ev, 5)),
                        ("MESHER failures", mesher(ev)), ("KILLED (no file_end)", killed(ev))):
        parts.append(f"\n== {title}\n{body}")
    return "\n".join(parts)


def sql(paths: list[Path], query: str) -> str:
    try:
        import duckdb  # type: ignore[import-not-found]
    except ImportError:
        return "duckdb is not installed here: use the other commands (plain Python), or `pip install duckdb`"
    con = duckdb.connect()
    files = ", ".join("'" + str(p).replace("'", "''") + "'" for p in paths)
    # the operator's own local paths and SQL: nothing here comes from an untrusted party
    con.execute(f"create view ev as select * from read_json_auto([{files}], format='newline_delimited', "  # noqa: S608
                f"union_by_name=true, ignore_errors=true)")
    res = con.execute(query)
    cols = [d[0] for d in res.description]
    return _table(res.fetchall(), cols)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("src")
    p.add_argument("--refresh", action="store_true", help="re-copy a gs:// prefix")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary")
    f = sub.add_parser("failures")
    f.add_argument("--by", default="phase,defect_class,op,reason_key")
    t = sub.add_parser("timeline")
    t.add_argument("--file", required=True)
    t.add_argument("--no-heartbeat", action="store_true")
    sub.add_parser("walltime")
    x = sub.add_parser("exceptions")
    x.add_argument("--top", type=int, default=10)
    sub.add_parser("mesher")
    sub.add_parser("killed")
    q = sub.add_parser("sql")
    q.add_argument("query")
    a = p.parse_args(argv)
    paths = files_of(a.src, a.refresh)
    if not paths:
        print(f"no *events*.jsonl under {a.src}", file=sys.stderr)
        return 1
    if a.cmd == "sql":
        print(sql(paths, a.query))
        return 0
    ev = load(paths)
    out = {"summary": lambda: summary(ev),
           "failures": lambda: failures(ev, [k.strip() for k in a.by.split(",") if k.strip()]),
           "timeline": lambda: timeline(ev, a.file, not a.no_heartbeat),
           "walltime": lambda: walltime(ev), "exceptions": lambda: exceptions(ev, a.top),
           "mesher": lambda: mesher(ev), "killed": lambda: killed(ev)}[a.cmd]()
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
