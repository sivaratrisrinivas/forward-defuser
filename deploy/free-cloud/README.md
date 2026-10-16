# Deploy on free tiers (no VM)

Render (app) + Neon (Postgres with pgvector) + Upstash (Redis) + GitHub Actions (keep-alive). All have free plans;
limits change, so check each pricing page. Not verified from this repo: current free-tier limits, and whether
Render's free plan keeps the in-process worker running between requests (the keep-alive ping is there for that).

1. **Neon**: create a project, copy the connection string (with `?sslmode=require`) into `DATABASE_URL`.
   The first boot runs `CREATE EXTENSION vector` through the migrations.
2. **Upstash**: create a Redis database, copy its `rediss://` URL into `REDIS_URL`.
3. **Render**: New > Blueprint > pick this repo (`render.yaml`). Fill the secrets it asks for. Generate
   `DATA_KEY` with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`,
   and `HASH_PEPPER`, `ADMIN_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` with `openssl rand -hex 32`.
   Set `PUBLIC_BASE_URL` to the service URL Render shows.
4. **Webhook**: `uv run python scripts/set_webhook.py` with the same env.
5. **Keep-alive**: in the GitHub repo, Settings > Variables, add `KEEPALIVE_URL` = the service URL.

A real `ANTHROPIC_API_KEY` is the one paid item; there is no free Claude API key.
