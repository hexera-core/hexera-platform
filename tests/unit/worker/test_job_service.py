# Responsibility: Verify quota checks fire in order, a job read is owner-scoped, and a signed URL is cached till expiry.
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.providers as provcfg
from meshpipeline.application.job_service import JobService, _signed_url_cache


def _make_svc():
    return JobService()


def _mock_db():
    return AsyncMock()



async def test_check_quotas_passes_when_under_limits():
    svc = _make_svc()
    db  = _mock_db()
    with (
        patch("meshpipeline.application.job_service.job_repo.count_active_for_owner", new=AsyncMock(return_value=0)),
        patch("meshpipeline.application.job_service.job_repo.count_total_active",     new=AsyncMock(return_value=0)),
        patch.object(polcfg, "MAX_JOBS_PER_OWNER",  2),
        patch.object(polcfg, "MAX_CONCURRENT_JOBS", 10),
    ):
        await svc.check_quotas(db, "user-1")


async def test_check_quotas_raises_when_user_limit_reached():
    svc = _make_svc()
    db  = _mock_db()
    with (
        patch("meshpipeline.application.job_service.job_repo.count_active_for_owner", new=AsyncMock(return_value=2)),
        patch("meshpipeline.application.job_service.job_repo.count_total_active",     new=AsyncMock(return_value=2)),
        patch.object(polcfg, "MAX_JOBS_PER_OWNER",  2),
        patch.object(polcfg, "MAX_CONCURRENT_JOBS", 10),
    ):
        with pytest.raises(ValueError, match="active job"):
            await svc.check_quotas(db, "user-1")


async def test_check_quotas_raises_when_global_limit_reached():
    svc = _make_svc()
    db  = _mock_db()
    with (
        patch("meshpipeline.application.job_service.job_repo.count_active_for_owner", new=AsyncMock(return_value=0)),
        patch("meshpipeline.application.job_service.job_repo.count_total_active",     new=AsyncMock(return_value=10)),
        patch.object(polcfg, "MAX_JOBS_PER_OWNER",  2),
        patch.object(polcfg, "MAX_CONCURRENT_JOBS", 10),
    ):
        with pytest.raises(ValueError, match="capacity"):
            await svc.check_quotas(db, "user-1")


async def test_check_quotas_user_limit_checked_before_global():
    svc = _make_svc()
    db  = _mock_db()
    count_total = AsyncMock(return_value=99)
    with (
        patch("meshpipeline.application.job_service.job_repo.count_active_for_owner", new=AsyncMock(return_value=2)),
        patch("meshpipeline.application.job_service.job_repo.count_total_active",     new=count_total),
        patch.object(polcfg, "MAX_JOBS_PER_OWNER",  2),
        patch.object(polcfg, "MAX_CONCURRENT_JOBS", 10),
    ):
        with pytest.raises(ValueError, match="active job"):
            await svc.check_quotas(db, "user-1")



async def test_get_job_returns_none_for_missing_job():
    svc = _make_svc()
    db  = _mock_db()
    with patch("meshpipeline.application.job_service.job_repo.get_for_owner", new=AsyncMock(return_value=None)):
        result = await svc.get_job(db, uuid.uuid4(), "any-owner")
    assert result is None


async def test_get_job_passes_the_callers_owner_into_the_query():
    svc = _make_svc()
    db  = _mock_db()
    seen = {}

    async def _scoped(_db, job_id, owner_id, *, organization_id=""):
        seen["owner"] = owner_id
        return None                       # a foreign job is simply absent

    with patch("meshpipeline.application.job_service.job_repo.get_for_owner", new=_scoped):
        result = await svc.get_job(db, uuid.uuid4(), "other-owner")

    assert result is None
    assert seen["owner"] == "other-owner", "the caller's owner was not used to scope the query"


async def test_get_job_returns_job_when_owner_matches():
    svc = _make_svc()
    db  = _mock_db()
    _id = uuid.uuid4()
    mock_job = MagicMock()
    mock_job.owner_id = "correct-owner"
    with patch("meshpipeline.application.job_service.job_repo.get_for_owner", new=AsyncMock(return_value=mock_job)):
        result = await svc.get_job(db, _id, "correct-owner")
    assert result is mock_job



async def test_signed_url_cache_hit_skips_the_store():
    svc = _make_svc()
    _signed_url_cache.clear()

    key = "test/artifact.stl"
    _signed_url_cache[key] = ("https://cached-url", time.monotonic() + 3600)

    mock_store = MagicMock()
    with patch("meshpipeline.contracts.object_storage.get_object_store", return_value=mock_store):
        url = await svc.signed_url(key)

    assert url == "https://cached-url"
    mock_store.create_download_url.assert_not_called()

    _signed_url_cache.clear()


async def test_signed_url_cache_miss_calls_the_store_and_caches():
    svc = _make_svc()
    _signed_url_cache.clear()

    key = "test/artifact-new.stl"
    mock_store = MagicMock()
    mock_store.create_download_url.return_value = "https://fresh-url"

    with (
        patch("meshpipeline.contracts.object_storage.get_object_store", return_value=mock_store),
        patch.object(provcfg, "MINIO_SIGNED_URL_TTL", 600),
    ):
        url = await svc.signed_url(key)

    assert url == "https://fresh-url"
    mock_store.create_download_url.assert_called_once()

    assert key in _signed_url_cache
    cached_url, expiry = _signed_url_cache[key]
    assert cached_url == "https://fresh-url"
    assert expiry > time.monotonic()
    assert expiry < time.monotonic() + 600

    _signed_url_cache.clear()


