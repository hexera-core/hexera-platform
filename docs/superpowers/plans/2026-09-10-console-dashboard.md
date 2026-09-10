# Console Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the single-page console into a sidebar dashboard with a route per concern, wearing hexera.ai's visual system, and add the five read endpoints that architecture needs.

**Architecture:** Phase 1 adds collection endpoints to a product API that is currently entirely fetch-by-id, each scoped through the existing `tenant_scope.scope()` predicate and paginated keyset-style. Phase 2 ports hexera.ai's CSS primitives into the shared `ui/` stylesheet tree. Phase 3 restructures the Next.js console into `(auth)` and `(dashboard)` route groups over those endpoints. Phase 4 links the marketing site to the console.

**Tech Stack:** FastAPI + SQLAlchemy 2 async + Alembic (no migration this cycle), Next.js 16 App Router + Auth.js 5 beta + Firebase Web SDK 12, plain CSS in `ui/css/`, pytest (`asyncio_mode = "auto"`), `node --test` via tsx.

**Spec:** [`docs/superpowers/specs/2026-09-10-console-dashboard-design.md`](../specs/2026-09-10-console-dashboard-design.md)

## Global Constraints

- **No migration.** Five endpoints, four repository methods, one service method. Schema is untouched. (Spec §2, decision 9.)
- **Both UI trees stay byte-identical.** Every edit under `ui/` is applied identically to `apps/console/public/static/`. `tests/unit/deploy/test_console_ui_copy_parity.py` gates this; `index.html` is the only exempt file.
- **Every tenant-scoped read goes through `tenant_scope.scope(model, owner_id=, organization_id=)`.** Never hand-write an `owner_id ==` filter in a new read.
- **Pagination is keyset on `(created_at, id)` DESC.** `limit` defaults to 25, clamps to 100. Never `OFFSET`.
- **`organization_name` is honoured only on the provision path** in `account_service.resolve_or_provision`. (Decision 6.)
- **API-key credentials may not manage API keys.** `principal.credential is Credential.api_key` → 403. (Decision 7.)
- **Fonts, exactly:** `Saira+Semi+Condensed:wght@300;400;500;600`, `Geist:wght@300;400;500;600`, `Geist+Mono:wght@400;500`.
- **Palette:** `--gold: #ff4f00`, `--gold-ink: #1a0a02`, steel `#57708f`, canvas base `#0d0d0c`.
- **Label voice:** Geist Mono 500, `text-transform: uppercase`, `letter-spacing: 0.16em` for every label, eyebrow, nav item, table header and status word.
- **No canvas in the console.** `.page-bg` + `.grain` only. Never import `fluid.js`.
- **Route tests call the handler function directly** with monkeypatched services, following `tests/unit/api/test_credits_route.py`. Do not stand up an HTTP client.
- Run the Python suite with `pytest tests/unit/...`; run console tests from `apps/console/` with `pnpm test`.

---

# Phase 1 — Backend reads

## Task 1: Keyset pagination helper and the run list

**Files:**
- Create: `src/meshpipeline/api/pagination.py`
- Modify: `src/meshpipeline/persistence/repositories/job_repository.py` (add `list_for_owner` after `get_for_owner`, around line 36)
- Modify: `src/meshpipeline/api/v1/simulation.py` (add a collection route above `@router.get("/{job_id}")` at line 122)
- Create: `src/meshpipeline/api/schemas/listing.py`
- Test: `tests/unit/api/test_pagination.py`, `tests/unit/api/test_simulation_list_route.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str`
  - `decode_cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | None`
  - `clamp_limit(limit: int) -> int`
  - `Page` dataclass with fields `items: list`, `next_cursor: str | None`
  - `JobRepository.list_for_owner(db, owner_id, *, organization_id="", limit=25, before=None) -> list[tuple[SimulationJob, str | None]]` — the second tuple element is the joined `ChatSession.task_label`
  - `simulation.list_jobs(limit=25, cursor=None, owner_id=..., organization_id=...) -> dict`

**Why the route ordering matters:** FastAPI matches in declaration order, and `@router.get("/{job_id}")` already exists. A collection route declared *after* it is never reached — `""` would be captured. Declare `@router.get("")` first.

- [ ] **Step 1: Write the failing cursor test**

```python
# tests/unit/api/test_pagination.py
# Responsibility: Verify the cursor round-trips and refuses anything it did not mint.
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from meshpipeline.api import pagination

ROW = uuid.UUID("33333333-3333-3333-3333-333333333333")
AT = datetime(2026, 9, 10, 12, 30, 45, tzinfo=UTC)


def test_a_cursor_round_trips_to_the_values_it_was_built_from():
    assert pagination.decode_cursor(pagination.encode_cursor(AT, ROW)) == (AT, ROW)


def test_an_absent_cursor_decodes_to_none_rather_than_raising():
    assert pagination.decode_cursor(None) is None
    assert pagination.decode_cursor("") is None


def test_a_cursor_this_module_did_not_mint_decodes_to_none():
    # A caller can put anything in a query string. A malformed cursor must read as "start at the
    # beginning" rather than 500 a proven caller -- the same narrowing tenant_scope applies to a
    # malformed organisation id.
    for junk in ("not-base64", "!!!!", "YWJj", pagination.encode_cursor(AT, ROW)[:-4]):
        assert pagination.decode_cursor(junk) is None


def test_the_limit_clamps_to_the_documented_bounds():
    assert pagination.clamp_limit(25) == 25
    assert pagination.clamp_limit(0) == 1
    assert pagination.clamp_limit(-5) == 1
    assert pagination.clamp_limit(1000) == 100
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `pytest tests/unit/api/test_pagination.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'meshpipeline.api.pagination'`

- [ ] **Step 3: Write the pagination helper**

```python
# src/meshpipeline/api/pagination.py
# Responsibility: Encode and decode the opaque cursor every collection route paginates on.
# Owns: the rule that a cursor this module did not mint reads as "no cursor", never as an error.
# Boundaries: strings and tuples; which table is being paged belongs to the repository.
from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime

#: The page size a caller gets without asking, and the ceiling it cannot exceed. The ceiling is
#: what stops one request scanning an entire tenant's history.
DEFAULT_LIMIT = 25
MAX_LIMIT = 100

_SEPARATOR = "|"


@dataclass(frozen=True)
class Page:
    """One page of rows plus the cursor that reaches the next one.

    `next_cursor` is None on the last page. It is set only when the page came back FULL: a short
    page cannot have more behind it, and minting a cursor there would give the caller one more
    round trip that returns nothing.
    """

    items: list
    next_cursor: str | None


