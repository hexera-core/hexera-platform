# Responsibility: Verify the resolved publication inventory still matches its reviewed contract.
# Boundaries: discovery stays the scanner's; this compares what it finds against the committed manifest.
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "devtools" / "quality"))
import publication_authority as PA  # noqa: E402

#: check:      python devtools/quality/publication_authority.py check
#: regenerate: python devtools/quality/publication_authority.py regenerate   (never automatic)
#: totals:     python devtools/quality/publication_authority.py summary
REGENERATE = "python devtools/quality/publication_authority.py regenerate"

SITES = PA.scan()
FOUND = PA.manifest(SITES)
EXPECTED = json.loads(PA.MANIFEST.read_text())


def _by_context(rows: list[dict]) -> dict:
    return {r["context"]: r for r in rows}


def _where(row: dict) -> str:
    return (f"{row['path']}::{row['qualname']}::{row['semantic']}#{row['ordinal']} "
            f"recv={row['receiver']!r}")


# the resolver's own completeness - a manifest of an incomplete scan proves nothing


def test_the_scan_is_complete_before_it_is_compared():
    problems = PA.completeness_problems(SITES)
    assert problems == [], (
        "the inventory cannot be trusted as a contract while these hold:\n  "
        + "\n  ".join(problems))


# drift, reported per record


def test_no_publication_site_was_added_without_reconciling_the_manifest():
    added = [r for c, r in _by_context(FOUND).items() if c not in _by_context(EXPECTED)]
    assert added == [], (
        "these publications exist in source but not in the reviewed manifest:\n  "
        + "\n  ".join(f"{_where(r)} -> {r['authority']} / {r['lifecycle']} "
                      f"roots={r['roots']}" for r in added)
        + f"\n\nIf that is intended, review them and run:\n  {REGENERATE}")


def test_no_manifest_record_lost_its_source_site():
    removed = [r for c, r in _by_context(EXPECTED).items() if c not in _by_context(FOUND)]
    assert removed == [], (
        "the manifest still records these publications but the scanner no longer finds them:\n  "
        + "\n  ".join(f"{_where(r)} -> {r['authority']} / {r['lifecycle']}" for r in removed)
        + f"\n\nIf they were genuinely removed, run:\n  {REGENERATE}")


def test_no_record_changed_its_authority_lifecycle_gating_or_roots():
    found, expected = _by_context(FOUND), _by_context(EXPECTED)
    changed = []
    for context in sorted(set(found) & set(expected)):
        a, b = found[context], expected[context]
        for field in ("authority", "lifecycle", "gated_required", "roots",
                      "receiver", "method"):
            if a[field] != b[field]:
                changed.append(f"{_where(a)}\n      {field}: expected {b[field]!r}, "
                               f"found {a[field]!r}")
    assert changed == [], (
        "these publications were reclassified:\n  " + "\n  ".join(changed)
        + f"\n\nIf the new classification is correct, run:\n  {REGENERATE}")


# structural guards the per-record diff alone would not catch


def test_every_syntactic_candidate_has_at_least_one_context_record():
    by_site: dict = {}
    for s in SITES:
        by_site.setdefault(s.canonical, []).append(s)
    empty = sorted(c for c, rs in by_site.items() if not rs)
    assert empty == [], f"these candidates resolved to no context at all: {empty}"


def test_a_polymorphic_site_keeps_every_one_of_its_contexts():
    # one arm of a multi-context site disappearing is silent loss, not a classification change
    expected_arms: dict = {}
    for r in EXPECTED:
        key = f"{r['path']}::{r['qualname']}::{r['semantic']}#{r['ordinal']}"
        expected_arms.setdefault(key, set()).add((r["authority"], r["lifecycle"]))
    found_arms: dict = {}
    for r in FOUND:
        key = f"{r['path']}::{r['qualname']}::{r['semantic']}#{r['ordinal']}"
        found_arms.setdefault(key, set()).add((r["authority"], r["lifecycle"]))
    lost = {k: sorted(v - found_arms.get(k, set()))
            for k, v in expected_arms.items() if v - found_arms.get(k, set())}
    assert lost == {}, f"these sites lost a reachable context: {lost}"


def test_no_two_records_share_a_context_identity():
    seen: dict = {}
    for r in FOUND:
        seen.setdefault(r["context"], 0)
        seen[r["context"]] += 1
    dupes = {c: n for c, n in seen.items() if n > 1}
    assert dupes == {}, f"context identity is not unique: {dupes}"


def test_the_protocol_vocabulary_still_matches_the_inventory():
    # a vocabulary change that nobody reconciled would silently shrink or grow discovery
    vocab = set(PA.semantic_vocabulary().values())
    used = {r["semantic"] for r in EXPECTED}
    assert used <= vocab, (
        f"the manifest records events the protocols no longer declare: {sorted(used - vocab)}")


def test_no_execution_owned_publication_uses_the_plain_contract():
    offenders = [_where(r) for r in FOUND
                 if r["lifecycle"] == PA.L_EXECUTION and r["authority"] != PA.GATED]
    assert offenders == [], f"these run under a claim but publish ungated: {offenders}"


def test_the_post_ownership_closings_are_never_gated():
    closings = [r for r in FOUND if r["semantic"] == "closing"
                and r["lifecycle"] in (PA.L_TERMINAL, PA.L_MAINTENANCE)]
    assert len(closings) == 6, f"expected six ungated closing sites, found {len(closings)}"
    # the SPELLING matters too: a plain publisher has no `aclosing` to call
    gated = [_where(r) for r in closings
             if r["gated_required"] or r["authority"] == PA.GATED
             or r["method"].startswith("a")]
    assert gated == [], (
        "these publish precisely when the claim is gone; gating them makes them "
        f"unpublishable: {gated}")


# determinism


def test_serialization_is_byte_identical_for_unchanged_source():
    assert PA.serialize(SITES) == PA.serialize(PA.scan())
    assert PA.serialize(SITES) == PA.MANIFEST.read_text(), (
        f"the committed manifest is not what this source serializes to; run:\n  {REGENERATE}")


def test_checking_never_rewrites_the_manifest():
    before = PA.MANIFEST.read_bytes()
    PA.completeness_problems(PA.scan())
    PA.manifest(PA.scan())
    assert PA.MANIFEST.read_bytes() == before, "a check wrote to the manifest"
