# The held-out freeze plan (lane C13, for LEAD-3 to put to the owner)

The held-out set (25 files, `corpora/v4/provisional_sample_task43/PROVISIONAL_heldout.json`) is sealed: nobody has
read, meshed or listed a held-out file, and the once-only slot `model-ledger/slot/held_out/1` is free. This is how it
runs, what is pinned, and what the owner must decide BEFORE the freeze.

## The one command

```bash
git tag -a crs-v4-freeze-<YYYYMMDD> <commit> -m "CRS v4 freeze: <why>"     # 1. the freeze
git push origin crs-v4-freeze-<YYYYMMDD>
bash .superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v4/launch.sh crs-v4-freeze-<YYYYMMDD> heldout --dry-run   # 2.
bash .superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v4/launch.sh crs-v4-freeze-<YYYYMMDD> heldout             # 3. ONCE
python3 .superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1r/v4_doc.py --run <run dir> --doc <doc.md>          # 4. the doc
```

Step 2 must exit 0 before step 3. It touches no held-out object, creates no VM, calls no model.

## What is pinned, and where it is enforced

| what | pinned to | enforced by |
|---|---|---|
| code | the commit the freeze tag names | `freeze_guard.py`: refuses with no `crs-v4-freeze-*` tag, or if the run commit is not that tag's commit |
| required fixes in that commit | B18 anytime `39f52d73`, C12a valid-not-clean gaming flags `2bd03537`, C13 per-original-shard spend `03764a28`, C14b label-drift stratum `6d96618a`, C14b resume `e92a6c75`, B22 catastrophic-loss rule `ff8da7b05` + proofs `537e25efa`, C15 degenerate Boolean `ec06ce89`, C15 inside-out on the BREP `b0866cf7` | `freeze_guard.py --require` (REQUIRE_COMMITS): refuses if any is not an ancestor |
| the sealed manifest | read FROM the frozen commit, content sha256 `2ba86ba2…46a56` | `launch.sh`: refuses if the frozen commit's copy hashes differently |
| the wave | `heldout_v4`: input_only, staged, search_det, search_ai; held_out type; heldout split | `evalspec`, `prep.py` (one group, held_out type only for the held-out split) |
| the cap | $250 (`model_ledger.WAVE_CAPS_USD["held_out"]`), sliced $250 / n per shard | each shard's ledger; the report (per ORIGINAL shard) |
| the once-only slot | `gs://dev-crsv3-transfer-224734058693/model-ledger/slot/held_out/1` | dry run READS it and refuses if held; a real run CLAIMS it (create-if-absent) before any VM; the report VERIFIES it (`--markers-dir`) |
| the report | `--frozen-commit <sha>`, slot verified | `collect.sh` -> `post.py` -> `report.py` |
| the image | `crsv4-mesh@sha256:3e8c6fae…e3592`, label `1d6ed735` (the mesh build v4 and the fast loop ran) | `image_label.py`: refuses a label other than IMAGE_COMMIT |
| the verdict cache | FRESH, this run's own prefix | `VERDICT_CACHE=fresh` in the profile (the shared cache would keep held-out verdicts where every later dev run reads) |
| budget per file | the frozen commit's Budget.ops (60) and file wall; FILE_USD_CAP $1.00, as dev | launch.sh reads the commit's own defaults |
| inputs | listed by NAME only (no wildcard), on the real run only | `launch.sh` HELDOUT branch |
| packing | one group, ~12 held-out shards + 1 clean-control shard, 1.5 vCPU per shard, two c2d-16 | `prep.py`; REGIONS overridable at launch if quota moved |

Measured by the dry run on `03764a28` (2026-11-24): guard FROZEN, compile OK, 25 held-out objects not touched, 13 shard
slots on 2 VMs / 32 vCPU, about 97 min wall, ~52 vCPU-h ≈ $2.44 VM, model caps $35 of $300, slot free, cache fresh.

## What the run produces

- The held-out wave and the 40-file clean control (a NORMAL wave alongside, so the frozen commit's regressions are
  measured in the same run without spending the slot).
- `report_dev` (the held-out report, frozen commit and slot verified) and `report_clean`; then the doc (`v4_doc.py`),
  headed "HELD-OUT run …: ONCE ONLY, frozen <sha>, NEVER for tuning", with per arm: clean, regressions, AI marginal
  with its paired count, fidelity columns (volume, area, bbox, max dev, body count), gaming flags on clean AND on
  valid-not-clean delivered outputs (named, with numbers), wall-stopped rows and the clean rate without them, harness
  crashes as their own class, cost, cores used vs available.

## DECISIONS FOR THE OWNER, before the freeze

1. **The delivery rule (b). DONE (owner ruling; Lane B's B22 `ff8da7b05`):** under 10% of the original part, or an original body lost, is never ranked best and never delivered. What follows is the original note.
   **The delivery rule (b).** Whether a delivery under some volume/area fraction of the input is refused, whatever
   its validity. It changes what is delivered and therefore the held-out numbers: decide it BEFORE the freeze, or the
   one unrepeatable run measures the rule we then change. (v4 delivered `0051_00510567` VALID with 0.07% of the part.)
2. **Label drift. DONE (C14b, `6d96618a`, LEAD-3's go):** a drifted file is now its own named stratum with this
   commit's input verdict, in no rate; the wave is refused only past 25% drift (a broken checker). What follows is
   the original note.
   **Label drift.** The held-out labels (clean / unclean per file) came from the task-43 probe on much older code.
   `report.py` REFUSES a wave where an input judges clean on the run commit but the split says unclean; v4's dev report
   hit exactly that on 4 files (00551787, 00576061, 00607513, 00615057). If a held-out label has drifted, the held-out
   report refuses too, and `post.py` falls back to a labelled sensitivity report minus the named files. It cannot be
   checked in advance without judging held-out files. Options: (a) accept the sensitivity fallback as the result;
   (b) change the rule before the freeze so a drifted file is reported as its own stratum (as excluded and declined
   files are) rather than refusing the wave. My recommendation is (b), decided now, because the held-out run cannot be
   repeated to try again.
3. **A failure after the slot is claimed. DONE (C14b, `e92a6c75`):** `HELDOUT_RESUME_TS=<stamp> bash launch.sh
   <freeze tag> heldout` relaunches under the same wave id, ledgers restored. Refused unless the slot holds that run's
   id and no held-out VM still exists. The success path cannot be exercised without claiming the real slot; it was
   checked by its two refusals and by review. What follows is the original note.
   **A failure after the slot is claimed.** `launch.sh` makes a new run stamp, hence a new wave id, on every launch,
   and the slot holds the FIRST id: a run that dies after its claim cannot be relaunched, only resumed by hand with the
   same wave id, or the slot released by a deliberate human edit. Options: accept one shot; or let me add a
   `HELDOUT_RESUME_TS` that relaunches under the same wave id (claim_wave_slot already allows resuming the same id).
   I recommend adding it: it is small, and a VM stockout should not spend the held-out set.
4. **FILE_USD_CAP $1.00**, the dev standard, keeps the AI arm comparable with dev; the $250 is a ceiling (~$35 of caps).
5. **The image** stays mesh build `1d6ed735` unless the frozen commit needs a newer toolchain (the label check refuses a
   mismatch; repin deliberately).

## Not to be done

No dev or fast-loop tool may read a held-out row, doc or verdict for tuning (`evalspec.refuse_heldout_for_tuning`).
The held-out doc is for the owner and the record.
