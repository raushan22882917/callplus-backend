# Email: opens and clicks, AI send times, the checker, subject lines, autopilot

Everything here helps a person write and time a campaign. None of it decides who may be mailed: opt-outs, the
suppression list, consent, the postal address in the footer and the plan's email allowance are checked in code on
every send (see `EMAIL_COMPLIANCE.md`).

## Opens and clicks

Each campaign email carries a one-pixel image and every web address in the message is replaced by a signed redirect.
`email_events` stores one row per open or click (campaign, person, time, link, browser).

| Address | Does |
| --- | --- |
| `GET /api/v1/email/t/o/{token}.gif` | Records an open, returns a 1x1 GIF. Always an image, even for a bad token. |
| `GET /api/v1/email/t/c/{token}` | Records a click, redirects (302) to the address inside the token. Bad token: 404. |

- The token is HMAC-signed with a key derived from `JWT_SECRET_KEY`. The destination is *inside* the signature, so the
  redirect cannot be pointed at a site we did not put in the email, and a token cannot be moved to another workspace.
- Mail scanners and crawlers (a `bot`, `crawler`, `scanner`, `prefetch`… user agent) are ignored.
- **Opens are an estimate.** Some mail apps (Apple Mail Privacy Protection) load images in advance, so opens run high;
  some block images, so they run low. Clicks are exact. The reports say this next to the numbers. Each person counts
  once per campaign.
- The unsubscribe link and footer are never wrapped.
- `EMAIL_TRACKING_ENABLED=false` sends plain links and no pixel.
- Privacy: tracking is personal data. Say so in the privacy notice; the DPDP/GDPR consent position for your own
  customers is yours to decide (see `DPDP_COMPLIANCE.md`).

## Best time to send (`GET /api/v1/email/ai/send-times`)

Returns up to three different days, each with a time, a 0-100 score relative to the best, a plain-language reason and
a `source`: `default`, `mixed` or `learned`.

- Opens and clicks (a click counts three) are put on a weekday-by-hour grid in the **workspace time zone**.
- With little data the default leans on when business email is read: Tuesday to Thursday, mid-morning and early
  afternoon. As events arrive the workspace's own pattern takes over (fully at about 60 weighted events).
- Most opens come soon after a send, so raw counts mostly say "when we sent". Within one campaign each weekday-hour counts
  as the square root of its events, so one burst cannot rule the answer, and a pattern across campaigns does.
- Only 08:00-20:59 local is ever offered, never earlier than 30 minutes from now.

`POST /api/v1/campaigns/{id}/auto-schedule {at?, launch?}` sets the send time to one of those slots (the best if `at`
is omitted). With `launch: true` a draft is also started through the normal launch route, so every launch check runs.

## The checker (`POST /api/v1/email/ai/check`)

Deterministic arithmetic on the text, no AI call: the same email always gets the same score and every lost point has a
stated reason. Looks at subject length, shouting, exclamation marks, "Re:" tricks, thin or over-long bodies, link
count, shortened links, spam-filter words, unknown `{{blanks}}` and missing personalisation. `good` is 80 or more, `fair`
60 or more, otherwise `risky`. It does not replace a real deliverability test.

## Subject lines (`POST /api/v1/email/ai/subjects`)

The AI returns five angles (benefit, curiosity, direct, personal, question); each is then scored by the checker and
the list is sorted by score. If the AI is unavailable the route says so and nothing else changes.

## One campaign's report (`GET /api/v1/email/campaigns/{id}/report`)

Sent, opened, clicked, rates and the top links. `GET /api/v1/email/overview` gains `opens`, `clicks` and per-campaign
`opened` / `clicked` (people, each counted once).

## Weekly autopilot (`/api/v1/email/autopilot`)

`GET` / `PUT` the setting, `POST .../run` to draft now. Stored on the workspace (`tenants.email_autopilot`).

Once a week, on the chosen weekday from 08:00 local, the scheduler (`email.autopilot`, every 15 minutes) does, for each
workspace that turned it on:

1. Skip if there is no sender or no postal address (and try again later; the week is not used up).
2. Collect everyone who can be emailed, minus the suppressed and (if required) those without consent. None: skip.
3. Ask the AI to write the email from the goal and from how recent campaigns performed.
4. Pick the best send time and run the checker.
5. Create the campaign and enrol the audience. **Review** mode leaves a draft for a person to start. **Send** mode
   schedules it, but only when the check scores 70 or more; a weaker draft stays a draft.

A manual "draft now" is always a draft, even in Send mode. The scheduler's own run stays inside each workspace's plan:
the email allowance is reserved at send time like any campaign.

## Not built yet

- Per-recipient send time (each person at their own best hour). Today the whole campaign goes at one time.
- A/B tests that send variants to a slice and roll out the winner.
- Bounce and complaint webhooks for a workspace's *own* Resend/Brevo account (see `OAUTH_SETUP.md`).
- Machine-open filtering beyond user agents (for example Apple's proxy ranges).

## Deploying

- **Migration `v2n4p6r8m0o5`** adds `email_events` (with row-level security) and `tenants.email_autopilot`. The API
  container runs migrations on start; to do it by hand run `make migrate`.
- **`PUBLIC_BASE_URL`** must be the public https address of the API. Tracking pixels, tracked links and the unsubscribe
  link are built from it. If it is still `http://localhost:8000`, links in sent email point nowhere.
- **Scheduler:** the new `email-autopilot` entry is in the beat schedule, so a running beat and worker pick it up on
  restart. No new service or queue.
- The public tracking and unsubscribe routes scope their database write to the workspace named in the signed token, so
  they work when the API connects as the row-level-security role.
- `EMAIL_TRACKING_ENABLED` (default true) switches opens and clicks off for the whole deployment.
