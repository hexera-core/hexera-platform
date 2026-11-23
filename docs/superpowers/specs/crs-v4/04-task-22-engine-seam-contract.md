# Task 22 — the engine seam: frozen interfaces

**Status: FROZEN 2026-11-23 by the controller.** Four lanes (22a-22d) build against this document and
against fakes of each other. A lane that needs a signature changed **stops and tells the controller**;
it does not change it unilaterally, because its neighbour is already building against the old one.

Everything below was read off the tree at `995d9651`, not designed here. Line numbers are that commit.

## Why this exists

A wiring audit found the engine seam is not one missing function but a **chain of orphans**: every
link exists, passes its own tests, and is called by nothing on a production path.

| link | state at `995d9651` |
|---|---|
| `Workspace.from_brep` (`workspace.py:109`) | **zero callers.** Nothing in production turns a STEP into a Workspace |
| `stages.run_staged` (`stages.py:406`) | no caller in `src`/`devtools` |
| `search.arm_generators` (`search.py:579`) | no production caller |
| `search.search` (`search.py:616`) | no caller outside tests |
| `search.deliverable` (`search.py:1068`) | no caller outside `test_search.py` |
| `render.view` (`render.py:19`) | zero callers; `proposer.py:481` reads `site.view_png`, which nothing sets |
| `repair_v4._run_search` (`__init__.py:51`) | raises `SearchUnavailable` |

Per-task review could not catch any of it: each task was complete against its own brief, and "is
anything calling this?" lies outside the brief being reviewed.

## The signatures (do not change these)

```python
# entry point, already correct - only _run_search's BODY changes
def repair_v4(source: str, dest: str, *, budget: Budget, deadline_s: float) -> V4Result

@dataclass(frozen=True)
class V4Result:
    trace_path: Path | None
    verdict_summary: dict = field(default_factory=dict)
    delivered: bool = False          # False => dest carries the upload's bytes (PASSTHROUGH)

# Budget: the placeholder in __init__.py:17 is DELETED and re-exported from search,
# whose field names it already mirrors: (wall_s, ops, model_calls, model_usd)

# 22a: in
Workspace.from_brep(pool: WorkerPool, brep_path: str, labels: dict | None = None,
                    *, timeout_s: float = 120) -> Workspace
#   NOTE: from_brep does NO I/O of its own. Reading names out of the STEP is the CALLER's job,
#   through the kernel fns "read_step_xcaf" (xcaf_io.py:300) / "write_step_xcaf" (:311).
#   It takes a stable-id -> label-dict map.

# 22a: out
deliverable(result: SearchResult, input_verdict: Any,
            *, score_fn: Callable[[Any], Score] | None = None) -> Workspace | None
#   None means "ship the input unchanged". The rule: deliver the best state ONLY if it passes at
#   least as many panel members as the input; a clean input is never replaced.

# 22b
run_staged(pool: WorkerPool, ws: Workspace, *, params: dict[str, dict] | None = None) -> StagedResult
default_judge(*, h_mm: float) -> Judge

# 22c
arm_generators(arm: str, **ai_kwargs: Any) -> tuple[list[Generator], AIProposer | None]
search(pool, start: Workspace, original: str, generators: list[Generator], budget: Budget,
       *, workers: int, k_confirm: int = 3, patience: int = 3, k_propose: int = 12,
       judge: Judge | None = None, h_mm: float | None = None,
       clock: Callable[[], float] = time.monotonic) -> SearchResult

# 22d
view(pool: WorkerPool, ws: Workspace, site: Site, *, timeout_s: float = 120.0) -> bytes | None
find_sites(pool: WorkerPool, ws: Workspace, verdict: Any, *, h_mm: float, ...)
#   view() returns PNG bytes or None. 22d sets site.view_png; `view_png` is deliberately NOT part
#   of a site's identity (sites.py:790), so setting it must not change any site id.
```

## The call order 22a owns

```
read_step_xcaf(source)  ->  brep path + labels
Workspace.from_brep(pool, brep, labels)     ->  start workspace
run_staged(pool, ws)                        ->  22b: the start state for the search
arm_generators(arm)                         ->  22c
search(pool, start, original=source, ...)   ->  22c
deliverable(result, input_verdict)          ->  Workspace | None
write_step_xcaf  OR copy the source bytes   ->  dest
V4Result(trace_path=..., verdict_summary=..., delivered=...)
```

## Rules binding every lane

- **`CheckerUnavailable` is infrastructure, never a verdict.** A pool death, a missing mesher or a
  dead checker gives `Verdict.inconclusive`, excluded by `counts_for_scoreboard`. A file that was
  never measured must not read as a file that failed.
- **A passthrough is not a failure and not a success.** `delivered=False` means `dest` holds the
  upload's bytes. `SearchUnavailable` is OUR limitation and must never read as a statement about
  the customer's file.
- **Cost.** The $1.00 per-file ceiling holds through `ledger.admit`'s per-call reservation;
  `Budget.model_usd` is advisory (checked once per expansion). The ledger `run_arm` builds at
  `run_arm.py:238` is currently **discarded** - 22c must pass it to the proposer, because a cap
  nothing consults is not a cap.
- **The v4 VM has no model key** (`vm_startup.sh:11`) and `run_arm` installs no router. 22c must say
  what it needs rather than assume a key exists.
- **No OCCT in the parent process.** Everything through `WorkerPool.call`; shapes cross as BREP file
  paths; only `kernel_calls.REGISTRY` names are callable.
- **`_run_search`'s own comment (`__init__.py:52-59`) lists two more obligations:** delete the
  placeholder `Budget`, and fill the `defects`/`operations` of the delivered report, which
  `conservative._repair_via_v4` currently leaves empty.

## What the integration owner proves

One native end-to-end test, per arm (`input_only`, `staged`, `search_det`, `search_ai`), on a real
injected defect, asserting: a STEP written at `dest`, a verdict in `verdict_summary`, and a report row.
**It must fail loudly if any arm produces nothing or any stage is a stub** - that is the whole point,
and it is the proof per-task review structurally cannot give.
