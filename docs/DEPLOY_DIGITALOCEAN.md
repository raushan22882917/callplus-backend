# Deploying on your own DigitalOcean droplet

Everything lives in `deploy/digitalocean/`. One droplet runs the whole stack in Docker:
Postgres 16 (pgvector), Redis, the API, a Celery worker, the Celery scheduler, the realtime call
gateway, the dashboard, and Caddy in front for automatic HTTPS. Only ports 22, 80 and 443 are open;
Postgres and Redis are not reachable from outside.

**Size:** Ubuntu 22.04 or 24.04, 4 GB RAM / 2 vCPU is the minimum (the bootstrap adds 2 GB swap);
8 GB is comfortable with the dashboard on the same machine.

## Backend only (dashboard hosted elsewhere)
The backend can run on the droplet while the dashboard stays where it is (for example Railway).
- Deploy with `./deploy.sh root@DROPLET_IP --backend-only`. In `.env` set `APP_DOMAIN=http://localhost:8081`
  (no certificate is requested for it), `FRONTEND_URL` to the dashboard's real address, and
  `CORS_ORIGINS_JSON` to every dashboard address allowed to call the API, for example
  `["https://callplus.autobot360.com"]`. Only the `api` and `rt` DNS names are needed.
- Point the dashboard at the new API: the address is baked in when the dashboard is built, so set
  `NEXT_PUBLIC_API_BASE_URL=https://api.yourdomain.com/api/v1` on the dashboard's host and **rebuild** it.
  Do this only after `https://api.yourdomain.com/healthz` answers, and after deciding about the data
  (a new database is empty, so nobody could sign in).
- To host the dashboard on the droplet as well, pass the frontend folder to `deploy.sh` instead of `--backend-only`.

## One address for dashboard and API
`deploy.sh` with the dashboard folder serves **both** on `API_DOMAIN` (for example
`https://api.yourdomain.com`): the API, webhooks, widget script, OAuth and MCP answer on their own paths
(`/api/*`, `/mcp`, `/oauth/*`, `/.well-known/*`, `/healthz`) and every other address is the dashboard. Only the
`api` and `rt` DNS names are required. A separate `APP_DOMAIN` name (for example `app.yourdomain.com`) is
optional: add its DNS record, set `APP_DOMAIN` in `.env` and redeploy.

## 1. DNS
Create three `A` records pointing at the droplet's IP, for example:

| Name | Serves |
|---|---|
| `app.yourdomain.com` | the dashboard |
| `api.yourdomain.com` | the API, chat widget script, webhooks, MCP |
| `rt.yourdomain.com` | Twilio's live-call audio (WebSocket) |

Wait until they resolve before step 4, or Caddy cannot get certificates.

## 2. Prepare the server (once)
```bash
cd deploy/digitalocean
ssh root@DROPLET_IP 'bash -s' < bootstrap.sh
```
This installs Docker, the firewall, fail2ban, automatic security updates, swap, and the nightly backup job.

## 3. Configure
```bash
cp .env.example .env
./gen-secrets.sh        # paste the five generated lines into .env
```
Fill in the rest of `.env` (domains, Sarvam, Twilio, Razorpay, Meta, seller details). Keep this
file private: it is git-ignored and deploy.sh copies it to the server with mode 600. Set
`META_WHATSAPP_VERIFY_TOKEN` to something of your own; production refuses the default.

## 4. Deploy
```bash
./deploy.sh root@DROPLET_IP /path/to/call-center-frontend
```
It copies the code, builds the images on the server, starts everything and waits for the API to be
healthy (migrations run when the API starts). Run the same command for every later update; data is kept.

## 5. Point the providers at the new server
- **Twilio** (each number): voice webhook `https://api.yourdomain.com/api/v1/webhooks/telephony/twilio/voice`
  and status callback `.../twilio/status`, exactly as they are set today but on the new domain.
  Media streams use `wss://rt.yourdomain.com/ws/twilio` (the API tells Twilio this itself).
- **Razorpay:** webhook `https://api.yourdomain.com/api/v1/webhooks/payment/razorpay` with the same
  secret as `RAZORPAY_WEBHOOK_SECRET`; enable `payment.captured`, `order.paid` and the subscription events.
- **Meta:** WhatsApp callback `https://api.yourdomain.com/api/v1/webhooks/whatsapp/meta`
  with your verify token.
- **Twilio SMS** (if used): `https://api.yourdomain.com/api/v1/channels/sms/webhook`.
- **Chat widgets** already on customers' sites load `widget.js` from the old API address. Keep the old
  address answering (or have customers paste the new snippet) until they are moved.

## Moving the data from Railway
1. Quiet hour: stop the old API and worker (`railway` dashboard) so nothing writes.
2. Dump: enable a TCP proxy on the Railway Postgres service, then
   `pg_dump "$RAILWAY_PUBLIC_URL" --no-owner | gzip > railway.sql.gz`.
3. Copy it to the droplet and run `./restore.sh railway.sql.gz` (it asks you to type `restore`).
4. Switch the provider webhooks (step 5) and turn the proxy off.
Keep Railway running but idle for a few days as the way back. Starting fresh instead skips all of this.

## Daily care
- **Logs:** `docker compose -f docker-compose.prod.yml logs -f api` (also `worker`, `beat`, `realtime`, `caddy`).
- **Status:** `docker compose -f docker-compose.prod.yml ps`
- **Backups:** nightly at 02:30 into `/var/backups/callpulse` (14 days). Set `BACKUP_RCLONE_REMOTE`
  and install rclone to also copy each file to DigitalOcean Spaces. **Practise a restore once** on a
  spare droplet; an untested backup is a hope, not a backup.
- **Update:** `./deploy.sh ...` again. **Roll back:** deploy the previous commit.
- **Run exactly one `beat`.** Two schedulers run every job twice. If Railway's worker also runs beat
  against the same database, stop it when you move.

## What this does not give you
Monitoring and alerts (add UptimeRobot on `https://api.yourdomain.com/healthz`), a second server for
failover, and managed-database features. For a bigger setup, DigitalOcean Managed Postgres (with the
pgvector extension) can replace the `postgres` container: set `DATABASE_URL` and drop the service.