def clamp_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}{_SEPARATOR}{row_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    """The values a cursor names, or None for anything this module did not mint.

    FAILS TO None, NEVER TO AN EXCEPTION. A cursor arrives in a query string, so any caller can
    send any bytes; raising here would turn a typo in a URL into a 500 for a proven caller. Reading
    a bad cursor as "start at the beginning" narrows rather than widens - the caller sees their own
    first page, never somebody else's rows, because the tenant predicate is applied independently.
    """
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    head, separator, tail = raw.partition(_SEPARATOR)
    if not separator:
        return None
    try:
        return datetime.fromisoformat(head), uuid.UUID(tail)
    except (ValueError, AttributeError):
        return None
```

- [ ] **Step 4: Run it to make sure it passes**

Run: `pytest tests/unit/api/test_pagination.py -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/api/pagination.py tests/unit/api/test_pagination.py
git commit -m "feat(api): add the keyset cursor every collection route will page on"
```

- [ ] **Step 6: Write the failing route test**

```python
# tests/unit/api/test_simulation_list_route.py
# Responsibility: Verify the run list is the caller's own, newest first, and paged.
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from meshpipeline.api import pagination
from meshpipeline.api.v1 import simulation

pytestmark = pytest.mark.asyncio

OWNER = "engineer@example.com"
OTHER = "stranger@example.com"
BASE = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


@dataclass
class _Job:
    id: uuid.UUID
    status: str
    created_at: datetime
    ended_at: datetime | None = None
    current_attempt: int = 1
    failed_reason: str | None = None


def _rows(owner: str, count: int):
    return [
        (_Job(id=uuid.uuid4(), status="succeeded", created_at=BASE - timedelta(hours=n)),
         f"{owner} run {n}")
        for n in range(count)
    ]


@pytest.fixture
def listing(monkeypatch):
    store = {OWNER: _rows(OWNER, 3), OTHER: _rows(OTHER, 3)}
    seen: dict = {}

    async def fake_list(db, owner_id, *, organization_id="", limit=25, before=None):
        seen["limit"] = limit
        seen["before"] = before
        seen["organization_id"] = organization_id
        return store.get(owner_id, [])[:limit]

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(simulation.job_repo, "list_for_owner", fake_list)
    monkeypatch.setattr(simulation, "get_db", lambda: _NullSession())
    return store, seen


async def test_the_list_is_the_callers_own_runs(listing):
    store, _ = listing
    payload = await simulation.list_jobs(owner_id=OWNER, organization_id="")
    assert [item["id"] for item in payload["items"]] == [str(job.id) for job, _ in store[OWNER]]


async def test_another_owners_runs_are_not_reachable(listing):
    payload = await simulation.list_jobs(owner_id="nobody@example.com", organization_id="")
    assert payload["items"] == []
    assert payload["next_cursor"] is None


async def test_the_task_label_rides_along_from_the_joined_session(listing):
    payload = await simulation.list_jobs(owner_id=OWNER, organization_id="")
    assert payload["items"][0]["task_label"] == f"{OWNER} run 0"


async def test_a_full_page_offers_a_cursor_to_the_next_one(listing):
    payload = await simulation.list_jobs(owner_id=OWNER, organization_id="", limit=3)
    assert payload["next_cursor"] is not None
    assert pagination.decode_cursor(payload["next_cursor"]) is not None


async def test_a_short_page_offers_no_cursor(listing):
    # Three rows exist and four were asked for, so there is provably nothing behind this page.
    payload = await simulation.list_jobs(owner_id=OWNER, organization_id="", limit=4)
    assert payload["next_cursor"] is None


async def test_the_limit_is_clamped_before_it_reaches_the_repository(listing):
    _, seen = listing
    await simulation.list_jobs(owner_id=OWNER, organization_id="", limit=100_000)
    assert seen["limit"] == pagination.MAX_LIMIT


async def test_a_malformed_cursor_reads_as_the_first_page_rather_than_failing(listing):
    _, seen = listing
    await simulation.list_jobs(owner_id=OWNER, organization_id="", cursor="!!!not-a-cursor!!!")
    assert seen["before"] is None


async def test_the_organisation_reaches_the_repository_so_it_can_scope_on_it(listing):
    _, seen = listing
    await simulation.list_jobs(owner_id=OWNER, organization_id="11111111-1111-1111-1111-111111111111")
    assert seen["organization_id"] == "11111111-1111-1111-1111-111111111111"
```

- [ ] **Step 7: Run it to make sure it fails**

Run: `pytest tests/unit/api/test_simulation_list_route.py -v`
Expected: FAIL — `AttributeError: module 'meshpipeline.api.v1.simulation' has no attribute 'list_jobs'`

- [ ] **Step 8: Add the repository method**

Insert into `src/meshpipeline/persistence/repositories/job_repository.py`, immediately after `get_for_owner`. The file already imports `select` and `tenant_scope`; add `from sqlalchemy import tuple_` to its imports and `from meshpipeline.persistence.models import ChatSession` alongside the existing model imports.

```python
    async def list_for_owner(self, db: AsyncSession, owner_id: str, *,
                             organization_id: str = "", limit: int = 25,
                             before: tuple[datetime, uuid.UUID] | None = None
                             ) -> list[tuple[SimulationJob, str | None]]:
        """One page of this tenant's runs, newest first, each with its session's task label.

        THE JOIN IS OUTER on purpose: `chat_sessions.job_id` is nullable and set only once a
        conversation reaches a run, so an inner join would silently hide every job submitted
        outside the chat path. A run with no session reads as a null label, which the route
        renders as an em dash - not as a missing row.

        Keyset, not OFFSET: this list is append-mostly and read newest-first, where OFFSET skips
        or repeats rows as new runs arrive between one page and the next.
        """
        statement = (
            select(SimulationJob, ChatSession.task_label)
            .outerjoin(ChatSession, ChatSession.job_id == SimulationJob.id)
            .where(tenant_scope.scope(SimulationJob, owner_id=owner_id,
                                      organization_id=organization_id))
            .order_by(SimulationJob.created_at.desc(), SimulationJob.id.desc())
            .limit(limit))
        if before is not None:
            # ROW COMPARISON, not `created_at < x OR (created_at = x AND id < y)`. Postgres
            # compares the tuple lexicographically in one predicate, which is both correct at a
            # timestamp tie and the shape an index on (created_at, id) can serve.
            statement = statement.where(
                tuple_(SimulationJob.created_at, SimulationJob.id) < before)
        result = await db.execute(statement)
        return [(row[0], row[1]) for row in result.all()]
```

- [ ] **Step 9: Add the shared page serialiser**

```python
# src/meshpipeline/api/schemas/listing.py
# Responsibility: Give every collection route one envelope, so a client writes one pager.
# Boundaries: shape only - what a row contains belongs to the route that read it.
from __future__ import annotations

import uuid
from datetime import datetime

from meshpipeline.api import pagination


def page(items: list[dict], *, limit: int, last_key: tuple[datetime, uuid.UUID] | None) -> dict:
    """The one response shape every list route answers with.

    A cursor is minted ONLY for a page that came back full. A short page cannot have more behind
    it, and offering a cursor there costs the caller a round trip that returns nothing.
    """
    full = len(items) == limit
    return {
        "items": items,
        "next_cursor": (pagination.encode_cursor(*last_key)
                        if full and last_key is not None else None),
    }
```

- [ ] **Step 10: Add the route**

In `src/meshpipeline/api/v1/simulation.py`, add `from meshpipeline.api import pagination`, `from meshpipeline.api.schemas import listing`, and `from meshpipeline.persistence.repositories.job_repository import JobRepository` to the imports, add `job_repo = JobRepository()` beside the existing `svc = JobService()`, and declare this route **above** `@router.get("/{job_id}")`:

```python
@router.get("")
async def list_jobs(limit: int = pagination.DEFAULT_LIMIT, cursor: str | None = None,
                    owner_id: str = Depends(owner_dep),
                    organization_id: str = Depends(org_dep)) -> dict:
    # DECLARED BEFORE `/{job_id}`. FastAPI matches in declaration order, so a collection route
    # placed after that one is unreachable - "" would be captured as a job id.
    bounded = pagination.clamp_limit(limit)
    async with get_db() as db:
        rows = await job_repo.list_for_owner(db, owner_id, organization_id=organization_id,
                                             limit=bounded,
                                             before=pagination.decode_cursor(cursor))
    items = [{
        "id": str(job.id),
        "status": getattr(job.status, "value", job.status),
        "task_label": task_label,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "ended_at": job.ended_at.isoformat() if job.ended_at else None,
        "attempts": job.current_attempt,
        "failed_reason": getattr(job.failed_reason, "value", job.failed_reason),
    } for job, task_label in rows]
    last = (rows[-1][0].created_at, rows[-1][0].id) if rows else None
    return listing.page(items, limit=bounded, last_key=last)
```

- [ ] **Step 11: Run the route tests**

Run: `pytest tests/unit/api/test_simulation_list_route.py -v`
Expected: PASS, 8 tests

- [ ] **Step 12: Run the whole API suite to check nothing regressed on route ordering**

Run: `pytest tests/unit/api -q`
Expected: PASS. If `test_api_simulation.py` fails with a 422 on a job-id route, the new route was declared below `/{job_id}` — move it above.

- [ ] **Step 13: Commit**

```bash
git add src/meshpipeline/api/v1/simulation.py src/meshpipeline/api/schemas/listing.py \
        src/meshpipeline/persistence/repositories/job_repository.py \
        tests/unit/api/test_simulation_list_route.py
git commit -m "feat(api): list a tenant's runs, newest first, keyset paged"
```

---

## Task 2: The conversation list

**Files:**
- Modify: `src/meshpipeline/persistence/repositories/session_repository.py` (add `list_for_owner` after `get_for_owner`, around line 32)
- Modify: `src/meshpipeline/api/v1/chat.py` (add a collection route above `@router.get("/history/{session_id}")` at line 25)
- Test: `tests/unit/api/test_chat_list_route.py`

**Interfaces:**
- Consumes: `pagination.clamp_limit`, `pagination.decode_cursor`, `pagination.DEFAULT_LIMIT`, `listing.page` from Task 1.
- Produces: `ChatSessionRepository.list_for_owner(db, owner_id, *, organization_id="", limit=25, before=None) -> list[ChatSession]`; `chat.list_sessions(limit=25, cursor=None, owner_id=..., organization_id=...) -> dict`.

**The repository class is `SessionRepository`**, and `chat.py` does not currently hold one at module scope — `get_chat_history` constructs it inside the function body with a local import. The test below monkeypatches `chat.session_repo`, so Step 3 must **hoist** that construction to module level:

```python
from meshpipeline.persistence.repositories.session_repository import SessionRepository

session_repo = SessionRepository()
```

and change `get_chat_history` to use the module-level instance, deleting its two local lines. This matches how `simulation.py` holds `svc = JobService()` and how `credits.py` reaches `credit_service`, and it is what makes both routes patchable from a test.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/api/test_chat_list_route.py
# Responsibility: Verify the conversation list is the caller's own and paged like every other.
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from meshpipeline.api import pagination
from meshpipeline.api.v1 import chat

pytestmark = pytest.mark.asyncio

OWNER = "engineer@example.com"
BASE = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


@dataclass
class _Session:
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    task_label: str | None = None
    job_id: uuid.UUID | None = None
    messages: list = None


@pytest.fixture
def listing(monkeypatch):
    rows = [
        _Session(id=uuid.uuid4(), created_at=BASE - timedelta(hours=n),
                 updated_at=BASE - timedelta(hours=n), task_label=f"study {n}",
                 messages=[{"role": "user", "content": "hi"}])
        for n in range(3)
    ]
    store = {OWNER: rows}
    seen: dict = {}

    async def fake_list(db, owner_id, *, organization_id="", limit=25, before=None):
        seen["limit"] = limit
        seen["before"] = before
        return store.get(owner_id, [])[:limit]

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(chat.session_repo, "list_for_owner", fake_list)
    monkeypatch.setattr(chat, "get_db", lambda: _NullSession())
    return rows, seen


async def test_the_list_is_the_callers_own_conversations(listing):
    rows, _ = listing
    payload = await chat.list_sessions(owner_id=OWNER, organization_id="")
    assert [item["id"] for item in payload["items"]] == [str(row.id) for row in rows]


async def test_another_owners_conversations_are_not_reachable(listing):
    payload = await chat.list_sessions(owner_id="nobody@example.com", organization_id="")
    assert payload["items"] == []


async def test_the_row_carries_a_message_count_not_the_messages(listing):
    # The list must not ship every transcript to render a table. A count is what the page needs;
    # the transcript is what /chat/history/{id} is for.
    payload = await chat.list_sessions(owner_id=OWNER, organization_id="")
    assert payload["items"][0]["message_count"] == 1
    assert "messages" not in payload["items"][0]


async def test_the_limit_is_clamped_before_it_reaches_the_repository(listing):
    _, seen = listing
    await chat.list_sessions(owner_id=OWNER, organization_id="", limit=100_000)
    assert seen["limit"] == pagination.MAX_LIMIT


async def test_a_malformed_cursor_reads_as_the_first_page(listing):
    _, seen = listing
    await chat.list_sessions(owner_id=OWNER, organization_id="", cursor="nonsense")
    assert seen["before"] is None
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `pytest tests/unit/api/test_chat_list_route.py -v`
Expected: FAIL — `AttributeError: module 'meshpipeline.api.v1.chat' has no attribute 'list_sessions'`

- [ ] **Step 3: Add the repository method**

Insert into `session_repository.py` after `get_for_owner`. Add `from sqlalchemy import tuple_` to its imports if absent.

```python
    async def list_for_owner(self, db: AsyncSession, owner_id: str, *,
                             organization_id: str = "", limit: int = 25,
                             before: tuple[datetime, uuid.UUID] | None = None
                             ) -> list[ChatSession]:
        """One page of this tenant's conversations, newest first.

        Ordered on `created_at` rather than `updated_at` even though the latter is what a user
        thinks of as recency: `updated_at` moves under the keyset, so a row edited mid-scroll
        would jump pages and could be served twice or skipped. Recency-ordering is a later
        decision that needs a stable sort key, not this one.
        """
        statement = (
            select(ChatSession)
            .where(tenant_scope.scope(ChatSession, owner_id=owner_id,
                                      organization_id=organization_id))
            .order_by(ChatSession.created_at.desc(), ChatSession.id.desc())
            .limit(limit))
        if before is not None:
            statement = statement.where(
                tuple_(ChatSession.created_at, ChatSession.id) < before)
        result = await db.execute(statement)
        return list(result.scalars().all())
```

- [ ] **Step 4: Hoist the repository and add the route**

In `chat.py`, add `from meshpipeline.api import pagination`, `from meshpipeline.api.schemas import listing`, and `from meshpipeline.api.security import org_dep` if absent. Hoist `session_repo` as described above, then declare this **above** `@router.get("/history/{session_id}")`:

```python
@router.get("")
async def list_sessions(limit: int = pagination.DEFAULT_LIMIT, cursor: str | None = None,
                        owner_id: str = Depends(owner_dep),
                        organization_id: str = Depends(org_dep)) -> dict:
    bounded = pagination.clamp_limit(limit)
    async with get_db() as db:
        rows = await session_repo.list_for_owner(db, owner_id, organization_id=organization_id,
                                                 limit=bounded,
                                                 before=pagination.decode_cursor(cursor))
    items = [{
        "id": str(row.id),
        "task_label": row.task_label,
        "job_id": str(row.job_id) if row.job_id else None,
        "message_count": len(row.messages or []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    } for row in rows]
    last = (rows[-1].created_at, rows[-1].id) if rows else None
    return listing.page(items, limit=bounded, last_key=last)
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/unit/api/test_chat_list_route.py tests/unit/api/test_api_chat.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/meshpipeline/api/v1/chat.py \
        src/meshpipeline/persistence/repositories/session_repository.py \
        tests/unit/api/test_chat_list_route.py
git commit -m "feat(api): list a tenant's conversations"
```

---

## Task 3: The credit ledger

**Files:**
- Modify: `src/meshpipeline/persistence/repositories/credit_ledger_repository.py` (add `list_for_org` after `balance`)
- Modify: `src/meshpipeline/application/credit_service.py` (add `history` after `balance`, line 39)
- Modify: `src/meshpipeline/api/v1/credits.py` (add `/history`)
- Test: `tests/unit/api/test_credits_history_route.py`

**Interfaces:**
- Consumes: `pagination.*` and `listing.page` from Task 1.
- Produces: `CreditLedgerRepository.list_for_org(db, *, organization_id: uuid.UUID, limit=25, before=None) -> list[CreditLedgerEntry]`; `credit_service.history(db, *, organization_id: uuid.UUID, limit=25, before=None) -> list[CreditLedgerEntry]`; `credits.read_history(limit=25, cursor=None, organization_id="") -> dict`.

**Existing behaviour to preserve:** `credits.read_balance` answers `{"balance": 0, "unit": "credits"}` for an absent *or malformed* organisation rather than raising. `/history` must degrade the same way — to an empty page.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/api/test_credits_history_route.py
# Responsibility: Verify the ledger is the caller's own organisation's, and degrades like the balance.
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from meshpipeline.api.v1 import credits

pytestmark = pytest.mark.asyncio

OWN_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")
BASE = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


@dataclass
class _Entry:
    id: uuid.UUID
    entry_type: str
    amount: int
    reason: str
    created_at: datetime


@pytest.fixture
def ledger(monkeypatch):
    store = {
        OWN_ORG: [_Entry(id=uuid.uuid4(), entry_type="grant", amount=500,
                         reason="signup", created_at=BASE - timedelta(hours=n))
                  for n in range(2)],
    }

    async def fake_history(db, *, organization_id, limit=25, before=None):
        return store.get(organization_id, [])[:limit]

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(credits.credit_service, "history", fake_history)
    monkeypatch.setattr(credits, "get_db", lambda: _NullSession())
    return store


async def test_the_ledger_is_the_callers_own(ledger):
    payload = await credits.read_history(organization_id=str(OWN_ORG))
    assert [entry["amount"] for entry in payload["items"]] == [500, 500]
    assert payload["items"][0]["entry_type"] == "grant"


async def test_another_organisations_ledger_is_not_reachable(ledger):
    payload = await credits.read_history(organization_id=str(OTHER_ORG))
    assert payload["items"] == []


async def test_a_caller_with_no_organisation_reads_an_empty_page_rather_than_failing(ledger):
    # The same degradation read_balance already ships: mid-migration is not a reason to 500.
    assert await credits.read_history(organization_id="") == {"items": [], "next_cursor": None}


async def test_a_malformed_organisation_reads_an_empty_page_rather_than_raising(ledger):
    assert await credits.read_history(organization_id="not-a-uuid") == {"items": [],
                                                                        "next_cursor": None}
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `pytest tests/unit/api/test_credits_history_route.py -v`
Expected: FAIL — `AttributeError: module 'meshpipeline.api.v1.credits' has no attribute 'read_history'`

- [ ] **Step 3: Add the repository method**

```python
    async def list_for_org(self, db: AsyncSession, *, organization_id: uuid.UUID,
                           limit: int = 25,
                           before: tuple[datetime, uuid.UUID] | None = None
                           ) -> list[CreditLedgerEntry]:
        """One page of an organisation's movements, newest first.

        Served directly by `ix_credit_ledger_org_created`, which 0003 created for the balance
        query and this ordering alike - the reason decision 9 adds no index this cycle.

        Organisation-scoped ONLY, with no owner fallback: the ledger has no `owner_id` column,
        because a credit belongs to the tenant rather than to whoever happened to spend it.
        """
        statement = (
            select(CreditLedgerEntry)
            .where(CreditLedgerEntry.organization_id == organization_id)
            .order_by(CreditLedgerEntry.created_at.desc(), CreditLedgerEntry.id.desc())
            .limit(limit))
        if before is not None:
            statement = statement.where(
                tuple_(CreditLedgerEntry.created_at, CreditLedgerEntry.id) < before)
        result = await db.execute(statement)
        return list(result.scalars().all())
```

Add `from sqlalchemy import tuple_` and `from datetime import datetime` to the file's imports if absent.

- [ ] **Step 4: Add the service method**

In `credit_service.py`, after `balance`:

```python
async def history(db: AsyncSession, *, organization_id: uuid.UUID, limit: int = 25,
                  before: tuple[datetime, uuid.UUID] | None = None) -> list:
    return await credit_ledger_repo.list_for_org(db, organization_id=organization_id,
                                                 limit=limit, before=before)
```

Add `from datetime import datetime` if absent, and use whatever name the module already binds the repository to.

- [ ] **Step 5: Add the route**

In `credits.py`, add `from meshpipeline.api import pagination` and `from meshpipeline.api.schemas import listing`:

```python
@router.get("/history")
async def read_history(limit: int = pagination.DEFAULT_LIMIT, cursor: str | None = None,
                       organization_id: Annotated[str, Depends(org_dep)] = "") -> dict:
    bounded = pagination.clamp_limit(limit)
    # THE SAME NARROWING read_balance applies. An absent or malformed organisation is a
    # deployment between the migration and the image that fills the column, not a bad request:
    # it reads as an empty ledger, never as a 500 for a caller who has already proven who it is.
    if not organization_id:
        return listing.page([], limit=bounded, last_key=None)
    try:
        organization = uuid.UUID(organization_id)
    except ValueError:
        return listing.page([], limit=bounded, last_key=None)

    async with get_db() as db:
        rows = await credit_service.history(db, organization_id=organization, limit=bounded,
                                            before=pagination.decode_cursor(cursor))
    items = [{
        "id": str(row.id),
        "entry_type": getattr(row.entry_type, "value", row.entry_type),
        # SIGNED, exactly as stored. A grant is positive and a debit negative, so a client sums
        # the column rather than branching on the type - the same reason the column is signed.
        "amount": row.amount,
        "reason": row.reason,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    } for row in rows]
    last = (rows[-1].created_at, rows[-1].id) if rows else None
    return listing.page(items, limit=bounded, last_key=last)
```

- [ ] **Step 6: Run the tests**

Run: `pytest tests/unit/api/test_credits_history_route.py tests/unit/api/test_credits_route.py -v`
Expected: PASS — the four pre-existing balance tests must still pass unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/meshpipeline/api/v1/credits.py src/meshpipeline/application/credit_service.py \
        src/meshpipeline/persistence/repositories/credit_ledger_repository.py \
        tests/unit/api/test_credits_history_route.py
git commit -m "feat(api): read an organisation's credit ledger"
```

---

## Task 4: The organisation and its members

**Files:**
- Modify: `src/meshpipeline/persistence/repositories/membership_repository.py` (add `list_members`)
- Create: `src/meshpipeline/api/v1/organization.py`
- Modify: `src/meshpipeline/api/v1/router.py` (mount it)
- Test: `tests/unit/api/test_organization_route.py`

**Interfaces:**
- Consumes: nothing from earlier tasks — this route is not paged. An organisation's membership is bounded by decision (one member today, and the invite flow is an explicit non-goal), so a page envelope would be ceremony.
- Produces: `MembershipRepository.list_members(db, *, organization_id: uuid.UUID) -> list[tuple[User, MembershipRole]]`; `organization.read_organization(owner_id=..., organization_id=...) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/api/test_organization_route.py
# Responsibility: Verify the organisation view is the caller's own and degrades to an owner view.
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from meshpipeline.api.v1 import organization

pytestmark = pytest.mark.asyncio

OWNER = "engineer@example.com"
OWN_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")


@dataclass
class _Org:
    id: uuid.UUID
    name: str
    slug: str


@dataclass
class _User:
    id: uuid.UUID
    email: str
    name: str


@pytest.fixture
def org(monkeypatch):
    row = _Org(id=OWN_ORG, name="Acme Aerospace", slug="org-abc-123")
    members = [(_User(id=uuid.uuid4(), email=OWNER, name="An Engineer"), "owner")]

    async def fake_get(db, organization_id):
        return row if organization_id == OWN_ORG else None

    async def fake_members(db, *, organization_id):
        return members if organization_id == OWN_ORG else []

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(organization.organization_repo, "get", fake_get)
    monkeypatch.setattr(organization.membership_repo, "list_members", fake_members)
    monkeypatch.setattr(organization, "get_db", lambda: _NullSession())
    return row


async def test_the_organisation_is_the_callers_own(org):
    payload = await organization.read_organization(owner_id=OWNER, organization_id=str(OWN_ORG))
    assert payload["organization"]["name"] == "Acme Aerospace"
    assert payload["organization"]["slug"] == "org-abc-123"
    assert payload["members"] == [{"email": OWNER, "name": "An Engineer", "role": "owner"}]


async def test_another_organisation_is_not_reachable(org):
    payload = await organization.read_organization(owner_id=OWNER, organization_id=str(OTHER_ORG))
    assert payload["organization"] is None
    assert payload["members"] == []


async def test_a_caller_with_no_organisation_sees_themselves_rather_than_an_error(org):
    # Decision 12: credits.py set the precedent that a mid-migration caller is degraded, not
    # refused. An empty members list would read as "your account vanished".
    payload = await organization.read_organization(owner_id=OWNER, organization_id="")
    assert payload["organization"] is None
    assert payload["members"] == [{"email": OWNER, "name": OWNER, "role": "owner"}]


async def test_a_malformed_organisation_degrades_the_same_way(org):
    payload = await organization.read_organization(owner_id=OWNER, organization_id="not-a-uuid")
    assert payload["members"] == [{"email": OWNER, "name": OWNER, "role": "owner"}]
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `pytest tests/unit/api/test_organization_route.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'meshpipeline.api.v1.organization'`

- [ ] **Step 3: Add the repository method**

In `membership_repository.py`. Add `from meshpipeline.persistence.models import User` to its imports:

```python
    async def list_members(self, db: AsyncSession, *, organization_id: uuid.UUID
                           ) -> list[tuple[User, MembershipRole]]:
        """Everyone who acts within this organisation, with the role they act in.

        Joined rather than two queries: the console renders name, address and role in one table,
        and a membership without its user is not a row anything can display.
        """
        result = await db.execute(
            select(User, Membership.role)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.organization_id == organization_id)
            .order_by(Membership.created_at.asc()))
        return [(row[0], row[1]) for row in result.all()]
```

Check whether `organization_repo` already exposes a `get(db, organization_id)`. If it does not, add it:

```python
    async def get(self, db: AsyncSession, organization_id: uuid.UUID) -> Organization | None:
        return await db.get(Organization, organization_id)
```

- [ ] **Step 4: Write the route**

```python
# src/meshpipeline/api/v1/organization.py
# Responsibility: Report which organisation the caller acts within, and who else does.
# Boundaries: reading only - nothing here creates, renames or invites, all of which are non-goals.
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from meshpipeline.api.security import org_dep, owner_dep
from meshpipeline.persistence.repositories.membership_repository import MembershipRepository
from meshpipeline.persistence.repositories.organization_repository import OrganizationRepository
from meshpipeline.persistence.session import get_db

router = APIRouter()

organization_repo = OrganizationRepository()
membership_repo = MembershipRepository()


@router.get("")
async def read_organization(owner_id: Annotated[str, Depends(owner_dep)] = "",
                            organization_id: Annotated[str, Depends(org_dep)] = "") -> dict:
    parsed = _parsed(organization_id)
    if parsed is None:
        # DECISION 12. An absent or malformed organisation is a deployment between 0004 and the
        # image that fills the column, or a lookup that could not run - the same states
        # credits.py answers 0 for. Answering with an empty member list would read to the person
        # whose account it is as though their account had vanished, so the caller is shown
        # themselves: the honest degraded view, and exactly what tenant_scope's owner fallback
        # means everywhere else.
        return {"organization": None,
                "members": [{"email": owner_id, "name": owner_id, "role": "owner"}]}

    async with get_db() as db:
        row = await organization_repo.get(db, parsed)
        members = await membership_repo.list_members(db, organization_id=parsed)

    return {
        "organization": ({"id": str(row.id), "name": row.name, "slug": row.slug}
                         if row else None),
        "members": [{"email": user.email, "name": user.name or user.email,
                     "role": getattr(role, "value", role)} for user, role in members],
    }


def _parsed(organization_id: str) -> uuid.UUID | None:
    if not organization_id:
        return None
    try:
        return uuid.UUID(organization_id)
    except (ValueError, AttributeError, TypeError):
        return None
```

- [ ] **Step 5: Mount it**

In `src/meshpipeline/api/v1/router.py`, add `organization` to the import list and:

```python
router.include_router(organization.router, prefix="/organization", tags=["organization"])
```

- [ ] **Step 6: Run the tests**

Run: `pytest tests/unit/api/test_organization_route.py -v && pytest tests/unit/api -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/meshpipeline/api/v1/organization.py src/meshpipeline/api/v1/router.py \
        src/meshpipeline/persistence/repositories/membership_repository.py \
        src/meshpipeline/persistence/repositories/organization_repository.py \
        tests/unit/api/test_organization_route.py
git commit -m "feat(api): read the caller's organisation and its members"
```

---

## Task 5: API key management, and the credential that may not do it

**Files:**
- Modify: `src/meshpipeline/application/api_key_service.py` (add `list_for_owner`; thread `organization_id` into `revoke`)
- Create: `src/meshpipeline/api/v1/api_keys.py`
- Modify: `src/meshpipeline/api/v1/router.py`
- Test: `tests/unit/api/test_api_keys_route.py`

**Interfaces:**
- Consumes: `principal_dep` and `Principal` from `api/security.py` and `contracts/identity.py`.
- Produces: `api_key_service.list_for_owner(db, *, owner_id, organization_id="") -> list[ApiKey]`; `api_keys.list_keys(principal)`, `api_keys.create_key(body, principal)`, `api_keys.revoke_key(key_id, principal)`.

**This is the security-critical task in the plan.** `resolve_principal` accepts `Authorization: Bearer hx_live_…` on every `/api/v1` route. Without the refusal below, a leaked key mints its own replacements and revoking the original accomplishes nothing.

**Two dependencies, not one, and this is not optional.** The route needs `principal.credential`, which `owner_dep` discards — but `tests/unit/hygiene/test_architecture_fitness.py::test_job_mutating_routes_require_authentication` walks the AST of every `api/` module and fails any `POST`/`PATCH`/`PUT`/`DELETE` handler without a parameter defaulted to `Depends(owner_dep)`. A router that took `principal_dep` alone would fail that gate, and the honest fix is to take both rather than to add an entry to `_UNAUTHENTICATED_ROUTE_ALLOWED` — these routes *are* authenticated. It costs nothing: `owner_dep` is itself `Depends(principal_dep)`, and FastAPI resolves a dependency once per request and shares it, so the credential is verified once either way. That is exactly what `owner_dep`'s own comment in `api/security.py` says.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/api/test_api_keys_route.py
# Responsibility: Verify keys are the caller's own, minted once, and unmanageable by a key.
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from meshpipeline.api.v1 import api_keys
from meshpipeline.contracts.identity import Credential, Principal

pytestmark = pytest.mark.asyncio

OWNER = "engineer@example.com"
ORG = "11111111-1111-1111-1111-111111111111"
NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


def _console_principal() -> Principal:
    return Principal(owner_id=OWNER, organization_id=ORG,
                     credential=Credential.signed_header)


def _key_principal() -> Principal:
    return Principal(owner_id=OWNER, organization_id=ORG, credential=Credential.api_key,
                     key_id=str(uuid.uuid4()))


@dataclass
class _Row:
    id: uuid.UUID
    name: str
    key_prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    expires_at: datetime | None


@dataclass
class _Issued:
    key_id: str
    owner_id: str
    name: str
    plan: str
    key_prefix: str
    secret: str
    presented: str
    expires_at: datetime | None


@pytest.fixture
def keys(monkeypatch):
    rows = [_Row(id=uuid.uuid4(), name="ci", key_prefix="hx_live_abc123def456",
                 created_at=NOW, last_used_at=None, revoked_at=None, expires_at=None)]
    revoked: list = []

    async def fake_list(db, *, owner_id, organization_id=""):
        return rows if owner_id == OWNER else []

    async def fake_issue(db, *, owner_id, name="", plan="", organization_id=None,
                         expires_at=None):
        return _Issued(key_id=str(uuid.uuid4()), owner_id=owner_id, name=name, plan=plan,
                       key_prefix="hx_live_newkey000000",
                       secret="s3cr3t", presented="hx_live_newkey000000_s3cr3t",
                       expires_at=None)

    async def fake_revoke(db, *, owner_id, key_id, organization_id="", now=None):
        revoked.append((owner_id, key_id))
        return key_id == rows[0].id

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(api_keys.api_key_service, "list_for_owner", fake_list)
    monkeypatch.setattr(api_keys.api_key_service, "issue", fake_issue)
    monkeypatch.setattr(api_keys.api_key_service, "revoke", fake_revoke)
    monkeypatch.setattr(api_keys, "get_db", lambda: _NullSession())
    return rows, revoked


async def test_the_list_is_the_callers_own_keys_and_carries_no_secret(keys):
    rows, _ = keys
    payload = await api_keys.list_keys(principal=_console_principal(), owner_id=OWNER)
    assert [item["key_prefix"] for item in payload["items"]] == [rows[0].key_prefix]
    serialised = repr(payload)
    assert "key_hash" not in serialised
    assert "secret" not in serialised


async def test_creating_a_key_returns_the_presented_secret_exactly_once(keys):
    payload = await api_keys.create_key(body=api_keys.CreateKeyIn(name="ci"),
                                        principal=_console_principal(), owner_id=OWNER)
    assert payload["presented"] == "hx_live_newkey000000_s3cr3t"
    # And the list that follows must not carry it.
    listed = await api_keys.list_keys(principal=_console_principal(), owner_id=OWNER)
    assert all("presented" not in item for item in listed["items"])


async def test_revoking_reports_whether_a_live_key_was_revoked(keys):
    rows, _ = keys
    assert await api_keys.revoke_key(key_id=rows[0].id, principal=_console_principal(),
                                     owner_id=OWNER) == {"revoked": True}
    assert await api_keys.revoke_key(key_id=uuid.uuid4(), principal=_console_principal(),
                                     owner_id=OWNER) == {"revoked": False}


async def test_an_api_key_credential_may_not_list_keys(keys):
    with pytest.raises(HTTPException) as caught:
        await api_keys.list_keys(principal=_key_principal(), owner_id=OWNER)
    assert caught.value.status_code == 403


async def test_an_api_key_credential_may_not_mint_a_key(keys):
    # THE ONE THAT MATTERS. Without this, a leaked key mints its own replacements and revoking
    # the original accomplishes nothing -- the key becomes permanent persistence.
    with pytest.raises(HTTPException) as caught:
        await api_keys.create_key(body=api_keys.CreateKeyIn(name="pivot"),
                                   principal=_key_principal(), owner_id=OWNER)
    assert caught.value.status_code == 403


async def test_an_api_key_credential_may_not_revoke_a_key(keys):
    # Revocation too: otherwise a leaked key revokes the owner's real keys as a denial of service.
    with pytest.raises(HTTPException) as caught:
        await api_keys.revoke_key(key_id=uuid.uuid4(), principal=_key_principal(), owner_id=OWNER)
    assert caught.value.status_code == 403


async def test_the_refusal_names_the_credential_rather_than_pretending_the_key_is_invalid(keys):
    # Deliberately NOT the undifferentiated 401 the rest of the auth surface answers with. This
    # discloses no account existence, and a caller acting on a valid key needs to be told the
    # operation wants a console session rather than left believing their key expired.
    with pytest.raises(HTTPException) as caught:
        await api_keys.create_key(body=api_keys.CreateKeyIn(name="x"),
                                   principal=_key_principal(), owner_id=OWNER)
    assert "console" in caught.value.detail.lower()
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `pytest tests/unit/api/test_api_keys_route.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'meshpipeline.api.v1.api_keys'`

- [ ] **Step 3: Add the service method and thread the organisation into revoke**

In `api_key_service.py`, add after `revoke`:

```python
async def list_for_owner(db: AsyncSession, *, owner_id: str,
                         organization_id: str = "") -> list:
    return await api_key_repo.list_for_owner(db, owner_id, organization_id=organization_id)
```

and change `revoke` to forward the organisation the repository already accepts:

```python
async def revoke(db: AsyncSession, *, owner_id: str, key_id: uuid.UUID,
                 organization_id: str = "", now: datetime | None = None) -> bool:
    # organization_id is forwarded so revocation scopes the same way the listing does. Without
    # it a key visible in the list (organisation-scoped) could be un-revokable (owner-scoped)
    # the moment an organisation holds more than one member, which `memberships` already allows.
    return await api_key_repo.revoke(db, owner_id=owner_id, key_id=key_id,
                                     organization_id=organization_id,
                                     at=now or _now())
```

- [ ] **Step 4: Write the route**

```python
# src/meshpipeline/api/v1/api_keys.py
# Responsibility: Let a console session mint, list and revoke the keys that act on its behalf.
# Owns: the rule that a key may not manage keys.
# Boundaries: transport over api_key_service; the credential's meaning belongs to api/security.py.
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from meshpipeline.api.security import owner_dep, principal_dep
from meshpipeline.application import api_key_service
from meshpipeline.contracts.identity import Credential, Principal
from meshpipeline.persistence.session import get_db

router = APIRouter()

#: THE REFUSAL, and it deliberately says something. Every other refusal on this API is
#: undifferentiated because the difference would disclose whether an account or a key exists.
#: This one discloses nothing of the sort - the caller has already proven a valid key - and a
#: person holding one needs to know the operation wants a console session rather than concluding
#: their key expired.
_KEY_CANNOT_MANAGE_KEYS = ("API keys cannot manage API keys. Sign in to the console to mint or "
                           "revoke a key.")


class CreateKeyIn(BaseModel):
    #: what the holder calls it. Display only, never used to find a key.
    name: str = Field(default="", max_length=128)


def _require_console_session(principal: Principal) -> None:
    """Refuse a caller acting on an API key.

    `resolve_principal` accepts `Authorization: Bearer hx_live_…` on EVERY /api/v1 route, so
    without this gate a leaked key mints its own replacements and revoking the original
    accomplishes nothing: the key becomes permanent, self-renewing persistence in the account.
    The same argument covers revocation, where a leaked key would instead revoke the owner's real
    keys as a denial of service. `Principal.credential` already carries the distinction, so the
    whole gate is one comparison.
    """
    if principal.credential is Credential.api_key:
        raise HTTPException(status_code=403, detail=_KEY_CANNOT_MANAGE_KEYS)


def _row(row) -> dict:
    # THE SECRET HALF IS NOT HERE, and neither is key_hash. `key_prefix` is the public half and is
    # what the holder matches their own copy against; the secret existed once, in the response to
    # the POST that minted it.
    return {
        "id": str(row.id),
        "name": row.name,
        "key_prefix": row.key_prefix,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


@router.get("")
async def list_keys(principal: Annotated[Principal, Depends(principal_dep)],
                    owner_id: str = Depends(owner_dep)) -> dict:
    _require_console_session(principal)
    async with get_db() as db:
        rows = await api_key_service.list_for_owner(
            db, owner_id=owner_id, organization_id=principal.organization_id)
    return {"items": [_row(row) for row in rows]}


@router.post("", status_code=201)
async def create_key(body: CreateKeyIn,
                     principal: Annotated[Principal, Depends(principal_dep)],
                     # BOTH dependencies, deliberately. `owner_dep` is what
                     # test_job_mutating_routes_require_authentication looks for in the AST, and
                     # it resolves from the SAME cached principal - one credential check, not two.
                     owner_id: str = Depends(owner_dep)) -> dict:
    _require_console_session(principal)
    organization = None
    if principal.organization_id:
        try:
            organization = uuid.UUID(principal.organization_id)
        except ValueError:
            # Narrow to no organisation rather than raise, exactly as tenant_scope does. A key
            # stamped with the owner alone still authenticates; one that 500s is never issued.
            organization = None

    async with get_db() as db:
        issued = await api_key_service.issue(db, owner_id=owner_id, name=body.name,
                                             organization_id=organization)
    # `presented` IS THE SECRET. It exists in this response and nowhere else - not in the row,
    # not in the list, not in a log. A lost key is replaced, never looked up.
    return {"id": issued.key_id, "name": issued.name, "key_prefix": issued.key_prefix,
            "presented": issued.presented}


@router.delete("/{key_id}")
async def revoke_key(key_id: uuid.UUID,
                     principal: Annotated[Principal, Depends(principal_dep)],
                     owner_id: str = Depends(owner_dep)) -> dict:
    _require_console_session(principal)
    async with get_db() as db:
        revoked = await api_key_service.revoke(db, owner_id=owner_id, key_id=key_id,
                                               organization_id=principal.organization_id)
    # False for a key that does not exist AND for one already revoked - the repository's
    # `revoked_at is null` predicate makes a repeat revocation report False rather than rewriting
    # the moment it happened. Another tenant's key id is indistinguishable from a missing one.
    return {"revoked": revoked}
```

- [ ] **Step 5: Mount it, and confirm no middleware logs the response body**

In `router.py`:

```python
router.include_router(api_keys.router, prefix="/api-keys", tags=["api-keys"])
```

The 09-10 design describes `/auth/session` as "excluded from request-body logging", but there is **no body-logging middleware in this repository** — `grep -rn "auth/session" src/` finds only `api/auth.py` itself and two comments. There is therefore no exclusion list to add to, and nothing to do beyond confirming that:

```bash
grep -rn "presented\|MintedKey" src/meshpipeline/ | grep -i "log"
```

returns nothing. If a body-logging middleware is ever added, `/api/v1/api-keys` and `/auth/session` are the two routes that must be excluded from it.

- [ ] **Step 6: Run the tests**

Run: `pytest tests/unit/api/test_api_keys_route.py -v`
Expected: PASS, 7 tests

- [ ] **Step 7: Run the security and architecture-fitness suites**

Run: `pytest tests/unit/security tests/unit/api tests/unit/hygiene/test_architecture_fitness.py -q`
Expected: PASS. A failure in `test_job_mutating_routes_require_authentication` naming `api/v1/api_keys.py` means the `owner_dep` parameter was dropped from `create_key` or `revoke_key` — add it back rather than allow-listing the route.

- [ ] **Step 8: Commit**

```bash
git add src/meshpipeline/api/v1/api_keys.py src/meshpipeline/api/v1/router.py \
        src/meshpipeline/application/api_key_service.py \
        tests/unit/api/test_api_keys_route.py
git commit -m "feat(api): manage API keys from a console session, and only from one"
```

---

## Task 6: Sign-up names the organisation

**Files:**
- Modify: `src/meshpipeline/api/auth.py` (accept `organization_name`)
- Modify: `src/meshpipeline/application/account_service.py` (`resolve_or_provision`, `_provision`)
- Test: `tests/unit/application/test_account_provisioning_name.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `account_service.resolve_or_provision(db, token, *, now=None, organization_name="")`; `POST /auth/session` accepts an optional `organization_name` body field.

**Decision 6 is the whole point of this task.** `organization_name` is caller-supplied on an endpoint that also resolves *existing* accounts. It is honoured only when this call provisions a genuinely new account.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/application/test_account_provisioning_name.py
# Responsibility: Verify a caller-supplied organisation name reaches provisioning and nothing else.
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from meshpipeline.application import account_service
from meshpipeline.contracts.firebase_token import VerifiedToken

pytestmark = pytest.mark.asyncio


@dataclass
class _User:
    id: uuid.UUID
    email: str
    name: str = ""
    firebase_uid: str | None = None


@dataclass
class _Org:
    id: uuid.UUID
    name: str
    slug: str


@dataclass
class _Recorder:
    organizations: list = field(default_factory=list)


def _token(uid: str, email: str, *, verified: bool = True) -> VerifiedToken:
    return VerifiedToken(uid=uid, email=email, email_verified=verified, name="An Engineer")


@pytest.fixture
def provisioning(monkeypatch):
    recorder = _Recorder()
    users: dict = {}

    async def fake_get_by_uid(db, uid):
        return users.get(uid)

    async def fake_get_by_email(db, email):
        return next((user for user in users.values() if user.email == email), None)

    async def fake_create_user(db, *, email, name, firebase_uid):
        user = _User(id=uuid.uuid4(), email=email, name=name, firebase_uid=firebase_uid)
        users[firebase_uid] = user
        return user

    async def fake_create_org(db, *, name, slug):
        org = _Org(id=uuid.uuid4(), name=name, slug=slug)
        recorder.organizations.append(org)
        return org

    async def fake_create_membership(db, *, user_id, organization_id, role):
        return None

    async def fake_grant(db, *, organization_id):
        return 500

    async def fake_record_login(db, *, user_id, at, email_verified):
        return None

    async def fake_org_for_email(db, email):
        return recorder.organizations[0].id if recorder.organizations else None

    async def fake_attach(db, *, user_id, firebase_uid):
        for user in users.values():
            if user.id == user_id:
                user.firebase_uid = firebase_uid
                return True
        return False

    monkeypatch.setattr(account_service.user_repo, "get_by_firebase_uid", fake_get_by_uid)
    monkeypatch.setattr(account_service.user_repo, "get_by_email", fake_get_by_email)
    monkeypatch.setattr(account_service.user_repo, "create", fake_create_user)
    monkeypatch.setattr(account_service.user_repo, "record_login", fake_record_login)
    monkeypatch.setattr(account_service.user_repo, "attach_firebase_uid", fake_attach)
    monkeypatch.setattr(account_service.organization_repo, "create", fake_create_org)
    monkeypatch.setattr(account_service.membership_repo, "create", fake_create_membership)
    monkeypatch.setattr(account_service.membership_repo, "organization_id_for_email",
                        fake_org_for_email)
    monkeypatch.setattr(account_service.credit_service, "grant_signup_credits", fake_grant)
    return recorder, users


async def test_a_new_account_names_its_organisation_after_the_company(provisioning):
    recorder, _ = provisioning
    await account_service.resolve_or_provision(
        None, _token("uid-1", "engineer@acme.test"), organization_name="Acme Aerospace")
    assert recorder.organizations[0].name == "Acme Aerospace"


async def test_without_a_company_the_organisation_falls_back_to_the_address(provisioning):
    recorder, _ = provisioning
    await account_service.resolve_or_provision(None, _token("uid-2", "solo@acme.test"))
    assert recorder.organizations[0].name == "solo@acme.test"


async def test_a_blank_company_falls_back_rather_than_naming_an_organisation_empty(provisioning):
    recorder, _ = provisioning
    await account_service.resolve_or_provision(
        None, _token("uid-3", "blank@acme.test"), organization_name="   ")
    assert recorder.organizations[0].name == "blank@acme.test"


async def test_an_existing_account_signing_in_again_cannot_rename_its_organisation(provisioning):
    # DECISION 6. The uid path resolves an account that already exists, so the field is ignored.
    recorder, _ = provisioning
    await account_service.resolve_or_provision(
        None, _token("uid-4", "repeat@acme.test"), organization_name="Original Name")
    await account_service.resolve_or_provision(
        None, _token("uid-4", "repeat@acme.test"), organization_name="Attacker Renamed This")
    assert len(recorder.organizations) == 1
    assert recorder.organizations[0].name == "Original Name"


async def test_a_linking_token_cannot_name_an_organisation(provisioning):
    # The linking path attaches a uid to a row that already exists -- somebody else's tenant,
    # from 0004's backfill. It provisions nothing, so it must create no organisation and rename
    # none either.
    recorder, users = provisioning
    backfilled = _User(id=uuid.uuid4(), email="veteran@acme.test", name="Veteran",
                       firebase_uid=None)
    users["__backfilled__"] = backfilled
    await account_service.resolve_or_provision(
        None, _token("uid-5", "veteran@acme.test"), organization_name="Attacker Named This")
    assert not any(org.name == "Attacker Named This" for org in recorder.organizations)
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `pytest tests/unit/application/test_account_provisioning_name.py -v`
Expected: FAIL — `TypeError: resolve_or_provision() got an unexpected keyword argument 'organization_name'`

- [ ] **Step 3: Thread the name through provisioning only**

In `account_service.py`, change the two signatures:

```python
async def resolve_or_provision(db: AsyncSession, token: VerifiedToken, *,
                               now: datetime | None = None,
                               organization_name: str = "") -> Account:
```

and inside the signup branch, `user = await _provision(db, token)` becomes:

```python
                user = await _provision(db, token, organization_name=organization_name)
```

Then:

```python
async def _provision(db: AsyncSession, token: VerifiedToken, *, organization_name: str = ""):
    # ONE TRANSACTION, four writes. The session this runs in is committed by the caller's
    # `get_db()` context, so a failure anywhere here leaves no user without an organisation and
    # no organisation without its grant.
    #
    # `organization_name` IS CALLER-SUPPLIED and reaches this function on the provision path
    # ALONE (design decision 6). The uid path resolves an account that already exists and the
    # linking path attaches a uid to somebody else's backfilled tenant; honouring a name on
    # either would let any token rename an organisation it did not create. Blank falls back to
    # the address, which is the behaviour every account provisioned before this cycle got.
    name = (organization_name or "").strip() or token.email
    user = await user_repo.create(db, email=token.email, name=token.name,
                                  firebase_uid=token.uid)
    organization = await organization_repo.create(db, name=name,
                                                  slug=_slug_for(token.uid))
    await membership_repo.create(db, user_id=user.id, organization_id=organization.id,
                                 role=MembershipRole.owner)
    granted = await credit_service.grant_signup_credits(db, organization_id=organization.id)
    logger.info("provisioned organisation %s for a new account, granted %s credits",
                organization.id, granted)
    return user
```

`_slug_for` is untouched — it derives from the uid, and its injectivity is what keeps `_provision`'s `IntegrityError` handler honest.

- [ ] **Step 4: Accept the field at the endpoint**

In `src/meshpipeline/api/auth.py`, add the body field and forward it:

```python
async def create_session(
    id_token: Annotated[str, Body(embed=True)] = "",
    organization_name: Annotated[str, Body(embed=True)] = "",
    x_api_key: Annotated[str | None, Header()] = None,
) -> dict:
```

and:

```python
            account = await account_service.resolve_or_provision(
                db, verified, organization_name=organization_name)
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/unit/application/test_account_provisioning_name.py tests/unit/api -q`
Expected: PASS. Every pre-existing `resolve_or_provision` test must still pass — the new argument is keyword-only with a default.

- [ ] **Step 6: Commit**

```bash
git add src/meshpipeline/api/auth.py src/meshpipeline/application/account_service.py \
        tests/unit/application/test_account_provisioning_name.py
git commit -m "feat(auth): name a new organisation after the company that signed up"
```

---

# Phase 2 — The visual system

Every task in this phase edits `ui/` and then copies to `apps/console/public/static/`. The copy is not optional; `test_console_ui_copy_parity.py` fails the build otherwise. Use exactly:

```bash
rsync -a --delete --exclude index.html ui/ apps/console/public/static/
```

## Task 7: Port the site's chrome and re-anchor the tokens

**Files:**
- Create: `ui/css/chrome.css` (and its copy)
- Modify: `ui/css/tokens.css` (and its copy)
- Modify: `apps/console/src/app/_components/legacy-styles.tsx`
- Test: `tests/unit/deploy/test_console_ui_copy_parity.py` (existing, not modified), `tests/unit/hygiene/test_visual_system.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces: the class names `.page-bg`, `.grain`, `.nav`, `.brand`, `.brand__mark`, `.brand__word`, `.nav__link`, `.btn`, `.btn--gold`, `.btn--ghost`, `.cta-mono`, `.label` — every later console task styles against these and defines no new colour.

**Source of truth:** `/Users/kitts/Documents/dev/hexera/hexera-site/styles.css` with `orange-theme.css`'s overrides already applied. Read both before writing; the second file overrides `--gold`, `--teal`, `--bg` and the hero art from the first, and only the resolved values belong in `chrome.css`.

- [ ] **Step 1: Write the failing guard test**

```python
# tests/unit/hygiene/test_visual_system.py
# Responsibility: Keep the console's fonts and palette on the values hexera.ai actually ships.
# Boundaries: a read-only repository gate; it renders nothing and starts no browser.
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[3]
CSS = REPO / "ui" / "css"
LEGACY_STYLES = REPO / "apps" / "console" / "src" / "app" / "_components" / "legacy-styles.tsx"


def test_the_console_requests_the_display_weight_the_site_uses():
    # Saira 300 is the site's display voice -- tall and thin. Requesting 400 as the lightest
    # weight is what made the console's headings read as a generic dark dashboard.
    request = LEGACY_STYLES.read_text()
    assert "Saira+Semi+Condensed:wght@300;400;500;600" in request
    assert "Geist:wght@300;400;500;600" in request
    assert "Geist+Mono:wght@400;500" in request


def test_the_canvas_is_anchored_on_the_sites_own_black():
    tokens = (CSS / "tokens.css").read_text()
    assert "--canvas:#0d0d0c" in tokens.replace(" ", "")


def test_the_chrome_carries_the_sites_primitives():
    chrome = (CSS / "chrome.css").read_text()
    for selector in (".page-bg", ".grain", ".nav", ".brand__word", ".nav__link",
                     ".btn--gold", ".btn--ghost"):
        assert selector in chrome, f"chrome.css is missing {selector}"


def test_the_console_ships_no_second_accent():
    # One hot colour. A stray hex that is neither the accent, the steel linework nor a status
    # role is how a palette becomes six colours nobody chose.
    banned = re.compile(r"#(?:ffba00|0fb6ac|0a8b84|ddab46|e8c879)", re.IGNORECASE)
    for sheet in CSS.glob("*.css"):
        found = banned.findall(sheet.read_text())
        assert not found, f"{sheet.name} carries a retired palette value: {found}"


def test_no_stylesheet_reaches_for_the_hero_canvas():
    # Decision 3: the console is .page-bg + .grain, like the site's own contact page. fluid.js is
    # 40 KB and the console already ships vtk.js.
    for sheet in CSS.glob("*.css"):
        assert "fluid" not in sheet.read_text().lower(), f"{sheet.name} references the hero canvas"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `pytest tests/unit/hygiene/test_visual_system.py -v`
Expected: FAIL — `chrome.css` does not exist; the font test fails on the missing weight 300.

- [ ] **Step 3: Write `ui/css/chrome.css`**

Port the primitives. Read the two site stylesheets first and carry their real values; the skeleton below fixes the responsibilities, the selectors and the tokens, and the exact numbers come from the source.

```css
/* Responsibility: Carry hexera.ai's own chrome primitives - background, nav, brand, buttons, labels. */
/* Boundaries: chrome only; the auth column and the dashboard shell are styled by their own sheets. */

/* /
   PORTED, NOT INVENTED. Every value here is lifted from hexera.ai's styles.css with
   orange-theme.css's overrides already applied, which is what the site actually serves. The
   console wears the same button as the marketing page rather than a lookalike that drifts on
   the next site tweak.

   NOT a shared package: hexera-site is a static repository with no build step, so consuming an
   npm module there would mean giving it a bundler. The drift this accepts is recorded in the
   design's section 10.
/ */

/* The still background the site's non-hero pages use. No canvas - see decision 3. */
.page-bg{position:fixed;inset:0;z-index:0;pointer-events:none;
  background:var(--canvas);
  background-image:
    radial-gradient(120% 90% at 72% 40%,transparent 0%,transparent 42%,rgba(8,8,7,.45) 74%,rgba(6,6,5,.9) 100%),
    linear-gradient(100deg,rgba(7,7,6,.92) 0%,rgba(11,11,10,.6) 34%,transparent 70%)}

.grain{position:fixed;inset:0;z-index:1;pointer-events:none;opacity:.04;mix-blend-mode:screen;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}

/* Nav - the site's fixed bar, used by the auth pages. */
.nav{position:fixed;top:0;left:0;right:0;height:4.5rem;z-index:20;
  display:flex;align-items:center;justify-content:space-between;gap:1.5rem;
  padding:0 clamp(1.25rem,4vw,4rem);border-bottom:1px solid transparent}
.nav.is-stuck{background:rgba(13,13,12,.9);backdrop-filter:blur(14px) saturate(1.1);
  border-bottom-color:var(--border)}

.brand{display:inline-flex;align-items:center;gap:.6rem;color:var(--text)}
.brand__mark{flex:none;display:block;height:24px;width:auto}
.brand__word{font-family:var(--sans);font-weight:600;letter-spacing:.24em;font-size:.95rem;
  padding-left:.05em}

/* THE MONO READOUT VOICE. Every label, eyebrow, nav item, table header and status word in the
   console wears this. It is the single largest thing separating a wind-tunnel instrument from a
   generic dark dashboard, and it is why .label is a primitive rather than a one-off. */
.nav__link,.label{font-family:var(--mono);font-weight:500;font-size:.595rem;
  letter-spacing:.16em;text-transform:uppercase;color:var(--text-muted)}
.nav__link{position:relative;padding:.4rem 0;transition:color .25s cubic-bezier(.16,1,.3,1)}
.nav__link::after{content:"";position:absolute;left:0;bottom:-.1rem;width:100%;height:1px;
  background:var(--steel);transform:scaleX(0);transform-origin:left;
  transition:transform .35s cubic-bezier(.16,1,.3,1)}
.nav__link:hover{color:var(--text)}
.nav__link:hover::after{transform:scaleX(1)}

/* Buttons - the site's exact geometry and transition. */
.btn{--brd:var(--border-strong);
  display:inline-flex;align-items:center;justify-content:center;gap:.55em;
  font-family:var(--sans);font-weight:500;font-size:.95rem;line-height:1;
  padding:.9em 1.4em;border:1px solid var(--brd);border-radius:4px;cursor:pointer;
  white-space:nowrap;background:transparent;color:var(--text);
  transition:transform .3s cubic-bezier(.16,1,.3,1),background .3s cubic-bezier(.16,1,.3,1),
             border-color .3s cubic-bezier(.16,1,.3,1),box-shadow .3s cubic-bezier(.16,1,.3,1),
             color .3s cubic-bezier(.16,1,.3,1)}
.btn:hover{transform:translateY(-2px)}
.btn:disabled{opacity:.5;cursor:default;transform:none}
.btn--gold{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);
  font-weight:600}
.btn--gold:hover{background:var(--accent-hover);
  box-shadow:0 10px 40px rgba(255,79,0,.32),0 0 0 1px rgba(255,112,51,.6)}
.btn--ghost{font-size:.9rem;padding:.7em 1.1em;color:var(--text);
  background:rgba(13,13,12,.42);backdrop-filter:blur(6px)}
.btn--ghost:hover{border-color:var(--steel);
  box-shadow:0 0 0 1px rgba(109,139,175,.4),0 8px 30px rgba(109,139,175,.12)}
.cta-mono{font-family:var(--mono);font-weight:500;font-size:.682rem;letter-spacing:.16em;
  text-transform:uppercase;padding-right:1.1em}
```

- [ ] **Step 4: Re-anchor the tokens**

In `ui/css/tokens.css`, change only the surface ladder's base and the radius, leaving every role name in place:

```css
  /* surfaces - the site's own near-neutral black (orange-theme.css --bg), then an elevation
     ladder each step lighter. The console had drifted to #0a0a09/#0f0f0e, half a shade cooler
     and darker than the page a user arrives from; re-anchoring is what makes the two read as
     one product rather than two dark apps. */
  --canvas:#0d0d0c;
  --background:#131312;
  --surface-1:#161615;
  --surface-2:#1c1c1a;
  --surface-raised:#232321;
  --viewer-frame:#131312;
  --viewer-canvas:#0d0d0c;
```

and:

```css
  --radius:4px;          /* the site's own button/input radius */
  --radius-tight:2px;    /* hairline chrome: chips, dots, rules */
```

- [ ] **Step 5: Load the new sheet and the full font request**

In `legacy-styles.tsx`, add `"/static/css/chrome.css"` to `legacyStylesheets` immediately after `tokens.css`, and replace the font href with:

```
https://fonts.googleapis.com/css2?family=Saira+Semi+Condensed:wght@300;400;500;600&family=Geist:wght@300;400;500;600&family=Geist+Mono:wght@400;500&display=swap
```

Apply the same font `<link>` to `ui/index.html` so the two trees request identical fonts.

- [ ] **Step 6: Copy into the console tree**

```bash
rsync -a --delete --exclude index.html ui/ apps/console/public/static/
```

- [ ] **Step 7: Run both guards**

Run: `pytest tests/unit/hygiene/test_visual_system.py tests/unit/deploy/test_console_ui_copy_parity.py -v`
Expected: PASS. A parity failure means step 6 was skipped.

- [ ] **Step 8: Commit**

```bash
git add ui/ apps/console/public/static/ \
        apps/console/src/app/_components/legacy-styles.tsx \
        tests/unit/hygiene/test_visual_system.py
git commit -m "feat(ui): port hexera.ai's chrome primitives and re-anchor the palette"
```

---

## Task 8: The auth surface stylesheet

**Files:**
- Create: `ui/css/auth.css` (and its copy)
- Test: `tests/unit/hygiene/test_visual_system.py` (extend)

**Interfaces:**
- Consumes: `.page-bg`, `.grain`, `.nav`, `.btn--gold`, `.label` and every token from Task 7.
- Produces: `.auth`, `.auth__panel`, `.auth__title`, `.auth__sub`, `.auth__form`, `.auth__field`, `.auth__input`, `.auth__error`, `.auth__alt`, `.auth__foot` — consumed by Tasks 11 and 12.

- [ ] **Step 1: Extend the guard test**

Append to `tests/unit/hygiene/test_visual_system.py`:

```python
def test_the_auth_surface_defines_the_classes_its_pages_consume():
    auth = (CSS / "auth.css").read_text()
    for selector in (".auth__panel", ".auth__title", ".auth__form", ".auth__field",
                     ".auth__input", ".auth__error", ".auth__alt"):
        assert selector in auth, f"auth.css is missing {selector}"


def test_the_auth_title_uses_the_display_face_at_its_thin_weight():
    auth = (CSS / "auth.css").read_text()
    title = auth[auth.index(".auth__title"):auth.index(".auth__title") + 400]
    assert "var(--display)" in title
    assert "font-weight:300" in title.replace(" ", "")
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `pytest tests/unit/hygiene/test_visual_system.py -v`
Expected: FAIL — `auth.css` does not exist.

- [ ] **Step 3: Write `ui/css/auth.css`**

```css
/* Responsibility: Style the four unauthenticated pages - the column, its panel and its form. */
/* Boundaries: the auth surface only; it defines no colour of its own and no dashboard rule. */

/* /
   These pages used to borrow the console's chat scaffold - #app, #stage, .chat-col, #empty -
   and style their inputs from a CSSProperties object in auth-form-styles.ts. A login form
   rendered inside a conversation column is why the surface read as unfinished. This sheet is
   the surface's own, and it sits on the site's chrome rather than the product's.
/ */

.auth{position:relative;z-index:10;min-height:100svh;display:flex;align-items:center;
  justify-content:center;padding:calc(4.5rem + 4vh) clamp(1.25rem,4vw,4rem) 4vh}

.auth__panel{width:100%;max-width:26rem;display:flex;flex-direction:column;gap:1.5rem}

/* Saira 300 - the site's display voice. A page title is the ONE place it appears in the
   console; everything else is Geist or the mono readout. */
.auth__title{font-family:var(--display);font-weight:300;font-size:clamp(2rem,1.2rem+2.4vw,2.9rem);
  line-height:1.02;letter-spacing:0;color:var(--text-bright)}
.auth__title em{font-style:normal;color:var(--accent)}

.auth__sub{color:var(--text-muted);font-size:.95rem;line-height:1.6}

.auth__form{display:flex;flex-direction:column;gap:.85rem}
.auth__field{display:flex;flex-direction:column;gap:.4rem}
.auth__field > .label{color:var(--text-faint)}

.auth__input{background:var(--surface-1);border:1px solid var(--border);border-radius:var(--radius);
  color:var(--text);font-family:var(--sans);font-size:.95rem;line-height:1.5;padding:.8rem .9rem;
  transition:border-color .25s cubic-bezier(.16,1,.3,1)}
.auth__input:hover{border-color:var(--border-strong)}
.auth__input::placeholder{color:var(--text-faint)}

/* The error is mono, like every other machine-emitted line in this product, and it carries a
   mark as well as a colour - meaning is never in the hue alone. */
.auth__error{display:flex;align-items:flex-start;gap:.5rem;font-family:var(--mono);
  font-size:.72rem;line-height:1.55;letter-spacing:.02em;color:var(--danger);
  background:var(--danger-soft);border:1px solid var(--danger);
  border-radius:var(--radius-tight);padding:.6rem .7rem}
.auth__error::before{content:"!";flex:none;font-weight:500}
.auth__note{border-color:var(--steel);background:var(--steel-soft);color:var(--steel-bright)}
.auth__note::before{content:"i"}

.auth__alt{display:flex;flex-wrap:wrap;gap:1rem;font-size:.85rem;color:var(--text-muted)}
.auth__alt a{color:var(--text);text-decoration:none;border-bottom:1px solid var(--border-strong);
  padding-bottom:1px}
.auth__alt a:hover{border-bottom-color:var(--accent)}

.auth__foot{margin-top:.5rem;color:var(--text-faint);font-size:.78rem;line-height:1.6}

@media (max-width:520px){
  .auth{padding-top:calc(4.5rem + 2vh)}
  .auth__panel{max-width:100%}
}
```

- [ ] **Step 4: Copy, then run the guards**

```bash
rsync -a --delete --exclude index.html ui/ apps/console/public/static/
```

Run: `pytest tests/unit/hygiene/test_visual_system.py tests/unit/deploy/test_console_ui_copy_parity.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ui/ apps/console/public/static/ tests/unit/hygiene/test_visual_system.py
git commit -m "feat(ui): give the auth pages their own surface"
```

---

## Task 9: The dashboard shell stylesheet and the legacy restyle

**Files:**
- Create: `ui/css/dashboard.css` (and its copy)
- Modify: `ui/css/theme.css` (and its copy) — the header block, `#empty`, the chip rules
- Test: `tests/unit/hygiene/test_visual_system.py` (extend)

**Interfaces:**
- Consumes: every token from Task 7.
- Produces: `.shell`, `.sidebar`, `.sidebar__brand`, `.sidebar__nav`, `.sidebar__item`, `.sidebar__item--current`, `.sidebar__item--unavailable`, `.sidebar__foot`, `.sidebar__account`, `.main`, `.page`, `.page__head`, `.page__title`, `.card`, `.table`, `.status`, `.status--ok`, `.status--run`, `.status--fail`, `.empty` — consumed by Tasks 10 and 13–16.

**Do not delete rules from `shell.css`, `chat.css`, `timeline.css`, `result.css`, `viewer.css` or `workbench.css`.** `main.js`, `stage.js` and `viewer.js` select on those class names at runtime. `theme.css` is loaded last and is where values move, exactly as its own header comment describes.

- [ ] **Step 1: Extend the guard test**

```python
def test_the_dashboard_shell_defines_the_classes_its_pages_consume():
    dashboard = (CSS / "dashboard.css").read_text()
    for selector in (".shell", ".sidebar__nav", ".sidebar__item--current",
                     ".sidebar__item--unavailable", ".page__title", ".table", ".status--fail",
                     ".empty"):
        assert selector in dashboard, f"dashboard.css is missing {selector}"


def test_the_legacy_component_sheets_keep_the_selectors_main_js_drives():
    # main.js, stage.js and viewer.js select these at runtime. Restyling moves values; deleting a
    # selector silently breaks the workbench in a way no unit test would catch.
    required = {
        "shell.css": ("#upload-bar", "#stage", ".chat-col", "#notice"),
        "chat.css": (".im",),
        "workbench.css": ("#workbench",),
    }
    for filename, selectors in required.items():
        text = (CSS / filename).read_text()
        for selector in selectors:
            assert selector in text, f"{filename} lost {selector}"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `pytest tests/unit/hygiene/test_visual_system.py -v`
Expected: FAIL — `dashboard.css` does not exist.

- [ ] **Step 3: Write `ui/css/dashboard.css`**

```css
/* Responsibility: Style the authenticated shell - the sidebar, the page frame and its tables. */
/* Boundaries: the shell only; the conversation, timeline, viewer and workbench keep their sheets. */

.shell{display:grid;grid-template-columns:15rem 1fr;min-height:100svh;
  background:var(--canvas)}

/* Sidebar. */
.sidebar{display:flex;flex-direction:column;gap:1.4rem;padding:1.15rem 0;
  background:var(--background);border-right:1px solid var(--border);
  position:sticky;top:0;height:100svh}
.sidebar__brand{display:flex;align-items:center;gap:.55rem;padding:0 1.15rem;color:var(--text)}
.sidebar__brand img{height:20px;width:auto;display:block;flex:none}
.sidebar__brand span{font-family:var(--sans);font-weight:600;letter-spacing:.24em;font-size:.8rem}

.sidebar__nav{display:flex;flex-direction:column;gap:.1rem;padding:0 .55rem}
.sidebar__group{padding:1rem .6rem .35rem;color:var(--text-faint)}

.sidebar__item{display:flex;align-items:center;gap:.6rem;padding:.5rem .6rem;
  border-radius:var(--radius);color:var(--text-muted);text-decoration:none;
  font-family:var(--mono);font-weight:500;font-size:.66rem;letter-spacing:.16em;
  text-transform:uppercase;
  border-left:2px solid transparent;
  transition:color .2s cubic-bezier(.16,1,.3,1),background .2s cubic-bezier(.16,1,.3,1)}
.sidebar__item:hover{color:var(--text);background:var(--surface-1)}
/* The current section is marked by a rule in the accent AND by aria-current on the element.
   Colour alone never carries it. */
.sidebar__item--current{color:var(--text-bright);background:var(--surface-1);
  border-left-color:var(--accent)}
/* A section whose data does not exist yet is TEXT, not a link - saying why it is absent beats
   linking to a page that cannot render. Carried from the admin console's own sections.ts. */
.sidebar__item--unavailable{color:var(--text-faint);cursor:default;opacity:.55}

.sidebar__foot{margin-top:auto;display:flex;flex-direction:column;gap:.55rem;
  padding:0 1.15rem;border-top:1px solid var(--border);padding-top:1rem}
.sidebar__stat{display:flex;align-items:baseline;justify-content:space-between;gap:.5rem}
.sidebar__stat b{font-family:var(--mono);font-weight:500;font-size:.8rem;color:var(--text);
  font-variant-numeric:tabular-nums}
.sidebar__account{display:flex;flex-direction:column;gap:.4rem}
.sidebar__account small{color:var(--text-faint);font-size:.72rem;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}

/* The icon rail the run routes collapse to, so the mesh keeps its width. */
.shell--rail{grid-template-columns:3.25rem 1fr}
.shell--rail .sidebar__brand span,
.shell--rail .sidebar__item span,
.shell--rail .sidebar__group,
.shell--rail .sidebar__foot{display:none}
.shell--rail .sidebar__item{justify-content:center;padding:.5rem 0}

/* Page frame. */
.main{min-width:0;display:flex;flex-direction:column}
.page{padding:2.2rem clamp(1.25rem,3vw,2.6rem);display:flex;flex-direction:column;gap:1.6rem;
  max-width:76rem;width:100%}
.page__head{display:flex;align-items:flex-end;justify-content:space-between;gap:1rem;
  flex-wrap:wrap}
.page__title{font-family:var(--display);font-weight:300;
  font-size:clamp(1.7rem,1.2rem+1.4vw,2.4rem);line-height:1.05;color:var(--text-bright)}
.page__sub{color:var(--text-muted);font-size:.9rem}

.card{background:var(--surface-1);border:1px solid var(--border);border-radius:var(--radius);
  padding:1.15rem 1.25rem}

/* Tables. The header row is the mono readout voice; the cells are Geist with tabular numerals
   so a column of counts lines up. */
.table{width:100%;border-collapse:collapse;font-size:.87rem}
.table th{text-align:left;padding:.55rem .75rem;border-bottom:1px solid var(--border);
  font-family:var(--mono);font-weight:500;font-size:.62rem;letter-spacing:.16em;
  text-transform:uppercase;color:var(--text-faint);white-space:nowrap}
.table td{padding:.7rem .75rem;border-bottom:1px solid var(--border);color:var(--text-muted);
  font-variant-numeric:tabular-nums}
.table tbody tr:hover td{background:var(--surface-1);color:var(--text)}
.table a{color:var(--text);text-decoration:none}
.table a:hover{color:var(--accent)}

/* Status. A WORD and a mark, never a colour alone. */
.status{display:inline-flex;align-items:center;gap:.42rem;font-family:var(--mono);
  font-weight:500;font-size:.62rem;letter-spacing:.16em;text-transform:uppercase}
.status::before{content:"";width:6px;height:6px;border-radius:var(--radius-tight);flex:none;
  background:var(--text-faint)}
.status--ok::before{background:var(--ok)}
.status--run::before{background:var(--accent)}
.status--fail::before{background:var(--danger)}

.empty{padding:2.6rem 0;color:var(--text-faint);font-size:.88rem;line-height:1.75}

@media (max-width:860px){
  .shell{grid-template-columns:3.25rem 1fr}
  .sidebar__brand span,.sidebar__item span,.sidebar__group,.sidebar__foot{display:none}
  .sidebar__item{justify-content:center;padding:.5rem 0}
}
```

- [ ] **Step 4: Restyle the header block in `theme.css`**

The chip row is being deleted from the markup in Phase 3, so `theme.css`'s header rules lose their subjects. Replace its `/* Shell. */` block's `header` and `#empty` rules with values that match the new tokens, and leave `#upload-bar`, `#upload-btn`, `#notice` and everything below alone:

```css
/* Shell. The header survives only inside the workbench, where it carries the run's own controls;
   the brand, the balance and the health dot moved to the sidebar. */
header{background:var(--background);color:var(--text);height:46px;
  border-bottom:1px solid var(--border);gap:12px}
.chip{background:transparent;border:1px solid var(--border);color:var(--text-muted);
  border-radius:var(--radius-tight);font-family:var(--mono);font-size:.62rem;
  letter-spacing:.16em;text-transform:uppercase;padding:5px 10px}
.chip:hover{border-color:var(--border-strong);color:var(--text)}
.dot{background:var(--steel-muted);border-radius:var(--radius-tight);width:6px;height:6px}
.dot.online{background:var(--ok)}
.dot.error{background:var(--danger)}
#empty{color:var(--text-faint);font-size:13px}
```

- [ ] **Step 5: Copy, then run the guards**

```bash
rsync -a --delete --exclude index.html ui/ apps/console/public/static/
```

Run: `pytest tests/unit/hygiene/test_visual_system.py tests/unit/deploy/test_console_ui_copy_parity.py -v`
Expected: PASS

- [ ] **Step 6: Check the workbench still renders in a real browser**

Run: `make test-ui`
Expected: PASS. This tier boots real headless Chrome against the shipped UI and is the only thing that catches a stylesheet that breaks the mesh viewer. If Chrome is absent it FAILS rather than skips — install it or set `CHROME_BIN`.

- [ ] **Step 7: Commit**

```bash
git add ui/ apps/console/public/static/ tests/unit/hygiene/test_visual_system.py
git commit -m "feat(ui): add the dashboard shell and restyle the header onto the new tokens"
```

---

# Phase 3 — The console

## Task 10: Route groups, layouts and the section list

**Files:**
- Create: `apps/console/src/app/_components/sections.ts`, `apps/console/src/app/_components/sidebar.tsx`, `apps/console/src/app/_components/site-chrome.tsx`
- Create: `apps/console/src/app/(auth)/layout.tsx`, `apps/console/src/app/(dashboard)/layout.tsx`
- Create: `apps/console/src/lib/hexera-api/console-fetch.ts`
- Modify: `apps/console/src/app/_components/legacy-styles.tsx` (split into two exports)
- Test: `apps/console/src/app/_components/sections.test.ts`, `apps/console/src/lib/hexera-api/console-fetch.test.ts`

**Interfaces:**
- Consumes: the class names from Tasks 7–9.
- Produces:
  - `CONSOLE_SECTIONS: readonly ConsoleSection[]` where `ConsoleSection = { href: string; label: string; group: "product" | "account"; available: boolean }`
  - `currentSection(pathname: string): string` — the `href` whose section a path belongs to
  - `<Sidebar email={string} credits={string} signOut={ReactNode} />` — a Client Component; it derives the current section from `usePathname()`
  - `<AuthChrome>{children}</AuthChrome>`
  - `AuthStyles` and `DashboardStyles` from `legacy-styles.tsx`
  - `consoleFetch<T>(path: string, ownerId: string): Promise<T | null>` — the shared soft-failing reader every dashboard page uses

- [ ] **Step 1: Write the failing tests**

```ts
// apps/console/src/app/_components/sections.test.ts
import assert from "node:assert/strict";
import test from "node:test";

import { CONSOLE_SECTIONS, currentSection } from "./sections";

test("every section has a unique href", () => {
  const hrefs = CONSOLE_SECTIONS.map((section) => section.href);
  assert.equal(new Set(hrefs).size, hrefs.length);
});

test("the product group leads and the account group follows", () => {
  const groups = CONSOLE_SECTIONS.map((section) => section.group);
  const firstAccount = groups.indexOf("account");
  assert.ok(firstAccount > 0, "there should be product sections before the account ones");
  assert.ok(
    groups.slice(firstAccount).every((group) => group === "account"),
    "the account group must not be interleaved with the product one",
  );
});

test("currentSection matches a nested path to the section that owns it", () => {
  // /runs/<uuid> and /runs/new both belong to /runs, or the sidebar loses its mark the moment
  // somebody opens a run.
  assert.equal(currentSection("/runs/8f14e45f-ceea-467a-9b1e-2c1d0e4a1b22"), "/runs");
  assert.equal(currentSection("/runs/new"), "/runs");
  assert.equal(currentSection("/settings/api-keys"), "/settings/api-keys");
  assert.equal(currentSection("/conversations/abc"), "/conversations");
});

test("currentSection matches the overview only on the root itself", () => {
  // "/" is a prefix of every path; a naive startsWith would mark the overview current forever.
  assert.equal(currentSection("/"), "/");
  assert.notEqual(currentSection("/usage"), "/");
});

test("currentSection returns an empty string for a path no section owns", () => {
  assert.equal(currentSection("/sign-in"), "");
});
```

```ts
// apps/console/src/lib/hexera-api/console-fetch.test.ts
import assert from "node:assert/strict";
import test from "node:test";

import { readJson } from "./console-fetch";

test("readJson returns the parsed body of an ok response", async () => {
  const response = new Response(JSON.stringify({ items: [1, 2] }), { status: 200 });
  assert.deepEqual(await readJson(Promise.resolve(response)), { items: [1, 2] });
});

test("readJson returns null for a non-ok response rather than throwing", async () => {
  const response = new Response("nope", { status: 503 });
  assert.equal(await readJson(Promise.resolve(response)), null);
});

test("readJson returns null when the request rejects", async () => {
  // fetch() REJECTS rather than resolving on a connection failure -- refused, DNS failure. An
  // uncaught rejection here propagates out of a server component and takes the whole page down,
  // which is the failure creditBalance() in the old console page was written to avoid.
  assert.equal(await readJson(Promise.reject(new Error("ECONNREFUSED"))), null);
});

test("readJson returns null when the body is not json", async () => {
  const response = new Response("<html>a proxy error page</html>", { status: 200 });
  assert.equal(await readJson(Promise.resolve(response)), null);
});

test("splitPath keeps a query string out of the path segments", async () => {
  // proxy.ts percent-encodes every segment and then copies the search off the REQUEST url. A
  // path handed over whole would encode "?" into "%3F" and the query would silently vanish --
  // /simulation?limit=5 would quietly return 25 rows.
  const { splitPath } = await import("./console-fetch");
  assert.deepEqual(splitPath("simulation?limit=5"), { segments: ["simulation"], search: "limit=5" });
  assert.deepEqual(splitPath("/credits/history"), {
    segments: ["credits", "history"],
    search: "",
  });
});
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `cd apps/console && pnpm test`
Expected: FAIL — `Cannot find module './sections'`

- [ ] **Step 3: Write the section list**

```ts
// apps/console/src/app/_components/sections.ts
// The product console's sections, in the order the sidebar shows them.
//
// `available` is the honest state of the DATA, not a feature flag, and it is kept even though
// every section is available today -- the same field the admin console's ADMIN_SECTIONS carries,
// for the same reason: a section that cannot render must say why rather than link to a page that
// cannot. The next cycle that adds a section before its endpoint needs this field to already be
// here.
export type ConsoleSection = {
  href: string;
  label: string;
  group: "product" | "account";
  available: boolean;
};

export const CONSOLE_SECTIONS: readonly ConsoleSection[] = [
  { href: "/", label: "Overview", group: "product", available: true },
  { href: "/runs", label: "Runs", group: "product", available: true },
  { href: "/conversations", label: "Conversations", group: "product", available: true },
  { href: "/usage", label: "Usage", group: "product", available: true },
  { href: "/settings/account", label: "Account", group: "account", available: true },
  { href: "/settings/api-keys", label: "API keys", group: "account", available: true },
  { href: "/settings/organization", label: "Organisation", group: "account", available: true },
] as const;

/** Which section's href owns this path, or "" if none does.
 *
 * Longest match wins, so "/settings/api-keys" beats a hypothetical "/settings". The root is
 * matched exactly rather than by prefix: "/" is a prefix of every path, and a naive startsWith
 * would leave Overview marked current on every page in the console.
 */
export function currentSection(pathname: string): string {
  if (pathname === "/") {
    return "/";
  }
  const matches = CONSOLE_SECTIONS.filter(
    (section) =>
      section.href !== "/" &&
      (pathname === section.href || pathname.startsWith(`${section.href}/`)),
  );
  return matches.reduce((longest, section) =>
    section.href.length > longest.href.length ? section : longest,
    { href: "", label: "", group: "product", available: true } as ConsoleSection,
  ).href;
}
```

- [ ] **Step 4: Write the soft-failing reader**

```ts
// apps/console/src/lib/hexera-api/console-fetch.ts
import { proxyProductApiRequest } from "@/lib/product-api/proxy";

//: How long a dashboard page waits for the product API before rendering the degraded row. A
//: page that hangs is worse than a page that says it could not load: the render is a server
//: render, so the whole route stalls behind it.
const TIMEOUT_MS = 3_000;

/** The parsed body, or null for every failure.
 *
 * BOTH the catch and the timeout matter, and neither alone is enough -- the same reasoning the
 * old console page's creditBalance() carried. fetch() REJECTS rather than resolving with a
 * non-ok Response on a connection failure (refused, DNS), and an uncaught rejection propagates
 * out of a server component and takes the entire page down rather than one panel. A
 * slow-but-not-failing API is just as damaging without a bound on time.
 */
export async function readJson<T>(response: Promise<Response>): Promise<T | null> {
  try {
    const resolved = await response;
    if (!resolved.ok) {
      return null;
    }
    return (await resolved.json()) as T;
  } catch {
    return null;
  }
}

/** Read a product API path as the signed-in owner. Fails soft, always.
 *
 * `path` may carry a query string ("simulation?limit=5"). THE TWO HALVES TRAVEL SEPARATELY:
 * proxy.ts builds its target from `routePath.map(encodeURIComponent).join("/")` and then copies
 * the search from the REQUEST's own url. Passing "simulation?limit=5" as one segment would
 * percent-encode the "?" into the path and the query would silently vanish.
 */
export function splitPath(path: string): { segments: string[]; search: string } {
  const [pathname, search = ""] = path.replace(/^\/+/, "").split("?");
  return { segments: pathname.split("/").filter(Boolean), search };
}

export async function consoleFetch<T>(path: string, ownerId: string): Promise<T | null> {
  const { segments, search } = splitPath(path);
  const url = `http://console.internal/api/v1/${segments.join("/")}${search ? `?${search}` : ""}`;
  return readJson<T>(
    Promise.race([
      proxyProductApiRequest(new Request(url), segments, ownerId),
      rejectOnAbort(AbortSignal.timeout(TIMEOUT_MS)),
    ]),
  );
}

function rejectOnAbort(signal: AbortSignal): Promise<never> {
  return new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(signal.reason ?? new Error("timed out")));
  });
}
```

- [ ] **Step 5: Run the tests**

Run: `cd apps/console && pnpm test`
Expected: PASS

- [ ] **Step 6: Split the stylesheet component**

Replace `legacy-styles.tsx`'s single `LegacyStyles` export with two, so the auth pages stop shipping the chat, timeline, viewer and workbench sheets they never used:

```tsx
const TOKENS = ["/static/css/tokens.css", "/static/css/chrome.css"];

