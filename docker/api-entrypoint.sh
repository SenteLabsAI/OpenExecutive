#!/bin/sh
# Drop root after ensuring the /data volume is writable by the runtime user.
# Named volumes are often root-owned on first mount; chown once then gosu.
set -e
if [ "$(id -u)" = "0" ]; then
  mkdir -p /data
  chown -R openexec:openexec /data 2>/dev/null || true
  exec gosu openexec "$@"
fi
exec "$@"
