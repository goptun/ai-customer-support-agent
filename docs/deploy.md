# Deploy

Public at `https://matheusramos.dev/projects/support/`, next to the other portfolio demos, on a VPS shared with other production services. Cloudflare proxies the DNS record and terminates public TLS; nginx on the VPS terminates TLS from Cloudflare with an Origin Certificate and, in the main site's vhost, strips the `/projects/support/` prefix and proxies to the app on `127.0.0.1:8003`. Postgres, Redis and the logistics mock publish no ports. The old `https://support.matheusramos.dev` answers 301 to the new path.

1. Cloudflare → Turnstile: a widget for `matheusramos.dev` (mode Invisible); its site key and secret go in `.env` (`TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY`), nowhere else. Set a monthly spend limit on the LLM provider's account: the daily cap (`LLM_CALLS_PER_DAY`) bounds calls, not money.
2. App: `.env` from `.env.example` (strong `JWT_SECRET` and `POSTGRES_PASSWORD`, LLM provider, `ROOT_PATH=/projects/support`, `DEV_TOKEN_ENABLED` unset), then `docker compose up -d --build` in this directory (Compose project `support`; touches no other project; see *Before any recreate*).
3. Old subdomain: `sudo cp deploy/nginx/support.matheusramos.dev /etc/nginx/sites-available/` (enabled in `sites-enabled`), `sudo nginx -t && sudo systemctl reload nginx`. It now only redirects, but still needs its proxied `support` A record and its Origin Certificate in `/etc/nginx/ssl/support.matheusramos.dev.{pem,key}` (Cloudflare → SSL/TLS → Origin Server → Create Certificate for that name, key `chmod 600`; SSL mode Full (strict)). It goes before step 4: it no longer defines the rate-limit zones, and two files defining them fail `nginx -t`.
4. Main vhost: the config of `matheusramos.dev` lives in the `portfolio-website` repository (`infra/nginx/matheusramos.dev.conf`), which is what gets copied to the VPS. Paste [`deploy/nginx/support-limits.conf`](../deploy/nginx/support-limits.conf) at the top of that file (http context, outside every `server`) and [`deploy/nginx/support-location.conf`](../deploy/nginx/support-location.conf) inside the `server_name matheusramos.dev` HTTPS block, then deploy it as `portfolio-website/DEPLOY.md` says (`nginx -t` before the reload: a broken config takes every site on the VPS down).
5. Smoke test: the page answers 200, a Demo Visitor is admitted and chats in the browser, `/projects/support/docs` loads, the rest of the site keeps its CSP, a burst on `/projects/support/chat` gets `429` and `/projects/support/auth/dev-token` is a 404.
6. Demo Reset and backups: install both timers (below) and run each once by hand.
7. An external uptime check on `https://matheusramos.dev/projects/support/`.

**Before any recreate** (`down`, `up --build`, `--force-recreate`, moving this directory): read this section, run `docker diff` on each `support-*` container and keep anything in it that isn't in the image, and run it here only, so Compose acts on the project `support` alone. The `support` project is the only one ever recreated from this repo: never `docker compose` another project's file, `docker system prune` or touch another project's containers.

## Rate limits (nginx)

The `/projects/support/` location limits requests per client IP (`limit_req`: `/chat` 12/min with a burst of 6, the rest 5/s), answering `429`. Behind Cloudflare the client is `CF-Connecting-IP`, used as the `limit_req` key and passed on as `X-Real-IP`. It is not set with `real_ip_header`: realip would rewrite the address before nginx checks `conf.d/00-cloudflare-only.conf` (the allowlist that lets only Cloudflare in), and every real client would be denied. Cloudflare overwrites the header, and the allowlist leaves only private networks able to forge it.

## Security headers (nginx)

The location repeats every security header of the main site (on nginx < 1.29.3 one `add_header` in a location drops all those inherited from the `server`) with a single change to the site's CSP: `https://challenges.cloudflare.com` in `script-src` and `frame-src`, for Turnstile. The rest of the site keeps its own. `/projects/support/docs` alone also allows `cdn.jsdelivr.net` and inline scripts and styles, which FastAPI's Swagger UI needs; that page shows only the OpenAPI schema. A change to the site's headers in `portfolio-website` has to be copied into both locations.