const AUTH_SHEETS = [...TOKENS, "/static/css/auth.css", "/static/css/a11y.css"];

// The workbench routes need the whole legacy tree: main.js, stage.js and viewer.js select on
// class names these sheets own. theme.css stays LAST -- it is the overlay that restyles the
// light-mode literals the component sheets still carry.
const DASHBOARD_SHEETS = [
  ...TOKENS,
  "/static/css/dashboard.css",
  "/static/css/shell.css",
  "/static/css/chat.css",
  "/static/css/timeline.css",
  "/static/css/result.css",
  "/static/css/viewer.css",
  "/static/css/workbench.css",
  "/static/css/a11y.css",
  "/static/css/theme.css",
];

export function AuthStyles() {
  return <Sheets hrefs={AUTH_SHEETS} />;
}

export function DashboardStyles() {
  return <Sheets hrefs={DASHBOARD_SHEETS} />;
}
```

Keep the existing `legacyFontLinks` array (with Task 7's updated href) and render it from a shared `Sheets` component alongside the stylesheet links.

- [ ] **Step 7: Write the two layouts and the sidebar**

`apps/console/src/app/_components/site-chrome.tsx`:

```tsx
/* eslint-disable @next/next/no-img-element */
import Link from "next/link";

//: hexera.ai. The auth pages are the seam between the marketing site and the product, so their
//: brand mark goes BACK to where the user came from rather than to the console root they cannot
//: reach yet.
const SITE_URL = "https://hexera.ai";

