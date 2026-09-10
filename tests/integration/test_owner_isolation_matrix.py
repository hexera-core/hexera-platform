# Responsibility: Verify every owner-scoped route refuses a foreign identity and discloses no existence.
# Boundaries: the HTTP identity boundary; what each route then does for its rightful owner is elsewhere.
from __future__ import annotations

import hashlib
import hmac
import os
import socket
import subprocess
import sys
import time
import uuid

import pytest

pytestmark = pytest.mark.asyncio

if not os.getenv("DATABASE_URL") or not os.getenv("MINIO_ENDPOINT"):
    pytest.skip("real PostgreSQL and MinIO endpoints are required", allow_module_level=True)

_API_KEY = "isolation-api-key"
_USER_SECRET = "isolation-user-secret"
_A = "tenant-alpha"
_B = "tenant-bravo"

#: A STEP-shaped body. The upload boundary only needs a plausible suffix and non-empty content to
#: reach the owner-scoped write; the geometry itself is not what this suite is about.
_BODY = b"ISO-10303-21;\n" + bytes((i * 7 + 3) % 251 for i in range(4000))


def _headers(owner: str, *, sign: bool = True, wrong_sig: bool = False) -> dict:
    h = {"X-API-Key": _API_KEY, "X-User-Id": owner}
    if wrong_sig:
        h["X-User-Sig"] = "0" * 64
    elif sign:
        h["X-User-Sig"] = hmac.new(_USER_SECRET.encode(), owner.encode(),
                                   hashlib.sha256).hexdigest()
    return h


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    # THE HARDENED CONFIGURATION, which is the only one where owner identity means anything: both
    # secrets set, so X-User-Id must be signed. The dev path where identity is self-asserted is a
    # different contract and is covered by the unit tier.
    import httpx
    tmp = tmp_path_factory.mktemp("isolation")
    port = _free_port()
    log = tmp / "api.log"
    fh = log.open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "meshpipeline.runtime.api_server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=tmp,
        # MAX_CONCURRENT_JOBS is a WHOLE-SYSTEM quota and this tier shares one database, so jobs
        # left active by unrelated suites refuse this suite's one upload for capacity: measured at
        # 48 active against a limit of 20, which errored all eight tests here at setup for a reason
        # that has nothing to do with owner identity. The other suites that reach the real upload
        # route raise it in-process; this one runs the API in a subprocess, so it is raised the only
        # way that process will see it. Only the system-wide limit is raised - the per-owner one
        # still applies to these tenants, and quotas are proven in their own suite.
        env={**os.environ, "MESH_API_KEY": _API_KEY, "USER_TOKEN_SECRET": _USER_SECRET,
             "JOBS_DIR": str(tmp / "staging"), "MAX_CONCURRENT_JOBS": "1000000"},
        stdout=fh, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"API exited early:\n{log.read_text()[-3000:]}")
        try:
            httpx.get(f"{base}/health", timeout=1.0)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail("API never became reachable")
    try:
        yield base, log
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        fh.close()


async def _client(base):
    import httpx
    return httpx.AsyncClient(base_url=base, timeout=60.0)


@pytest.fixture(scope="module")
def alphas_session(api):
    # One real resource owned by A, created through the real upload route.
    import httpx
    base, _ = api
    r = httpx.post(f"{base}/api/v1/upload/step-file", headers=_headers(_A),
                   files={"file": ("part.step", _BODY, "application/octet-stream")},
                   timeout=60.0)
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


# identity itself

async def test_every_owner_scoped_route_refuses_an_unsigned_identity(api, alphas_session):
    # WITHOUT the signature the caller is only asserting a name. In the hardened configuration that
    # is refused before the route runs, which is what stops a valid API key from impersonating.
    base, _ = api
    async with await _client(base) as c:
        calls = [
            ("GET", f"/api/v1/chat/history/{alphas_session}", None),
            ("POST", "/api/v1/chat/message", {"session_id": alphas_session, "content": "hi"}),
            ("GET", f"/api/v1/simulation/{uuid.uuid4()}", None),
            ("GET", f"/api/v1/simulation/{uuid.uuid4()}/surface", None),
            ("POST", f"/api/v1/simulation/{uuid.uuid4()}/dispute", {"comment": "x"}),
            ("POST", "/api/v1/ws/ticket", {"job_id": str(uuid.uuid4())}),
        ]
        for method, path, body in calls:
            r = await c.request(method, path, headers=_headers(_A, sign=False), json=body)
            assert r.status_code == 401, f"{method} {path} accepted an unsigned identity: {r.status_code}"
            r = await c.request(method, path, headers=_headers(_A, wrong_sig=True), json=body)
            assert r.status_code == 401, f"{method} {path} accepted a bad signature: {r.status_code}"


