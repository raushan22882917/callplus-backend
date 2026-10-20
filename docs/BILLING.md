# Billing: what it costs us, what we charge, and what the free tier allows

This is the analysis behind the plan tiers in `apps/api/app/services/plans.py`.
The numbers come from `scripts/cost_model.py`; run it again whenever a provider's
price changes (`python scripts/cost_model.py`). Every input there is labelled, and
the ones marked **ASSUMPTION** below could not be confirmed from the provider's own
page, so check them against your console before relying on the margins.

## Decisions

| | |
|---|---|
| **Free tier** | **10 AI call minutes in total** (once, not per month), at most 5 minutes per call, 50 WhatsApp messages, 1 agent, 1 number, 2 seats, 5 knowledge documents, 1 API key, no campaigns, no recording, one call at a time, 5 outbound calls a day |
| **Starter** | ₹2,999 / month, 300 minutes, 1,000 WhatsApp messages |
| **Growth** | ₹12,999 / month, 1,500 minutes, 5,000 WhatsApp messages |
| **Scale** | ₹54,999 / month, 6,000 minutes, 25,000 WhatsApp messages |
| **Enterprise** | Custom. Volume rates, from about ₹7 per minute |
| **Annual** | Ten months' price for twelve (two months free) |
| **Allowances** | Hard caps. See "Overage" below |

Prices are in rupees, excluding GST.

## What we found in the code

1. **Plan limits existed but nothing enforced them.** `check_plan_limit` was only
   read by a display endpoint. No call, message or record was ever refused.
2. **There was no free tier.** A new sign-up could run unlimited AI calls at our cost.
3. **Three different price lists** were live: the database catalogue (₹4,900 / 19,900
   / 49,900), the old landing page (₹14,999 / 49,999) and the usage defaults (₹6 per
   minute). None of them was derived from cost.
4. **The old landing prices lose money.** Growth at ₹14,999 with 3,500 minutes is a
   −11% margin at full use, and its ₹3.80 per minute overage is −75% on outbound calls
   (an outbound minute costs about ₹6.64 to serve).
5. **WhatsApp usage was under-counted.** Usage is counted from saved `Message` rows,
   but the AI tool, appointment reminders, campaigns, follow-ups and CSAT surveys send
   without saving one, so those messages were never counted. Any cap or bill built
   on that number would have been too low. Sends are now metered at the moment Meta
   accepts them (`integrations/whatsapp.py: record_usage`); callers that already save a
   `Message` pass `metered=False`.
6. **Minutes were metered as fractional seconds**, but Twilio bills each call rounded
   up to a whole minute. Plan allowances now count billable minutes (rounded up per
   call). The older indicative spend estimate in `/usage` still uses fractional minutes.
7. **Paid overage cannot be collected.** There is no usage invoicing, only flat
   Razorpay subscriptions.

## What an AI minute costs us

Per minute of an AI call, in rupees:

| Part | Inbound | Outbound | Source |
|---|---|---|---|
| Twilio carrier | 0.66 | 4.36 | Outbound to India mobile is **$0.0496/min** ([Twilio](https://www.twilio.com/en-us/voice/pricing/in)). Inbound is **ASSUMPTION** (about $0.0075; Twilio's page does not list it) |
| Media stream / carrier buffer | 0.35 | 0.35 | **ASSUMPTION** |
| Sarvam speech-to-text | 0.49 | 0.49 | **₹0.49/min** pay-as-you-go ([Sarvam](https://www.sarvam.ai/api-pricing)). Streamed for the whole call |
| Sarvam text-to-speech (bulbul v3) | 1.20 | 1.20 | **₹3.00 per 1,000 characters**; assumes about 400 characters per call minute |
| Sarvam LLM (sarvam-105b) | 0.40 | 0.40 | ₹15 / ₹60 per million input / output tokens; assumes 4 turns a minute, 3,000 tokens in and 250 out per turn |
| **Total** | **2.94** | **6.64** | |

Blended at 60% inbound: **₹4.42 per minute.** The older indicative setting
`ai_call_cost_paise_per_minute` (₹4.00, used by the AI analytics page) is within about 10% of that;
set it to `442` once you have replaced the assumptions with real measurements.

Other costs that are not per minute:

| Cost | Amount | Note |
|---|---|---|
| Phone number | about ₹176 / month each | Twilio's page starts at $1.15; India local is closer to $2 (**ASSUMPTION**) |
| WhatsApp (Meta, India) | utility ₹0.115, marketing ₹0.8631, service free in the 24-hour window | Marketing rate rose in 2026. India rates come from secondary sources; Meta's own page links a CSV. Meta no longer makes utility messages inside the window free (from 1 Oct 2026) |
| Razorpay | 2% + 18% GST on the fee, about 2.4% | Subscriptions on cards are quoted 0.9% plus platform fees |
| Hosting (Railway) | about ₹4,750 / month at the current estimate | Dominated by memory ($28.71 of $30.46 so far this period) |

## Plan margins

Gross margin on voice, WhatsApp, numbers and Razorpay fees, by how much of the allowance
the customer uses (the model assumes 60% inbound calls and ₹0.25 average per WhatsApp message):

| Plan | Price | Per included minute | 100% used | 60% used | 40% used |
|---|---|---|---|---|---|
| Starter | ₹2,999 | ₹10.00 | 33% | 54% | 65% |
| Growth | ₹12,999 | ₹8.67 | 30% | 54% | 67% |
| Scale | ₹54,999 | ₹9.17 | 32% | 55% | 67% |

Customers typically use well under their whole allowance, so real margins should sit
near the 60% column. The hosting bill is covered by about three Starter customers.

## Free tier cost

The most one sign-up can cost us, in rupees:

| | Cost |
|---|---|
| 10 minutes, all outbound | 66 |
| 10 minutes, all inbound | 29 |
| 50 WhatsApp messages, all marketing | 43 |
| One phone number, first month | 176 |
| **Worst case in total** | **about 286** |

The 10-minute pool is lifetime, so abandoned accounts stop costing minutes. The phone
number is the part that recurs: a free account that provisions a platform number keeps
costing about ₹176 a month. If free sign-ups pile up, release numbers on inactive free
accounts, or require a verified phone number before provisioning.

## How the limits are enforced

| Limit | Where |
|---|---|
| Minutes pool and per-call cap | `effective_max_call_seconds` clamps the call to the smallest of the agent's cap, the plan's per-call cap and the minutes left. Used by the call config the voice gateway reads and by both turn endpoints, so a free call ends at the pool, not past it |
| No new calls once minutes are gone | Outbound: `guard_outbound` (the one choke point all five outbound paths use). Inbound: the Twilio voice webhook plays a short neutral message and hangs up; the caller never hears about plans |
| One call at a time, 5 outbound a day | `ensure_outbound_allowed` / `ensure_inbound_allowed` |
| Agents, numbers, seats, documents, API keys | `ensure_can_create`, called before the record is created (and before a number is bought) |
| Campaigns, recording | `ensure_feature` |
| WhatsApp messages | `ensure_whatsapp_available` before each send; a campaign pauses instead of failing each contact |

Errors come back as HTTP **402** (HTTP **429** for rate-style limits) with
`{"detail", "code", "tier", "upgrade_path": "/billing"}`.

Entitlements are stored on the tenant (`plan_tier`, `plan_overrides`), not in the
tenant-editable `pricing_plans` table, so a workspace cannot raise its own limits.
Only the Razorpay webhook and a platform admin
(`PUT /api/v1/platform/admin/tenants/{id}/plan`) can change them.

## Moving between plans

* Subscription **activated or charged** moves the tenant to the plan's tier.
* Subscription **cancelled or completed** moves it back to Free, but only if it is
  still on that plan's tier, so a hand-granted Enterprise deal is not downgraded.
* Payment **halted** keeps the tier while Razorpay retries.
* Existing tenants were set to **`legacy`** (unlimited) by the migration so nothing
  changes under them. New sign-ups start on `SIGNUP_PLAN_TIER` (default `free`).

## Overage

Allowances are hard caps. A workspace that reaches its monthly minutes is stopped, and
the 402 tells it to upgrade. The per-minute overage rates in `plans.py` (₹10 / 9 / 8) are
reference prices for when usage invoicing exists. Until then, a platform admin can grant
bonus minutes or `allow_overage` per workspace:

```json
{"tier": "starter", "overrides": {"bonus_minutes": 100}}
```

## Before this goes live

1. **Create the plans in Razorpay** and set `plans.provider_plan_id` for the six rows the
   migration adds. The migration only reprices rows with no Razorpay id; a plan already
   linked to Razorpay keeps its price there and must be re-created by hand.
2. **Confirm the assumptions** marked above in the Twilio and Meta consoles, and replace
   the conversation-shape guesses (characters and turns per minute) with numbers from real calls.
3. **Decide what to do with `legacy` tenants.** They stay unlimited until you move them.
4. **Free sign-ups need friction** (email verification, a phone check) before this is open to
   the internet, or the free pool is a free-calls machine.
5. **Overage invoicing** is the next build if you want to sell beyond the allowance.
6. **Exchange rate.** The model uses ₹88 per US dollar; carrier costs move with it.