export function AuthChrome({ children }: { children: React.ReactNode }) {
  return (
    <>
      <div aria-hidden="true" className="page-bg" />
      <div aria-hidden="true" className="grain" />
      <header className="nav">
        <a aria-label="Hexera home" className="brand" href={SITE_URL}>
          <img alt="" className="brand__mark" src="/static/assets/logo.png" />
          <span className="brand__word">HEXERA</span>
        </a>
        <div className="nav__end" style={{ display: "flex", alignItems: "center", gap: "1.4rem" }}>
          <a className="nav__link" href={`${SITE_URL}/contact.html`}>
            Contact
          </a>
          <Link className="btn btn--ghost cta-mono" href="/sign-in">
            Sign in
          </Link>
        </div>
      </header>
      <main className="auth">{children}</main>
    </>
  );
}
```

`apps/console/src/app/(auth)/layout.tsx`:

```tsx
import { AuthStyles } from "@/app/_components/legacy-styles";
import { AuthChrome } from "@/app/_components/site-chrome";

export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <AuthStyles />
      <AuthChrome>{children}</AuthChrome>
    </>
  );
}
```

`apps/console/src/app/_components/sidebar.tsx`.

**Two constraints decide this file's shape, and they pull against each other:**
- A Server Component layout **cannot read the pathname**, so it cannot mark the current section. Only a Client Component calling `usePathname()` can — hence `"use client"`.
- `SignOutButton` (`auth-buttons.tsx`) carries an inline `"use server"` action, and Next refuses to build a module that both defines an inline Server Action and is reachable from a Client Component's import graph — the same constraint `auth-form-styles.ts`'s header comment already records. So the sidebar **cannot import it**; the layout renders it and passes it down as a prop.

```tsx
"use client";