async def test_the_upload_route_refuses_an_unsigned_identity(api):
    base, _ = api
    async with await _client(base) as c:
        for hdr in (_headers(_A, sign=False), _headers(_A, wrong_sig=True)):
            r = await c.post("/api/v1/upload/step-file", headers=hdr,
                             files={"file": ("p.step", _BODY, "application/octet-stream")})
            assert r.status_code == 401, r.status_code


# cross-owner access to a resource that really exists

async def test_a_foreign_owner_cannot_read_a_session_that_exists(api, alphas_session):
    base, _ = api
    async with await _client(base) as c:
        mine = await c.get(f"/api/v1/chat/history/{alphas_session}", headers=_headers(_A))
        assert mine.status_code == 200, mine.text

        theirs = await c.get(f"/api/v1/chat/history/{alphas_session}", headers=_headers(_B))
        assert theirs.status_code == 404, theirs.status_code
        # NON-DISCLOSURE: B must not be able to tell "exists but not yours" from "does not exist".
        unknown = await c.get(f"/api/v1/chat/history/{uuid.uuid4()}", headers=_headers(_B))
        assert theirs.status_code == unknown.status_code
        assert theirs.json() == unknown.json(), (
            "a foreign session answers differently from an unknown one, which discloses existence")


async def _provisioned_owner(db, *, org_name: str = "", organization_id=None):
    # A REAL account, unlike _A/_B above: tenant-alpha and tenant-bravo are self-asserted header
    # identities with no `users` row, so `_organization_for` resolves them to "" and every
    # assertion above exercises tenant_scope's OWNER fallback, never its organisation branch. This
    # helper is what lets a test reach the other branch: a genuine user, in a genuine
    # organisation, so the Principal the route sees carries a real, non-empty organization_id.
    #
    # `organization_id`, when given, joins an EXISTING organisation instead of minting a new one -
    # this is what lets a test provision a second, distinct owner who is nonetheless a TEAMMATE:
    # the one variable that isolates "scoped on the organisation" from "scoped on the owner" is
    # holding the organisation fixed while the owner changes.
    from meshpipeline.persistence.models import MembershipRole
    from meshpipeline.persistence.repositories.membership_repository import MembershipRepository
    from meshpipeline.persistence.repositories.organization_repository import (
        OrganizationRepository,
    )
    from meshpipeline.persistence.repositories.user_repository import UserRepository

    suffix = uuid.uuid4().hex[:12]
    email = f"{org_name or 'org'}-{suffix}@example.com"
    if organization_id is None:
        org = await OrganizationRepository().create(db, name=org_name or "org",
                                                     slug=f"org-{suffix}")
        organization_id = org.id
    user = await UserRepository().create(db, email=email, name="", firebase_uid=f"uid-{suffix}")
    await MembershipRepository().create(db, user_id=user.id, organization_id=organization_id,
                                        role=MembershipRole.owner)
    # COMMITTED HERE, not left for fixture teardown: the API subprocess reads through its own
    # connection, so the row must be visible before the HTTP calls below are made, not after the
    # test function returns.
    await db.commit()
    return email, organization_id


async def _org_scoped_session(api, db):
    # THE ONE PAIR OF IDENTITIES that isolates the variable: org_a_teammate shares NOTHING with
    # the session's own owner_id except the organisation, and org_b_owner shares NOTHING with it
    # except being a different owner_id in a different organisation. Only testing both - one
    # succeeding, one refused - proves a read keys on organization_id rather than on owner_id: a
    # regression to plain owner_id matching would 404 BOTH of them (the foreign-organisation
    # caller is, after all, also a different owner_id), and only the teammate case would catch
    # that regression.
    base, _ = api
    org_a_owner, org_a_id = await _provisioned_owner(db, org_name="org-a")
    org_a_teammate, _ = await _provisioned_owner(db, organization_id=org_a_id)
    org_b_owner, _ = await _provisioned_owner(db, org_name="org-b")

    async with await _client(base) as c:
        upload = await c.post("/api/v1/upload/step-file", headers=_headers(org_a_owner),
                              files={"file": ("part.step", _BODY, "application/octet-stream")})
        assert upload.status_code == 200, upload.text
        session_id = upload.json()["session_id"]

    return session_id, org_a_owner, org_a_teammate, org_b_owner


