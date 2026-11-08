#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
exec docker compose --env-file "$root/secrets/beta/compose.env" -f "$root/deploy/beta/compose.yaml" "$@"