/* eslint-disable @next/next/no-img-element */
import Link from "next/link";
import { usePathname } from "next/navigation";

import { CONSOLE_SECTIONS, currentSection } from "@/app/_components/sections";

type SidebarProps = {
  email: string;
  credits: string;
  /** Rendered, never imported: SignOutButton's inline "use server" action cannot be reached
   *  from a Client Component's import graph. The layout builds it and hands it over. */
  signOut: React.ReactNode;
};

export function Sidebar({ credits, email, signOut }: SidebarProps) {
  const current = currentSection(usePathname());
  const product = CONSOLE_SECTIONS.filter((section) => section.group === "product");
  const account = CONSOLE_SECTIONS.filter((section) => section.group === "account");

  return (
    <aside className="sidebar">
      <Link className="sidebar__brand" href="/">
        <img alt="" src="/static/assets/logo.png" />
        <span>HEXERA</span>
      </Link>

      <nav aria-label="Console sections" className="sidebar__nav">
        {product.map((section) => (
          <Item current={current} key={section.href} section={section} />
        ))}
        <p className="sidebar__group label">Account</p>
        {account.map((section) => (
          <Item current={current} key={section.href} section={section} />
        ))}
      </nav>

      <div className="sidebar__foot">
        <div className="sidebar__stat">
          <span className="label">Credits</span>
          <b>{credits}</b>
        </div>
        <div className="sidebar__account">
          <small title={email}>{email}</small>
          {signOut}
        </div>
      </div>
    </aside>
  );
}

