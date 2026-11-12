# Pack pricing: competitors, our cost, and the price we set

Written 12 Nov 2026. Rupee figures use ₹94 = $1, the live rate the app's wallet showed that day and the rate in
`scripts/cost_model.py`. All prices are before GST. Run `python scripts/cost_model.py` to recompute every margin below.

## The result

| Pack (30 days) | Was | Now | Our cost at full use | Margin at full use | Margin at 60% use | Worst case |
|---|---|---|---|---|---|---|
| Call, 250 AI minutes | ₹2,499 | **₹2,199** (≈ $23.39) | ₹1,570 | 29% | 49% | +2% |
| WhatsApp, 1,000 messages | ₹1,499 | **₹899** (≈ $9.56) | ₹271 | 70% | 81% | +2% |
| Email, 5,000 emails | ₹999 (3,000 emails and 500 SMS) | **₹799** (≈ $8.50) | ₹66 | 92% | 94% | 74% |
| All three, 10% off | ₹4,497 | **₹3,507** (≈ $37.31) | | | | |

**How the price was chosen.** For each pack it is the lowest round price at which the worst case still earns a margin, so no
customer can cost more than they pay. The worst case is every call outbound, every WhatsApp message a paid marketing
message and email at the stress cost. "Margin" is gross margin: it counts voice, messages, phone
numbers and the payment fee. It does not count hosting (about ₹5,100 a month), support or tax on profit.