async def test_a_teammate_in_the_same_organisation_can_read_a_session_that_exists(api, db):
    # THE POSITIVE HALF of the pair - see _org_scoped_session. Reachable ONLY if the read scopes
    # on organization_id: org_a_teammate is a genuinely different owner_id from the session's own,
    # so under a regression to owner-only matching this would 404, which is exactly the failure
    # this test exists to catch. The pre-existing test_a_foreign_owner_cannot_read_a_session_
    # that_exists already proves owner_id isolation on its own; this is what the organisation
    # widens beyond it.
    base, _ = api
    session_id, org_a_owner, org_a_teammate, _ = await _org_scoped_session(api, db)
    async with await _client(base) as c:
        mine = await c.get(f"/api/v1/chat/history/{session_id}", headers=_headers(org_a_owner))
        assert mine.status_code == 200, mine.text

        teammates = await c.get(f"/api/v1/chat/history/{session_id}",
                                headers=_headers(org_a_teammate))
        assert teammates.status_code == 200, teammates.text


async def test_a_foreign_organisation_cannot_read_a_session_that_exists(api, db):
    # THE NEGATIVE HALF of the pair - see _org_scoped_session and the teammate case above for what
    # the two together prove.
    base, _ = api
    session_id, org_a_owner, _, org_b_owner = await _org_scoped_session(api, db)
    async with await _client(base) as c:
        mine = await c.get(f"/api/v1/chat/history/{session_id}", headers=_headers(org_a_owner))
        assert mine.status_code == 200, mine.text

        theirs = await c.get(f"/api/v1/chat/history/{session_id}", headers=_headers(org_b_owner))
        assert theirs.status_code == 404, theirs.status_code
        # NON-DISCLOSURE, exactly as the foreign-owner case above: an unknown session must answer
        # identically to a real one that belongs to another organisation.
        unknown = await c.get(f"/api/v1/chat/history/{uuid.uuid4()}", headers=_headers(org_b_owner))
        assert theirs.status_code == unknown.status_code
        assert theirs.json() == unknown.json(), (
            "a foreign-organisation session answers differently from an unknown one, "
            "which discloses existence")


async def test_a_foreign_owner_cannot_post_into_a_session_that_exists(api, alphas_session):
    base, _ = api
    async with await _client(base) as c:
        r = await c.post("/api/v1/chat/message", headers=_headers(_B),
                         json={"session_id": alphas_session, "content": "let me in"})
        assert r.status_code in (403, 404), r.status_code
        assert "tenant-alpha" not in r.text, "the refusal named the owning tenant"


# random identifiers on every remaining owner-scoped route

@pytest.mark.parametrize("path_tmpl,method,body", [
    ("/api/v1/simulation/{jid}", "GET", None),
    ("/api/v1/simulation/{jid}/surface", "GET", None),
    ("/api/v1/simulation/{jid}/dispute", "POST", {"comment": "rebuild it"}),
    ("/api/v1/ws/ticket", "POST", None),
])
async def test_a_random_identifier_is_refused_without_disclosing_anything(
        api, path_tmpl, method, body):
    base, _ = api
    jid = str(uuid.uuid4())
    payload = {"job_id": jid} if path_tmpl.endswith("ticket") else body
    async with await _client(base) as c:
        r = await c.request(method, path_tmpl.format(jid=jid), headers=_headers(_B), json=payload)
        assert r.status_code in (403, 404), f"{path_tmpl} -> {r.status_code}"
        low = r.text.lower()
        for leak in ("sources/", "postgresql", "traceback", "minio", "bucket"):
            assert leak not in low, f"{path_tmpl} refusal leaked {leak!r}: {r.text[:200]}"


async def test_no_refusal_wrote_a_credential_or_key_into_the_log(api):
    _, log = api
    text = log.read_text()
    for secret in (_USER_SECRET, _API_KEY):
        assert secret not in text, f"the API log contains {secret!r}"
    assert "Traceback" not in text, "an owner refusal produced a traceback in the log"