function Item({
  current,
  section,
}: {
  current: string;
  section: (typeof CONSOLE_SECTIONS)[number];
}) {
  // An unavailable section renders as TEXT, never a link. Linking to a page that cannot render
  // is worse than saying why it is not there -- the admin console's own rule.
  if (!section.available) {
    return (
      <span className="sidebar__item sidebar__item--unavailable" title="Not available yet">
        <span>{section.label}</span>
      </span>
    );
  }
  const isCurrent = section.href === current;
  return (
    <Link
      aria-current={isCurrent ? "page" : undefined}
      className={`sidebar__item${isCurrent ? " sidebar__item--current" : ""}`}
      href={section.href}
    >
      <span>{section.label}</span>
    </Link>
  );
}
```

`apps/console/src/app/(dashboard)/layout.tsx`:

```tsx
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { SignOutButton } from "@/app/_components/auth-buttons";
import { DashboardStyles } from "@/app/_components/legacy-styles";
import { Sidebar } from "@/app/_components/sidebar";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type CreditBalance = { balance: number; unit: string };

export default async function DashboardLayout({ children }: { children: React.ReactNode }) {
  const session = await auth();
  const ownerId = ownerIdFromSession(session);
  if (!session || !ownerId) {
    redirect("/sign-in");
  }
  // DECISION 4. An unverified address does not reach the product. /verify-email lives in the
  // (auth) group, which has no such gate, so there is no redirect loop.
  if (!session.emailVerified) {
    redirect("/verify-email");
  }

  const credits = await consoleFetch<CreditBalance>("credits", ownerId);

  return (
    <>
      <DashboardStyles />
      <div className="shell">
        <Sidebar
          credits={credits ? `${credits.balance}` : "--"}
          email={ownerId}
          signOut={<SignOutButton />}
        />
        <main className="main">{children}</main>
      </div>
    </>
  );
}
```

- [ ] **Step 8: Typecheck and lint**

Run: `cd apps/console && pnpm typecheck && pnpm lint`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add apps/console/src/app/_components/ apps/console/src/app/\(auth\)/ \
        apps/console/src/app/\(dashboard\)/ apps/console/src/lib/hexera-api/console-fetch.ts
git commit -m "feat(console): add the auth and dashboard shells"
```

---

## Task 11: Rewrite the three existing auth pages

**Files:**
- Move: `sign-in/page.tsx`, `sign-up/page.tsx`, `forgot-password/page.tsx` into `(auth)/`
- Modify: `apps/console/src/app/_components/auth-forms.tsx`
- Delete: `apps/console/src/app/_components/auth-form-styles.ts`
- Test: `apps/console/src/app/_components/auth-form-messages.test.ts` (extend)

**Interfaces:**
- Consumes: `.auth__*` from Task 8, `AuthChrome` from Task 10.
- Produces: `<SignUpForm />` posting `{ idToken, organizationName }`; `<SignInForm />`; `<ForgotPasswordForm />`.

- [ ] **Step 1: Write the failing test for the new sign-up validation message**

Append to `auth-form-messages.test.ts`:

```ts
import { messageForSignUp } from "./auth-form-messages";

test("messageForSignUp explains a password Identity Platform considers too weak", () => {
  assert.equal(messageForSignUp({ code: "auth/weak-password" }), "Choose a longer password.");
});

test("messageForSignUp reports an address already in use without inventing a cause", () => {
  // Firebase applies its own enumeration protection, so echoing this specific code tells an
  // attacker nothing it would not tell them directly.
  assert.equal(
    messageForSignUp({ code: "auth/email-already-in-use" }),
    "That email already has an account.",
  );
});

test("messageForSignUp falls back honestly for a code it does not know", () => {
  assert.equal(
    messageForSignUp({ code: "auth/some-future-code" }),
    "Could not create the account. Try again.",
  );
});
```

- [ ] **Step 2: Run it**

Run: `cd apps/console && pnpm test`
Expected: PASS — these assert existing behaviour, and they are the regression net for the rewrite that follows.

- [ ] **Step 3: Rewrite the forms**

Replace `auth-forms.tsx` wholesale. Delete `auth-form-styles.ts` and its import; every element takes a class name. `VerifyEmailBanner` is removed — Task 12 replaces it with a page.

```tsx
"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { signIn } from "next-auth/react";
import {
  createUserWithEmailAndPassword,
  sendEmailVerification,
  sendPasswordResetEmail,
  signInWithEmailAndPassword,
  updateProfile,
} from "firebase/auth";

import { firebaseAuth } from "@/lib/firebase/client";
import {
  explainSessionError,
  messageForSignIn,
  messageForSignUp,
} from "@/app/_components/auth-form-messages";

function Field({
  autoComplete,
  label,
  minLength,
  name,
  type,
}: {
  autoComplete: string;
  label: string;
  minLength?: number;
  name: string;
  type: string;
}) {
  return (
    <label className="auth__field">
      <span className="label">{label}</span>
      <input
        autoComplete={autoComplete}
        className="auth__input"
        minLength={minLength}
        name={name}
        required
        type={type}
      />
    </label>
  );
}

export function SignUpForm() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(formData: FormData) {
    setBusy(true);
    setError(null);
    const name = String(formData.get("name") ?? "").trim();
    const company = String(formData.get("company") ?? "").trim();
    const email = String(formData.get("email") ?? "");
    const password = String(formData.get("password") ?? "");
    try {
      const credential = await createUserWithEmailAndPassword(firebaseAuth(), email, password);
      // BEFORE the token is minted, so token.name carries the display name into users.name with
      // no API change. A token minted first would carry the address as the name forever.
      if (name) {
        await updateProfile(credential.user, { displayName: name });
      }
      await sendEmailVerification(credential.user);
      const idToken = await credential.user.getIdToken(true);
      const result = await signIn("credentials", {
        idToken,
        organizationName: company,
        redirect: false,
      });
      if (result?.error) {
        setError(explainSessionError(result.error, "Could not create the account. Try again."));
        return;
      }
      // The account exists but the address is not proven, so the dashboard layout will bounce
      // straight to /verify-email. Going there directly saves the user a redirect they would
      // otherwise see as a flash of the console they cannot use.
      router.push("/verify-email");
      router.refresh();
    } catch (cause) {
      setError(messageForSignUp(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form action={submit} className="auth__form">
      <Field autoComplete="name" label="Full name" name="name" type="text" />
      <Field autoComplete="organization" label="Company" name="company" type="text" />
      <Field autoComplete="email" label="Work email" name="email" type="email" />
      <Field
        autoComplete="new-password"
        label="Password"
        minLength={8}
        name="password"
        type="password"
      />
      {error ? (
        <div aria-live="polite" className="auth__error" role="status">
          {error}
        </div>
      ) : null}
      <button className="btn btn--gold" disabled={busy} type="submit">
        {busy ? "Creating account…" : "Create account"}
      </button>
    </form>
  );
}

export function SignInForm() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(formData: FormData) {
    setBusy(true);
    setError(null);
    try {
      const credential = await signInWithEmailAndPassword(
        firebaseAuth(),
        String(formData.get("email") ?? ""),
        String(formData.get("password") ?? ""),
      );
      const idToken = await credential.user.getIdToken();
      const result = await signIn("credentials", { idToken, redirect: false });
      if (result?.error) {
        setError(explainSessionError(result.error, "Could not sign in. Try again."));
        return;
      }
      router.push("/");
      router.refresh();
    } catch (cause) {
      setError(messageForSignIn(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form action={submit} className="auth__form">
      <Field autoComplete="email" label="Email" name="email" type="email" />
      <Field autoComplete="current-password" label="Password" name="password" type="password" />
      {error ? (
        <div aria-live="polite" className="auth__error" role="status">
          {error}
        </div>
      ) : null}
      <button className="btn btn--gold" disabled={busy} type="submit">
        {busy ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}

export function ForgotPasswordForm() {
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(formData: FormData) {
    setBusy(true);
    setMessage(null);
    try {
      await sendPasswordResetEmail(firebaseAuth(), String(formData.get("email") ?? ""));
    } catch {
      // Deliberately swallowed. sendPasswordResetEmail does not distinguish "no such account"
      // from success, and this page must not either -- reporting anything else would tell an
      // attacker whether the address has an account, which Firebase itself refuses to do.
    } finally {
      setBusy(false);
      setMessage("If that address has an account, a reset link is on its way.");
    }
  }

  return (
    <form action={submit} className="auth__form">
      <Field autoComplete="email" label="Email" name="email" type="email" />
      {message ? (
        <div aria-live="polite" className="auth__error auth__note" role="status">
          {message}
        </div>
      ) : null}
      <button className="btn btn--gold" disabled={busy} type="submit">
        {busy ? "Sending…" : "Send reset link"}
      </button>
    </form>
  );
}
```

- [ ] **Step 4: Carry `organizationName` through the provider**

In `apps/console/src/auth.ts`, add the credential field:

```ts
      credentials: {
        idToken: { label: "ID token", type: "text" },
        organizationName: { label: "Organisation name", type: "text" },
      },
```

In `apps/console/src/lib/auth/firebase-session.ts`, read it and forward it:

```ts
  const organizationName =
    typeof credentials.organizationName === "string" ? credentials.organizationName : "";
```

and include it in the POST body:

```ts
    body: JSON.stringify({ id_token: idToken, organization_name: organizationName }),
```

Widen the parameter type to `Partial<Record<"idToken" | "organizationName", unknown>>`.

- [ ] **Step 5: Rewrite the three pages**

Move each into `(auth)/` and reduce it to its panel. `sign-in/page.tsx`:

```tsx
import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { SignInForm } from "@/app/_components/auth-forms";
import { firebaseConfiguredOnServer } from "@/lib/firebase/server-config";

type SignInPageProps = {
  searchParams?: Promise<{ signup?: string | string[] }>;
};

export default async function SignInPage({ searchParams }: SignInPageProps) {
  if (await auth()) {
    redirect("/");
  }
  const params = await searchParams;
  const signup = Array.isArray(params?.signup) ? params.signup[0] : params?.signup;

  return (
    <div className="auth__panel">
      <h1 className="auth__title">
        Sign in to <em>Hexera</em>
      </h1>
      {signup === "closed" ? (
        <div className="auth__error auth__note" role="status">
          This deployment is not accepting new accounts.
        </div>
      ) : null}
      {firebaseConfiguredOnServer() ? (
        <>
          <SignInForm />
          <div className="auth__alt">
            <Link href="/sign-up">Create an account</Link>
            <Link href="/forgot-password">Forgot your password?</Link>
          </div>
        </>
      ) : (
        // A console whose Identity Platform project was never set up must say so rather than
        // presenting a form that would fail on every submit.
        <div className="auth__error" role="status">
          Sign-in is not configured for this deployment.
        </div>
      )}
    </div>
  );
}
```

`sign-up/page.tsx` keeps its existing `signupEnabled()` server check and `redirect("/sign-in?signup=closed")` verbatim, and renders:

```tsx
      <h1 className="auth__title">
        Start meshing in <em>hours</em>
      </h1>
      <p className="auth__sub">
        Every new account starts with signup credits. No card, no sales call.
      </p>
      <SignUpForm />
      <div className="auth__alt">
        <Link href="/sign-in">Already have an account? Sign in</Link>
      </div>
```

`forgot-password/page.tsx` renders:

```tsx
      <h1 className="auth__title">Reset your password</h1>
      <p className="auth__sub">
        Enter the address on your account and Identity Platform sends a reset link.
      </p>
      <ForgotPasswordForm />
      <div className="auth__alt">
        <Link href="/sign-in">Back to sign in</Link>
      </div>
```

Each keeps its `firebaseConfiguredOnServer()` guard around the form.

- [ ] **Step 6: Delete the dead module**

```bash
git rm apps/console/src/app/_components/auth-form-styles.ts
```

- [ ] **Step 7: Verify, typecheck, lint**

Run: `cd apps/console && pnpm test && pnpm typecheck && pnpm lint`
Expected: PASS. `pnpm lint` must report no unused import of `auth-form-styles`.

- [ ] **Step 8: Commit**

```bash
git add -A apps/console/src
git commit -m "feat(console): rewrite the auth pages onto the site's surface"
```

---

## Task 12: The verification wall

**Files:**
- Create: `apps/console/src/app/(auth)/verify-email/page.tsx`, `apps/console/src/app/_components/verify-email-form.tsx`
- Test: `apps/console/src/app/_components/verify-email-refresh.test.ts`, `apps/console/src/app/_components/verify-email-refresh.ts`

**Interfaces:**
- Consumes: `.auth__*` from Task 8; `firebaseAuth()` from `@/lib/firebase/client`.
- Produces: `refreshVerifiedSession(deps): Promise<"verified" | "still-unverified" | "signed-out">` — the pure, testable core of the continue control.

**This task exists because of the failure mode in spec §5.** `auth.ts`'s `jwt` callback copies `emailVerified` from `user` on sign-in and never re-reads it. Clicking the link in the email cannot update the session. The continue control must force a token refresh with `getIdToken(true)`; the cached token still carries `email_verified: false` and would leave a genuinely verified user walled out forever.

- [ ] **Step 1: Write the failing test**

```ts
// apps/console/src/app/_components/verify-email-refresh.test.ts
import assert from "node:assert/strict";
import test from "node:test";

import { refreshVerifiedSession } from "./verify-email-refresh";

function deps(overrides = {}) {
  const calls: { forceRefresh: boolean[]; signedIn: string[] } = {
    forceRefresh: [],
    signedIn: [],
  };
  const user = {
    emailVerified: true,
    reload: async () => {},
    getIdToken: async (force: boolean) => {
      calls.forceRefresh.push(force);
      return "fresh-token";
    },
  };
  return {
    calls,
    dependencies: {
      currentUser: () => user,
      signIn: async (token: string) => {
        calls.signedIn.push(token);
        return { error: undefined as string | undefined };
      },
      ...overrides,
    },
  };
}

test("a verified user gets a session minted from a FORCE-REFRESHED token", async () => {
  // THE WHOLE POINT. getIdToken() without `true` returns the cached token, which still carries
  // email_verified: false -- so the wall would never lift for somebody who has genuinely
  // clicked the link. This assertion is the regression net for that silent failure.
  const { calls, dependencies } = deps();
  assert.equal(await refreshVerifiedSession(dependencies), "verified");
  assert.deepEqual(calls.forceRefresh, [true]);
  assert.deepEqual(calls.signedIn, ["fresh-token"]);
});

test("a user who has not clicked the link yet is told so and no session is minted", async () => {
  const { calls, dependencies } = deps({
    currentUser: () => ({
      emailVerified: false,
      reload: async () => {},
      getIdToken: async () => "unused",
    }),
  });
  assert.equal(await refreshVerifiedSession(dependencies), "still-unverified");
  assert.deepEqual(calls.signedIn, []);
});

test("a signed-out user is reported as such rather than throwing", async () => {
  // The Firebase SDK's currentUser is null after a page reload until it rehydrates, and null
  // after a real sign-out. Dereferencing it would throw inside a click handler.
  const { dependencies } = deps({ currentUser: () => null });
  assert.equal(await refreshVerifiedSession(dependencies), "signed-out");
});

test("reload runs before emailVerified is read", async () => {
  // emailVerified is a cached property on the SDK's user object. Reading it without reload()
  // returns whatever was true when the page loaded -- which is always false here.
  const order: string[] = [];
  const { dependencies } = deps({
    currentUser: () => ({
      get emailVerified() {
        order.push("read");
        return true;
      },
      reload: async () => {
        order.push("reload");
      },
      getIdToken: async () => "fresh-token",
    }),
  });
  await refreshVerifiedSession(dependencies);
  assert.deepEqual(order.slice(0, 2), ["reload", "read"]);
});

test("a session that will not mint is reported as still-unverified rather than as verified", async () => {
  const { dependencies } = deps({
    signIn: async () => ({ error: "CredentialsSignin" }),
  });
  assert.equal(await refreshVerifiedSession(dependencies), "still-unverified");
});
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd apps/console && pnpm test`
Expected: FAIL — `Cannot find module './verify-email-refresh'`

- [ ] **Step 3: Write the refresh core**

```ts
// apps/console/src/app/_components/verify-email-refresh.ts
// The continue control's logic, kept free of React and of the Firebase SDK so it can be tested
// by calling it. The component below it does nothing but wire the real dependencies in.

export type VerifyUser = {
  emailVerified: boolean;
  reload: () => Promise<void>;
  getIdToken: (forceRefresh: boolean) => Promise<string>;
};

export type RefreshDeps = {
  currentUser: () => VerifyUser | null;
  signIn: (idToken: string) => Promise<{ error?: string }>;
};

export type RefreshOutcome = "verified" | "still-unverified" | "signed-out";

/** Re-mint the session against a freshly-refreshed ID token.
 *
 * THE SESSION DOES NOT LEARN ABOUT VERIFICATION ON ITS OWN. auth.ts's jwt callback copies
 * emailVerified from `user` at sign-in and nothing re-reads it per request, so a user who clicks
 * the link in their email still carries a JWT that says false. Two things are therefore
 * mandatory here and neither is optional:
 *
 *   reload()            - emailVerified is a CACHED property on the SDK's user object
 *   getIdToken(true)    - without the force flag the SDK hands back the cached token, whose
 *                         email_verified claim is still false, and the wall never lifts
 */
export async function refreshVerifiedSession(deps: RefreshDeps): Promise<RefreshOutcome> {
  const user = deps.currentUser();
  if (!user) {
    return "signed-out";
  }
  await user.reload();
  if (!user.emailVerified) {
    return "still-unverified";
  }
  const idToken = await user.getIdToken(true);
  const result = await deps.signIn(idToken);
  // A session that will not mint is NOT a verified session, whatever the address says. Reporting
  // "verified" here would send the user to a dashboard layout that bounces them straight back.
  return result?.error ? "still-unverified" : "verified";
}
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/console && pnpm test`
Expected: PASS, 5 new tests

- [ ] **Step 5: Write the component and the page**

`apps/console/src/app/_components/verify-email-form.tsx`:

