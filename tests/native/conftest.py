# Responsibility: Configure the native tier's collection and record its provenance.
# Boundaries: collection concerns only.
from __future__ import annotations

import os

import pytest


# The native terminal tier drops and clears application tables. It may only do that to a database
# provisioned for the current run, proven from the live server: the marker stored inside the
# database, the database this connection is actually in, and the cluster it belongs to. A tier
# configured with a PostgreSQL endpoint but no proof stops here, before any fixture body runs.
def pytest_collection(session):
    if not os.getenv("POSTGRES_HOST"):
        return                      # the service-free native tiers reach no database at all
    from tests import disposable_database as dd
    from tests import harness_provisioning as hp

    import meshpipeline.settings.providers as provcfg

    try:
        hp.set_session_authority(dd.authorize(provcfg.POSTGRES_DSN))
    except dd.NotDisposableError as exc:
        raise pytest.UsageError(
            f"the native tier refuses to run against this database.\n\n{exc}\n\n"
            "It clears application tables between engines. Run it through "
            "`make test-native-terminal`, which provisions a database for the run and stamps it.",
        ) from None


def pytest_collection_modifyitems(config, items):
    for item in items:
        item.add_marker(pytest.mark.native)


@pytest.fixture(scope="session")
def canonical_provenance():
    # Production provenance: the mesh image must run the INSTALLED distribution (site/dist-packages),
    # not a source checkout on PYTHONPATH, and report a real release version. The version is not
    # pinned to a literal here so it never restates the single authority (meshpipeline.__init__).
    import meshpipeline
    ver = meshpipeline.__version__
    assert ver and ver[0].isdigit(), f"unexpected product version {ver!r}"
    assert "site-packages" in meshpipeline.__file__ or "dist-packages" in meshpipeline.__file__, \
        f"meshpipeline is not running from an installed distribution: {meshpipeline.__file__}"
    return meshpipeline.__file__, ver
