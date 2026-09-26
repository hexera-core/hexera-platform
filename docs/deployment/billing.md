# Billing: turning Stripe on for an environment

How an environment starts charging, and what an operator does by hand that the deploy cannot. Billing
is **opt-in per environment**. If any of the settings below is empty, that environment does not
charge: the API answers its billing routes `503`, the console's Billing page says plans are not on
sale, and the deploy skips the meter sweep and says why.

Design and model: #47 (hybrid tiers, overage metered in arrears), the credit gate (a signup grant is a
budget, not a free tier) and overage-only metering (0008).

---

## 1. What charges, and when

| Event | Ledger | Stripe |
|---|---|---|
| Signup | `grant` of `SIGNUP_GRANT_CREDITS` | nothing |
| A period is paid (`invoice.paid`, `subscription_create`/`subscription_cycle`) | `grant` of the tier's `included_credits` | the flat recurring price |
| A job **succeeds** | `debit` of `JOB_BASE_CREDITS + minutes × CREDITS_PER_MESH_MINUTE` | nothing yet |
| …and the balance did not cover all of it, **for a paying tenant** | the uncovered part is recorded as the debit's `overage` and paid back as a `refund` | the meter sweep reports that `overage` to the meter within ~15 minutes |
| A job fails | nothing | nothing |

A tenant **without** a paid plan never accrues overage. Its balance can go slightly negative, which
is the last run's minutes, and the credit gate then refuses its next run until it buys a plan.

## 2. In Stripe, once per Stripe account (sandbox and live separately)

1. **A meter** with event name **`mesh_job_credits`**, aggregation **sum**, customer mapping on
   `stripe_customer_id`. The name is fixed in code (`adapters/stripe_billing.OVERAGE_EVENT_NAME`).
   If the name is wrong nothing fails: usage just goes into a meter nobody bills on.
2. **Per purchasable tier (`starter`, `team`)** a Product with two Prices:
   - a **recurring monthly** flat price → `STRIPE_PRICE_<TIER>`
   - a **metered** usage price on the meter above, per credit → `STRIPE_PRICE_<TIER>_OVERAGE`
   `enterprise` gets no price. It is invoiced from the admin console.
3. **A webhook endpoint** at `https://<api-host>/api/v1/webhooks/stripe`, sending exactly:
   `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`,
   `customer.subscription.deleted`, `invoice.paid`, `invoice.payment_failed`. Its signing secret
   is the `whsec_…` value below.
4. **A restricted key** (`rk_…`) with write access to Customers, Subscriptions, Checkout Sessions,
   Invoices, Invoice Items, Billing Portal and Meter Events. Never use a secret key (`sk_…`): it can
   also move money out.
5. **Customer portal** enabled (Settings → Billing → Customer portal) with cancellation, payment
   method updates and **subscription updates across the Starter and Team products**. The console
   sends a subscriber to the portal to change tier, and the API refuses a second checkout, because
   checkout would create a second subscription rather than change the first. Without subscription
   updates in the portal, a customer cannot upgrade at all.

## 3. In Secret Manager, per GCP project

Put a value in each container **before** naming it to the deploy. A reference to a container with no
version produces a revision that never becomes ready.

```bash
printf '%s' 'rk_live_…'  | gcloud secrets versions add stripe-api-key        --project <project> --data-file=-
printf '%s' 'whsec_…'    | gcloud secrets versions add stripe-webhook-secret --project <project> --data-file=-
openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add admin-api-key --project <project> --data-file=-
```

`create-secrets.sh` creates the three containers. `admin-api-key` is the admin console's
cross-tenant billing credential, and nothing else presents it.

## 4. In GitHub, per environment (Settings → Environments → `dev` / `prod` → Variables)

| Variable | Value |
|---|---|
| `STRIPE_API_KEY_SECRET` | `stripe-api-key` |
| `STRIPE_WEBHOOK_SECRET_SECRET` | `stripe-webhook-secret` |
| `ADMIN_API_KEY_SECRET` | `admin-api-key` |
| `STRIPE_PRICE_STARTER`, `STRIPE_PRICE_STARTER_OVERAGE`, `STRIPE_PRICE_TEAM`, `STRIPE_PRICE_TEAM_OVERAGE` | the `price_…` ids from §2 |
| `CONSOLE_BASE_URL` | the console's public origin, e.g. `https://console.hexera.ai`. Optional when the environment has a `CONSOLE_DOMAIN`, which it defaults to. Required otherwise, and the API stage refuses rather than send a paying customer back to `localhost` |

These are container **names** and price ids. No value here is a secret. Then deploy `app=true`.
The API stage binds the credentials and price ids, and the new **Meter sweep** stage provisions
`<deployment>-meter-sweep`, a Cloud Run job on the application image running as the API's identity
every 15 minutes.

## 5. Proving it works

```bash
# the catalogue as the deployment sees it: billing_enabled true, starter/team purchasable
curl -s https://<api-host>/api/v1/billing/plans -H "x-api-key: …"
# the sweep ran, and what it did
gcloud run jobs executions list --job <deployment>-meter-sweep --region us-central1 --project <project>
gcloud logging read 'resource.labels.job_name="<deployment>-meter-sweep"' --limit 20 --freshness 1h
```

End to end, in a Stripe **sandbox**:
1. Buy Starter from `/settings/billing` with card `4242 4242 4242 4242`.
2. Check that the page returns with the success banner and, within a minute, shows Starter and a
   balance raised by the allowance.
3. Run meshes past the allowance.
4. Check that the job's next execution logs `reported > 0` and that the customer's upcoming invoice
   in Stripe shows the overage.

## 6. Turning it off

Clear `STRIPE_API_KEY_SECRET` in the environment's variables and redeploy `app=true`. The API stage
drops the Stripe bindings and price ids without needing `API_ENV_PRUNE`, and the billing routes
return `503`. The sweep stage **pauses** the existing schedule rather than deleting the job, so
turning billing back on resumes it. Existing subscriptions keep billing in Stripe until they are
cancelled there.
