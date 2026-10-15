#!/bin/sh
# Copy the mounted TLS key to a place only postgres can read (PostgreSQL refuses a key it does
# not own or that others can read; host bind mounts cannot promise either), then start as usual.
set -eu
if [ -f /run/faoj-tls/server.key ]; then
  install -d -o postgres -g postgres -m 700 /var/lib/postgresql/tls
  install -o postgres -g postgres -m 600 /run/faoj-tls/server.key /var/lib/postgresql/tls/server.key
  install -o postgres -g postgres -m 644 /run/faoj-tls/server.crt /var/lib/postgresql/tls/server.crt
fi
exec docker-entrypoint.sh "$@"