```tsx
"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { signIn } from "next-auth/react";
import { sendEmailVerification } from "firebase/auth";

import { firebaseAuth } from "@/lib/firebase/client";
import { refreshVerifiedSession } from "@/app/_components/verify-email-refresh";

export function VerifyEmailForm({ email }: { email: string }) {
  const router = useRouter();
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function resend() {
    setBusy(true);
    setNote(null);
    try {
      const user = firebaseAuth().currentUser;
      if (!user) {
        setNote("Your session expired. Sign in again to resend the link.");
        return;
      }
      await sendEmailVerification(user);
      setNote(`Sent again to ${email}. Identity Platform throttles repeats, so give it a minute.`);
    } catch {
      // Best effort, and deliberately not diagnosed: the SDK's throttling error and a transient
      // network failure are indistinguishable from here, and the control stays available.
      setNote("Could not send just now. Try again in a moment.");
    } finally {
      setBusy(false);
    }
  }

  async function checkAgain() {
    setBusy(true);
    setNote(null);
    try {
      const outcome = await refreshVerifiedSession({
        currentUser: () => firebaseAuth().currentUser,
        signIn: async (idToken) => {
          const result = await signIn("credentials", { idToken, redirect: false });
          return { error: result?.error ?? undefined };
        },
      });
      if (outcome === "verified") {
        router.push("/");
        router.refresh();
        return;
      }
      setNote(
        outcome === "signed-out"
          ? "Your session expired. Sign in again to continue."
          : "That address is not verified yet. Open the link in your inbox, then check again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="auth__form">
        <button className="btn btn--gold" disabled={busy} onClick={checkAgain} type="button">
          {busy ? "Checking…" : "I've verified — continue"}
        </button>
        <button className="btn" disabled={busy} onClick={resend} type="button">
          Resend the link
        </button>
      </div>
      {note ? (
        <div aria-live="polite" className="auth__error auth__note" role="status">
          {note}
        </div>
      ) : null}
    </>
  );
}
```

`apps/console/src/app/(auth)/verify-email/page.tsx`:

```tsx
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { SignOutButton } from "@/app/_components/auth-buttons";
import { VerifyEmailForm } from "@/app/_components/verify-email-form";
import { ownerIdFromSession } from "@/lib/auth/session";

export default async function VerifyEmailPage() {
  const session = await auth();
  const ownerId = ownerIdFromSession(session);
  if (!session || !ownerId) {
    redirect("/sign-in");
  }
  // Already proven. Sitting on this page with a verified session is a dead end, so it sends the
  // user where they were going.
  if (session.emailVerified) {
    redirect("/");
  }

  return (
    <div className="auth__panel">
      <h1 className="auth__title">
        Check your <em>inbox</em>
      </h1>
      <p className="auth__sub">
        A verification link is on its way to <strong>{ownerId}</strong>. Open it, then continue —
        your account and its signup credits are already waiting.
      </p>
      <VerifyEmailForm email={ownerId} />
      <p className="auth__foot">
        The sender is <code>noreply</code> at our Identity Platform domain, so check spam if it is
        not there in a minute. Wrong address? Sign out and create the account again.
      </p>
      <SignOutButton />
    </div>
  );
}
```

- [ ] **Step 6: Typecheck and lint**

Run: `cd apps/console && pnpm test && pnpm typecheck && pnpm lint`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add apps/console/src/app
git commit -m "feat(console): wall the product behind a verified address"
```

---

## Task 13: The workbench moves onto its routes

**Files:**
- Modify: `ui/js/main.js` (and its copy) — `bootLive()` and `attachJob`
- Create: `apps/console/src/app/(dashboard)/runs/new/page.tsx`, `apps/console/src/app/(dashboard)/runs/[jobId]/page.tsx`, `apps/console/src/app/_components/workbench.tsx`
- Delete: `apps/console/src/app/(console)/page.tsx`
- Test: `tests/ui/test_browser.py` (existing, run it), `tests/unit/hygiene/test_visual_system.py` (extend)

**Interfaces:**
- Consumes: `DashboardStyles` from Task 10.
- Produces: `<Workbench ownerId={string} bootJobId={string | null} />` — the markup `main.js` drives, plus the two globals it now reads.

**The deep link already exists.** `bootLive()` reads `?job=<id>` and either re-attaches to a live stream with `startStream(0)` or replays a finished run and shows its result and mesh. This task adds a second way to name that job and a second way to write the URL, and changes nothing else.

- [ ] **Step 1: Extend the guard test**

```python
def test_main_js_accepts_a_server_injected_job_as_well_as_the_query_string():
    main = (REPO / "ui" / "js" / "main.js").read_text()
    assert "__HEXERA_BOOT_JOB__" in main
    assert "__HEXERA_ROUTED__" in main
    # ui/index.html sets neither and must keep working exactly as it does today.
    assert 'params.get("job")' in main
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `pytest tests/unit/hygiene/test_visual_system.py -v`
Expected: FAIL — neither global is present.

- [ ] **Step 3: Make the two additive changes to `main.js`**

In `attachJob`, replace the `history.replaceState` line:

```js
  // WHERE THE RUN LIVES IN THE URL. The console routes runs at /runs/<id>; ui/index.html has no
  // routes and keeps the query string it has always used. Neither global set means the second
  // branch, which is today's behaviour byte for byte.
  try {
    const routed = globalThis.__HEXERA_ROUTED__;
    history.replaceState(null, "",
      routed ? "/runs/" + id : location.pathname + "?job=" + id);
  } catch { /* ignore */ }
```

In `bootLive`, replace the deep-link read:

```js
  const params = new URLSearchParams(location.search);
  // TWO WAYS TO NAME THE RUN, one meaning. The query string is the original contract and still
  // the only one ui/index.html uses; the global is how the console's /runs/<id> route hands the
  // id over without a redirect through a query string it would then have to clean up.
  const deepLinkJob = params.get("job") || globalThis.__HEXERA_BOOT_JOB__ || null;
```

- [ ] **Step 4: Copy and verify parity**

```bash
rsync -a --delete --exclude index.html ui/ apps/console/public/static/
```

Run: `pytest tests/unit/hygiene/test_visual_system.py tests/unit/deploy/test_console_ui_copy_parity.py -v`
Expected: PASS

- [ ] **Step 5: Extract the workbench markup**

`apps/console/src/app/_components/workbench.tsx` — the body of the deleted `(console)/page.tsx` with the header chips, the credit pill, the sign-out button and the verification banner removed:

```tsx
import Script from "next/script";

export function Workbench({
  bootJobId,
  ownerId,
}: {
  bootJobId: string | null;
  ownerId: string;
}) {
  const publicApiBaseUrl = process.env.NEXT_PUBLIC_HEXERA_API_BASE_URL ?? "";

  return (
    <>
      <Script id="hexera-console-session" strategy="beforeInteractive">
        {`
          globalThis.__HEXERA_API_WS_BASE_URL__ = ${JSON.stringify(publicApiBaseUrl)};
          globalThis.__HEXERA_ROUTED__ = true;
          globalThis.__HEXERA_BOOT_JOB__ = ${JSON.stringify(bootJobId)};
          try { localStorage.setItem("mg_uid", ${JSON.stringify(ownerId)}); } catch {}
        `}
      </Script>

      <div id="lb">
        <img alt="" id="lb-img" />
      </div>

      <div id="app">
        <header>
          <button aria-pressed="false" className="chip" hidden id="wb-toggle" type="button">
            Hide conversation
          </button>
          <div className="h-spacer" />
          <div aria-live="polite" className="chip chip-status" id="api-status" role="status">
            <div className="dot" id="dot" />
            <span id="api-lbl">checking...</span>
          </div>
        </header>

        <div aria-live="polite" id="notice" role="status" />

        <div id="upload-bar">
          <input
            aria-label="Upload a geometry file"
            className="sr-only"
            data-accept-from-capabilities=""
            id="step-file-input"
            type="file"
          />
          <span id="file-label">No geometry file selected</span>
          <button className="btn btn--gold" id="upload-btn" type="button">
            Upload geometry
          </button>
        </div>

        <div id="stage" />
        <div aria-label="Delivered mesh" id="workbench" />

        <div id="input-bar">
          <div id="input-inner">
            <textarea
              aria-label="Message Hexera"
              disabled
              id="chat-input"
              placeholder="Upload a geometry file to begin..."
              rows={1}
            />
            <button disabled id="send-btn" type="button">
              Send
            </button>
          </div>
        </div>
      </div>

      <Script src="/static/js/main.js" strategy="afterInteractive" type="module" />
    </>
  );
}
```

- [ ] **Step 6: Add the two routes**

`(dashboard)/runs/new/page.tsx`:

```tsx
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { Workbench } from "@/app/_components/workbench";
import { ownerIdFromSession } from "@/lib/auth/session";

export default async function NewRunPage() {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  return <Workbench bootJobId={null} ownerId={ownerId} />;
}
```

`(dashboard)/runs/[jobId]/page.tsx`:

```tsx
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { Workbench } from "@/app/_components/workbench";
import { ownerIdFromSession } from "@/lib/auth/session";

export default async function RunPage({
  params,
}: {
  params: Promise<{ jobId: string }>;
}) {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  const { jobId } = await params;
  // The id is handed to main.js as a global rather than checked here. bootLive() already resolves
  // it against the API and says so honestly when the job belongs to somebody else -- duplicating
  // that check server-side would be a second source of truth for the same refusal.
  return <Workbench bootJobId={jobId} ownerId={ownerId} />;
}
```

- [ ] **Step 7: Delete the old page**

```bash
git rm apps/console/src/app/\(console\)/page.tsx
```

- [ ] **Step 8: Verify in a real browser**

Run: `cd apps/console && pnpm typecheck && pnpm lint` then `make test-ui`
Expected: PASS. `make test-ui` is what catches a module that will not parse or an asset that 404s after the move.

- [ ] **Step 9: Commit**

```bash
git add -A ui apps/console
git commit -m "feat(console): route the workbench at /runs/new and /runs/<id>"
```

---

## Task 14: The run and conversation lists

**Files:**
- Create: `apps/console/src/app/(dashboard)/runs/page.tsx`, `apps/console/src/app/(dashboard)/conversations/page.tsx`, `apps/console/src/app/(dashboard)/conversations/[sessionId]/page.tsx`
- Create: `apps/console/src/app/_components/run-status.ts`, `apps/console/src/app/_components/run-status.test.ts`

**Interfaces:**
- Consumes: `consoleFetch` from Task 10; `GET /api/v1/simulation` from Task 1; `GET /api/v1/chat` from Task 2; `.table`, `.status*`, `.empty`, `.page*` from Task 9.
- Produces: `statusClass(status: string): string`, `formatDuration(startedIso: string | null, endedIso: string | null): string`.

- [ ] **Step 1: Write the failing test**

```ts
// apps/console/src/app/_components/run-status.test.ts
import assert from "node:assert/strict";
import test from "node:test";

import { formatDuration, statusClass } from "./run-status";

test("statusClass maps each terminal state to its own mark", () => {
  assert.equal(statusClass("succeeded"), "status status--ok");
  assert.equal(statusClass("failed"), "status status--fail");
  assert.equal(statusClass("running"), "status status--run");
});

test("statusClass falls back to the neutral mark for a state it does not know", () => {
  // JobStatus can gain a member without this file changing. An unknown state must render as a
  // neutral row, never crash the table.
  assert.equal(statusClass("some_future_state"), "status");
  assert.equal(statusClass(""), "status");
});

test("formatDuration reports a finished run in whole units", () => {
  assert.equal(formatDuration("2026-09-10T09:00:00Z", "2026-09-10T09:04:30Z"), "4m 30s");
  assert.equal(formatDuration("2026-09-10T09:00:00Z", "2026-09-10T11:30:00Z"), "2h 30m");
  assert.equal(formatDuration("2026-09-10T09:00:00Z", "2026-09-10T09:00:12Z"), "12s");
});

test("formatDuration reports an unfinished or unstarted run as an em dash", () => {
  assert.equal(formatDuration("2026-09-10T09:00:00Z", null), "—");
  assert.equal(formatDuration(null, "2026-09-10T09:00:00Z"), "—");
  assert.equal(formatDuration(null, null), "—");
});

test("formatDuration does not report a negative duration", () => {
  // Clock skew between the API host and the worker can end a job before it starts on paper.
  assert.equal(formatDuration("2026-09-10T09:00:10Z", "2026-09-10T09:00:00Z"), "—");
});
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd apps/console && pnpm test`
Expected: FAIL — `Cannot find module './run-status'`

- [ ] **Step 3: Write the helpers**

```ts
// apps/console/src/app/_components/run-status.ts
// Presentation for a run's two derived columns. Pure, so the table's behaviour is testable
// without rendering it.

const MARKS: Record<string, string> = {
  succeeded: "status status--ok",
  failed: "status status--fail",
  running: "status status--run",
};

/** The class a status word wears. Meaning is carried by the WORD as well as the mark; the class
 *  only adds the mark, and an unrecognised status still renders as a neutral row rather than
 *  breaking the table. JobStatus can gain a member without this file changing. */
export function statusClass(status: string): string {
  return MARKS[status] ?? "status";
}

/** How long a finished run took, in whole units.
 *
 * An em dash for anything that has not both started and ended, and for a negative span: clock
 * skew between the API host and the worker can record an end before a start, and "-3s" in a
 * table reads as a bug in the product rather than as a bug in a clock.
 */
export function formatDuration(startedIso: string | null, endedIso: string | null): string {
  if (!startedIso || !endedIso) {
    return "—";
  }
  const seconds = Math.round(
    (new Date(endedIso).getTime() - new Date(startedIso).getTime()) / 1000,
  );
  if (!Number.isFinite(seconds) || seconds < 0) {
    return "—";
  }
  if (seconds < 60) {
    return `${seconds}s`;
  }
  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  }
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/console && pnpm test`
Expected: PASS, 5 new tests

- [ ] **Step 5: Write the runs page**

