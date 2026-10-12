# Deploy

Public at `https://support.matheusramos.dev`, on a VPS shared with other production services. Cloudflare proxies the DNS record and terminates public TLS; nginx on the VPS terminates TLS from Cloudflare with an Origin Certificate (no certbot) and proxies to the app on `127.0.0.1:8003`. Postgres, Redis and the logistics mock publish no ports.

1. Cloudflare: `support` A record → the VPS, proxied; SSL mode Full (strict).
2. Cloudflare → SSL/TLS → Origin Server → Create Certificate for `support.matheusramos.dev`; save as `/etc/nginx/ssl/support.matheusramos.dev.pem` and `.key` (key `chmod 600`).
3. vhost: `sudo cp deploy/nginx/support.matheusramos.dev /etc/nginx/sites-available/`, `sudo ln -s /etc/nginx/sites-available/support.matheusramos.dev /etc/nginx/sites-enabled/`, `sudo nginx -t && sudo systemctl reload nginx`.
4. App: `.env` from `.env.example` (strong `JWT_SECRET` and `POSTGRES_PASSWORD`, LLM provider), then `docker compose up -d --build` in this directory (Compose project `support`; touches no other project).
5. Smoke test: the `curl` from [Run locally](development.md#run-locally) against `https://support.matheusramos.dev`.
6. Backups: install the timer (below).

**Before any recreate** (`down`, `up --build`, `--force-recreate`, moving this directory): read this section, run `docker diff` on each `support-*` container and keep anything in it that isn't in the image, and run it here only, so Compose acts on the project `support` alone. The `support` project is the only one ever recreated from this repo: never `docker compose` another project's file, `docker system prune` or touch another project's containers.

## Rate limits (nginx)

The vhost limits requests per client IP (`limit_req`: `/chat` 12/min with a burst of 6, the rest 5/s), answering `429`. Behind Cloudflare the client is `CF-Connecting-IP`, used as the `limit_req` key and passed on as `X-Real-IP`. It is not set with `real_ip_header`: realip would rewrite the address before nginx checks `conf.d/00-cloudflare-only.conf` (the allowlist that lets only Cloudflare in), and every real client would be denied. Cloudflare overwrites the header, and the allowlist leaves only private networks able to forge it.

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
