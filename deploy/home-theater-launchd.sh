#!/bin/sh
# launchd entry point: home-theater-launchd.sh <command> [args...]
#
#   1. Waits up to HOME_THEATER_MOUNT_WAIT_SECONDS for HOME_THEATER_MOUNT, then starts
#      regardless: the dashboard should come up, and a missing NAS shows on Status.
#   2. Holds a caffeinate -i assertion for as long as the app runs. That stops idle
#      sleep only: the display can still sleep, and closing the lid still sleeps.
#   3. Rotates HOME_THEATER_LOG_DIR hourly, since launchd never does.
#   4. Execs the command, so launchd's pid IS the app. macOS privacy (TCC) then judges
#      the python binary, not this shell or caffeinate, which is why caffeinate is a
#      side process here rather than `caffeinate -i command`.

set -u

MOUNT="${HOME_THEATER_MOUNT:-}"
WAIT_SECONDS="${HOME_THEATER_MOUNT_WAIT_SECONDS:-120}"
LOG_DIR="${HOME_THEATER_LOG_DIR:-}"
CAFFEINATE="${HOME_THEATER_CAFFEINATE:-/usr/bin/caffeinate}"
MOUNT_TABLE="${HOME_THEATER_MOUNT_TABLE:-/sbin/mount}"
ROTATE="$(dirname "$0")/rotate-logs.sh"

if [ "$#" -eq 0 ]; then
    echo "usage: $0 <command> [args...]" >&2
    exit 64
fi

if [ -n "$MOUNT" ]; then
    waited=0
    until "$MOUNT_TABLE" | grep -F " on $MOUNT (" >/dev/null; do
        if [ "$waited" -ge "$WAIT_SECONDS" ]; then
            echo "home-theater: $MOUNT not mounted after ${WAIT_SECONDS}s, starting anyway" >&2
            break
        fi
        sleep 5
        waited=$((waited + 5))
    done
fi

if [ -x "$CAFFEINATE" ]; then
    "$CAFFEINATE" -i -w "$$" &
fi

if [ -n "$LOG_DIR" ]; then
    (
        while kill -0 "$$" 2>/dev/null; do
            sleep 3600
            /bin/sh "$ROTATE" "$LOG_DIR"
        done
    ) &
fi

exec "$@"
