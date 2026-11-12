# AI outreach and SMS campaigns

How the backend reaches a customer by itself, and what stays under fixed rules.

## AI follow-up (`channel = "auto"`)

A follow-up with channel `auto` has a plain-words **goal** ("we missed their call, offer help") instead of a message.
Each run (`services/outreach.py`, driven by the existing follow-up worker every minute):

1. **Code decides what is allowed.** A channel is used only if the customer has the contact detail, has not opted out
   (do-not-disturb list, email suppression list, email consent switch), the channel is connected, an email footer address
   exists, and WhatsApp's 24 hour rule allows free text (or an approved template is given).
2. **The AI orders the allowed channels and writes each message.** It prefers the channel the customer last wrote on. It may
   only use facts from the goal. If the AI is busy or returns nothing usable, the step is retried (3 times, 10 minutes apart)
   and then fails. It never sends a generic message instead.
3. **It sends on the first channel that works,** in the customer's inbox thread, as the AI agent.
4. **It waits** (`fallback_after_minutes`, default 120). If the customer has written or called in that time it stops.
   Otherwise the next channel is tried. A channel that errors is skipped straight away.
5. Nothing automatic goes out between 21:00 and 09:00 in the business's timezone. The step waits for morning.

`purpose` is `marketing` (default: opt-outs, footer and STOP line apply) or `service` (a reminder or receipt: the marketing
opt-out does not block it).

Ways to start one:

* API: `POST /api/v1/operations/follow-ups` with `channel: "auto"`, `due_at`, `goal`, optional `purpose` and `fallback_after_minutes`.
* AI agents and workflows: the tool `reach_out_to_customer` (`goal`, `delay_minutes`, `purpose`).
* Recipe **Message people who did not pick up** (`missed_call_ai_message`): when a call ends unanswered the AI messages them.

Progress is in the follow-up's `payload.auto`: `tried` (each channel, ok or the error), `sent_at`, `result`.

## SMS campaigns

`channel: "sms"` with `sms_body` (same `{{name}}` / `{{first_name}}` blanks). `POST /campaigns/ai-sms` drafts one with the AI.
"Reply STOP to opt out." is added to every message. Enrolment skips numbers without a phone and numbers on the
do-not-disturb list, and the send checks again per contact. Launch is refused until SMS is turned on under Channels with a
number to send from. Each message is metered against the plan and filed in the customer's SMS thread, so a reply (or STOP)
lands in the inbox.

## Still not automatic

* Mail sent through a connected Gmail, Microsoft or SMTP mailbox reports no bounces or complaints.
* Two customer records for the same person are not merged.
* Staff-typed follow-ups from the inbox ignore opt-outs on purpose: a person wrote them.
* The AI's wording is not reviewed before it is sent. Turn the recipe off, or use `purpose` and goals carefully, if you want review.
