# Email compliance

What a workspace gets before it sends marketing email, and what it still has to do itself.
Transactional mail (password reset, invites, receipts, inbox replies) is exempt and ignores all of this.

## What the backend does

| Need | How |
|---|---|
| One-click unsubscribe (Gmail and Yahoo bulk-sender rule, RFC 8058) | Every campaign email carries `List-Unsubscribe: <link>` and `List-Unsubscribe-Post: List-Unsubscribe=One-Click`, plus an Unsubscribe link in the footer. The link is signed (HMAC of workspace and address, no expiry) at `/api/v1/email/unsubscribe/{token}`. `GET` only shows a button, so a mail scanner opening the link cannot unsubscribe anyone. `POST` (the button, or the mail app's one-click) unsubscribes. |
| Suppression list | Table `email_suppressions`, one row per workspace and address, with the reason: `unsubscribed`, `bounced`, `complaint` or `manual`. Enrolment leaves these addresses out and the send re-checks each one, so an unsubscribe that arrives mid-run still stops the next email. A skipped contact is marked failed with `skipped: true` and the reason. |
| Staff control | `GET/POST /email/suppressions`, `DELETE /email/suppressions/{id}`. Staff can remove `manual` and `bounced` rows, never `unsubscribed` or `complaint`. |
| Bounces and complaints | `POST /email/events/resend` (Svix signature, 5 minute replay window) and `POST /email/events/brevo` (bearer token). A permanent bounce or a spam complaint adds the address. Soft bounces, deliveries and opens are ignored. Both refuse every request until their secret is set (`EMAIL_RESEND_WEBHOOK_SECRET`, `EMAIL_BREVO_WEBHOOK_TOKEN`). The workspace comes from a `tenant` tag added to each message, so an event without it is ignored. |
| Postal address and sender name (CAN-SPAM) | Taken from Settings > Privacy (`address`, `legal_name`) and put in the footer. Launching an email campaign is refused (422), and a send fails, until an address is on file. |
| Marketing consent | Optional. Turn on `email_marketing_requires_consent` in the privacy profile and only people with a granted `marketing` consent get email. Staff record consent with `POST /email/consents`. Unsubscribing also withdraws the person's marketing consent record. |

## One opt-out across email, SMS, WhatsApp and calls

An opt-out on any channel applies to all of them, for every phone number and email address on the same customer record:

* Unsubscribing by email, withdrawing marketing consent on the public privacy page or by staff, or replying **STOP** (also STOP ALL, UNSUBSCRIBE, QUIT, OPT OUT) on WhatsApp, SMS or email puts the customer's emails on the suppression list and their numbers on the do-not-disturb list, and withdraws their marketing and call-back consent.
* The reply to STOP is sent back on the same channel, and the AI agent does not answer that message.
* Only the whole message counts. "cancel" and "end" do not, because they usually mean an appointment.
* WhatsApp campaigns now skip numbers on the do-not-disturb list (voice campaigns already did).
* Service messages are not blocked: staff follow-ups, post-call messages, invites and booking mail still go out.
* Another customer record with a different phone and email is not touched, even if it is the same person. Matching is by phone or email on the customer record.
* There is no START / opt back in yet. Staff can remove a number from the do-not-disturb list, but an unsubscribed email stays locked.

## What it does not do

* **Gmail, Microsoft and SMTP mailboxes send no delivery events.** Only the Resend and Brevo API paths report bounces and complaints. Microsoft Graph accepts no `List-Unsubscribe` header, so mail sent through a connected Microsoft mailbox has the footer link but not the one-click header.
* **No double opt-in** (confirmation email) yet. Consent is whatever staff record, or the chat widget records.
* **No DNS checks.** SPF, DKIM and DMARC for the sending domain are set at the domain and the provider. Gmail and Yahoo expect all three for bulk senders.
* **No spam-rate monitoring.** Gmail asks senders to stay under 0.3% complaints (Postmaster Tools). Nothing pauses a campaign automatically yet.
* `PUBLIC_BASE_URL` must be the public API origin, because unsubscribe links are built from it.