# headers, caching and forwarded-host handling

async def test_a_ticket_response_is_never_cached(api, alphas_session):
    # A ticket is single-use. If an intermediary or the browser cached the response, a replayed
    # request would hand back a string the store has already consumed.
    base, _ = api
    async with await _client(base) as c:
        r = await c.post("/api/v1/ws/ticket", headers=_headers(_A),
                         json={"job_id": str(uuid.uuid4())})
        cache = (r.headers.get("cache-control") or "").lower()
        assert r.status_code in (404, 200), r.status_code
        if r.status_code == 200:
            assert "no-store" in cache or "no-cache" in cache, (
                f"a single-use ticket response is cacheable: cache-control={cache!r}")


async def test_security_headers_are_present_on_api_responses(api):
    base, _ = api
    async with await _client(base) as c:
        r = await c.get("/health")
        assert r.headers.get("x-content-type-options", "").lower() == "nosniff"
        assert (r.headers.get("x-frame-options") or "").upper() in ("DENY", "SAMEORIGIN")


async def test_a_forged_forwarded_host_does_not_reach_the_response(api):
    # The API is expected to sit behind a proxy it trusts. A caller-supplied Host or
    # X-Forwarded-Host must not be reflected into a redirect or body, which is what turns a
    # forwarded header into a poisoning primitive.
    base, _ = api
    async with await _client(base) as c:
        r = await c.get("/health", headers={"X-Forwarded-Host": "evil.example",
                                            "X-Forwarded-Proto": "http"})
        assert "evil.example" not in r.text, "a forged X-Forwarded-Host was reflected into the body"
        assert "evil.example" not in (r.headers.get("location") or ""), \
            "a forged X-Forwarded-Host reached a redirect target"


async def test_metrics_exposes_no_identity_or_credential(api, alphas_session):
    base, _ = api
    async with await _client(base) as c:
        r = await c.get("/metrics")
        if r.status_code != 200:
            return                       # metrics is optional in this deployment shape
        body = r.text
        for leak in (_USER_SECRET, _API_KEY, "sources/", "postgresql://"):
            assert leak not in body, f"/metrics exposed {leak!r}"


# the chat dispatch path, for an owner who really has an organisation

#: The approved configuration a seeded snapshot carries. The intake conversation that would
#: normally produce one needs a model provider this tier does not run, so the gate is seeded with
#: exactly the shape `agents/intake/approval` writes - see test_intake_approval_postgres.py.
_APPROVED_PATCHES = [{"name": "inlet", "type": "inlet"}, {"name": "outlet", "type": "outlet"},
                     {"name": "wall", "type": "wall"}]
_APPROVED_PAYLOAD = {"domain": "duct", "request_txt": "r" * 120, "review_brief_txt": "b" * 90,
                     "dimensionality": "3D", "purpose": "internal_cfd",
                     "input_kind": "fluid-domain", "mesh_engine": "gmsh",
                     "mesh_fidelity": "standard", "engine_source": "user_direct",
                     "engine_params": {"element_order": "2"}, "patches": _APPROVED_PATCHES}


