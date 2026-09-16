# The console becomes a dashboard — design

Date: 2026-09-10
Status: proposed
Scope: the product console's information architecture, its visual system, and the five read
endpoints the architecture needs. Plus a nav commit in the `hexera-site` repository.

Amends: §4 of [`2026-09-10-signup-and-credits-design.md`](2026-09-10-signup-and-credits-design.md).
That section's Identity Platform decision, `/auth/session` contract, linking rules and schema all
survive intact. Its single sentence "An unverified email may sign in to a new account and is shown
a persistent banner" is reversed by decision 4 here.

## 1. Why this exists

The console is one page. `apps/console/src/app/(console)/page.tsx` renders an upload bar, a chat
column, a mesh workbench and a header of status chips, and that is the entire authenticated
product. There is nowhere to see a past run, no way to read the credit ledger that
`0003_identity_and_credits` created, and no way to mint an API key even though
`api_key_repository` has implemented `create`, `list_for_owner`, `revoke` and `mark_used` since
`0002_api_keys`.

The three authentication pages are worse. Each reuses the console's own chat scaffold — `#app`,
`#stage`, `.chat-col`, `#empty` — as a login form container, styles its inputs from a
`CSSProperties` object in `auth-form-styles.ts`, and puts a `role="status"` chip in the top right
that reports the name of the page you are already looking at ("sign in required", "create an
account", "reset your password"). Nothing about the surface resembles hexera.ai.

This cycle gives the product a shell it can grow into: a sidebar, a route per concern, an account
menu, and a visual system taken from the marketing site rather than approximated.

### Verified starting facts

Each read from the code in this repository on 2026-09-10, and from
`/Users/kitts/Documents/dev/hexera/hexera-site` at the same commit its `?v=` query strings name.

| Fact | Evidence |
|---|---|
| The product API lists nothing | Every route in `api/v1/` is fetch-by-id except `GET /api/v1/credits`; `simulation.py` has `/{job_id}`, `/{job_id}/surface`, `/{job_id}/surface.vtk`, `/{job_id}/dispute` and no collection route |
| `api_key_repository` is complete and unreachable | `create`, `list_for_owner`, `revoke`, `mark_used` all exist; `grep -rn api_key src/meshpipeline/api/` matches only `MESH_API_KEY` header checks and `security.py`'s bearer path |
| `job_repository` cannot list | Only `get_for_owner`, `get_internal`, `count_active_for_owner`, `count_total_active` |
| `session_repository` cannot list | Only `get_for_owner`, `get_internal`, `get_by_job_id`, `get_for_update` |
| `credit_ledger_repository` cannot list | `append` and `balance`, nothing else |
| `membership_repository` cannot list members | `create` and `organization_id_for_email` |
| The credential kind is already on the Principal | `Credential.api_key` / `signed_header` / `self_asserted` in `contracts/identity.py`; `Principal.credential` and `Principal.key_id` |
| One predicate scopes every tenant read | `tenant_scope.scope(model, owner_id=, organization_id=)`, which falls back to `owner_id` when the organisation is absent or malformed |
| `main.js` already resolves a deep link | `bootLive()` reads `?job=<id>`: re-attaches to a live stream with `startStream(0)`, or `replay()`s a finished run and shows its result and mesh |
| `main.js` owns the URL after a run starts | `attachJob` calls `history.replaceState(null, "", location.pathname + "?job=" + id)` |
| The two UI trees must stay byte-identical | `tests/unit/deploy/test_console_ui_copy_parity.py` diffs `ui/` against `apps/console/public/static/`, excluding only `index.html` |
| Tailwind is installed and unused | `tailwindcss` and `@tailwindcss/postcss` in `apps/console/package.json` devDependencies; `src/app/globals.css` is a single comment line |
| The console's tokens already aim at the site | `ui/css/tokens.css` names `#ff4f00`, Saira Semi Condensed, Geist and Geist Mono, and its header comment says it wears "the finish hexera.ai actually ships" |
| …but has drifted from it | Console `--canvas` is `#0a0a09` and `--background` `#0f0f0e`; the site's `orange-theme.css` sets `--bg: #0d0d0c`. Console requests Saira at 400;500;600; the site requests 300;400;500;600 |
| The site's non-hero pages carry no canvas | `contact.html` is `.page-bg` + `.grain` + `.nav` + a form + `.footer`; `fluid.js` and the mesh canvas are `index.html` only |
| The site has no console entry point | `index.html` and `contact.html` `nav__end` is `Contact` plus a `Join waitlist` CTA pointing at `contact.html` |
| The consoles live on their own subdomains | `2026-09-09-console-domains-design.md`: `console.hexera.ai`, `admin.hexera.ai`, and `dev.` variants |
| The admin console already solved the shell | `(admin)/layout.tsx` is a sidebar plus `ADMIN_SECTIONS`, where `available: false` renders as text because "linking to a page that cannot render is worse than saying why it is not there" |
| `emailVerified` is frozen into the session JWT | `auth.ts`'s `jwt` callback copies it from `user` on sign-in only; nothing re-reads it per request |

## 2. Goals and non-goals

**Goals**

- A left sidebar with a route per concern, and an account menu, replacing the chip row.
- Past runs, past conversations, the credit ledger, the organisation's members and API keys are
  all readable from the console.
- API keys can be minted and revoked from the console — the first use of a repository that has
  been complete and unreachable for two cycles.
- Sign-up collects a name and a company, and the organisation is named after the company rather
  than after the user's email address.
- An unverified address cannot reach the product.
- The console and hexera.ai are visually one product: same fonts, same palette, same button, same
  mono-readout label voice.
- hexera.ai links to the console.

**Non-goals**

- Spending credits. Unchanged from the 09-10 signup design: the ledger is still write-once-on-grant
  and `/usage` is a read.
- Inviting members. `/settings/organization` lists the membership; it does not create one.
  `memberships` supports multi-user organisations and no UI writes to it.
- Renaming or deleting an organisation after provisioning.
- Changing your email address. Decision 4 of the 09-10 design still holds: `owner_id` is the
  lowercased email, and a change would drift it.
- SSO. Still a provider toggle nobody has turned on.
- Rewriting the workbench. `main.js`, `stream.js`, `viewer.js` and the chat/timeline/result
  stylesheets keep their behaviour; they get restyled and receive two additive changes to
  `main.js`'s boot and URL handling, and nothing else.
- `admin-console`. Unchanged. It contributes the shell pattern and takes nothing back.
- A migration. Five endpoints, four repository methods, no schema change.

## 3. Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | Port the site's CSS primitives into `ui/css/chrome.css` rather than approximate them | The console should wear the same `.btn--gold`, not a lookalike that drifts on the next site tweak. The values are lifted post-`orange-theme.css` override, which is what actually ships. |
| 2 | Plain CSS in the existing stylesheet tree; Tailwind stays unused | The visual scope is everything, including the `main.js`-driven chat, timeline, viewer and workbench sheets. Those are CSS either way, so a second token system in the same app buys nothing and guarantees the two copies of every value disagree eventually. |
| 3 | No canvas in the console | `contact.html` is the precedent: the site's own non-hero page is `.page-bg` + `.grain`. `fluid.js` is 40 KB and the console already ships `vtk.js`. |
| 4 | **An unverified address is blocked from the product**, reversing §4 of the 09-10 design | The product owner's call, made 2026-09-10. The reversed reasoning is recorded honestly: blocking accepts a real support burden, because Identity Platform's default sender is `noreply@<project>.firebaseapp.com` and lands in spam more often than a custom domain would, which turns a deliverability problem into a signup-blocking one rather than a dismissable nag. Accepted. |
| 5 | The verification wall re-mints the session rather than polling | `session.emailVerified` is written into the Auth.js JWT at `authorize()` time and never re-read. Clicking the link in the email cannot update it. See §5. |
| 6 | `organization_name` is honoured only on the provision path | It is a caller-supplied string on an endpoint that also resolves existing accounts. Accepting it on the link or uid paths would let any token rename an organisation it merely belongs to — or, on the link path, one it has just proven an address for but does not own. |
| 7 | An API-key credential may not manage API keys | `resolve_principal` accepts `Authorization: Bearer hx_live_…` on every `/api/v1` route. Without an explicit refusal, a leaked key mints its own replacements and revoking the original accomplishes nothing. `Principal.credential` already carries the distinction, so the gate is one comparison. |
| 8 | Keyset pagination on `(created_at, id)` descending, not `OFFSET` | A ledger and a run list are both append-mostly and read newest-first, where `OFFSET` skips or repeats rows as new ones arrive mid-scroll. |
| 9 | No index migration this cycle | `simulation_jobs` and `chat_sessions` index `owner_id` and `organization_id` singly; a filtered, ordered read will index-scan then sort. At present row counts that is not worth a migration. The trigger to add `(organization_id, created_at DESC)` is a `/runs` page that measures slow, not a hypothetical. `credit_ledger` already has `ix_credit_ledger_org_created`, which serves `/usage` exactly. |
| 10 | `main.js` keeps its `?job=` contract; the new routes add to it | `ui/index.html` is still served from `STATIC_DIR` and still deep-links that way, and the parity test ships both copies. Two additive reads (`__HEXERA_BOOT_JOB__`, `__HEXERA_ROUTED__`) are a far smaller change than re-homing the contract. |
| 11 | Sections keep the admin console's `available` flag even though all are available | The field is the honest way to add a section before its data exists, and this cycle is not the last one. Removing it and re-adding it later is the same work twice. |
| 12 | `/settings/organization` degrades to an owner-derived view rather than erroring | `credits.py` set the precedent: an organisation-less caller reads `0` rather than a failure, because that state is a deployment between the migration and the image that fills the column. The same reasoning applies here. |

### Assumptions taken without an explicit answer

Recorded because they were raised and the approval was general. Any of the three can be reversed
without touching the architecture.

1. **The gold CTA on hexera.ai becomes `Sign up`**, and "Join waitlist" survives in the footer's
   "Get started" column. This moves the site's funnel from waitlist-gated to open registration.
2. **`CONSOLE_SIGNUP_ENABLED` stays `true`.** If prod should remain waitlist-gated, `/sign-up`
   needs a designed "request access" state rather than today's bare redirect to
   `/sign-in?signup=closed`, and assumption 1 is wrong.
3. **The site keeps its own copy of the ported primitives.** `chrome.css` is a port, not a shared
   package: hexera-site is a static repository with no build step (`<link href="styles.css?v=65">`),
   so consuming an npm package there means giving it a bundler. §10 records the drift risk this
   accepts.

## 4. The visual system

### Fonts

One request, matching the site's exactly, replacing the narrower one in `legacy-styles.tsx`:

```
Saira+Semi+Condensed:wght@300;400;500;600
Geist:wght@300;400;500;600
Geist+Mono:wght@400;500
```

Weight 300 is the omission that matters. The site's display voice is Saira at 300 — tall, thin,
aerospace-signage — and the console requesting 400 as its lightest is why its headings read as a
generic dark dashboard.

### Roles

- **Saira Semi Condensed 300** — page titles only.
- **Geist 400–600** — body, form fields, table cells, buttons.
- **Geist Mono 500, uppercase, `letter-spacing: 0.16em`** — every label, eyebrow, sidebar nav item,
  table header and status word. This is the site's `.nav__link` treatment, and carrying it through
  the dashboard is the single largest contributor to the console reading as the same product.

### `ui/css/chrome.css` — new

The site's primitives, lifted from `styles.css` with `orange-theme.css`'s overrides already
applied, so the file states the values that actually ship:

`.page-bg`, `.grain`, `.nav`, `.brand`, `.brand__mark`, `.brand__word`, `.nav__link`, `.btn`,
`.btn--gold`, `.btn--ghost`, `.cta-mono`, `.fend__*`.

### `ui/css/tokens.css` — retuned

Re-anchor the elevation ladder on the site's real base rather than the drifted one: `--canvas`
becomes `#0d0d0c`, with `--background`, `--surface-1`, `--surface-2` and `--surface-raised`
re-derived as steps above it. Radius unifies on the site's `4px` for buttons and inputs, `2px` for
hairline chrome. Every existing role name survives; only values move, which is the same discipline
the file's own header comment already describes.

### `ui/css/auth.css` and `ui/css/dashboard.css` — new

The auth column and the sidebar shell respectively. Both consume `chrome.css` and `tokens.css` and
introduce no new colour.

### Where the stylesheets are loaded

`legacy-styles.tsx` today is imported by each of the four pages individually, because each page was
its own root. Under route groups it moves into the two layouts: `(auth)/layout.tsx` loads
`tokens.css`, `chrome.css` and `auth.css`; `(dashboard)/layout.tsx` loads those plus `shell.css`,
`chat.css`, `timeline.css`, `result.css`, `viewer.css`, `workbench.css`, `a11y.css`, `theme.css`
and `dashboard.css`. The auth pages stop shipping the chat, timeline, viewer and workbench sheets
they never used.

`apps/console/src/app/globals.css` stays the one-line stub it is. Decision 2 leaves Tailwind
uninvoked, and the file exists only because `layout.tsx` imports it.

## 5. Authentication

### `/sign-up`

Fields: name, company, email, password.

1. `createUserWithEmailAndPassword`
2. `updateProfile({ displayName: name })` — before the token is minted, so `token.name` carries it
   into `users.name` with no API change
3. `sendEmailVerification`
4. `getIdToken()`
5. `signIn("credentials", { idToken, organizationName })`

The Credentials provider gains a second field, and `authorizeFirebaseSession` forwards it to
`POST /auth/session` as `organization_name`.

Because `emailVerified` is false, `(dashboard)/layout.tsx` sends the new account to
`/verify-email`.

### `/verify-email`

A full page in the `(auth)` group — not a banner, and not inside the dashboard shell, so there is
no redirect loop. It states which address the mail went to, offers a resend (Identity Platform
applies its own throttling), offers sign-out, and does not offer an address change.

**Its continue control is the subtle part.** `auth.ts`'s `jwt` callback copies `emailVerified` from
`user` on sign-in and nothing re-reads it, so clicking the link in the email leaves the session JWT
saying `false` forever. The control must:

```
currentUser.reload()  ->  getIdToken(true)  ->  signIn("credentials", { idToken })
```

`getIdToken(true)` is what forces a fresh token carrying the new `email_verified` claim; without
the `true` the SDK returns the cached one and the wall never lifts for a user who has genuinely
verified. This is the failure mode most likely to ship silently, so it gets its own test.

### `(dashboard)/layout.tsx`

`redirect("/sign-in")` when there is no session or no owner id — today's rule, moved up from the
page. `redirect("/verify-email")` when `!session.emailVerified` — decision 4.

### Copy

Rewritten throughout in the product's own vernacular. The three status chips, and sentences like
"Sign in to open the Hexera console.", are deleted rather than restyled.

## 6. Route map

```
(auth)                        site chrome, .page-bg + .grain, no sidebar
  /sign-in
  /sign-up
  /forgot-password
  /verify-email

(dashboard)                   sidebar shell
  /                           overview: balance, last 5 runs, new-run CTA
  /runs                       list
  /runs/new                   the workbench
  /runs/[jobId]               the workbench, booted against an existing job
  /conversations
  /conversations/[sessionId]
  /usage                      balance + ledger
  /settings/account
  /settings/api-keys
  /settings/organization
```

`/sign-out` keeps its redirect to `/sign-in`.

The `(console)` route group is removed. Its single page becomes `(dashboard)/runs/new/page.tsx`,
which keeps the workbench markup, the `beforeInteractive` session script and the `main.js` tag; the
credit-balance fetch, the sign-out button and the verification banner leave it for the sidebar,
the account menu and `/verify-email` respectively.

### The sidebar

Brand → section nav → spacer → credit balance → API health dot → account menu (email, Settings,
Sign out). The health dot and the balance move here from the header, where they were chips.
`sections.ts` mirrors `ADMIN_SECTIONS`'s `{ href, label, available }` shape.

On `/runs/*` the sidebar renders as an icon rail so the mesh keeps its width; `workbench.css`
already implements the drawer behaviour this pairs with.

### The two `main.js` changes

Both additive, both preserving today's behaviour when the new globals are absent:

- `bootLive()` resolves the job as `params.get("job") || globalThis.__HEXERA_BOOT_JOB__`, so
  `/runs/[jobId]` can server-inject the id the same way `(console)/page.tsx` already injects
  `mg_uid` and the WebSocket base URL.
- `attachJob`'s `history.replaceState` writes `/runs/<id>` when `globalThis.__HEXERA_ROUTED__` is
  set, and today's `location.pathname + "?job=" + id` otherwise.

`ui/index.html` sets neither global and is unaffected.

## 7. Backend

Five endpoints and four repository methods. No migration.

| Route | Method added | Scoping |
|---|---|---|
| `GET /api/v1/simulation` | `job_repository.list_for_owner` | `tenant_scope.scope`, `owner_dep` + `org_dep` |
| `GET /api/v1/chat` | `session_repository.list_for_owner` | same |
| `GET /api/v1/credits/history` | `credit_ledger_repository.list_for_org` | `org_dep`; empty org reads empty, per `credits.py` |
| `GET /api/v1/organization` | `membership_repository.list_members` | `org_dep`; degrades per decision 12 |
| `GET/POST/DELETE /api/v1/api-keys` | none — repository is complete | `owner_dep` + `org_dep`, **plus decision 7's refusal** |

All list routes take `limit` (default 25, max 100) and an opaque `cursor` encoding
`(created_at, id)`, and answer newest-first.

### `POST /api/v1/api-keys`

Mints via `contracts/api_key.mint()` and returns `MintedKey.presented` — the only time the secret
exists outside the caller's hands. `key_hash` is stored, `secret` is not, and the response is
excluded from request-body logging the same way `/auth/session` already is.

Refuses with 403 when `principal.credential is Credential.api_key`, and that refusal is
deliberately *not* the undifferentiated 401 the rest of the auth surface uses: it discloses nothing
about account existence, and a caller acting on a key needs to be told the operation requires a
console session rather than left guessing their key is invalid.

### `POST /auth/session`

Gains an optional `organization_name`. `account_service.resolve_or_provision` forwards it to
`_provision`, which passes it to `organization_repo.create` instead of `token.email`. The uid path
and the linking path ignore it entirely — decision 6.

`_slug_for` is unchanged: it derives from the uid, not the name, and its injectivity is what keeps
`_provision`'s `IntegrityError` handler honest.

## 8. Degraded states

Every list page follows the pattern `creditBalance()` in `(console)/page.tsx` already
established — a bounded timeout raced against the fetch, the whole thing caught, rendering an
honest "could not load" row rather than propagating out of the server component and taking down the
page. That function's comment records why both halves are needed: `fetch()` rejects rather than
resolving on a connection failure, and a slow-but-not-failing API stalls the render without a time
bound.

`firebaseConfiguredOnServer()`'s "not configured for this deployment" message is kept on all four
auth pages. A console whose Identity Platform project was never set up must still say so rather
than present a form that fails on every submit.

## 9. Testing

- `tests/unit/deploy/test_console_ui_copy_parity.py` gates every stylesheet edit into both trees.
  It is not modified; it is the reason each CSS change is applied twice.
- One Python test per new endpoint covering the happy path, the empty-organisation fallback, and
  **tenant isolation** — that a caller scoped to one organisation cannot read another's rows.
- A dedicated test that `POST /api/v1/api-keys` refuses a `Credential.api_key` principal.
- Console `node --test` coverage for `sections.ts` and the auth message helpers, matching
  `nav.test.ts` and `auth-form-messages.test.ts`.
- A test for the `getIdToken(true)` refresh path in §5, because a cached token is the silent
  failure that would leave verified users walled out.

## 10. Costs accepted

- **`chrome.css` is a port, not a shared source.** hexera-site and the console can drift, and
  nothing gates it — unlike the two console trees, which the parity test does gate. Extracting a
  shared package means giving the static site a build step, which is a larger change than this
  cycle earns. Revisit when the site next gets one.
- **Blocking on verification costs support load.** Decision 4, and the `firebaseapp.com` sender is
  the reason. A custom sending domain in Identity Platform is the mitigation, and it is not in
  this cycle.
- **No composite index.** Decision 9. `/runs` will index-scan and sort until row counts say
  otherwise.
- **`main.js` gains two globals.** Small, additive, and untyped, in a file that is deliberately
  plain ES modules with no build step.

## 11. Open questions

1. Should `/runs` show disputes as their own rows, or fold a re-review into the run it amends?
   `dispute_operation_key` and `execution_generation` make either possible; the design assumes the
   latter.
2. Does `/settings/api-keys` need per-key expiry at creation? `api_keys.expires_at` exists and is
   nullable; the design mints without a term.
