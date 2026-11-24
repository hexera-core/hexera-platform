# Lane D13: the code-writing action (design approach C), scoped

Status: **design only, no implementation.** It only becomes work if v1r concludes "the op library is the ceiling". Base read: `799bcefc`.

## 0. What approach C says, and where this departs from it

The spec (`docs/superpowers/specs/2026-11-22-crs-search-repair-design.md`) gives C one row and one non-goal:
- §4: "Code-writing agent: the model writes OCP Python in a sandbox, in a loop with the checker. Most expressive. Hard to make safe, reproducible and cheap, and recipes are hard to reuse. Deferred as an escape-hatch action inside B."
- §2: "reserved as a later escape hatch inside the search".

This design keeps the core of C (the model writes OCP Python, run in a sandbox, judged by the checker) and the placement ("an action inside B"). It departs in five places, each on purpose:

| # | C as stated | here | why |
|---|---|---|---|
| D1 | "a code-writing **agent** ... in a loop with the checker" | ONE bounded **action**, `write_op`, inside the existing proposer turn and search loop. No second agent loop. | B's loop (search + proposer + re-prompt, D9) already is the loop with the checker. A second loop would need its own budgets, trace and stop rules, and could not be measured as a marginal over `search_ai`. |
| D2 | "writes OCP Python" (unconstrained) | a **pure function** `repair(shape, ctx) -> shape` over an in-memory shape. No file, process or network I/O; the harness does all I/O. | Makes the sandbox small, the result judgeable like any op, and the code replayable (D4). |
| D3 | "in a sandbox" | a **fresh jailed child process per code candidate**, never the warm kernel pool | §2 below |
| D4 | "recipes are hard to reuse" | **the code IS the recipe**: stored verbatim, content-hashed, parameterised by stable IDs, and verified to replay before it is stored. | C's own objection, answered rather than accepted. |
| D5 | "hard to make cheap" | **gated as an escape hatch**: offered only after op proposals on a site have failed, with its own count, CPU and size caps, and charged to the op budget by the work it does. | Keeps `search_ai` vs `search_det` a fair comparison, and keeps cost bounded and reported. |

## 1. What the model may write

- **One function**, in one tool call `write_op{targets, source, rationale, declares}`:

  ```
  def repair(shape: TopoDS_Shape, ctx: Ctx) -> TopoDS_Shape
  ```

  - `ctx` is read-only. It gives:
    - `ctx.resolve(stable_id) -> TopoDS_Face/Edge/Solid` for the candidate's `targets` only;
    - `ctx.h_mm` and `ctx.diagonal_mm`;
    - the current open sites (ids and kinds);
    - `ctx.declare_added(face)` / `ctx.declare_removed(stable_id)`.
  - Declarations are claims; layer 3 verifies them (§4).
