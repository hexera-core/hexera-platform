# The rulebook

Every decision a mesh run makes on its own is made by a written rule. Each rule has a stable ID,
and each decision the run records in its "why" record names the rule behind it
(`contracts/decision_record.py`). This file is the one home for those rules.

Rulebook version: **1**

How the book is kept honest:

- A rule's ID never changes and is never reused. A rule that stops applying is marked `retired`,
  not deleted.
- **In force** means the code makes this decision today. **Planned** means the ID is reserved
  for a later PR (named in brackets) and no code may cite it yet.
- `tests/unit/hygiene/test_rulebook.py` checks, on every PR, that every rule ID the code cites is
  in force here, that every in-force rule here is cited by the code, and that the version above
  matches `RULES_VERSION` in `contracts/decision_record.py`.
- Changing what a rule decides means changing this book in the same PR and raising the version.

Each decision in the record also says where it came from:

| Source | Meaning |
|---|---|
| `rule` | The code decided by the rule, from measured or declared facts. |
| `user` | The user decided (named an engine, or confirmed one). The rule says why that stands. |
| `ai-lever` | The AI proposed one approved lever, inside its limits (not used yet: AI levers are off). |
| `guard-clock` | A clock decided. These are being removed (D3); each one is listed so a run that differs here is explained. |

---

## ENG: which engine meshes the run

| ID | Status | Rule |
|---|---|---|
| ENG-1 | in force | **Only engines that can take the case are ranked.** An engine is a candidate only if its declared capability covers the flow, the file's form, the number of separate passages and the layers asked for. |
| ENG-2 | planned (D1) | **Home turf first.** When the shape's measured facts put an engine "home", that engine comes first. |
| ENG-3 | planned (D1) | **The scoreboard overrides home turf only when it clearly wins:** the challenger has at least 20 runs on this shape class, and its 95% lower bound is above the home engine's 95% upper bound. |
| ENG-4 | in force | **The best lab record on shapes like this one comes first.** Engines are ranked by their measured record on this shape class (`engines/fitness.py`). An engine whose declared turf puts the shape outside is never recommended. Ties go to fewer broken meshes, then fewer bad cells, then the declared turf, layers and ladder order. |
| ENG-5 | in force | **The fallback ladder walks the same ranking**, after the approved engine, minus the engines already tried and the engines that would refuse the case. |
| ENG-6 | in force | **The user's pick stands.** An engine the user named is never switched on its own; another engine is only offered. A dispute keeps the engine of the mesh it disputes. |
| ENG-7 | in force | **An engine that reads the upload's own format comes first**, measured or not: a conversion loses what the file describes (an ECXML's parts, materials and powers). |
| ENG-8 | in force | **Nothing measured, nothing named: the declared default.** With no measured ranking, a run nobody chose an engine for takes the engine the declarations name for its flow. |
| ENG-9 | in force | **The user confirmed an engine the measured ranking did not put first** (for example, the ranking was not ready while the user was asked). The confirmed engine stands. This is a gap D1 closes: the proposal should always be the ranked one. |

## SW: whether a run moves to another engine after a failed attempt

Decided by `pipeline/engine_fallback.decide`. The run moves only on measured facts, never on a
review. The order the run moves in is ENG-5.

| ID | Status | Rule |
|---|---|---|
| SW-1 | in force | **Move on: the mesh came out broken** (checkMesh's fatal defects). The next engine in the ladder runs at once. |
| SW-2 | in force | **Move on: the engine could not build a mesh** (it crashed, stopped early or refused the input). |
| SW-3 | in force | **Move on: the same measured failure repeated** on this engine. |
| SW-4 | in force | **Move on: the shared attempts go to the untried engine** when a retry here would leave it no attempt of its own. |
| SW-5 | in force | **Stay: a measured check failed, and this engine's own levers are tried first**, because a retry here still leaves every untried engine an attempt. |
| SW-6 | in force | **Stay: a retry here fits** and still leaves every untried engine an attempt. |
| SW-7 | in force | **Stay: a review never moves the run.** The mesh passed every measured check. |
| SW-8 | in force | **Stay: the engine is not ours to change** (the user named it, or a dispute; see ENG-6). |
| SW-9 | in force | **Stay: another engine cannot change this failure** (the request, the approval or a region split). |
| SW-10 | in force | **Stay: no untried engine delivers the approved mesh** (same boundaries, units, cell kind and layers). |
| SW-11 | in force | **Stay: the untried engines would refuse** this many separate passages in one job. |
| SW-12 | in force | **Stay: no regular attempt is left.** |
| SW-13 | in force | **Stay: too little time left** for the next engine's run. A clock decides this one (source `guard-clock`); D3 removes it. |
| SW-14 | in force | **Stay: the fallback ladder is switched off** by a setting. |
| SW-15 | in force | **No decision: the last attempt did not fail.** |

## DEL: which valid mesh a run delivers

When a run built more than one valid mesh, `application/review_delivery.rank_key` ranks them by a
fixed order of measured facts. The first fact that separates the winner from the runner-up is the
rule. An invalid mesh is never delivered.

| ID | Status | Rule |
|---|---|---|
| DEL-0 | in force | **The only valid mesh** the run built is the one delivered. |
| DEL-1 | in force | **A mesh the review confirmed is the wrong problem ranks last** (a cited, inspected wrong-problem finding, not a concern). |
| DEL-2 | in force | **A mesh that passes every fixed quality limit beats one that does not.** |
| DEL-3 | in force | **Fewer failed quality checks wins.** |
| DEL-4 | in force | **Meeting more of the approved plan wins** (layers on the wall, wake refinement, stated sizes). |
| DEL-5 | in force | **Better worst cells win**, compared to the whole degree of non-orthogonality. |
| DEL-6 | in force | **On a measured tie, the mesh whose review passed wins.** D2 retires this: a review will no longer rank meshes. |
| DEL-7 | in force | **Lower worst non-orthogonality wins** (exact value). |
| DEL-8 | in force | **Lower worst skewness wins.** |
| DEL-9 | in force | **More wall covered by prism layers wins.** |
| DEL-10 | in force | **The earlier attempt wins** when everything measured is the same. |

## VER: pass, delivered with concerns, or fail (planned, D2)

| ID | Status | Rule |
|---|---|---|
| VER-1 | planned (D2) | **Fail:** no valid mesh after the ladder and the switches. |
| VER-2 | planned (D2) | **Delivered with concerns:** valid, but at least one production limit is missed; each one listed as measured vs. limit. |
| VER-3 | planned (D2) | **Clean pass:** every gating row and every production limit is met. |
| VER-4 | planned (D2) | **Wrong problem:** decided by measured checks only. |

## Reserved families

These ID families are reserved. Their rules are written by the PRs that build them, and no code
may cite them before then.

| Family | For | Written by |
|---|---|---|
| `PLAN-<engine>-n` | the numbers of each engine's computed plan | D4 to D8 |
| `LAD-<engine>-n` | each engine's fixed ladder of approved levers | D4 to D8 |
| `TIME-n` | planned-work limits and the guard clocks | D3 |