The floors are close to the prices: about ₹2,162 for Call and ₹884 for WhatsApp. Email is held to a stricter test
(see "Email" below): it keeps 10% of its price at the stress cost, which puts its floor at about ₹215. We price it near the market instead (about 5% under Brevo's cheapest plan) because it is a full marketing tool, not a bare sender. ₹2,149 for Call
loses 0.6% in the worst case, which is why it is ₹2,199 and not lower.

**The bundle.** Each pack clears its own worst case, and so does the 10% bundle. One workspace that uses every allowance in
full in the same month, with every call outbound and every WhatsApp message a paid marketing message, still leaves the bundle
about 7% of its price. At 60% use it keeps about 62%. (When the Email pack was ₹299 the bundle could lose about 6% in that
worst month; pricing the Email pack near the market removed that risk.)

## What competitors charge

Figures are from vendor pages and, mostly, from comparison blogs, many written by vendors. They disagree with each
other in places, so treat them as ranges and check a vendor's own page before quoting it to a customer.

### AI calls, per minute

| Seller | Price | What it includes |
|---|---|---|
| **Us, Call pack** | **₹8.80** | Phone line, speech, AI, recording, 2 numbers, campaigns |
| Vapi | $0.05 (₹4.7) platform fee; about $0.13 to $0.30 (₹12 to ₹28) all in | Platform only. The AI model and the phone line are extra |
| Retell | $0.07 to $0.31 (₹6.6 to ₹29) | Depends on the voice and model chosen |
| Bland | about $0.09 (₹8.5); some sources say $0.11 to $0.14 (₹10 to ₹13) | Phone line billed separately |
| Synthflow | from $0.09 (₹8.5); $0.20 (₹18.8) on managed plans | |
| Sarvam Voice Agents (our supplier) | ₹3.50, ₹3.00 at Business volume | Phone line extra: about ₹0.40 a minute on Sarvam's own numbers |
| Ringg | ₹6 per connected minute | |
| CallMissed | ₹4 | Phone carriage extra, 30 second minimum |
| Indian market | ₹2 to ₹12; ₹3 to ₹6 is the common band; ₹4.50 to ₹9 for Hindi at volume | Often excludes telephony |

**Where we stand.** We are the cheapest "everything included" option against Vapi and Retell, and close to Bland and
Synthflow's headline price before their phone line is added. We are not the cheapest per minute in India: Sarvam, Ringg and
CallMissed quote ₹3.9 to ₹6, but they exclude the phone line, own their carrier, or both. Our carrier alone is ₹4.66 of the
₹6.94 an outbound minute costs, so going much lower than about ₹8.70 loses money on outbound calls. See "How to go
cheaper" below.

### WhatsApp

| Seller | Price | Notes |
|---|---|---|
| **Us, WhatsApp pack** | **₹899 for 1,000 messages (₹0.90 each)** | Meta's fee is inside the price |
| Meta (India) | utility and login codes ₹0.115, marketing ₹0.8631, replies inside 24 hours free | Some resellers quote ₹0.145 and ₹1.09 |
| AiSensy | Basic about ₹1,500 a month, Pro about ₹3,200 to ₹3,500 | Plus Meta's fee on every message |
| Interakt | about ₹2,499 to ₹3,499 a month | Plus Meta's fee |
| WATI | about ₹2,999 a month (₹2,199 if paid yearly) | Plus Meta's fee |
| Reseller mark-up | ₹0.10 to ₹0.30 on every message at some resellers | |

Sending 1,000 marketing messages elsewhere costs about ₹2,360 or more (₹1,500 plan plus ₹863 to Meta), before GST and any
mark-up. At ₹899 we are roughly 60 to 75% cheaper. We cannot go below about ₹884 without risking a loss, because a
workspace that sends only marketing messages costs us ₹863.

### SMS

SMS in India costs ₹0.10 to ₹0.35 a message on a DLT route. SMS is no longer in any pack: it is sold only on pay as you go
(₹0.75 a segment from the wallet), and it is not part of email marketing.

### Email

Prices are USD list prices on 12 Nov 2026, converted at ₹94. They come mostly from comparison sites that disagree with each
other, so check the vendor's own page before quoting one to a customer. Each row is the cheapest **paid** plan that covers
about 5,000 emails a month, which is what our pack includes.

| Seller | Kind | ₹ a month | Emails | ₹ per 1,000 | Notes |
|---|---|---|---|---|---|
| **Us, Email pack** | platform | **799** | 5,000 | 159.8 | |
| Amazon SES | sending only | 47 | 5,000 | 9.4 | Pay per email. No editor, no list tools, no reports |
| Zoho Campaigns (India) | platform | about 225 | 500 contacts | | Cheaper at tiny lists; about ₹3,600 a month at 10,000 contacts |
| MailerLite Growing Business | platform | about 940 | 500 subscribers | | About $15 at 1,000 subscribers (unverified) |
| Brevo Starter | platform | about 846 | 5,000 | 169.2 | About $9 a month more to remove the Brevo logo |
| Mailchimp Essentials | platform | about 1,222 | 5,000 | 244.4 | 500 contacts; about $75 at 5,000 contacts |
| Postmark Basic | sending only | 1,410 | 10,000 | 141.0 | Transactional only |
| Mailgun Basic | sending only | 1,410 | 10,000 | 141.0 | |
| SendGrid Essentials | sending only | 1,875 | 50,000 | 37.5 | Free plan is now a 60 day trial |
| Resend Pro | sending only | 1,880 | 50,000 | 37.6 | 3,000 a month free |

Free tiers also exist: Brevo 300 emails a day, Zoho Campaigns 6,000 to 12,000 a month, Resend 3,000 a month, Juvlon
20,000 a month, Mailchimp 500.

**Where we stand.** We are the cheapest paid *marketing platform* for 5,000 emails, by design only a little under the market: about 5% under Brevo, 15% under MailerLite and 35% under Mailchimp for the same volume, and we include the AI tools. We are **not** the cheapest at everything:
- Sending-only services (SES, SendGrid, Resend) are cheaper per email because they sell no editor, no contact tools and no
  reports. SES is about a ninth of our per-email price.
- Free tiers cost nothing, though they cap volume, add the vendor's logo or drop features.
- Zoho Campaigns is cheaper for a very small list (500 contacts).
We also price by emails sent, not by contacts, so a larger list costs the same until the sends run out.

**What email costs us.** Per 1,000 emails: ₹9.4 sending through Amazon SES (it works through the app's existing SMTP route),
₹37.6 on Resend Pro with its 50,000 plan full, and ₹84.6 on Resend beyond its plan. The pack is sized so that at ₹37.6 per
1,000 it still keeps 74% of its price (the floor is 10%, which pays for hosting, support and
tax). Up to about 18,600 emails would still clear that floor, so 5,000 leaves a cushion.

**Fixed cost.** A Resend Pro plan is a flat ₹1,880 a month however little is sent. Each Email pack customer leaves about
₹733 after sending and the payment fee, so about three customers pay for the plan. Until then, or if total sends across all
workspaces could pass 50,000 a month (at which point Resend charges ₹85 per 1,000), send through SES instead.

### Phone systems (for reference)

Knowlarity quotes about ₹1,999 to ₹3,499 per agent a month, MyOperator from about ₹2,500 a month, Exotel from about
$60 (₹5,640) a month with usage on top. These sell lines and seats, not AI minutes.

## What it costs us

Per AI minute, at ₹94 = $1 (`scripts/cost_model.py`):

| Part | Inbound | Outbound |
|---|---|---|
| Twilio carrier | 0.71 | 4.66 |
| Media stream buffer | 0.35 | 0.35 |
| Sarvam speech to text | 0.49 | 0.49 |
| Sarvam text to speech (Bulbul v3, ₹3 per 1,000 characters) | 1.20 | 1.20 |
| Sarvam language model | 0.24 | 0.24 |
| **Total** | **₹2.99** | **₹6.94** |

Blended at 60% inbound it is ₹4.57. Other costs: a phone number about ₹188 a month (a Call pack unlocks two), WhatsApp
₹0.25 average and ₹0.8631 worst, SMS ₹0.20 average and ₹0.35 worst, email ₹0.009 average (SES), ₹0.038 stress (Resend Pro, plan full) and ₹0.085 beyond a Resend plan, and the payment
fee of about 2.4%.

## Assumptions that could move the margins

These are marked ASSUMPTION in the cost model. Measure them from real usage and rerun it.

1. **Exchange rate.** Carrier and number costs are in dollars. The worst-case cushion on the Call pack is only about ₹35,
   and each ₹1 the rupee weakens adds about ₹16 to a full, all-outbound Call pack. A fall of about ₹2 erases the cushion.
   At ₹88 the same pack earned 2% worst case at ₹2,099. Rerun the model whenever the rate moves.
2. **Inbound carrier cost.** Twilio does not list its India inbound rate. We use about $0.0075 a minute.
3. **60% of minutes are inbound.** An outbound-heavy customer costs more.
4. **400 characters spoken per minute** and **4 conversation turns a minute.**
5. **Phone number rent of about $2 a month each.**

## How to go cheaper, and stay profitable

- **Newer Sarvam voice.** Sarvam now lists Bulbul V4 Flash at ₹2 per 1,000 characters (its page showed a 50% introductory offer
  ending 12 Nov), against ₹3 for the Bulbul v3 we use. Switching saves about ₹0.40 a minute, about ₹100 on a full Call pack.
  Check the voice quality in Hindi before switching.
- **An Indian carrier for outbound.** Outbound is ₹4.66 of carrier cost. A carrier at ₹1.50 a minute would cut the cost of a
  full Call pack by about ₹316 for a typical mix, and by ₹790 if every call is outbound. That is the room to lower the price.
- **Own-line minutes.** Browser calls and numbers a customer connects through Exotel cost us about ₹1.93, so the wallet
  already charges ₹2.50 for them.
- **Volume pricing from Sarvam.** Their per-minute and speech rates fall at Pro, Growth and Business volume.

## Sources

- AI voice platforms: [Morph LLM comparison](https://www.morphllm.com/best-ai-voice-agent-platforms), [Famulor, 10 platforms per minute](https://www.famulor.io/blog/ai-voice-agent-pricing-2026-what-10-platforms-actually-cost-per-minute)
- India voice pricing: [Ecorpit](https://ecorpit.com/ecorpit-ai-voice-agent-development-service-india-2026/), [Ringg](https://www.ringg.ai/ai-voice-agent-pricing), [CallMissed](https://www.callmissed.com/blog/indian-language-voice-ai-agents-affordable-picks)
- Our suppliers: [Sarvam voice agents](https://docs.sarvam.ai/voice-agents/pricing), [Sarvam API pricing](https://docs.sarvam.ai/api/getting-started/pricing), [Twilio India voice](https://www.twilio.com/en-us/voice/pricing/in)
- WhatsApp: [Meta pricing](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing), [AiSensy](https://m.aisensy.com/blog/whatsapp-api-new-pricing/), [MyOperator guide](https://myoperator.com/blog/whatsapp-business-api-pricing-india-2026), [WATI pricing audit](https://codingclave.com/blog/wati-pricing-india-2026), [BSP comparison](https://go4whatsup.com/blog/aisensy-vs-wati-vs-interakt-pricing-showdown-2026/)
- SMS: [MessageCentral A2P SMS India](https://www.messagecentral.com/en-in/blog/a2p-sms-pricing-india), [mTalkz](https://www.mtalkz.com/blog/bulk-sms-pricing)
- Email: [Dupple email services](https://dupple.com/learn/best-transactional-email-services), [MakerKit email providers](https://makerkit.dev/blog/saas/best-transactional-email-provider-saas), [Mailchimp](https://tinycommand.com/blogs/mailchimp-pricing-explained), [Brevo](https://dev.to/techsifted/brevo-pricing-2026-every-plan-explained-with-the-hidden-costs-4oe3), [Resend](https://www.withorb.com/pricing-atlas/resend), [Amazon SES](https://www.usecarly.com/blog/amazon-ses-pricing/), [SendGrid](https://www.usecarly.com/blog/sendgrid-pricing/), [Postmark](https://automationatlas.io/answers/postmark-pricing-explained-2026/), [MailerLite](https://sendx.io/blog/mailerlite-pricing), [Zoho Campaigns India](https://itforsme.in/pricing/zoho-campaigns-india)
- Phone systems: [Exotel vs Knowlarity](https://prospeo.io/s/exotel-vs-knowlarity), [Exotel vs MyOperator](https://tomba.io/blog/exotel-vs-myoperator)
