#!/bin/sh
set -eu
umask 077
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
backup_dir="$root/artifacts/backups"
mkdir -p "$backup_dir"
backup_path="$backup_dir/portfolio-$(date -u +%Y%m%dT%H%M%SZ).dump"
trap 'rm -f "$backup_path.partial"' EXIT HUP INT TERM
sh "$root/deploy/beta/compose.sh" exec -T postgres pg_dump -U postgres -d portfolio -Fc > "$backup_path.partial"
mv "$backup_path.partial" "$backup_path"
printf 'Database backup saved: %s\n' "$backup_path"
