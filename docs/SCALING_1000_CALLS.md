# Running 1,000 calls at once, and what a real SaaS still needs

Written 2026-11-13 from the code and deploy files. Nothing here was load-tested against real providers, so every capacity number below is an estimate to be checked with `scripts/loadtest_gateway.py` and a staged run.

## Where it stands today

| Part | Today | At 1,000 simultaneous calls |
|---|---|---|
| Server | One DigitalOcean droplet, 2 vCPU / 4 GB, runs everything | Not enough. The gateway alone needs several cores and 1–2 GB |
| Realtime gateway (Go) | 512 MB container, no call limit before this change | Now capped by `MAX_SESSIONS` (default 150). Needs 2–3 gateway nodes |
| API | 1 uvicorn process, DB pool 5+10 | Every spoken turn goes through the API. Now `UVICORN_WORKERS` and `DB_POOL_SIZE` are set from the environment |
| Postgres | Same droplet, `max_connections=100`, 256 MB shared buffers | Needs PgBouncer and a managed or dedicated database |
| Celery | 2 workers | Now `WORKER_CONCURRENCY` (default 4). Campaign dialling and post-call work need their own node |
| Per-workspace cap | 10 calls (`max_concurrent_calls_per_tenant`), workspace setting at most 20 | One customer cannot reach 1,000. Enterprise plans need a higher ceiling |
| Monitoring | Health checks only. No Sentry, Prometheus or alerts | Blind at scale |
| Backups | `backup.sh` on the same server; off-server copy only if `BACKUP_RCLONE_REMOTE` is set | Needs verified off-site copies and a restore drill |

## The 1,000-call arithmetic (estimates)

- **Audio network:** about 8 KB/s each way per call on the phone side, plus provider streams. Roughly 200 Mbps in total. Not the limit.
- **Gateway:** about 1–2 MB and a few goroutines per call, so 1–2 GB of memory and perhaps 5–10 cores for audio conversion. Plan 3 nodes at about 400 calls each, with `MAX_SESSIONS` set to match.
- **Turn path:** a caller speaks a turn every 6–10 seconds, so 100–170 turns per second. Each turn is a streamed LLM reply of 1–3 seconds, so 150–500 streams are open at once, and each turn does several database reads and writes. That is hundreds of queries per second.
- **Connections:** 16 API workers at 20 connections each is 320, which is more than Postgres allows (100). PgBouncer in transaction mode fixes this.
- **Provider concurrency:** this is the likeliest real ceiling. 1,000 calls means about 1,000 open speech-to-text streams and many text-to-speech requests. Self-serve Deepgram, ElevenLabs and Sarvam plans usually cap concurrency far below that. Get written concurrency limits for each, or negotiate enterprise limits, before promising anyone 1,000.
- **Telephony:** Twilio defaults to about 1 call per second of new calls. Exotel limits concurrent channels per account. Ramping 1,000 outbound calls at 10 per second takes 100 seconds. Ask each carrier to raise limits.
- **Cost:** voice provider usage will dwarf server cost. At 1,000 calls for an hour, that is 60,000 minutes of provider charges.

## Suggested topology for 1,000 calls

1. 3 gateway nodes (dedicated 8 vCPU), behind a load balancer, `MAX_SESSIONS` about 400 each.
2. 2 API nodes (4 vCPU), 6 workers each.
3. Managed Postgres with a standby, plus PgBouncer.
4. Managed Redis.
5. 1 Celery node for campaigns, post-call analysis and invoices, apart from the API.
6. A metrics and error stack (Sentry plus Prometheus/Grafana or equivalent) with alerts on active calls, turn latency, 5xx rate and provider errors.

## Done in this change

- `UVICORN_WORKERS`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT`, `WORKER_CONCURRENCY` and `MAX_SESSIONS` are now settings (defaults in `docker-compose.prod.yml`: 2 workers, pool 10+10, 4 Celery workers, 150 calls).
- The gateway turns away new calls over the cap with a 503 and `Retry-After`, and exposes `/statz` (active, peak, denied). Caddy hides `/statz` from the internet.
- `scripts/loadtest_gateway.py` opens many Media Streams sockets and reports accepted, turned away and dropped. It refuses to run against the production address.

## Still missing, most important first

1. **A real load test** with a stubbed backend and fake providers, then a staged run with real providers at 100, 300 and 1,000 calls.
2. **Provider and carrier limits** confirmed in writing (see above).
3. **Error tracking, metrics and alerts.**
4. **High availability:** database and gateway on separate machines, a standby database, tested restore.
5. **Deploys that do not drop calls:** recreating the API or gateway container ends calls in progress. Add a drain step: stop accepting new calls, wait for active ones, then replace.
6. **Autoscaling or at least a runbook** for adding gateway nodes when `/statz` shows load.
7. **Per-workspace limits that grow with the plan,** plus fair sharing so one customer cannot use all the capacity.
8. **Abuse and fraud controls for outbound and international calling** (spend caps, destination allow lists, velocity limits).
9. **A status page, support process and written SLA.**
10. **Legal pages:** `/terms` is still missing on the CallPlus site. Consent wording for voice cloning needs a lawyer's review. Commercial calling in India needs DLT registration and consent.

## Known test-suite state

Four tests fail in this environment, independent of these changes: two row-level-security tests (the test database user is a superuser, which ignores RLS) and the wallet tests in `tests/test_wallet.py`. They should be looked at before a release.
