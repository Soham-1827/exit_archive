#!/bin/sh
# Make the data directory writable by uid 10001, then run the command as 10001
# with no capabilities. Needed because a platform may mount a root-owned disk
# over /data. When already non-root (docker run --user), run the command as is.
set -eu

if [ "$(id -u)" -eq 0 ]; then
    D="${DATA_DIR:-/data}"
    if [ -L "$D" ]; then
        echo "entrypoint: refusing symlinked data directory: $D" >&2
        exit 1
    fi
    mkdir -p "$D"
    if [ "$(stat -c %u "$D")" != "10001" ]; then
        if ! chown 10001:10001 "$D"; then
            echo "entrypoint: cannot chown data directory to 10001: $D" >&2
            exit 1
        fi
    fi
    exec setpriv --reuid=10001 --regid=10001 --clear-groups \
        --inh-caps=-all --no-new-privs -- "$@"
fi

exec "$@"