```tsx
// apps/console/src/app/(dashboard)/runs/page.tsx
import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { formatDuration, statusClass } from "@/app/_components/run-status";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type Run = {
  id: string;
  status: string;
  task_label: string | null;
  created_at: string | null;
  ended_at: string | null;
  attempts: number;
  failed_reason: string | null;
};

type RunPage = { items: Run[]; next_cursor: string | null };

export default async function RunsPage({
  searchParams,
}: {
  searchParams?: Promise<{ cursor?: string | string[] }>;
}) {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  const params = await searchParams;
  const cursor = Array.isArray(params?.cursor) ? params.cursor[0] : params?.cursor;
  const page = await consoleFetch<RunPage>(
    `simulation${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
    ownerId,
  );

  return (
    <div className="page">
      <div className="page__head">
        <h1 className="page__title">Runs</h1>
        <Link className="btn btn--gold" href="/runs/new">
          New run
        </Link>
      </div>

      {page === null ? (
        // Fails soft, like every panel. A degraded API costs this table, never the page.
        <p className="empty">Could not reach the API just now. Reload to try again.</p>
      ) : page.items.length === 0 ? (
        <p className="empty">
          No runs yet. Upload a geometry file and describe the study you need — the first mesh
          takes minutes, not days.
        </p>
      ) : (
        <>
          <table className="table">
            <thead>
              <tr>
                <th>Status</th>
                <th>Study</th>
                <th>Started</th>
                <th>Duration</th>
                <th>Attempts</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((run) => (
                <tr key={run.id}>
                  <td>
                    <span className={statusClass(run.status)}>{run.status}</span>
                  </td>
                  <td>
                    <Link href={`/runs/${run.id}`}>{run.task_label ?? "Untitled study"}</Link>
                  </td>
                  <td>
                    {run.created_at ? new Date(run.created_at).toLocaleString() : "—"}
                  </td>
                  <td>{formatDuration(run.created_at, run.ended_at)}</td>
                  <td>{run.attempts}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {page.next_cursor ? (
            <Link
              className="btn"
              href={`/runs?cursor=${encodeURIComponent(page.next_cursor)}`}
            >
              Older runs
            </Link>
          ) : null}
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 6: Write the conversation list**

```tsx
// apps/console/src/app/(dashboard)/conversations/page.tsx
import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type Conversation = {
  id: string;
  task_label: string | null;
  job_id: string | null;
  message_count: number;
  created_at: string | null;
};

type ConversationPage = { items: Conversation[]; next_cursor: string | null };

export default async function ConversationsPage({
  searchParams,
}: {
  searchParams?: Promise<{ cursor?: string | string[] }>;
}) {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  const params = await searchParams;
  const cursor = Array.isArray(params?.cursor) ? params.cursor[0] : params?.cursor;
  const page = await consoleFetch<ConversationPage>(
    `chat${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
    ownerId,
  );

  return (
    <div className="page">
      <div className="page__head">
        <h1 className="page__title">Conversations</h1>
      </div>

      {page === null ? (
        <p className="empty">Could not reach the API just now. Reload to try again.</p>
      ) : page.items.length === 0 ? (
        <p className="empty">No conversations yet. Every run starts with one.</p>
      ) : (
        <>
          <table className="table">
            <thead>
              <tr>
                <th>Study</th>
                <th>Messages</th>
                <th>Started</th>
                <th>Run</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((conversation) => (
                <tr key={conversation.id}>
                  <td>
                    <Link href={`/conversations/${conversation.id}`}>
                      {conversation.task_label ?? "Untitled study"}
                    </Link>
                  </td>
                  <td>{conversation.message_count}</td>
                  <td>
                    {conversation.created_at
                      ? new Date(conversation.created_at).toLocaleString()
                      : "—"}
                  </td>
                  <td>
                    {conversation.job_id ? (
                      <Link href={`/runs/${conversation.job_id}`}>Open</Link>
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {page.next_cursor ? (
            <Link
              className="btn"
              href={`/conversations?cursor=${encodeURIComponent(page.next_cursor)}`}
            >
              Older conversations
            </Link>
          ) : null}
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 7: Write the conversation transcript**

The response shape is confirmed against `src/meshpipeline/api/v1/chat.py:35` — `{ session_id, messages, awaiting_confirmation, job_id }`.

```tsx
// apps/console/src/app/(dashboard)/conversations/[sessionId]/page.tsx
import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type Message = { role: string; content: string };

type History = {
  session_id: string;
  messages: Message[];
  awaiting_confirmation: boolean;
  job_id: string | null;
};

export default async function ConversationPage({
  params,
}: {
  params: Promise<{ sessionId: string }>;
}) {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  const { sessionId } = await params;
  const history = await consoleFetch<History>(`chat/history/${sessionId}`, ownerId);

  return (
    <div className="page">
      <div className="page__head">
        <h1 className="page__title">Conversation</h1>
        {history?.job_id ? (
          <Link className="btn" href={`/runs/${history.job_id}`}>
            Open the run
          </Link>
        ) : null}
      </div>

      {history === null ? (
        // A 404 for somebody else's session and an unreachable API both land here. The route
        // already refuses another tenant's id indistinguishably from a missing one, so this page
        // must not describe which of the two happened either.
        <p className="empty">Could not load this conversation. Reload to try again.</p>
      ) : history.messages.length === 0 ? (
        <p className="empty">This conversation has no messages.</p>
      ) : (
        <div style={{ display: "grid", gap: ".9rem" }}>
          {history.messages.map((message, index) => (
            <div className="card" key={`${message.role}-${index}`}>
              <p className="label">{message.role}</p>
              <p style={{ whiteSpace: "pre-wrap" }}>{message.content}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 8: Verify**

Run: `cd apps/console && pnpm test && pnpm typecheck && pnpm lint`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add apps/console/src/app
git commit -m "feat(console): list runs and conversations"
```

---

## Task 15: Overview and usage

**Files:**
- Create: `apps/console/src/app/(dashboard)/page.tsx`, `apps/console/src/app/(dashboard)/usage/page.tsx`
- Create: `apps/console/src/app/_components/ledger.ts`, `apps/console/src/app/_components/ledger.test.ts`

**Interfaces:**
- Consumes: `consoleFetch`; `GET /api/v1/credits` and `GET /api/v1/credits/history` from Task 3; `GET /api/v1/simulation` from Task 1; `statusClass` and `formatDuration` from Task 14.
- Produces: `formatSigned(amount: number): string`.

- [ ] **Step 1: Write the failing test**

```ts
// apps/console/src/app/_components/ledger.test.ts
import assert from "node:assert/strict";
import test from "node:test";

import { formatSigned } from "./ledger";

test("a grant reads as an explicit credit", () => {
  // The column is SIGNED in the database so a client sums it rather than branching on the type.
  // A bare "500" beside a "-20" reads as though only one of them has a direction.
  assert.equal(formatSigned(500), "+500");
});

test("a debit keeps its own sign rather than gaining a second one", () => {
  assert.equal(formatSigned(-20), "-20");
});

test("zero carries no sign", () => {
  assert.equal(formatSigned(0), "0");
});
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd apps/console && pnpm test`
Expected: FAIL — `Cannot find module './ledger'`

- [ ] **Step 3: Write the helper**

```ts
// apps/console/src/app/_components/ledger.ts

/** A ledger amount with its direction shown.
 *
 * `credit_ledger.amount` is signed by design -- a grant is positive and a debit negative, so the
 * balance is a plain SUM with no per-type arithmetic a new entry type could get wrong. Rendering
 * a positive amount without a "+" loses that symmetry in the one place a reader is comparing
 * the two.
 */
export function formatSigned(amount: number): string {
  if (amount === 0) {
    return "0";
  }
  return amount > 0 ? `+${amount}` : String(amount);
}
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/console && pnpm test`
Expected: PASS, 3 new tests

- [ ] **Step 5: Write the overview**

```tsx
// apps/console/src/app/(dashboard)/page.tsx
import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { formatDuration, statusClass } from "@/app/_components/run-status";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type Run = {
  id: string;
  status: string;
  task_label: string | null;
  created_at: string | null;
  ended_at: string | null;
};

export default async function OverviewPage() {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }

  // Both panels degrade independently: one failing must not blank the other, which is why these
  // are two awaited reads that each fail soft rather than one combined call.
  const [credits, runs] = await Promise.all([
    consoleFetch<{ balance: number; unit: string }>("credits", ownerId),
    consoleFetch<{ items: Run[] }>("simulation?limit=5", ownerId),
  ]);

  return (
    <div className="page">
      <div className="page__head">
        <h1 className="page__title">Overview</h1>
        <Link className="btn btn--gold" href="/runs/new">
          New run
        </Link>
      </div>

      <div className="card">
        <p className="label">Credit balance</p>
        <p style={{ fontSize: "1.8rem", fontFamily: "var(--mono)" }}>
          {credits ? `${credits.balance} ${credits.unit}` : "unavailable"}
        </p>
        <Link className="nav__link" href="/usage">
          See the ledger
        </Link>
      </div>

      <div>
        <p className="label">Recent runs</p>
        {runs === null ? (
          <p className="empty">Could not reach the API just now.</p>
        ) : runs.items.length === 0 ? (
          <p className="empty">
            Nothing has run yet. Upload a geometry file to start your first study.
          </p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Status</th>
                <th>Study</th>
                <th>Duration</th>
              </tr>
            </thead>
            <tbody>
              {runs.items.map((run) => (
                <tr key={run.id}>
                  <td>
                    <span className={statusClass(run.status)}>{run.status}</span>
                  </td>
                  <td>
                    <Link href={`/runs/${run.id}`}>{run.task_label ?? "Untitled study"}</Link>
                  </td>
                  <td>{formatDuration(run.created_at, run.ended_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 6: Write the usage page**

```tsx
// apps/console/src/app/(dashboard)/usage/page.tsx
import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { formatSigned } from "@/app/_components/ledger";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type Entry = {
  id: string;
  entry_type: string;
  amount: number;
  reason: string;
  created_at: string | null;
};

type LedgerPage = { items: Entry[]; next_cursor: string | null };

export default async function UsagePage({
  searchParams,
}: {
  searchParams?: Promise<{ cursor?: string | string[] }>;
}) {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  const params = await searchParams;
  const cursor = Array.isArray(params?.cursor) ? params.cursor[0] : params?.cursor;

  // Two independent soft reads: a failing ledger must not blank the balance, and vice versa.
  const [credits, ledger] = await Promise.all([
    consoleFetch<{ balance: number; unit: string }>("credits", ownerId),
    consoleFetch<LedgerPage>(
      `credits/history${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
      ownerId,
    ),
  ]);

  return (
    <div className="page">
      <div className="page__head">
        <h1 className="page__title">Usage</h1>
      </div>

      <div className="card">
        <p className="label">Balance</p>
        <p style={{ fontSize: "1.8rem", fontFamily: "var(--mono)" }}>
          {credits ? `${credits.balance} ${credits.unit}` : "unavailable"}
        </p>
        <p className="page__sub">
          What a credit buys is not decided yet, and nothing spends them in this release.
        </p>
      </div>

      {ledger === null ? (
        <p className="empty">Could not reach the API just now. Reload to try again.</p>
      ) : ledger.items.length === 0 ? (
        <p className="empty">
          No movements yet. Your signup grant appears here the moment it lands.
        </p>
      ) : (
        <>
          <table className="table">
            <thead>
              <tr>
                <th>When</th>
                <th>Type</th>
                <th>Amount</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {ledger.items.map((entry) => (
                <tr key={entry.id}>
                  <td>
                    {entry.created_at ? new Date(entry.created_at).toLocaleString() : "—"}
                  </td>
                  <td>
                    <span className="label">{entry.entry_type}</span>
                  </td>
                  <td>{formatSigned(entry.amount)}</td>
                  <td>{entry.reason || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {ledger.next_cursor ? (
            <Link
              className="btn"
              href={`/usage?cursor=${encodeURIComponent(ledger.next_cursor)}`}
            >
              Older movements
            </Link>
          ) : null}
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 7: Verify**

Run: `cd apps/console && pnpm test && pnpm typecheck && pnpm lint`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add apps/console/src/app
git commit -m "feat(console): add the overview and the credit ledger"
```

---

## Task 16: Settings

**Files:**
- Create: `apps/console/src/app/(dashboard)/settings/account/page.tsx`, `apps/console/src/app/(dashboard)/settings/api-keys/page.tsx`, `apps/console/src/app/(dashboard)/settings/organization/page.tsx`
- Create: `apps/console/src/app/_components/api-keys-panel.tsx`, `apps/console/src/app/_components/account-form.tsx`

**Interfaces:**
- Consumes: `GET/POST/DELETE /api/v1/api-keys` from Task 5; `GET /api/v1/organization` from Task 4; `.table`, `.card`, `.page*` from Task 9.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the api-keys panel**

A Client Component, because minting a key must display a secret that exists only in the POST response and must never be re-fetched.

```tsx
"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

type Key = {
  id: string;
  name: string;
  key_prefix: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
};

export function ApiKeysPanel({ keys }: { keys: Key[] }) {
  const router = useRouter();
  // THE MINTED SECRET LIVES HERE AND NOWHERE ELSE. It is in the POST response and in no row, no
  // list and no log; a lost key is replaced, never looked up. Holding it in component state is
  // what makes "shown once" literally true.
  const [minted, setMinted] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function create(formData: FormData) {
    setBusy(true);
    setError(null);
    try {
      const response = await fetch("/api/v1/api-keys", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ name: String(formData.get("name") ?? "") }),
      });
      if (!response.ok) {
        setError(
          response.status === 403
            ? "Only a console session can mint a key."
            : "Could not mint a key just now. Try again.",
        );
        return;
      }
      const body = (await response.json()) as { presented: string };
      setMinted(body.presented);
      router.refresh();
    } catch {
      setError("Could not reach the API. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: string) {
    setBusy(true);
    try {
      await fetch(`/api/v1/api-keys/${id}`, { method: "DELETE" });
      router.refresh();
    } catch {
      setError("Could not revoke that key. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <form action={create} className="card" style={{ display: "grid", gap: ".7rem" }}>
        <label className="auth__field">
          <span className="label">Key name</span>
          <input className="auth__input" name="name" placeholder="ci" type="text" />
        </label>
        <button className="btn btn--gold" disabled={busy} type="submit">
          Mint a key
        </button>
      </form>

      {minted ? (
        <div className="card" role="status">
          <p className="label">Copy this now — it is not shown again</p>
          <code style={{ wordBreak: "break-all" }}>{minted}</code>
        </div>
      ) : null}

      {error ? (
        <div className="auth__error" role="status">
          {error}
        </div>
      ) : null}

      {keys.length === 0 ? (
        <p className="empty">No keys yet. A key lets a script submit runs as you.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Prefix</th>
              <th>Created</th>
              <th>Last used</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {keys.map((key) => (
              <tr key={key.id}>
                <td>{key.name || "—"}</td>
                <td>
                  <code>{key.key_prefix}</code>
                </td>
                <td>{key.created_at ? new Date(key.created_at).toLocaleDateString() : "—"}</td>
                <td>
                  {key.last_used_at ? new Date(key.last_used_at).toLocaleDateString() : "never"}
                </td>
                <td>
                  {key.revoked_at ? (
                    <span className="status status--fail">revoked</span>
                  ) : (
                    <button
                      className="btn"
                      disabled={busy}
                      onClick={() => revoke(key.id)}
                      type="button"
                    >
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
```

- [ ] **Step 2: Write the API keys page**

```tsx
// apps/console/src/app/(dashboard)/settings/api-keys/page.tsx
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { ApiKeysPanel } from "@/app/_components/api-keys-panel";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type Key = {
  id: string;
  name: string;
  key_prefix: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
};

export default async function ApiKeysPage() {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  const keys = await consoleFetch<{ items: Key[] }>("api-keys", ownerId);

  return (
    <div className="page">
      <div className="page__head">
        <h1 className="page__title">API keys</h1>
      </div>
      <p className="page__sub">
        A key acts as you against the API. The secret half is shown once, when it is minted, and
        is not recoverable — a lost key is replaced, never looked up.
      </p>
      {keys === null ? (
        <p className="empty">Could not reach the API just now. Reload to try again.</p>
      ) : (
        <ApiKeysPanel keys={keys.items} />
      )}
    </div>
  );
}
```

- [ ] **Step 3: Write the organisation page**

```tsx
// apps/console/src/app/(dashboard)/settings/organization/page.tsx
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type Member = { email: string; name: string; role: string };

type OrganizationView = {
  organization: { id: string; name: string; slug: string } | null;
  members: Member[];
};

export default async function OrganizationPage() {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  const view = await consoleFetch<OrganizationView>("organization", ownerId);

  return (
    <div className="page">
      <div className="page__head">
        <h1 className="page__title">Organisation</h1>
      </div>

      {view === null ? (
        <p className="empty">Could not reach the API just now. Reload to try again.</p>
      ) : (
        <>
          <div className="card">
            <p className="label">Name</p>
            {/* The route degrades to an owner-derived view rather than erroring when the
                organisation cannot be resolved, so a null here is a degraded read, not an
                account without a tenant. */}
            <p>{view.organization?.name ?? "Not resolved"}</p>
            <p className="label" style={{ marginTop: ".8rem" }}>
              Slug
            </p>
            <p>
              <code>{view.organization?.slug ?? "—"}</code>
            </p>
          </div>

          <p className="page__sub">Inviting people is not available yet.</p>

          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Email</th>
                <th>Role</th>
              </tr>
            </thead>
            <tbody>
              {view.members.map((member) => (
                <tr key={member.email}>
                  <td>{member.name}</td>
                  <td>{member.email}</td>
                  <td>
                    <span className="label">{member.role}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Write the account page**

```tsx
// apps/console/src/app/(dashboard)/settings/account/page.tsx
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { AccountForm } from "@/app/_components/account-form";
import { ownerIdFromSession } from "@/lib/auth/session";

export default async function AccountPage() {
  const session = await auth();
  const ownerId = ownerIdFromSession(session);
  if (!ownerId) {
    redirect("/sign-in");
  }

  return (
    <div className="page">
      <div className="page__head">
        <h1 className="page__title">Account</h1>
      </div>

      <div className="card">
        <p className="label">Email</p>
        <p>{ownerId}</p>
        {/* Decision 4 of the 09-10 signup design: owner_id IS the lowercased address, and every
            job, geometry and chat session is scoped on it. Changing it here would strand all of
            them, so the UI does not offer it. */}
        <p className="page__sub">
          Your address identifies your account and cannot be changed here.
        </p>
      </div>

      <AccountForm name={session?.user?.name ?? ""} />
    </div>
  );
}
```

- [ ] **Step 5: Write the account form**

```tsx
"use client";

import { useState } from "react";
import { updatePassword, updateProfile } from "firebase/auth";

import { firebaseAuth } from "@/lib/firebase/client";

export function AccountForm({ name }: { name: string }) {
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(formData: FormData) {
    setBusy(true);
    setNote(null);
    const user = firebaseAuth().currentUser;
    if (!user) {
      setNote("Your session expired. Sign in again.");
      setBusy(false);
      return;
    }
    try {
      const displayName = String(formData.get("name") ?? "").trim();
      if (displayName && displayName !== name) {
        await updateProfile(user, { displayName });
      }
      const password = String(formData.get("password") ?? "");
      if (password) {
        await updatePassword(user, password);
      }
      setNote("Saved.");
    } catch (cause) {
      // Identity Platform requires a RECENT sign-in for a password change and says so with its
      // own code. Anything else is reported without a diagnosis this cannot know.
      const code = (cause as { code?: string })?.code ?? "";
      setNote(
        code === "auth/requires-recent-login"
          ? "Sign out and back in, then change your password."
          : "Could not save that. Try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <form action={submit} className="auth__form">
      <label className="auth__field">
        <span className="label">Display name</span>
        <input className="auth__input" defaultValue={name} name="name" type="text" />
      </label>
      <label className="auth__field">
        <span className="label">New password</span>
        <input
          autoComplete="new-password"
          className="auth__input"
          minLength={8}
          name="password"
          type="password"
        />
      </label>
      {note ? (
        <div aria-live="polite" className="auth__error auth__note" role="status">
          {note}
        </div>
      ) : null}
      <button className="btn btn--gold" disabled={busy} type="submit">
        {busy ? "Saving…" : "Save"}
      </button>
    </form>
  );
}
```

- [ ] **Step 6: Verify**

Run: `cd apps/console && pnpm test && pnpm typecheck && pnpm lint && pnpm build`
Expected: PASS. `pnpm build` is the first full compile of every new route.

- [ ] **Step 7: Run the whole repository suite**

Run: `pytest tests/unit -q && make test-ui`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add apps/console/src/app
git commit -m "feat(console): add account, API key and organisation settings"
```

---

# Phase 4 — The marketing site

## Task 17: hexera.ai links to the console

**Files (in `/Users/kitts/Documents/dev/hexera/hexera-site`, a SEPARATE repository — commit there, not here):**
- Modify: `index.html` (nav, footer)
- Modify: `contact.html` (nav, footer)

**Interfaces:**
- Consumes: the console's `/sign-in` and `/sign-up` routes from Tasks 11 and 12.
- Produces: nothing this repository reads.

**Assumption 1 from the spec applies here** and is the reversible part: the gold CTA becomes `Sign up`, and "Join waitlist" survives in the footer. If the product should stay waitlist-gated, keep the CTA as-is and add Sign in as a plain `.nav__link` only.

- [ ] **Step 1: Confirm you are in the other repository**

```bash
cd /Users/kitts/Documents/dev/hexera/hexera-site && git status --short && git rev-parse --abbrev-ref HEAD
```

Expected: a clean tree. If it is dirty, stop and ask — this is somebody else's working state.

- [ ] **Step 2: Update the nav in both pages**

Replace the `nav__end` block in `index.html` and `contact.html`:

```html
    <div class="nav__end">
      <a class="nav__link" href="contact.html">Contact</a>
      <a class="nav__link" href="https://console.hexera.ai/sign-in">Sign in</a>
      <a class="btn btn--ghost nav__cta cta-mono" href="https://console.hexera.ai/sign-up">Sign up <span aria-hidden="true">↗</span></a>
    </div>
```

- [ ] **Step 3: Update the footer's "Get started" column in both pages**

```html
        <div class="fend__col">
          <span class="fend__col-h">Get started</span>
          <a href="https://console.hexera.ai/sign-up">Create an account</a>
          <a href="https://console.hexera.ai/sign-in">Sign in</a>
          <a href="contact.html">Join waitlist</a>
          <a href="contact.html">Contact</a>
        </div>
```

- [ ] **Step 4: Check the pages still render**

```bash
python3 -m http.server 8080 --directory /Users/kitts/Documents/dev/hexera/hexera-site
```

Open `http://localhost:8080/index.html` and `http://localhost:8080/contact.html`. Confirm the nav shows Contact · Sign in · Sign up, the CTA keeps its `.cta-mono` treatment, and the hero demo still plays.

- [ ] **Step 5: Commit in the site repository**

```bash
cd /Users/kitts/Documents/dev/hexera/hexera-site
git add index.html contact.html
git commit -m "feat: link the nav and footer to the console"
```

- [ ] **Step 6: Return to this repository**

```bash
cd /Users/kitts/conductor/workspaces/hexera-platform/hong-kong
```

---

# Final verification

- [ ] **Run everything**

```bash
pytest tests/unit -q
make test-ui
cd apps/console && pnpm test && pnpm typecheck && pnpm lint && pnpm build
```

- [ ] **Confirm the parity gate specifically**

```bash
pytest tests/unit/deploy/test_console_ui_copy_parity.py -v
diff -rq ui apps/console/public/static --exclude index.html
```

Expected: no output from `diff`.

- [ ] **Walk the flow by hand**

Sign up with a fresh address → land on `/verify-email` → confirm the console is unreachable at `/`, `/runs`, `/usage` → click the link in the inbox → press "I've verified — continue" → land on the overview with a credit balance → mint an API key → confirm the secret shows once and the list shows only its prefix → present that key as `Authorization: Bearer` to `POST /api/v1/api-keys` and confirm a **403**.
