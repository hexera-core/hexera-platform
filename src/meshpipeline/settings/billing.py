# Responsibility: Read the deployment's Stripe configuration and answer whether billing is on.
# Boundaries: values and one predicate; it calls no API, and what a plan is ALLOWED to consume is
#             settings/plans.py. Nothing here decides policy.
from __future__ import annotations

from meshpipeline.settings.env import optional_env

#: THE SECRET KEY. Blank is the ordinary state, not a misconfiguration: every developer checkout and
#: every test run has no Stripe credential, and the whole billing surface is expected to be off
#: there. `enabled()` below is what the routes ask, so a blank key disables cleanly rather than
#: failing at the first call.
#:
#: A RESTRICTED KEY (rk_) IS THE INTENDED VALUE, not a secret key (sk_). This process needs to read
#: and write customers, subscriptions, checkout sessions and invoices - and nothing else. A secret
#: key additionally carries the power to move money out, issue refunds and rotate credentials, none
#: of which any code path here uses. The narrower key is the one whose leak is survivable.
STRIPE_API_KEY: str = optional_env("STRIPE_API_KEY", "")

#: THE WEBHOOK SIGNING SECRET (whsec_...). Separate from the API key and not derivable from it: this
#: one proves an inbound request came from Stripe. The webhook route REFUSES when it is blank rather
#: than trusting the body - see api/v1/stripe_webhook.py. An unverified webhook endpoint is an
#: unauthenticated write to the credit ledger, which is the whole reason this is not optional.
STRIPE_WEBHOOK_SECRET: str = optional_env("STRIPE_WEBHOOK_SECRET", "")

#: THE PINNED API VERSION. Pinned in code rather than inherited from the account's dashboard default
#: so that an account-level version bump - which someone else can perform, in a browser, without
#: touching this repository - cannot change the shape of the objects this code parses. Moving it is
#: a commit, with a diff and a review.
STRIPE_API_VERSION: str = "2026-08-26.dahlia"

#: WHERE STRIPE SENDS THE BROWSER BACK after checkout. The console's origin, not the API's: the
#: person is in the console when they start, and returning them to the API would strand them on a
#: JSON body.
CONSOLE_BASE_URL: str = optional_env("CONSOLE_BASE_URL", "http://localhost:3000")

#: THE PRICE IDS, one per tier, resolved from configuration rather than written here.
#:
#: WHY THESE ARE NOT CONSTANTS IN THIS FILE. A price id names an object inside ONE Stripe account.
#: The sandbox a developer tests against, the CI sandbox and the live account each mint their own,
#: so a literal here would be correct in exactly one environment and silently wrong in the other
#: two - and "wrong price id" surfaces as a failed checkout for a real customer, not as a test
#: failure. Configuration is also what lets pricing change without a deploy.
#:
#: Blank means the tier is not purchasable in this deployment, which is the honest state for a
#: checkout before anyone has created the prices.
STRIPE_PRICE_STARTER: str = optional_env("STRIPE_PRICE_STARTER", "")
STRIPE_PRICE_TEAM: str = optional_env("STRIPE_PRICE_TEAM", "")

#: THE METERED OVERAGE PRICES, billed in arrears alongside the tier's flat fee. The hybrid shape is
#: one subscription carrying two items: the flat tier price above, and the metered price below that
#: only draws once the tier's included allowance is spent.
STRIPE_PRICE_STARTER_OVERAGE: str = optional_env("STRIPE_PRICE_STARTER_OVERAGE", "")
STRIPE_PRICE_TEAM_OVERAGE: str = optional_env("STRIPE_PRICE_TEAM_OVERAGE", "")


def enabled() -> bool:
    # THE ONE QUESTION every caller asks before touching Stripe. A deployment without credentials is
    # not broken - it is a developer checkout, a test, or a self-hosted install that does not charge
    # - and the routes answer 503 rather than raising, so the rest of the product serves normally.
    #
    # BOTH CREDENTIALS, NOT JUST THE API KEY. With a key and no webhook secret the product would
    # take money and never learn that it had: checkout succeeds, the customer is charged, and then
    # every `checkout.session.completed`, `customer.subscription.created` and `invoice.paid` fails
    # signature verification at the door. The subscription is never recorded, the plan never moves,
    # and the period's allowance is never granted - a paying customer on the free tier, with no
    # error anywhere that names the cause.
    #
    # Half-configured is the dangerous state precisely because it LOOKS configured. Refusing the
    # whole surface keeps the two honest states - charging, or not charging - and removes the third.
    #
    # Read through the module so an operator's value and a test's monkeypatch both reach here.
    import meshpipeline.settings.billing as self

    return bool(self.STRIPE_API_KEY.strip()) and bool(self.STRIPE_WEBHOOK_SECRET.strip())


def price_for(plan: str) -> tuple[str, str]:
    # Read through the module rather than binding at import, the discipline settings/plans.py and
    # credit_service already follow: an operator's value and a test's monkeypatch must both reach
    # here. Returns (flat price, metered overage price); either may be blank.
    import meshpipeline.settings.billing as self

    table = {
        "starter": (self.STRIPE_PRICE_STARTER, self.STRIPE_PRICE_STARTER_OVERAGE),
        "team":    (self.STRIPE_PRICE_TEAM,    self.STRIPE_PRICE_TEAM_OVERAGE),
    }
    return table.get(plan.strip().lower(), ("", ""))
