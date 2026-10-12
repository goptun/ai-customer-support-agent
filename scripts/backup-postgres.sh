#!/usr/bin/env bash
# Daily dump of the support Postgres into ~/ops/backups/support, keeping the newest $KEEP.
# Run by deploy/systemd/support-backup.timer; safe to run by hand. Restore: docs/deploy.md → Backups.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$HOME/ops/backups/support}"
KEEP="${KEEP:-14}"

cd "$(dirname "$0")/.."
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

dump="$BACKUP_DIR/support-$(date -u +%Y%m%dT%H%M%SZ).dump"
partial="$dump.partial"
trap 'rm -f "$partial"' EXIT

# Credentials stay inside the container: pg_dump reads them from its own environment.
(umask 077; docker compose exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$partial")
# A dump pg_restore can't list is not a backup.
docker compose exec -T postgres pg_restore --list < "$partial" > /dev/null
mv "$partial" "$dump"
echo "wrote $dump ($(du -h "$dump" | cut -f1))"

ls -1t "$BACKUP_DIR"/support-*.dump | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f "$old"
  echo "removed $old"
done
