# DPDP Act compliance guide

India's Digital Personal Data Protection Act, 2023 and the DPDP Rules, 2025 (notified 13–14 November 2025).

> **This is a working guide, not legal advice.** It records what the software does, what it cannot do for you, and how
> each duty maps to a feature. Check the deadlines and wording against the Gazette text and ask a lawyer before you
> rely on it. The Rules phase in: the Board and definitions apply now; consent managers about a year after notification;
> most operating duties (notice, safeguards, breach reporting, retention, children, rights) **about 18 months after
> notification, around 14 May 2027**. Everything below is built so you can switch to it now.

## Who is responsible for what

- **Your customers' data**: your business (the tenant) is the *Data Fiduciary*. This platform is your *Data Processor*.
  The fiduciary owns the notice, the consent, the officer and the answers to people. The software gives you the tools.
- **Your staff's and your own account data**: this platform is the fiduciary. See "What the platform operator must do".

## What the software does

| Duty | Where | What it does |
|---|---|---|
| Notice before collecting (Rule 3) | Settings → Privacy → *Privacy notice and officer*; chat widget; `/privacy/<your-slug>` | Plain-language notice built from your legal name, purposes, retention and officer. Shown in the chat before anything is collected, with a link to the full public page. Each wording change bumps a version. |
| Consent, free, specific, withdrawable (s.6) | Chat widget gate; `consent_records` | The chat will not start until the visitor ticks the notice. The server refuses a session without it. Each agreement is stored per purpose (enquiry, call-back) with the notice version, a hash of the exact words, time, IP and browser. A call-back adds its own consent. Businesses that collect nothing about a person can switch the gate off (`privacy_consent: false`). |
| Withdraw as easily as giving (s.6(4)) | Public page *Withdraw consent*; Settings → Privacy → *Consent record* | One step, no sign-in, effective at once. Withdrawing call-back or marketing consent puts the number on the do-not-disturb list, so no campaign or call-back can dial it. Staff can record a phone withdrawal too. |
| Children (s.9, Rule 10) | Chat widget | The visitor confirms they are 18 or older with the consent. **Verifiable parental consent is not built.** See "Not covered". |
| Rights: access, correction, erasure, grievance, nominee (s.11–14, Rule 14) | Public page; Settings → Privacy → *Privacy requests* | Requests get a reference number and a due date (your target, at most 90 days). Staff must confirm identity first; data is not shown or deleted until they do. Access gives a ZIP; erasure runs the full erase. The person can check progress with their reference and contact. Overdue requests turn red and fail the checklist. |
| Erasure that is complete (s.8(7), Rule 8) | `services/platform_data.erase_customer` | Removes the customer, calls, chats, tickets, comments, website enquiries and CRM leads (with their notes and tasks), survey answers, and **the call audio at the provider** (not just the link). Strips name, phone and email off consent records and keeps only the proof that consent existed. Payment records stay (financial record) but are unlinked. If the provider cannot be reached the request says how many recordings still need deleting. |
| Storage limitation (s.8(7), Rule 8) | Settings → Security → *Data retention* | Daily job deletes transcripts, call audio at the provider, and website-chat enquiries / CRM leads that never became customers, older than your limits. A recording that fails to delete keeps its link and is retried next run. |
| Disclosure on calls | Settings → Privacy | The caller first hears that it is an AI and, if recording is on, that the call may be recorded. You can edit or turn off the wording. |
| Security safeguards (Rule 6) | Existing | Tenant isolation with row-level security, encrypted secrets, 2FA, IP allowlist, PII masking, audit log, rate limits on the public chat and privacy endpoints, signed webhooks. |
| Breach intimation (s.8(6), Rule 7) | Settings → Privacy → *Breach register* | Log an incident; the 72-hour Board-report clock starts at the discovery time. Drafts the message to affected people and the points the Board report must cover. Tracks "reported to the Board" and "people told". An overdue report fails the checklist. |
| Grievance officer and DPO contact (Rule 9, s.8(9)) | Settings → Privacy | Name, title, email and phone, shown in the notice and on the public page. |
| Self-check | Settings → Privacy → *Where you stand* | Eight items the software can verify, each linking to where to fix it. |
| Audit trail (Rule 6, 8) | `audit_logs` | Every privacy action (profile edits, request verified/completed/rejected, consent withdrawn, breach logged/updated, erasure) is logged with actor and time. |

## Not covered by the software: what you must do

1. **Name a grievance officer** and fill in Settings → Privacy. Publish `/privacy/<your-slug>` on your website footer.
2. **Retention logs.** Rule 8 expects logs of personal-data processing to be kept at least one year. Audit logs are not purged by this software; do not add a purge shorter than a year.
3. **Verifiable parental consent** if you serve under-18s (clinics, schools, tutors). The chat only asks visitors to confirm they are adults. For children's data you need your own process (Rule 10) or should not collect it through the chat.
4. **Contracts with processors.** Section 8(2) needs a contract with every processor. Yours, via this platform: the platform operator (your contract with us), plus the sub-processors below.
5. **Notify the Board.** The software drafts the 72-hour report; **you** send it through the Board's channel once it is live, and tell affected people.
6. **Cross-border transfers (s.16, Rule 15).** Data may flow to the services below. Transfers are allowed unless the Government restricts a country; watch for notifications.
7. **Significant Data Fiduciary duties (s.10, Rule 13)** (annual audit, DPIA, India-based DPO) apply only if the Government designates you.
8. **Staff training and an incident plan.** Nothing in software replaces them.
9. **Marketing messages.** Consent for call-backs does not cover promotions; get separate consent. Respect the TRAI DND and registration rules for commercial calls and SMS.
10. **Consent managers (Rule 4)** are not integrated; they start about a year after notification.

## Sub-processors (as built)

| Service | What it sees | Why |
|---|---|---|
| Twilio | Phone numbers, call audio and metadata, SMS content | Telephony and SMS |
| Sarvam AI | Call and chat text (speech-to-text, language model) | AI conversations |
| Meta (WhatsApp Cloud API), if you enable it | WhatsApp numbers and messages | WhatsApp channel |
| Razorpay, if you enable it | Customer name, phone and payment details | Payment links |
| SMTP provider, if configured | Email addresses and message bodies | Post-call email, invites |
| Railway | All stored data (database, cache) | Hosting; region set by the operator |

Keep this table in step with the integrations you actually turn on.

## What the platform operator must do (the fiduciary side)

- Publish the platform's own privacy notice and officer for the people who sign up (staff, owners).
- Sign processor terms with each tenant and with each sub-processor; keep this table current.
- Run production as a non-superuser database role so row-level security applies; keep backups encrypted and subject to the same retention.
- Report breaches affecting the platform to the Board and to the tenants (the processor's duty is to tell the fiduciary without delay).
- Keep the Railway spend limit and monitoring healthy: availability is part of "reasonable security safeguards".

## Using it day to day

1. Settings → Privacy: fill the officer and notice; set the answer-within days.
2. Settings → Security: turn on data retention (transcripts, recordings, enquiries).
3. Put your public page link on your site. The chat widget shows the notice automatically.
4. Check *Privacy requests* weekly; the checklist turns red when one is overdue.
5. If something goes wrong, open *Breach register* at once.

## How it is tested

`tests/test_privacy.py` (backend) covers the notice versioning, the consent gate and its records, withdrawal and the DND block, request deadlines and identity checks, full erasure including the provider-audio deletion and its failure path, retention and retry, the breach clock and drafts, the checklist, and the call disclosure. `src/components/privacy/privacy.test.tsx` (frontend) covers each screen and the public page.