- **Imports, by allowlist, checked statically and by the jail:**
  - `OCP.*` minus every I/O and process module: `STEPControl`, `IGESControl`, `StlAPI`, `BRepTools.Write*`, `OSD`, `Message` file printers, `Interface_Static` ...;
  - plus `math`, `itertools`, `functools`, `collections`, `numpy`.
  - No `os`, `sys`, `subprocess`, `socket`, `ctypes`, `importlib`, `builtins.open`, `eval`/`exec`/`compile`, `__import__`, `getattr` on module objects, or dunder access.
  - The static scan rejects early with a coded reason that the model can act on (A6's `reason` shape). **The scan is a usability filter, not the security boundary; the jail is.**
- **Size:** source <= 8 KB, one function, plus helpers in the same source.
- **Not allowed:**
  - touching meshers (they run outside, on the exported STEP, so code cannot special-case the panel);
  - threads (`BOPAlgo_*.SetRunParallel(True)`, `OSD_Parallel`), which are refused by the scan and also break determinism (§5);
  - reading any file.

## 2. The sandbox

**Where it runs: a fresh child per code candidate, NOT the warm kernel pool.**
- Pool workers are long-lived and shared across every op of a shard. Model code in one could leave state that poisons later calls, which is exactly Task 48's stale-reply lesson.
- Landlock cannot be applied and then lifted, so a jailed worker could no longer serve the ops that need files.

The child is `spawn`ed (not forked: no inherited state, no inherited fds) on the same eval VM or container as the shard, using the shard's one vCPU. Startup is about 1-2 s to import OCP; that is acceptable for an escape hatch.

**Jail:** extend `meshpipeline/sandbox/sandbox_exec.jail_preexec`, the existing seccomp + Landlock + rlimit jail, which fails CLOSED.

| control | setting | contains |
|---|---|---|
| env | **scrubbed** (`safe_exec.scrubbed_subprocess_env`), and the shard's `OPENAI_API_KEY` explicitly absent | the eval container HAS the model key in its env (vm_startup `--env-file secret.env`): without scrubbing, model code could read it |
| network | seccomp: `socket` -> EPERM (existing filter) | exfiltration, and calling the model or GCS |
| filesystem | Landlock: RO = interpreter + site-packages roots only; RW = one per-candidate scratch dir holding a COPY of the input BREP. **Not** `/eval/inputs` (all pool files), `/eval/ledger`, `/eval/code`, or `secret.env` | reading other inputs or secrets; writing outside the workspace |
| new privileges | `PR_SET_NO_NEW_PRIVS` (existing) | setuid escalation |
| exec | Landlock without EXECUTE outside the runtime roots; the static scan forbids `subprocess` | spawning tools |
| memory | `RLIMIT_AS` 3 GB (the c2d-8 has ~4 GB per vCPU, shared with the panel's workers) | blow-ups (child dies `MemoryError`/abort) |
| CPU | `RLIMIT_CPU` = code budget per candidate (start 60 s) + parent wall SIGKILL of the process group at 1.5x | infinite loops |
| output | `RLIMIT_FSIZE` 512 MB, `RLIMIT_NOFILE` 64 | disk fill |
| processes | container `--pids-limit` (RLIMIT_NPROC cannot be used: it counts BLAS threads; see `sandbox_exec` note) | fork bombs |

- **Linux only.** On macOS, or wherever seccomp/Landlock is unavailable, `write_op` is not offered (fail closed, as `run_python` does).
- **Production is out of scope:** eval only, behind a flag.

**The harness inside the child** loads the scratch BREP, calls `repair`, checks the return type, and writes the output BREP to the one path it chose. The **parent trusts nothing the child reports except the output file and the declarations**:
- it re-indexes the output (`Workspace.from_brep`; stable IDs are carried by fingerprint);
- it computes the Diff itself;
- it judges from scratch.

## 3. How a written op enters the search, and how it is budgeted

- **Entry:** a new tool `write_op` beside `propose` / `declare_unfixable`, producing `Candidate(op="code", targets, params={"source", "sha256", "declares"}, source="ai")`.
  - `validate_call` checks the schema, the size cap and the static scan. Anything it rejects is an `invalid` outcome with a coded reason, fed back on the next turn as D9 does.
  - From there it is a candidate like any other: applied on a fork, judged on the cheap tier, top-K confirmed on the full panel, ranked, and delivered only through the same delivery gate.
- **When it is offered (escape hatch):**
  - only after at least one op candidate for that site has been declined, failed, or judged worse (the proposer already knows this from its feedback);
  - only in a `search_ai_code` arm.
  - `search_ai` stays exactly as it is, so the code action's marginal is measured, not mixed in.
- **Budget: it is not one applied op.**
  - Per file: at most **3 code candidates**, **180 s of code CPU** in total, 8 KB of source each.
  - Op budget: a code candidate charges `ceil(child_cpu_s / OP_EQUIV_S)` op units, at least 1. `OP_EQUIV_S` is the measured median CPU of one applied library op (to be measured; it is in the trace). This keeps the arms at **equal op budget**, the fairness rule of §7.
  - Its wall time counts against the file's wall like everything else.
  - Model $: writing code needs a larger output allowance than an op call, so a `write_op` turn is reserved at the full 4,096 tokens (D12's reduced allowance does not apply to it). At $1.00 per file (§7.4) that is ~3 code turns at today's reservations (D11).
- **Reported separately:**
  - per arm and file: code candidates written, jail outcomes, ops charged, CPU, $;
  - and whether the delivered state's lineage contains a code step ("clean via code").

## 4. How it is judged: the same checker, no exceptions

- The output BREP goes through `_judged` -> `step_for` -> the **same verdict**: layer 1 (valid), the cheap tier for ranking, the **full panel** before it can be best or shipped, and **layer 3 fidelity**.
- **No code-specific relaxation anywhere.**
- Fidelity is the strictest path by default:
  - undeclared changes use the two-sided bound;
  - declared adds follow §7.2's one-sided rule and the 5%-of-diagonal fill-span cap;
  - declared removals are exempt only below the sliver width.
  - Body count is preserved (§7.1). Area and volume ratios are checked as today.
- **First slice:** declarations are DISALLOWED (§8). Code may only move or replace geometry within the two-sided bound. This removes the largest new trust question: "is this claimed add really intended?"
- A code state is a **regression** under the existing rule if it passes fewer panel members than its input. The clean control must stay at zero regressions.

## 5. Determinism and reproducibility

- **The model is out of the loop at replay.** The recipe is the source code, its sha256, the target selection rule, the OCP/OCCT version and the harness version.
- **Replay contract:** the same source on the same input BREP in the same jail gives a **byte-identical output BREP**.
  - OCCT is deterministic single-threaded; parallel modes are forbidden (§1).
  - Before a code step is admitted to a recipe, or reported as a clean result, it is **run a second time** in a fresh child, and the two outputs are compared by hash.
  - Non-determinism is recorded, and the step is not stored as a recipe (it can still be delivered: delivery is judged on the bytes actually delivered).
- **Reuse across files:**
  - code refers to geometry ONLY through `ctx.resolve(target)` for the targets it was given, never by index;
  - a recipe stores (site signature -> code), as `recipes.py` does for op sequences;
  - replay on a new file resolves the new site's targets.
  - Replayed code is judged like any candidate; a recipe is advice, never a verdict.
- **What a verdict depends on:** the input bytes, the code bytes, the OCP version and the harness version. All four go into the cache key of the code step's output (the verdict cache keys on the exported STEP, as today).

## 6. What is recorded

- **Trace** (`candidate` event):
  - the full source (size-capped, so nothing is truncated below the 8 KB limit) and its sha256;
  - jail outcome (ok / scan_rejected / timeout / crashed(signal) / memory / denied(syscall) / bad_return);
  - child wall, CPU and max RSS; stdout/stderr tails (2 KB);
  - declarations; ops charged; replay hash check result.
- **Rows / triage:** per file, code candidates by outcome; code-step lineage of the delivered state.
- **Recipes:** source + sha + signature + versions, written only from clean DEV outcomes, never held-out (§5.10 rule unchanged).
- **The model's conversation:** the `write_op` tool call is in the proposer transcript as any tool call.
- **Security log:** every jail denial (seccomp EPERM, Landlock EACCES, rlimit kill) as its own event, so an attempt is visible even when it fails harmlessly.

## 7. Failure modes and containment

| failure | contained by | outcome to the search |
|---|---|---|
| infinite loop / pathological compute | `RLIMIT_CPU`, then parent SIGKILL of the process group at 1.5x wall | `failed: timeout`. Counts toward patience; never a verdict on the file |
| OCCT segfault / abort | separate child: dies alone; the warm pool is untouched | `failed: crashed(signal)` |
| memory blow-up | `RLIMIT_AS` | `failed: memory` |
| write outside the workspace | Landlock RW = scratch only; the parent reads only its own output path | `denied`, logged |
| read other inputs, the ledger, `secret.env` | Landlock RO roots exclude them; env scrubbed | `denied`, logged |
| network / exfiltrate the model key | seccomp `socket` -> EPERM; the key is not in the child env | `denied`, logged |
| fork bomb | container `--pids-limit`; `subprocess`/`os` refused by the scan | `failed` |
| returns a non-shape, or an empty/void shape | harness type check | `failed: bad_return` |
| lies in declarations | declarations are claims; layer 3 checks the delivered geometry against the original | judged worse, or unfaithful |
| **passes the cheap tier and destroys the part** (e.g. returns a simplified hull that meshes) | never ranked best or shipped on the cheap tier alone; full panel + layer 3 (two-sided distance at the fidelity bound, area/volume ratios, body count) before best/delivery; the delivery gate requires >= input's panel passes | unfaithful -> not deliverable; recorded as `code_destroyed` in triage so the rate is measured |
| non-deterministic output | replay hash check | delivered only on its own judged bytes; never stored as a recipe |
| code that games a mesher | impossible by construction: meshers run outside the child, on the exported STEP | - |
| prompt injection via CAD labels steering the code | whatever the model writes runs in the same jail and is judged the same way | no new exposure beyond the jail |
| cost blow-up | <= 3 code candidates, 180 s CPU per file, 8 KB source, $1/file ceiling | stops; recorded |
| jail unavailable on the host | `write_op` not offered (fail closed) | arm reports "code action unavailable", not 0 gain |

## 8. The smallest first slice, measurable on the v1r dev files

**Precondition:** v1r concludes "the op library is the ceiling". Otherwise none of this is built.

1. **Jail + red-team suite FIRST, with no model involved.**
   - Extend `sandbox_exec` for the code child: scrubbed env, scratch-only Landlock, AS/CPU/FSIZE/NOFILE limits.
   - Add a test suite of hostile snippets that must each be contained: socket, reading `secret.env` and `/eval/inputs`, writing `/tmp/x` and `../`, `ctypes` segfault, `while True`, a 10 GB allocation, fork, `__import__('os')`, `OSD_Parallel`.
   - It runs native on jig and must be green before any model call.
2. **`write_op` tool + candidate type, modify-only.** No declarations: code may only change geometry within the two-sided fidelity bound. Offered only after an op candidate on that site has failed.
3. **Arm `search_ai_code`** = `search_ai` + the tool, same budgets, $1.00/file.
   - Run on the v1r dev files where `search_ai` ended not clean.
   - Run the clean control alongside (zero-regression check).
4. **Measure, per file:**
   - code candidates written; jail outcomes;
   - cheap pass / full pass / layer-3 rejects (`code_destroyed`);
   - clean gain over `search_ai` on the same files; replay-hash agreement; $ and CPU.
5. **Out of the first slice:** declarations, recipe storage of code, offering it outside the escape-hatch condition, production.

**Decision rule after the slice:**
- **Promising:** code yields full-panel passes with clean layer 3 and zero control regressions.
- **The op library was not the ceiling after all:** code passes the cheap tier but fails layer 3 or the panel (the model cannot do better geometrically either).
- **Unsafe to continue:** the jail is breached in red-team, or non-determinism is common.

## 9. Risks and open questions

- **Biggest technical risk: OCP surface area.** The allowlist is "OCP minus I/O", which is huge. The static scan cannot prove safety; the jail must carry it. If seccomp/Landlock is weaker than assumed on the fleet's kernel (Ubuntu 22.04 on c2d: Landlock ABI v1-v2), the slice stops at step 1. Verify the ABI on the fleet image before step 2.
- **Fairness:** `OP_EQUIV_S` must be measured, not guessed; otherwise the arm comparison is biased.
- **Cost:** ~3 code turns per file at $1.00 under today's 4,096-token reservations (D11). If code needs longer outputs, the per-file ceiling binds first. That is a reported outcome, not a reason to raise caps.
- **The model may simply be poor at OCP.** That is a measured answer too (decision rule above), just as §9 of the spec treats "the AI still adds little".
- **Open for the owner:**
  - whether a code-derived clean file may ever ship to a customer (this doc assumes eval only);
  - whether code recipes may ever leave the dev split's memory.