## Resource limits

The VPS has 2 vCPUs and no swap, and other production services already run on it. An OOM without swap kills whatever the kernel picks, so every `support` service has a hard `mem_limit` and the project as a whole stays under ~1.9 GiB:

| Service | `mem_limit` | `cpus` | Measured (peak) | Why |
| --- | --- | --- | --- | --- |
| app | 1280m | 1.0 | ~800 MiB (870 MiB) | fastembed model in memory plus the LangGraph runtime; ~45% headroom over the peak at start-up indexing. One vCPU leaves the other to everything else. |
| postgres | 384m | 0.5 | ~90 MiB (110 MiB) | Default `shared_buffers` (128 MB) plus a few connections; the data set is small. |
| redis | 64m | 0.25 | ~6 MiB | Counters, Working State and idempotency keys, all with a TTL; `maxmemory 48mb` with `volatile-ttl` evicts before the container limit is reached. |
| logistics | 128m | 0.25 | ~40 MiB | A single uvicorn worker. |

`cpus` caps a burst (the app embedding the Knowledge Base, a slow LLM turn) so it can't starve the other services; at idle nothing is used. Measure again with `docker stats --no-stream` after a change that adds memory (a bigger embedding model, more workers).

Every service has `restart: unless-stopped` and a healthcheck (`/health` on the app and the mock, `pg_isready`, `redis-cli ping`); `docker compose ps` shows `healthy`. Only the app publishes a port, on `127.0.0.1:8003`.

## Demo Reset

Every day at 04:00 UTC the Demo Reset rebuilds the demo data from scratch ([ADR-0006](adr/0006-demo-reset-rebuilds-the-database.md)): `python -m support_agent.reset` drops the `public` schema and rebuilds it (migrations, seed, Knowledge Base index, the LangGraph checkpoint tables), ends every other connection to the database (the app's pools check connections on checkout and reconnect; a `psql` or `pg_dump` running then is cut off too) and deletes every Redis key but the LLM budget's, so a reset never hands out a second day of LLM calls. Demo Visitors caught mid-visit get the restart signal. Requests in flight during the rebuild (seconds, mostly embedding the policies) may fail. It runs as a one-off container of the app's image, so before enabling the timer the app must be rebuilt and recreated with this code (`docker compose up -d --build app`, after the checks under *Before any recreate*): the reset needs the new image, and the running app needs the pools that reconnect. A missed run (VPS down at 04:00) is skipped, not caught up at boot in the middle of visits; while it runs it adds ~0.9 GiB (the embedding model) on top of the project's limits.

```sh
sudo cp deploy/systemd/support-demo-reset.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now support-demo-reset.timer
sudo systemctl start support-demo-reset.service && journalctl -u support-demo-reset.service -n 20  # by hand
```

## Backups

`scripts/backup-postgres.sh` writes a `pg_dump` (custom format) of the `support` database to `~/ops/backups/support/support-<UTC timestamp>.dump`, checks that `pg_restore` can read it and keeps the newest 14 (`KEEP`). The credentials never leave the container. A systemd timer runs it daily at 04:30 (São Paulo):

```sh
sudo cp deploy/systemd/support-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now support-backup.timer
sudo systemctl start support-backup.service && journalctl -u support-backup.service -n 5  # first run
```

Restore (the app is stopped so nothing writes during it; `--clean` drops and recreates every object):

```sh
docker compose stop app
docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' \
  < ~/ops/backups/support/support-<timestamp>.dump
docker compose start app
```

To check a dump without touching the live database, restore it into a throwaway one: `createdb -U "$POSTGRES_USER" restore_check` in the container, the same `pg_restore` with `-d restore_check`, then `dropdb`. Redis needs no backup: it only caches what Postgres rebuilds ([ADR-0001](adr/0001-postgres-checkpointer-for-paused-runs.md)).
