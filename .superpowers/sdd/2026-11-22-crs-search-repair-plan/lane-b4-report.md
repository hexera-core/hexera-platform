# Lane B4 report: recipe memory (design §5.10)

Branch `sdd/lane-b`, rebased on `d7338999` (B3 and lane C3 merged; the one conflict, with C3's new `_coverage` rows in `DetGenerator.propose`, was resolved so the order is recipes, then the sweep, then C3's coverage). Fakes only: no model call, no kernel, no VM, no spend.

## Headline
**Recipe memory is built and behind `CAD_REPAIR_V4_RECIPES` (off by default).**
- A clean file on the dev split writes its winning op sequence, keyed by an id-free failure signature.
- Every later shard reads a snapshot of what has been written:
  - DetGenerator replays the matching steps on the new site's own entities, before its fixed sweep.
  - The AI proposer sees them as `what_worked_on_similar_sites`.
- **Held-out and clean-control files can never write.** Three things enforce it, and tests pin each of them.

The A/B in miniature is a fake part whose site kind has no deterministic sweep (`test_a_clean_dev_file_teaches_the_det_arm_a_fix_it_could_not_find`):
1. `search_det` alone fails.
2. `search_ai` fixes it on dev and writes a recipe.
3. A later shard's `search_det` reads the recipe, replays it, and ends clean.

## Design

### Signature
`recipes.signature(site)` holds no id and no millimetres:
- kind
- hard codes
- which panel members fail at the site
- the neighbourhood's surface types
- a log2 bucket of dist/h
- a bucket of the largest angular span
- for open sites, a bucket of (smallest loop span / diagonal)

It is stable under any renaming of ids (tested), and the same part at 10x scale gives the same signature (tested). Different kinds are never similar.

### Similarity
Kinds must match exactly. Within a kind, the score is a weighted sum:
- 3 × Jaccard of failing meshers
- 2 × Jaccard of hard codes
- 2 × Jaccard of surface types
- 1 × each of the three buckets: 1 if equal, 0.5 if adjacent

The maximum is 10. Retrieval keeps hits at ≥50% of the maximum and returns the top 3 by (score, then support = the number of distinct clean files that witnessed the recipe, then recipe id, then step index). That makes it **a pure function of the snapshot and the query**: four books written in shuffled orders give identical results and an identical snapshot hash (tested).

### Recipe
A recipe is `recipe_from_path(SearchResult.path_steps)`. `path_steps` is new: one entry per op on the winning path, each with the sites of the state that op was applied to. Each step stores:
- the op;
- its targets as a **role** relative to the site it addressed: `none`, `site_faces`, `site_solids`, `site_entities`, or `free_loop[n]` (the n-th smallest loop);
- millimetre parameters as fractions of the part diagonal;
- that site's signature.

A step whose targets or id-bearing parameters cannot be expressed as a role (for example `remove_duplicate_faces` pairs) is kept, because the AI still sees it, but is marked `replayable: false`.

The hole box's path becomes `fill_loop free_loop[0] {G0}` → `make_volume none`. The same path with different ids gives the same recipe id (tested).

### Replay
Each step that matches a site is mapped onto **that site's** entities and **this part's** diagonal (tested: `tol_ladder` scales by 2x on a part twice the size). Replays become ordinary `det` candidates; the search judges them like any other. A wrong recipe costs one op.

### Store
Versioned JSON in the eval bucket (or a directory):
`recipes/v1/<signature key of step 1>/<recipe id>/<file sha256>.json`
- There is one object per (recipe, witnessing file), written create-if-absent: `if_generation_match=0` on GCS, `open(..., "x")` locally. Shards never overwrite each other, and support is simply the count of witnesses.
- No names or paths are stored. A provenance with `path`/`name` keys is filtered out (tested), and the witness must be a sha256.

### Snapshot
A shard loads the book once, so recipes written during a wave are seen by the **next** wave. A wave's results do not depend on which shard finished first. `prov["recipes"]` records the snapshot hash, the number of recipes, whether this shard writes, and its written / write-error counts.

### The dev-only rule, in three places
1. `RecipeStore.record` refuses any split that is not exactly `"dev"`, and refuses the `held_out` wave type even on a split labelled dev.
2. `RecipeMemory.for_shard` gives held-out and clean-control shards a **read-only** memory (no store).
3. `arms._search_arm` writes only when the output verdict is clean and the row is measured.

Mutation check: removing (1)'s split check, (1)'s wave check, or (2)'s read-only rule each turns tests red. So does putting the site id into the signature. An end-to-end `run_arm` test runs a file to **clean** on the held-out split and on the clean split and asserts that zero objects were written.

### Flag
- `CAD_REPAIR_V4_RECIPES` (bool, default false) and `CAD_REPAIR_V4_RECIPES_URI` are in the settings catalogue, and the docs roster is regenerated.
- Turning the flag on without a URI is refused before any file runs.
- **With the flag off nothing changes:** DetGenerator's candidates are identical (tested), and the system prompt is **byte-identical** to the one at `97823d62`. I checked that by rendering both. That matters because a changed prompt is a changed cache key and an uncontrolled variable in the A/B.
- With the flag on, one paragraph is added that explains the advice format (roles, relative mm, "advice, not instruction").

## Not done / judgement calls
- **The engine path (`repair_v4()`, `CAD_REPAIR_ENGINE=search`) does not read recipes.** The flag is wired for the eval waves (`run_arm`), which is what the A/B needs. Production use would want a decision on where the book lives and how fresh it is.
- **A recipe is keyed by its first step's site.** Each step also carries its own site signature, and retrieval indexes every step, so a `make_volume` learned at step 2 is found from a `no_solids` site on its own. Only the multi-step sequence shown to the AI is anchored on step 1.
- **The weights and the 50% threshold are untuned.** They need dev-wave data, which is what the A/B produces.
- **`run_stage` steps with id lists in their params** (`fill_edges`, `targets`, `rebuild`, `small`) are stored as not replayable.
- **No eviction.** A recipe that later fails often is still offered. Recording replay outcomes per recipe would be the next step once waves exist.

## Verification
- **New tests:** `tests/unit/cad/repair_v4/test_recipes.py` (27) and 5 in `tests/unit/dev/test_v4_arms.py`, including the end-to-end A/B and the held-out/clean never-write test. All pass.
- **Suites:** `PYTHONPATH=src pytest tests/unit/cad/repair_v4 tests/unit/dev tests/unit/contracts tests/unit/platform tests/unit/infra tests/unit/settings` gives **3889 passed, 40 failed**. Base `d7338999` gives 3857 passed and the **same 40** failed (`comm` is empty both ways): the 39 host-environment failures, plus `test_the_rendered_compose_carries_the_catalogue_value` (docker compose rendering on this host). The +32 are the new tests. The settings docs roster was regenerated for the two new variables.
- **Config certification:** `devtools/quality/config_inventory.py --full` passes (0 undeclared reads, 0 bypasses).
- ruff on every changed file: clean. mypy on `recipes.py`, `search.py`, `proposer.py`, `settings/cad_repair.py`: clean.
- **Not run:** the native suite. No live call was made.
