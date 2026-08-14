#!/bin/sh
set -eu

# Persistent volumes hide the ownership established in the image. Repair legacy
# root-owned mounts before dropping privileges so upgrades can still create the
# SQLite WAL and journal files.
if [ "$(id -u)" -eq 0 ]; then
    for data_dir in /data /var/data; do
        if [ -d "$data_dir" ]; then
            chown --recursive --no-dereference app:app "$data_dir"
        fi
    done
    exec gosu app "$@"
fi

exec "$@"