async def _approvable_session(base, db, owner: str, organization_id) -> uuid.UUID:
    """Upload real geometry for `owner`, then seed the confirmed unit and the live approval."""
    import httpx

    import meshpipeline.agents.intake.admission_token as at
    import meshpipeline.agents.intake.approval as ap
    import meshpipeline.agents.intake.engine_selection as es
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )
    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    r = httpx.post(f"{base}/api/v1/upload/step-file", headers=_headers(owner),
                   files={"file": ("part.step", _BODY, "application/octet-stream")},
                   timeout=60.0)
    assert r.status_code == 200, r.text
    session_id = uuid.UUID(r.json()["session_id"])

    sessions = SessionRepository()
    db.expire_all()
    row = await sessions.get_internal(db, session_id)
    assert row is not None and row.geometry_source_id, "the upload wrote no geometry source"

    # A synthetic STEP body declares no unit, so the session has no interpretation and the
    # approval transaction would refuse it. Stamped with the organisation, as upload.py does.
    recorded = await GeometryInterpretationRepository().record(
        db, owner_id=owner, geometry_source_id=row.geometry_source_id,
        unit=LengthUnit.millimetre, basis=ResolutionBasis.user_confirmed,
        evidence="seeded by the isolation matrix", organization_id=str(organization_id))
    await sessions.bind_geometry_interpretation(db, session_id,
                                                uuid.UUID(recorded.interpretation_id))

    user_msgs = len([m for m in (row.messages or []) if m.get("role") == "user"])
    selection = es.select_from_structured_input("gmsh", session_id=str(session_id),
                                                owner_id=owner, revision="r1")
    canonical = at.canonical_payload("gmsh", "internal_cfd", "fluid-domain", "3D",
                                     _APPROVED_PATCHES, {"element_order": "2"})
    intent = at.approved_intent_canonical(
        engine="gmsh", purpose="internal_cfd", input_kind="fluid-domain", dimensionality="3D",
        patches=_APPROVED_PATCHES, engine_params={"element_order": "2"},
        requested_mesh_fidelity="standard", request_txt=_APPROVED_PAYLOAD["request_txt"])
    snapshot = ap.create(
        owner_id=owner, session_id=str(session_id), selection_id=selection["id"], token_id="tok",
        canonical=canonical, fingerprint=at.fingerprint(canonical), intent_canonical=intent,
        intent_fingerprint=at.fingerprint(intent), payload=_APPROVED_PAYLOAD,
        summary=at.CONFIRM_REQUIREMENTS_ASK, proposal_revision="r1",
        proposal_msg_count=user_msgs)
    await sessions.set_intake_gate(db, session_id,
                                   {"selection": selection, "admission": None,
                                    "approval": snapshot})
    await db.commit()
    return session_id


async def test_a_job_approved_in_chat_is_readable_by_the_owner_who_approved_it(api, db):
    # THE PRIMARY PRODUCT FLOW, for the only identity that can catch this: a provisioned owner.
    # Every other approval test in the tree signs in as a self-asserted header name with no
    # `users` row, whose organisation resolves to "" - so the job is stamped owner_id-only AND
    # read back owner_id-only, and the two agree by accident. Give the caller a real organisation
    # and the write and the read no longer agree: `job_repo.create` in the approval transaction
    # must stamp organization_id, because `get_for_owner` compares on it, and NULL = <uuid> is
    # NULL. Without the stamp the user approves a run and then cannot see the job they started.
    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    base, _ = api
    owner, org_id = await _provisioned_owner(db, org_name="chat-approval")
    session_id = await _approvable_session(base, db, owner, org_id)

    async with await _client(base) as c:
        approved = await c.post("/api/v1/chat/message", headers=_headers(owner),
                                json={"session_id": str(session_id),
                                      "content": "yes, proceed"})

    # The LAUNCH needs a broker this tier does not run, so a 500 from the enqueue step is an
    # expected outcome here and is deliberately not asserted on: the job row is created, linked
    # and COMMITTED before any launch is attempted, which is the row this test is about.
    db.expire_all()
    linked = await SessionRepository().get_internal(db, session_id)
    assert linked.job_id is not None, (
        f"the chat approval created no job at all: {approved.status_code} {approved.text}")

    async with await _client(base) as c:
        status = await c.get(f"/api/v1/simulation/{linked.job_id}", headers=_headers(owner))
    assert status.status_code == 200, (
        "the owner who approved this run cannot read the job it created "
        f"({status.status_code}) - the approval transaction did not stamp organization_id, so "
        "the org-scoped read matches nothing")


async def test_a_job_approved_in_chat_carries_its_organisation(api, db):
    # The same defect stated as the column it lives in, so a regression names its own cause
    # rather than only its symptom. An unstamped row is also what would block 0005's NOT NULL.
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    base, _ = api
    owner, org_id = await _provisioned_owner(db, org_name="chat-approval-stamp")
    session_id = await _approvable_session(base, db, owner, org_id)

    async with await _client(base) as c:
        await c.post("/api/v1/chat/message", headers=_headers(owner),
                     json={"session_id": str(session_id), "content": "yes, proceed"})

    db.expire_all()
    linked = await SessionRepository().get_internal(db, session_id)
    assert linked.job_id is not None
    job = await JobRepository().get_internal(db, linked.job_id)
    assert str(job.organization_id) == str(org_id), (
        f"the approved job is stamped organization_id={job.organization_id!r}, expected "
        f"{org_id!r}: writes must stamp BOTH columns")
