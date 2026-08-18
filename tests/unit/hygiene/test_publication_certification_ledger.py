# Responsibility: Verify the certification ledger equals the manifest execution set.
# Boundaries: the ledger's shape and manifest agreement; the behavioural run is the integration tier's.
from __future__ import annotations

import json
from pathlib import Path

import pytest

QUALITY = Path(__file__).parents[3] / "devtools" / "quality"
MANIFEST = QUALITY / "publication_authority_manifest.json"
LEDGER = QUALITY / "publication_certification_ledger.json"

#: The retired historical rows, by the lifecycle the resolver now gives them. They are kept as
#: explanation and must never re-enter the execution denominator.
RETIRED_LIFECYCLE = "post-ownership-terminal"


@pytest.fixture(scope="module")
def ledger() -> dict:
    return json.loads(LEDGER.read_text())


@pytest.fixture(scope="module")
def execution_contexts() -> set[str]:
    return {f"{r['path']}::{r['qualname']}::{r['semantic']}#{r['ordinal']}"
            for r in json.loads(MANIFEST.read_text()) if r["lifecycle"] == "execution-owned"}


def test_the_ledger_is_exactly_the_manifests_execution_owned_set(ledger, execution_contexts):
    # THE fail-closed hinge: the manifest decides which contexts exist. A context added, removed
    # or renamed there makes this fail until the ledger is reconciled - it cannot drift quietly.
    rows = {r["canonical_id"] for r in ledger["rows"]}
    assert rows == execution_contexts, (
        "the ledger and the manifest disagree:\n"
        f"  in the ledger only  : {sorted(rows - execution_contexts)}\n"
        f"  in the manifest only: {sorted(execution_contexts - rows)}")


def test_the_ledgers_own_totals_describe_its_own_rows(ledger):
    # The counts are stored fields, so they can disagree with the rows they claim to summarise -
    # a ledger reporting "62 proven" over 40 rows would read as complete. WHICH rows must exist is
    # the manifest's decision and is checked above; this only refuses a header that lies about the
    # body. No literal totals here on purpose: pinning them adds nothing over the row-by-row checks
    # and turns every legitimate publication into a hand-edited number.
    rows = ledger["rows"]
    assert ledger["denominator"] == len(rows)
    assert ledger["proven"] == sum(1 for r in rows if r["status"] == "proven")
    assert ledger["unproven"] == sum(1 for r in rows if r["status"] != "proven")
    assert ledger["proven"] + ledger["unproven"] == len(rows)


def test_every_row_is_behaviourally_proven_by_a_named_scenario(ledger):
    for row in ledger["rows"]:
        assert row["status"] == "proven", f"{row['canonical_id']} is {row['status']}"
        scenario = row.get("proof_scenario", "")
        assert scenario and isinstance(scenario, str), \
            f"{row['canonical_id']} names no executed scenario"
        # a scenario identity, not a prose justification or a count
        assert ":" in scenario and " " not in scenario, \
            f"{row['canonical_id']} cites {scenario!r}, which is not a scenario identity"


def test_no_identity_appears_twice(ledger):
    ids = [r["canonical_id"] for r in ledger["rows"]]
    # A site added after the reconciliation carries no alias: there is no historical row for it to
    # claim, and giving it one would silently take an identity that belongs to a different site.
    aliases = [r["historical_alias"] for r in ledger["rows"] if r["historical_alias"]]
    assert len(set(ids)) == len(ids), "a canonical identity is listed more than once"
    assert len(set(aliases)) == len(aliases), "a historical row is claimed more than once"
    assert [r["historical_alias"] for r in ledger["rows"] if r["mapping"] == "new"] == [""], \
        "a new site claimed a historical alias"


def test_the_relocated_row_maps_one_to_one(ledger):
    # E024 is the same product announcement, moved from the worker-thread tool boundary to the
    # async executor boundary. It counts once, and its old identity is not also carried.
    relocated = [r for r in ledger["rows"] if r["mapping"] == "relocated"]
    assert len(relocated) == 1, f"{len(relocated)} relocated rows, not one"
    row = relocated[0]
    assert row["historical_alias"] == "E024"
    assert row["historical_id"] == "agents/builder/tools/meshing.py::run_mesh::meshing#1"
    assert row["canonical_id"] == (
        "agents/builder/executor.py::BuilderToolExecutor._run_announced_mesh::meshing#1")
    assert row["historical_id"] not in {r["canonical_id"] for r in ledger["rows"]}


def test_the_retired_terminal_rows_are_explained_and_excluded(ledger, execution_contexts):
    retired = ledger["historical_reconciliation"]["retired"]
    assert {r["historical_alias"] for r in retired} == {"E034", "E035"}
    for row in retired:
        assert row["current_lifecycle"] == RETIRED_LIFECYCLE
        assert row["reason"], f"{row['historical_alias']} is retired without an explanation"
        assert row["historical_id"] not in execution_contexts, \
            f"{row['historical_alias']} is back in the execution set"
        assert row["historical_id"] not in {r["canonical_id"] for r in ledger["rows"]}


def test_the_manifest_agrees_that_the_retired_rows_are_terminal(ledger):
    # Not merely absent from the execution set - positively classified as terminal by the
    # resolver, so nothing was dropped to make a total work.
    records = json.loads(MANIFEST.read_text())
    uploader = [r for r in records if r["path"] == "application/artifact_uploader.py"
                and r["qualname"] == "deliver_succeeded_run"]
    assert uploader, "the artifact uploader publications vanished from the manifest"
    for record in uploader:
        assert record["lifecycle"] != "execution-owned", (
            f"{record['qualname']}::{record['semantic']} is execution-owned again; the retirement "
            "was a resolver result, not a bookkeeping choice")


def test_every_historical_row_is_accounted_for_exactly_once(ledger):
    reconciliation = ledger["historical_reconciliation"]
    # Sites added since the reconciliation are not part of it. They are subtracted here rather
    # than folded into a bucket, so the historical 63 keeps meaning exactly what it meant.
    new = reconciliation["new_since_reconciliation"]
    assert new == sum(1 for r in ledger["rows"] if r["mapping"] == "new")
    counted = (len(ledger["rows"]) - new + reconciliation["retired_to_terminal_authority"])
    assert counted == reconciliation["historical_execution_rows"] == 63, (
        f"{counted} rows accounted for against {reconciliation['historical_execution_rows']} "
        "historical rows")
    assert (reconciliation["relocated_one_to_one"] + reconciliation["renamed_in_place"]
            + reconciliation["unchanged"] + new) == len(ledger["rows"])


def test_the_historical_aggregate_is_preserved_without_being_inferred(ledger):
    reconciliation = ledger["historical_reconciliation"]
    assert reconciliation["historical_result"] == "50 proven / 13 unproven / 63 rows"
    # The per-row historical split was never recorded. Saying so is the honest answer; inventing
    # one would be inferring promotion from an aggregate.
    assert reconciliation["per_row_historical_status"] == "unrecorded"
    assert reconciliation["per_row_historical_status_note"]


def test_a_refused_attempt_cannot_be_cited_as_proof(ledger):
    # Stale controls are named for what they are. A row proved by one would mean a refused
    # publication had promoted it, which is the one thing coverage may never count.
    for row in ledger["rows"]:
        scenario = row["proof_scenario"]
        assert not any(word in scenario for word in ("stale", "superseded", "refused")), (
            f"{row['canonical_id']} cites {scenario!r}, which is a refusal control")
