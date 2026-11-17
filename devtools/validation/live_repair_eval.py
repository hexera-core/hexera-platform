#!/usr/bin/env python3
# Responsibility: Drive a corpus of CAD files through a DEPLOYED environment's product path - upload,
#                 intake chat, approval, job - and collect each job's mesh verdict and repair report.
# Owns: the scripted chat answers, the per-file row shape, and the bounded concurrency.
# Boundaries: an HTTP client only. It holds an API key it is given (env HEXERA_API_KEY); it mints
#             nothing, changes no setting, and never touches the environment's infrastructure.
"""Live repair evaluation through the product, one arm at a time.

  login    trade an Identity Platform email/password for a console session, mint an API key
           (POST /api/v1/api-keys through the console proxy), print it once
  run      submit every file of a manifest (or a list) as a job, wait, write one row per file

Rows carry: job id, terminal status, final_result (mesh verdict: executor_success / outcome_code),
the repair_report artifact's `repair_attempt` (kept ops, stopped_because, model author / package
review measurements), wall-clock timings and the chat transcript length.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import httpx

TERMINAL = {"succeeded", "failed", "cancelled", "pending_review"}
FIRST_MESSAGE = ("I need a solid volume mesh of this part for a static stress FEA. "
                 "Use the default settings.")
DOMAIN_FEA = "The part's strength, stress or vibration (FEA)"


def _log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------------------------- login
def login(args) -> None:
    web_key = args.firebase_key
    email = os.environ["EVAL_EMAIL"]
    password = os.environ["EVAL_PASSWORD"]
    hdr = {"Referer": "https://hexera-dev.firebaseapp.com"}
    r = httpx.post(f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={web_key}",
                   json={"email": email, "password": password, "returnSecureToken": True},
                   headers=hdr, timeout=30)
    r.raise_for_status()
    id_token = r.json()["idToken"]
    with httpx.Client(base_url=args.console, timeout=60, follow_redirects=False) as c:
        csrf = c.get("/api/auth/csrf").json()["csrfToken"]
        r = c.post("/api/auth/callback/credentials",
                   data={"csrfToken": csrf, "idToken": id_token,
                         "organizationName": "crs live eval", "json": "true"})
        if r.status_code not in (200, 302):
            sys.exit(f"credentials callback -> {r.status_code}: {r.text[:300]}")
        sess = c.get("/api/auth/session").json()
        print("session:", {k: sess.get(k) for k in ("organizationId", "emailVerified")}, file=sys.stderr)
        r = c.post("/api/v1/api-keys", json={"name": args.key_name})
        if r.status_code != 201:
            sys.exit(f"api-keys -> {r.status_code}: {r.text[:300]}")
        print(r.json()["presented"])


# ----------------------------------------------------------------------------------------- run
class Api:
    def __init__(self, base: str, key: str):
        self.c = httpx.Client(base_url=base, timeout=180,
                              headers={"Authorization": f"Bearer {key}"})

    def req(self, method: str, path: str, *, retries: int = 6, ok=(200, 201, 202), **kw):
        last = None
        for i in range(retries):
            try:
                r = self.c.request(method, path, **kw)
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
                time.sleep(10 * (i + 1))
                continue
            if r.status_code in ok:
                return r
            last = f"{r.status_code}: {r.text[:400]}"
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(20 * (i + 1))
                continue
            raise RuntimeError(f"{method} {path} -> {last}")
        raise RuntimeError(f"{method} {path} -> gave up: {last}")


def upload(api: Api, path: Path) -> dict:
    size = path.stat().st_size
    begin = api.req("POST", "/api/v1/upload/direct",
                    json={"filename": path.name, "size_bytes": size}).json()
    with open(path, "rb") as fh:
        r = httpx.put(begin["upload_url"], content=fh.read(), headers=begin.get("headers") or {},
                      timeout=600)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"PUT upload_url -> {r.status_code}: {r.text[:300]}")
    fin = begin["finalize_url"]
    if fin.startswith("http"):
        fin = httpx.URL(fin).raw_path.decode()
    return api.req("POST", fin, json={"filename": path.name}).json()


def _answer(resp: dict) -> str:
    reply = (resp.get("reply") or "")
    low = reply.lower()
    choices = resp.get("choices") or []
    sends = [c.get("send", "") for c in choices]
    if any("FEA" in s for s in sends):
        return DOMAIN_FEA
    if "Static stress" in sends:
        return "Static stress"
    if resp.get("awaiting_confirmation"):
        return "yes"
    if resp.get("engine_choice"):
        return "yes"
    if re.search(r"\bunits?\b|millimet|inches|metres|meters", low) and "?" in reply:
        return "millimetres"
    if re.search(r"\bnotes?\b|anything (else|specific)", low) and "?" in reply:
        return "no"
    return "Use the defaults and proceed."


class Blocked(RuntimeError):
    """The product refused the file before any job existed (e.g. the FEA check's blocker)."""


def confirm_check(api: Api, session_id: str, transcript: list) -> dict | None:
    """The console's 'Proceed' on the geometry check panel: wait for the check, then confirm with
    what it found (the server fills an FEA route's kind and flow). Returns the chat turn the
    confirm ran (`next`), or None."""
    deadline = time.time() + 900
    check: dict = {}
    while time.time() < deadline:
        check = api.req("GET", f"/api/v1/geometry/{session_id}/check").json()
        if check.get("status") in ("ready", "failed", "unsupported", "off", "confirmed") \
                or check.get("confirmed"):
            break
        time.sleep(10)
    transcript.append({"check_status": check.get("status"),
                       "check_unit": check.get("unit") or (check.get("interpretation") or {}).get("unit")})
    if check.get("status") in ("off",):
        return None
    for body in ({}, {"unit": "mm"}):
        r = api.c.post(f"/api/v1/geometry/{session_id}/check/confirm", json=body, timeout=300)
        transcript.append({"confirm": body, "code": r.status_code, "detail": r.text[:400]})
        if r.status_code == 200:
            return r.json().get("next")
        if r.status_code == 409 and "unit" in r.text.lower() and not body:
            continue
        if r.status_code in (409, 422):
            raise Blocked(f"check/confirm {r.status_code}: {r.text[:400]}")
        raise RuntimeError(f"check/confirm {r.status_code}: {r.text[:300]}")
    return None


def chat_to_job(api: Api, session_id: str, transcript: list) -> str:
    content = FIRST_MESSAGE
    confirmed = False
    resp: dict | None = None
    for _turn in range(16):
        if resp is None:
            transcript.append({"user": content})
            try:
                resp = api.req("POST", "/api/v1/chat/message",
                               json={"session_id": session_id, "content": content}, retries=4).json()
            except RuntimeError as exc:
                msg = str(exc)
                transcript.append({"error": msg[:400]})
                if "no_confirmed_unit" in msg or "unit" in msg.lower():
                    content = "millimetres"
                    continue
                raise
        transcript.append({"reply": (resp.get("reply") or "")[:600],
                           "awaiting": resp.get("awaiting_confirmation"),
                           "choices": [c.get("send") for c in resp.get("choices") or []],
                           "engine_choice": bool(resp.get("engine_choice"))})
        if resp.get("job_id"):
            return str(resp["job_id"])
        low = (resp.get("reply") or "").lower()
        if "geometry check" in low and not confirmed:
            confirmed = True
            nxt = confirm_check(api, session_id, transcript)
            if nxt:
                resp = nxt
                continue
        content = _answer(resp)
        resp = None
    raise RuntimeError("intake did not reach a job in 16 turns")


def fetch_report(api: Api, job_id: str, status: dict) -> dict | None:
    for art in status.get("artifacts") or []:
        if art.get("artifact_type") == "repair_report":
            url = art.get("download_url")
            if not url:
                url = api.req("GET", f"/api/v1/simulation/{job_id}/artifacts/{art['id']}/download"
                              ).json().get("url")
            try:
                r = httpx.get(url, timeout=120)
                r.raise_for_status()
                return r.json()
            except Exception as exc:  # noqa: BLE001 - recorded on the row
                return {"_fetch_error": f"{type(exc).__name__}: {exc}"}
    return None


def run_one(api: Api, path: Path, name: str, out_dir: Path, job_timeout_s: int,
            adopt: str = "") -> dict:
    row: dict = {"file": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    t0 = time.time()
    transcript: list = []
    try:
        if adopt:
            # A job an earlier driver process dispatched and did not live to collect: poll it
            # rather than submit the file twice. Its upload/chat timings were not kept.
            job_id = adopt
            row["adopted"] = True
        else:
            up = upload(api, path)
            row["session_id"] = up["session_id"]
            row["t_upload_s"] = round(time.time() - t0, 1)
            job_id = chat_to_job(api, up["session_id"], transcript)
            row["t_dispatch_s"] = round(time.time() - t0, 1)
        row["job_id"] = job_id
        _log(f"{name}: job {job_id}")
        # THE DEADLINE RUNS FROM THE JOB'S OWN created_at, so a job collected by a later driver
        # process (--adopt) is held to the same wall as one this process dispatched.
        from datetime import datetime
        status = api.req("GET", f"/api/v1/simulation/{job_id}").json()
        created = datetime.fromisoformat(str(status["created_at"]).replace("Z", "+00:00"))
        deadline = created.timestamp() + job_timeout_s
        row["job_timeout_s"] = job_timeout_s
        while status.get("status") not in TERMINAL and time.time() < deadline:
            time.sleep(30)
            status = api.req("GET", f"/api/v1/simulation/{job_id}").json()
        if status.get("status") not in TERMINAL:
            row["wedged"] = True
            try:
                api.req("POST", f"/api/v1/simulation/{job_id}/cancel", retries=2)
            except RuntimeError as exc:
                row["cancel_error"] = str(exc)[:300]
            status = api.req("GET", f"/api/v1/simulation/{job_id}").json()
        row["status"] = status.get("status")
        row["final_result"] = status.get("final_result")
        row["artifact_types"] = [a.get("artifact_type") for a in status.get("artifacts") or []]
        row["status_doc"] = {k: v for k, v in status.items()
                             if k not in ("artifacts", "final_result")}
        row["repair_report"] = fetch_report(api, job_id, status)
    except Blocked as exc:
        row["status"] = "blocked_before_job"
        row["error"] = f"Blocked: {exc}"[:800]
    except Exception as exc:  # noqa: BLE001 - one file never blocks the arm
        row["error"] = f"{type(exc).__name__}: {exc}"[:800]
    row["t_total_s"] = round(time.time() - t0, 1)
    row["chat"] = transcript
    (out_dir / "rows").mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    (out_dir / "rows" / f"{safe}.json").write_text(json.dumps(row, indent=1, default=str))
    fr = row.get("final_result") or {}
    _log(f"{name}: {row.get('status')} outcome={fr.get('outcome_code')} "
         f"exec={fr.get('executor_success')} err={row.get('error', '')[:120]}")
    return row


def run(args) -> None:
    key = os.environ["HEXERA_API_KEY"]
    api = Api(args.api, key)
    roots = [Path(r).expanduser() for r in args.roots]
    files: list[tuple[str, Path]] = []
    if args.manifest:
        m = json.loads(Path(args.manifest).read_text())
        for f in m["files"]:
            p = next((r / f["name"] for r in roots if (r / f["name"]).exists()), None)
            if p is None:
                sys.exit(f"missing {f['name']}")
            files.append((f["name"], p))
    for extra in args.files or []:
        p = Path(extra).expanduser()
        files.append((p.name, p))
    if args.limit:
        files = files[: args.limit]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    done = {json.loads(p.read_text())["file"] for p in (out / "rows").glob("*.json")} \
        if (out / "rows").exists() and args.resume else set()
    todo = [(n, p) for n, p in files if n not in done]
    adopt: dict[str, str] = {}
    for log in args.adopt or []:
        for line in Path(log).read_text().splitlines():
            m = re.match(r"^\S+ (\S+): job ([0-9a-f-]{36})$", line)
            if m:
                adopt[m.group(1)] = m.group(2)
    # Adopted jobs first: they are already running and only need collecting.
    todo.sort(key=lambda np: 0 if np[0] in adopt else 1)
    _log(f"{len(todo)} files to run ({len(done)} already done), concurrency {args.concurrency}")
    lock = threading.Lock()
    rows = []
    with concurrent.futures.ThreadPoolExecutor(args.concurrency) as pool:
        futs = []
        for i, (n, p) in enumerate(todo):
            futs.append(pool.submit(run_one, api, p, n, out, args.job_timeout, adopt.get(n, "")))
            if i < args.concurrency:
                time.sleep(5)
        for f in concurrent.futures.as_completed(futs):
            with lock:
                rows.append(f.result())
    _log(f"arm finished: {len(rows)} rows")


# ----------------------------------------------------------------------------------- summarize
def compact(row: dict) -> dict:
    """One row, reduced to what the evidence reads: the mesh verdict, what repair did, what the
    model author and the reviewer said. Every field is read off the job's own records."""
    fr = row.get("final_result") or {}
    rep = row.get("repair_report") or {}
    ra = rep.get("repair_attempt") or {}
    # TWO SHAPES: a refused/declined attempt nests its record under detail.measurements; a
    # promoted repair carries measurements, operations and stopped_because at the top level.
    meas = ((ra.get("detail") or {}).get("measurements") or ra.get("measurements") or {})
    ops = meas.get("operations") or ra.get("operations") or []
    regions = meas.get("regions") or {}
    packages = regions.get("packages") or []
    author = meas.get("model_author") or {}
    calls = author.get("calls") or []
    out = {
        "file": row.get("file"), "sha256": row.get("sha256"), "job_id": row.get("job_id"),
        "status": row.get("status"), "error": row.get("error"), "wedged": row.get("wedged", False),
        "outcome_code": fr.get("outcome_code"), "failure_category": fr.get("failure_category"),
        "failed_gate": fr.get("failed_gate"), "failure_detail": (fr.get("failure_detail") or "")[:300],
        "meshed": bool(fr.get("executor_success")) and fr.get("outcome_code") == "success",
        "executor_success": fr.get("executor_success"),
        "delivered_by": (fr.get("engine_ladder") or {}).get("delivered_by"),
        "built_from_repaired_geometry": bool(fr.get("repair_lineage")),
        "repair_status": rep.get("repair_status"),
        "repair_route": ra.get("route"),
        "repair_outcome": ra.get("outcome") or ra.get("repair_status"),
        "repair_attempted": ra.get("attempted"), "repair_reason": (ra.get("reason") or "")[:300],
        "stopped_because": meas.get("stopped_because") or ra.get("stopped_because"),
        "planner": meas.get("planner"),
        "operations_attempted": meas.get("operations_attempted"),
        "operations_kept": meas.get("operations_kept", ra.get("operations_kept")),
        "kept_ops": [o.get("name") for o in ops if o.get("status") == "kept"],
        "packages": [{k: p.get(k) for k in ("region_index", "status", "reason", "deviation_mm",
                                            "deviation_samples", "deviation_faces", "reviewed",
                                            "review_reason", "review_s", "author")
                      if p.get(k) is not None}
                     for p in packages],
        "author_calls": [{k: c.get(k) for k in ("outcome", "reason", "region_index", "seconds",
                                                "elapsed_s", "observation_bytes") if k in c}
                         for c in calls],
        "author_verdicts": author.get("verdicts") or [],
        "repair_total_s": (meas.get("timings") or {}).get("total_s"),
        "t_dispatch_s": row.get("t_dispatch_s"), "t_total_s": row.get("t_total_s"),
    }
    for p, src in zip(out["packages"], packages):
        p["reason"] = (p.get("reason") or "")[:240]
        p["review_reason"] = (p.get("review_reason") or "")[:240] or None
        p["ops"] = [(v.get("name"), v.get("status")) for v in src.get("verdicts") or []]
    return out


def summarize(args) -> None:
    from collections import Counter
    rows = [compact(json.loads(p.read_text()))
            for p in sorted(Path(args.out, "rows").glob("*.json"))]
    pk = [p for r in rows for p in r["packages"]]
    calls = [c for r in rows for c in r["author_calls"]]
    s = {
        "files": len(rows),
        "meshed_after": sum(r["meshed"] for r in rows),
        "built_from_repaired_geometry": sum(r["built_from_repaired_geometry"] for r in rows),
        "meshed_from_repaired_geometry": sum(r["meshed"] and r["built_from_repaired_geometry"]
                                             for r in rows),
        "status": Counter(r["status"] for r in rows),
        "outcome_code": Counter(r["outcome_code"] for r in rows),
        "repair_route": Counter(r["repair_route"] for r in rows),
        "repair_outcome": Counter(r["repair_outcome"] for r in rows),
        "stopped_because": Counter(r["stopped_because"] for r in rows if r["stopped_because"]),
        "files_with_kept_ops": sum(bool(r["kept_ops"]) for r in rows),
        "kept_ops": Counter(o for r in rows for o in r["kept_ops"]),
        "packages": len(pk),
        "package_status": Counter(p.get("status") for p in pk),
        "reviewer": Counter(p.get("reviewed") for p in pk if p.get("reviewed")),
        "reviewer_reject_reasons": Counter(p.get("review_reason") for p in pk
                                           if p.get("reviewed") == "reject"),
        "author_calls": len(calls),
        "author_outcomes": Counter(c.get("outcome") for c in calls),
        "author_verdict_rows": sum(len(r["author_verdicts"]) for r in rows),
        "accepted_package_deviation_mm": sorted(p["deviation_mm"] for p in pk
                                                if p.get("reviewed") == "accept"
                                                and p.get("deviation_mm") is not None),
        "committed_package_deviation_mm": sorted(p["deviation_mm"] for p in pk
                                                 if p.get("status") == "committed"
                                                 and p.get("deviation_mm") is not None),
        "errors": [(r["file"], r["error"]) for r in rows if r["error"]],
        "wedged": [r["file"] for r in rows if r["wedged"]],
        "t_total_s_median": sorted(r["t_total_s"] or 0 for r in rows)[len(rows) // 2] if rows else None,
    }
    Path(args.json_out).write_text(json.dumps({"summary": s, "rows": rows}, indent=1, default=str))
    print(json.dumps(s, indent=1, default=str))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login")
    lg.add_argument("--console", required=True)
    lg.add_argument("--firebase-key", required=True)
    lg.add_argument("--key-name", default="live-repair-eval")
    rn = sub.add_parser("run")
    rn.add_argument("--api", required=True)
    rn.add_argument("--manifest")
    rn.add_argument("--roots", nargs="*", default=[])
    rn.add_argument("--files", nargs="*")
    rn.add_argument("--out", required=True)
    rn.add_argument("--concurrency", type=int, default=6)
    rn.add_argument("--job-timeout", type=int, default=5400)
    rn.add_argument("--limit", type=int, default=0)
    rn.add_argument("--resume", action="store_true")
    rn.add_argument("--adopt", nargs="*", help="earlier run logs: collect their dispatched jobs")
    sm = sub.add_parser("summarize")
    sm.add_argument("--out", required=True)
    sm.add_argument("--json-out", required=True)
    args = ap.parse_args()
    {"login": login, "run": run, "summarize": summarize}[args.cmd](args)


if __name__ == "__main__":
    main()
