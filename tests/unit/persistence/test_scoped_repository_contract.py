# Responsibility: Verify the repository double satisfies the scoped contract, and a permissive one would not.
from __future__ import annotations

from types import SimpleNamespace

import pytest
from tests.tenant_repositories import ScopedRepoDouble, scoped_repo_contract

OWNED = SimpleNamespace(id="job-owned", owner_id="owner-a")
FOREIGN = SimpleNamespace(id="job-foreign", owner_id="owner-b")


def _double():
    return ScopedRepoDouble({OWNED.id: OWNED, FOREIGN.id: FOREIGN})


@pytest.mark.parametrize(
    "name,check",
    scoped_repo_contract(_double, OWNED, FOREIGN, "job-does-not-exist"),
    ids=lambda v: v if isinstance(v, str) else "",
)
async def test_the_double_satisfies_the_scoped_contract(name, check):
    await check()


async def test_the_double_records_which_seam_was_used():
    repo = _double()
    await repo.get_for_owner(None, OWNED.id, OWNED.owner_id)
    assert repo.scoped_calls and not repo.internal_calls


async def test_a_permissive_double_would_fail_this_contract():
    class Permissive(ScopedRepoDouble):
        async def get_for_owner(self, db, object_id, owner_id):
            return self.rows.get(object_id)          # ignores ownership, as the old ones did

    checks = dict(scoped_repo_contract(
        lambda: Permissive({OWNED.id: OWNED, FOREIGN.id: FOREIGN}),
        OWNED, FOREIGN, "nope"))
    with pytest.raises(AssertionError):
        await checks["foreign row is absent"]()
