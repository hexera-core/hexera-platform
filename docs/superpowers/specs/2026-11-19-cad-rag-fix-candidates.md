# CAD RAG: retrieving fix candidates

Status: owner's idea, 2026-11-19, PARKED for validation after the CRS v3 evaluations land.
Nothing here is built. Recorded now so the design is not re-derived later.

## The idea, in the owner's framing

Isolate defects, vectorise them, and query a database populated with the corpus so a repair
run gets fix **candidates** back as fast as possible. The owner's own caveat is the correct
one and shapes everything below: *every similar defect will not have an identical fix.*
Retrieval therefore returns candidates to be judged, never an answer to be applied.

## Why this fits what already exists

`src/meshpipeline/cad/repair/memory.py` is already retrieval with the wrong index. It offers:

- `exact()` - keyed on part sha256 plus fingerprint.
- `family()` - keyed on defect code, mesh kind, stage and op counts.

Its own docstring records the limit honestly: a content-derived fingerprint is "a name good
for the length of one repair run", so two different parts matching is a coincidence of
geometry. `exact()` therefore fires only on identical bytes, and `family()` is a coarse
categorical bucket. **Nothing sits between them, and "between" is exactly where
similar-but-not-identical defects live.** That gap is the case for a vector index.

## What to embed

Not raw geometry. A scale- and pose-invariant descriptor of the defect neighbourhood:

| Component | Why |
|---|---|
| defect code, stage | the categorical anchor `family()` already uses |
| local topology: face/edge counts in the k-ring, edge valence, loop count | what the defect sits in |
| curvature and periodicity flags | separates a periodic face from a planar one |
| gap / sliver dimensions **normalised by part diagonal** | load-bearing: unnormalised, a 2 mm gap on a bracket and on a turbine casing embed alike and are different problems |
| solid count, contact-pair relation | touching vs interpenetrating is a different repair |

## Where it attaches

As a **third proposal source in the tournament**, beside the deterministic rules and the
investigator. Retrieval proposes; the cheap-oracle ladder ranks; the reviewer judges. It adds
no new trust surface, and memory's existing rule carries over unchanged: a remembered outcome
is advice, never an instruction, and never bypasses caps, declaration accounting or review.

It is also the cheapest source available - a vector query is milliseconds against a model
call's seconds - so it belongs at the bottom of the ladder, asked first because asking is
nearly free.

## Geometric and semantic, with different jobs

Both, hybrid, not either/or:

- **Geometric embedding** answers "what does this defect look like" - use it for recall.
- **Text embedding over semantic descriptions** answers "what did an engineer say this family
  needs" - use it to rerank.

The text side is also the eventual route to analysis intent, which the capability-expansion
plan parked as needing context the product does not yet carry.

## The flywheel

Every evaluation run emits labelled `(defect descriptor -> operation -> worked / failed /
deviation)` tuples. The benchmark runs therefore build the retrieval corpus as a side effect.
`memory.scrub()` already produces the right record shape for long-term storage: geometry keys
and raw bytes dropped at any nesting depth, non-finite floats nulled, codes reduced to their
first token so a path or a sentence of prose cannot be smuggled through.

## Honest ceiling

Retrieval sharpens selection among operations the catalogue can **express**; it cannot invent
one we do not have. The harvest showed the 59% volume-stage overlap family is mesher-side
rather than CAD-surgery territory, so expect the gain on the open-loop (227), tiny-shell (168)
and periodic-face (390) families, not on the majority.

## Not decided

Index technology, embedding model, whether the descriptor is learned or hand-built, and
whether cross-run storage is per-customer or shared. All of it waits on the v3 evaluations,
because without them there is no baseline to show a retrieval source improved anything.
