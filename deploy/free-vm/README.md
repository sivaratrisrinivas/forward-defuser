# Free deployment: one always-on VM

Cost: $0 on Oracle Cloud's Always Free tier (an Ampere A1 VM, up to 4 cores and 24 GB, which is far more than the
bot needs). Any Linux machine with Docker works the same way, including a spare computer at home.

Why a VM and not a free app platform: the bot needs an always-running worker, Postgres with the pgvector extension,
and Redis. Free tiers of app platforms either put web services to sleep (webhooks would be missed and the worker
stops), expire their free database, or don't offer pgvector. One VM runs everything with no sleeping and no expiry.

## Steps

1. Create the VM (Ubuntu 22.04 or 24.04, ARM or x86) and open ports 80 and 443 in its network security rules.
   Oracle needs a card for identity verification and says it doesn't charge Always Free resources; capacity in a
   region can be full, in which case try another region later. (Not verified here: Oracle's current free-tier limits.)
2. Get a hostname that points at the VM's public IP. A free one: a DuckDNS subdomain (duckdns.org).
3. On the VM: `curl -fsSL https://get.docker.com | sh`, then `git clone <your repo> && cd forward-defuser`.
4. `cp .env.example .env`, then `uv run python scripts/gen_keys.py >> .env` (or run `scripts/gen_keys.py` anywhere
   and paste the values). Edit `.env`: `PUBLIC_BASE_URL=https://<hostname>`, `SITE_DOMAIN=<hostname>`,
   `POSTGRES_PASSWORD=<long random string>` and `DATABASE_URL=postgresql://fd:<the same password>@db:5432/fd`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `ANTHROPIC_API_KEY`,
   `GOOGLE_API_KEY`, and any optional keys. The file stays on the VM and is gitignored.
5. `docker compose -f deploy/free-vm/docker-compose.yml --env-file .env up -d --build`. Caddy fetches the HTTPS
   certificate on first start (needs ports 80 and 443 reachable and the hostname resolving to the VM).
6. Register the Telegram webhook from the VM: `docker compose -f deploy/free-vm/docker-compose.yml exec app python scripts/set_webhook.py`.
7. Check `https://<hostname>/healthz`, open `https://<hostname>/admin` with your `ADMIN_TOKEN`, and message the bot.
8. Optional: add `deploy/free-vm/backup.sh` to cron for nightly database dumps.

## Updating

`git pull && docker compose -f deploy/free-vm/docker-compose.yml --env-file .env up -d --build`. Migrations run on start.

## Limits

One VM is one point of failure and there is no autoscaling. Throughput is bounded by `WORKER_CONCURRENCY` and the
model API, not the VM; the load test in the README ran on a comparable single machine. The Anthropic, Google and
Brave APIs themselves are not free; the `MAX_COST_*` settings cap spend.
