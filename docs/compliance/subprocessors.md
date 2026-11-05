# Subprocessors

Every third party that receives customer personal data. Before adding one: sign or accept its DPA,
record the transfer mechanism, update this table and [records-of-processing.md](records-of-processing.md),
and give customers the notice their contract requires.

| Subprocessor | Service | Data | Location | DPA | Transfer mechanism | Last reviewed |
|---|---|---|---|---|---|---|
| Google Cloud (Cloud Run, Cloud SQL, Memorystore, Cloud Storage, Cloud Logging, Compute Engine) | Hosting, database, storage, logs | All customer data | US (`us-central1`) | Cloud Data Processing Addendum — *confirm accepted in the console* | SCCs in the CDPA; DPF | — |
| Google Identity Platform + reCAPTCHA Enterprise | Sign-in, bot scoring | Email, uid, password hash, risk signals | US/global | Cloud Data Processing Addendum | SCCs; DPF | — |
| Stripe | Payments | Billing contact, card (never seen by Hexera), subscription | US | Stripe DPA (in Stripe Services Agreement) | SCCs; DPF | — |
| OpenAI | Model inference for intake and review | Conversation text, geometry descriptions, inspection images | US | OpenAI DPA — *must be executed* | SCCs | — |
| DeepSeek | Model inference, only where a route selects it | As above | China | *Confirm DPA and transfer basis before routing EU customer data* | — | — |
| DeepInfra | Model inference, only where a route selects it | As above | US | *Confirm DPA* | SCCs | — |
| Tavily | Web search, only when `WEB_SEARCH_PROVIDER=tavily` | Search queries | US | *Confirm DPA* | — | — |

A provider with no confirmed DPA and transfer basis must not receive EU customers' data in
production. Model routes are configured in `settings/routes.py`; review this table whenever a
production route changes.
