#!/bin/sh
# rotate-logs.sh <dir> [max_bytes] [keep]: rotate each *.log past max_bytes (default
# 20 MB), keeping `keep` gzipped generations (default 5).
#
# Copies then truncates: launchd holds the file open in append mode, so the app
# carries on writing to the emptied file. Lines written between the copy and the
# truncate are lost, which is the accepted price of no restart.

set -u

DIR="$1"
MAX_BYTES="${2:-20000000}"
KEEP="${3:-5}"

for log in "$DIR"/*.log; do
    [ -f "$log" ] || continue
    size=$(wc -c <"$log")
    [ "$size" -ge "$MAX_BYTES" ] || continue

    rm -f "$log.$KEEP.gz"
    i=$((KEEP - 1))
    while [ "$i" -ge 1 ]; do
        [ -f "$log.$i.gz" ] && mv "$log.$i.gz" "$log.$((i + 1)).gz"
        i=$((i - 1))
    done
    cp "$log" "$log.1" && : >"$log" && gzip -f "$log.1"
done
