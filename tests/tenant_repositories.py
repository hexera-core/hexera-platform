# Responsibility: Be the one way tests stand in for a tenant-scoped repository.
# Boundaries: it enforces the owner scoping the real repositories do, so a test cannot pass by ignoring tenancy.
from __future__ import annotations


class ScopedRepoDouble:

    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.internal_calls: list = []
        self.scoped_calls: list = []

    # request-facing
    async def get_for_owner(self, db, object_id, owner_id):
        self.scoped_calls.append((object_id, owner_id))
        row = self.rows.get(object_id)
        # the owner predicate is part of the query: a foreign row is simply not found
        return row if row is not None and getattr(row, "owner_id", None) == owner_id else None

    # trusted internal
    async def get_internal(self, db, object_id):
        self.internal_calls.append(object_id)
        return self.rows.get(object_id)

    # convenience for tests that only ever hold one row
    @classmethod
    def holding(cls, row):
        return cls({row.id: row})


def scoped_repo_contract(make_double, owned, foreign, unknown_id):
    async def owned_row_is_found():
        repo = make_double()
        assert await repo.get_for_owner(None, owned.id, owned.owner_id) is owned

    async def foreign_row_is_absent():
        repo = make_double()
        assert await repo.get_for_owner(None, foreign.id, owned.owner_id) is None

    async def unknown_row_is_absent():
        repo = make_double()
        assert await repo.get_for_owner(None, unknown_id, owned.owner_id) is None

    async def foreign_and_unknown_are_indistinguishable():
        repo = make_double()
        a = await repo.get_for_owner(None, foreign.id, owned.owner_id)
        b = await repo.get_for_owner(None, unknown_id, owned.owner_id)
        assert a is b is None

    async def internal_lookup_ignores_ownership():
        repo = make_double()
        assert await repo.get_internal(None, foreign.id) is foreign

    return [
        ("owned row is found", owned_row_is_found),
        ("foreign row is absent", foreign_row_is_absent),
        ("unknown row is absent", unknown_row_is_absent),
        ("foreign and unknown are indistinguishable", foreign_and_unknown_are_indistinguishable),
        ("internal lookup ignores ownership", internal_lookup_ignores_ownership),
    ]
