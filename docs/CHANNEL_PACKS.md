# Channel packs

Three packs, each paid for on its own, or all three together at a discount. A workspace holds any mix.

| Pack | Price / 30 days (before 18% GST) | Allowance | Also unlocks |
|---|---|---|---|
| **Call** | ₹2,199 | 250 AI call minutes | outbound campaigns, call recording, 2 phone numbers, 2 calls at once, 100 outbound calls a day, 15 minute calls |
| **WhatsApp** | ₹899 | 1,000 WhatsApp messages | |
| **Email + SMS** | ₹449 | 3,000 customer emails and 500 SMS segments | |
| **All three** | ₹3,192.30 (10% off ₹3,547) | the three allowances | |

Any pack also gives the workspace 3 AI agents, 5 team members, 25 knowledge documents and 3 API keys.
The prices and allowances are in `CATALOGUE` in `app/services/packs.py`. The bundle discount is
`PACK_BUNDLE_DISCOUNT_PERCENT` (default 10, capped at 50). Only the full set of three gets it.
The prices are the lowest round figures at which even the worst case (every call outbound, every WhatsApp message a
marketing message) still earns a margin. At full use the margins are 29% (Call), 70% (WhatsApp) and 69% (Email + SMS), and
49%, 81% and 81% at 60% use. Competitors, costs and assumptions are in `docs/PRICING_ANALYSIS.md`; rerun
`python scripts/cost_model.py` whenever a provider's price or the exchange rate moves.

## How a workspace buys

1. `GET /api/v1/packs/catalogue`: the packs, the bundle, GST for the workspace's state, and whether it may buy.
2. `POST /api/v1/packs/purchases` with `{"items": ["call", "whatsapp"], "provider": "razorpay" | "paypal",
   "billing_name", "gstin"?, "state_code"?}`. Pick one pack, two, or all three (all three is priced as the bundle).
   There is no amount in the request. The reply has the order for the browser to pay.
3. Razorpay: the browser calls `POST /packs/purchases/{id}/verify` with the payment id and signature.
   PayPal: `POST /packs/purchases/{id}/capture`, or `POST /packs/paypal/capture` with the order id after the redirect.
   Both providers' webhooks apply the purchase if the browser never comes back.
4. `GET /packs` shows what is held, when each pack ends and how much is used. `GET /packs/purchases` lists payments
   and `GET /packs/purchases/{id}/invoice` is the GST tax invoice (series `CPK/<financial year>/<number>`).

Each payment is one order and one invoice. Paying for a pack again before it ends extends it by 30 days and adds the
allowance to the same window. A pack that has ended allows nothing until it is bought again.

## What is enforced

* A workspace with no Call pack cannot place or answer AI calls; with no WhatsApp pack it cannot send WhatsApp;
  with no Email + SMS pack it cannot send customer email or SMS. The 402 says which pack to buy
  (`pack_required`) or that the allowance is used up (`pack_minutes_exhausted`, `pack_whatsapp_exhausted`,
  `pack_email_exhausted`, `pack_sms_exhausted`). They are hard caps: nothing is billed past the allowance.
* Usage is counted inside each pack's own 30 days, so what was used before buying never counts.
* Only a workspace on **Free** (or already on packs) can buy. A workspace on a monthly plan, pay-as-you-go or legacy gets a
  409: two billing models never run at the same time. The first purchase moves a Free workspace onto packs.

## Why it is safe

* **Prices come from the server.** The request names packs only. Extra fields such as an amount are ignored.
* **Nothing is granted before payment is proved.** Razorpay's checkout signature or a verified webhook, or a PayPal
  capture of exactly the dollars quoted, and the amount paid must equal the order total to the paisa.
* **Applied once.** The purchase row and the workspace row are locked, and a paid purchase is never applied again,
  so a double click, a retry or a webhook after the browser changes nothing.
* **Only your own orders.** Every route is scoped to the caller's workspace and needs the billing permission.
  A user cannot verify, capture or read another workspace's purchase.
* `tenants.pack_state` is written only by the credit step. It is not in any request schema.
* No more than 10 unpaid orders an hour per workspace. The owner's email must be verified. The audit log records
  `pack.purchase_started` and `pack.purchased` with the packs and total.
* Tables are row-level-secured like the other tenant tables.

## Not built yet

* **No automatic renewal.** Each pack is paid for 30 days at a time and is renewed by paying again. Auto-debit
  needs Razorpay Subscriptions with a plan created in the Razorpay dashboard for each pack and the bundle. Say so and
  it can be added on top of this, using the same `credit_purchase`.
* No renewal reminder email before a pack ends.
* A platform admin can set a workspace's tier to `packs`, but cannot yet grant a pack without a payment.
* Unused allowance does not carry over, and there are no refunds in the API.