async def test_signed_url_expired_entry_refreshes():
    svc = _make_svc()
    _signed_url_cache.clear()

    key = "test/expired.stl"
    _signed_url_cache[key] = ("https://old-url", time.monotonic() - 1)

    mock_store = MagicMock()
    mock_store.create_download_url.return_value = "https://new-url"

    with (
        patch("meshpipeline.contracts.object_storage.get_object_store", return_value=mock_store),
        patch.object(provcfg, "MINIO_SIGNED_URL_TTL", 600),
    ):
        url = await svc.signed_url(key)

    assert url == "https://new-url"
    mock_store.create_download_url.assert_called_once()

    _signed_url_cache.clear()


# THE LINK'S LIFE SCALES WITH THE FILE. A flat 15 minutes was shorter than a slow download of a
# 900 MB FEA deck, and a browser's resume of a dropped download is checked against the same link.

def test_a_small_file_keeps_the_configured_life():
    from meshpipeline.application.job_service import download_link_ttl
    with patch.object(provcfg, "MINIO_SIGNED_URL_TTL", 900):
        assert download_link_ttl(5_000_000) == 900
        assert download_link_ttl(0) == 900
        assert download_link_ttl(None) == 900


def test_a_multi_hundred_megabyte_file_gets_one_to_two_hours():
    from meshpipeline.application.job_service import download_link_ttl
    with patch.object(provcfg, "MINIO_SIGNED_URL_TTL", 900):
        assert download_link_ttl(500_000_000) == 4000            # about 67 minutes
        assert download_link_ttl(900_000_000) == 7200            # two hours
        assert 3600 <= download_link_ttl(450_000_000) <= 7200


def test_no_link_outlives_the_ceiling():
    from meshpipeline.application.job_service import DOWNLOAD_LINK_MAX_TTL_S, download_link_ttl
    with patch.object(provcfg, "MINIO_SIGNED_URL_TTL", 900):
        assert download_link_ttl(50_000_000_000) == DOWNLOAD_LINK_MAX_TTL_S == 12 * 3600


def test_the_life_grows_with_the_file():
    from meshpipeline.application.job_service import download_link_ttl
    with patch.object(provcfg, "MINIO_SIGNED_URL_TTL", 900):
        sizes = [0, 10**6, 10**8, 3 * 10**8, 10**9, 10**10, 10**11]
        lives = [download_link_ttl(s) for s in sizes]
        assert lives == sorted(lives)


async def test_the_listed_link_is_signed_for_the_files_size_as_an_attachment():
    svc = _make_svc()
    _signed_url_cache.clear()
    mock_store = MagicMock()
    mock_store.create_download_url.return_value = "https://deck-url"
    with (
        patch("meshpipeline.contracts.object_storage.get_object_store", return_value=mock_store),
        patch.object(provcfg, "MINIO_SIGNED_URL_TTL", 900),
    ):
        await svc.signed_url("jobs/j/deck.inp", 900_000_000)
    kw = mock_store.create_download_url.call_args.kwargs
    assert kw["expires_in"].total_seconds() == 7200
    assert kw["download_name"] == "deck.inp"
    _signed_url_cache.clear()


async def test_a_cached_link_is_reused_only_while_half_its_life_remains():
    # The old cache handed out a link with one minute left; a page that loaded it then had one
    # minute before every Download button on it returned 403.
    svc = _make_svc()
    _signed_url_cache.clear()
    mock_store = MagicMock()
    mock_store.create_download_url.side_effect = ["https://first", "https://second"]
    with (
        patch("meshpipeline.contracts.object_storage.get_object_store", return_value=mock_store),
        patch.object(provcfg, "MINIO_SIGNED_URL_TTL", 900),
    ):
        t0 = time.monotonic()
        with patch("meshpipeline.application.job_service.time.monotonic", return_value=t0):
            assert await svc.signed_url("k/a.zip", 1_000) == "https://first"
        with patch("meshpipeline.application.job_service.time.monotonic", return_value=t0 + 400):
            assert await svc.signed_url("k/a.zip", 1_000) == "https://first"
        with patch("meshpipeline.application.job_service.time.monotonic", return_value=t0 + 451):
            assert await svc.signed_url("k/a.zip", 1_000) == "https://second"
    _signed_url_cache.clear()


async def test_a_fresh_link_is_never_served_from_the_cache():
    svc = _make_svc()
    _signed_url_cache.clear()
    _signed_url_cache["k/deck.inp"] = ("https://cached", time.monotonic() + 3600)
    mock_store = MagicMock()
    mock_store.create_download_url.return_value = "https://minted-now"
    with (
        patch("meshpipeline.contracts.object_storage.get_object_store", return_value=mock_store),
        patch.object(provcfg, "MINIO_SIGNED_URL_TTL", 900),
    ):
        url, ttl = await svc.fresh_download_link("k/deck.inp", 600_000_000)
    assert url == "https://minted-now" and ttl == 4800
    mock_store.stat_object.assert_not_called()
    _signed_url_cache.clear()


async def test_a_fresh_link_for_an_unmeasurable_file_still_gets_the_floor():
    svc = _make_svc()
    mock_store = MagicMock()
    mock_store.stat_object.side_effect = RuntimeError("store down")
    mock_store.create_download_url.return_value = "https://minted-now"
    with (
        patch("meshpipeline.contracts.object_storage.get_object_store", return_value=mock_store),
        patch.object(provcfg, "MINIO_SIGNED_URL_TTL", 900),
    ):
        url, ttl = await svc.fresh_download_link("k/deck.inp", None)
    assert url == "https://minted-now" and ttl == 900
