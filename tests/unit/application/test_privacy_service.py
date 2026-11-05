# Responsibility: Verify erasure and export cover every owner-scoped table and refuse what they must.
from __future__ import annotations

import pytest

from meshpipeline.application import privacy_service
from meshpipeline.persistence import models
from meshpipeline.persistence.session import Base


def test_every_table_with_an_owner_id_is_covered():
    # A table added with an owner_id that erasure does not rewrite keeps the person's address
    # after they asked for it to be gone, and one the export does not read is an incomplete
    # access request. Both walk OWNER_SCOPED, so this is the one list to keep current.
    owned = {mapper.class_ for mapper in Base.registry.mappers
             if "owner_id" in mapper.columns}
    assert owned == set(privacy_service.OWNER_SCOPED), (
        f"add to or remove from OWNER_SCOPED: {sorted(c.__name__ for c in owned ^ set(privacy_service.OWNER_SCOPED))}")


def test_every_owner_scoped_table_also_carries_the_organisation():
    for model in privacy_service.OWNER_SCOPED:
        assert hasattr(model, "organization_id"), model.__name__


def test_the_pseudonym_names_the_organisation_and_no_person():
    assert privacy_service.erased_owner("org-1") == "erased:org-1"


def test_an_export_never_carries_a_credential_hash():
    assert "key_hash" in privacy_service._EXPORT_EXCLUDED_COLUMNS
    assert "key_hash" in {c.key for c in models.ApiKey.__mapper__.column_attrs}


@pytest.mark.asyncio
async def test_an_account_with_no_organisation_cannot_be_erased_here():
    with pytest.raises(privacy_service.ErasureRefused):
        await privacy_service.request_erasure(None, owner_id="a@example.com", organization_id="")
