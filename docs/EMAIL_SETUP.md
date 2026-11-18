# Platform email setup (password reset, invites, invoices)

Platform email goes out over HTTPS through Resend (or Brevo). SMTP still works as a fallback, but many hosts,
DigitalOcean included, block SMTP ports, and mail from a free `gmail.com` address is capped and often lands in spam.
Workspaces send their own customer email (campaigns, follow-ups) through their own key under Integrations.

1. Create a Resend account and add a sending domain you own, for example `mail.autobot360.com`.
2. Add the DNS records Resend shows: SPF, DKIM, and a DMARC record such as `v=DMARC1; p=none; rua=mailto:you@yourdomain`.
   Wait until Resend shows the domain as verified.
3. In Resend, add a webhook to `https://callplus.autobot360.com/api/v1/email/events/resend` for bounced, complained
   and delivered events. Copy its signing secret.
4. In `deploy/digitalocean/.env` set:
   ```
   EMAIL_API_PROVIDER=resend
   EMAIL_API_KEY=<the Resend API key>
   EMAIL_RESEND_WEBHOOK_SECRET=<the webhook signing secret>
   SMTP_FROM_EMAIL=no-reply@mail.autobot360.com
   ```
5. Deploy. On start the API logs `email setup:` lines for anything still wrong; none means it is complete.
6. Check it for real: use "Forgot password" with your own address, confirm the mail arrives in the inbox (not spam),
   then check Resend's dashboard shows it delivered.
