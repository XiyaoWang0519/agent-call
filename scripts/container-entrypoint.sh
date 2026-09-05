#!/bin/sh
set -eu

# Persistent volumes hide the ownership established in the image. Repair legacy
# root-owned mounts before dropping privileges so upgrades can still create the
# SQLite WAL and journal files.
if [ "$(id -u)" -eq 0 ]; then
    for data_dir in /data /var/data; do
        if [ -d "$data_dir" ]; then
            # Image-owned directories may be on a read-only root filesystem.
            # Leave correct ownership alone; repair only mismatched entries
            # in writable persistent volumes. find does not follow symlinks.
            find "$data_dir" \( ! -user app -o ! -group app \) \
                -exec chown --no-dereference app:app {} +
        fi
    done
    exec gosu app "$@"
fi

exec "$@"
