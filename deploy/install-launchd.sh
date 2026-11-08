#!/bin/sh
# Install the launchd service. Run this yourself, once, after reading it.
#
#   deploy/install-launchd.sh             install and start
#   deploy/install-launchd.sh --dry-run   print the plist it would install; change nothing
#   deploy/install-launchd.sh --uninstall stop and remove
#
# It refuses to run while something else listens on the dashboard port: stop the
# bare process first, or the service would fail to bind and retry forever.

set -eu

LABEL=com.homeTheater.app
ROOT=$(cd "$(dirname "$0")/.." && pwd)
AGENTS="$HOME/Library/LaunchAgents"
TARGET="$AGENTS/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/home-theater"
PORT="${PORT:-8000}"
DOMAIN="gui/$(id -u)"

render_plist() {
    sed -e "s#/Users/kligvasser/Projects/my-home-theater#$ROOT#g" \
        -e "s#/Users/kligvasser/Library#$HOME/Library#g" \
        "$ROOT/deploy/$LABEL.plist"
}

case "${1:-}" in
    --dry-run)
        render_plist
        exit 0
        ;;
    --uninstall)
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
        rm -f "$TARGET"
        echo "Removed $LABEL. Logs stay in $LOG_DIR."
        exit 0
        ;;
    "") ;;
    *)
        echo "usage: $0 [--dry-run|--uninstall]" >&2
        exit 64
        ;;
esac

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as yourself, not root: it is a per-user LaunchAgent." >&2
    exit 1
fi
if [ ! -x "$ROOT/.venv/bin/home-theater" ]; then
    echo "$ROOT/.venv/bin/home-theater not found. Create the venv and pip install -e . first." >&2
    exit 1
fi
# Re-installing: stop the old copy first, or it would look like the bare process.
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    sleep 2
fi
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $PORT is in use, most likely by the bare home-theater process:" >&2
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
    echo "Stop it, then run this again." >&2
    exit 1
fi

mkdir -p "$AGENTS" "$LOG_DIR"
chmod +x "$ROOT/deploy/home-theater-launchd.sh" "$ROOT/deploy/rotate-logs.sh"
render_plist >"$TARGET"
plutil -lint "$TARGET"

launchctl bootstrap "$DOMAIN" "$TARGET"

PYTHON=$("$ROOT/.venv/bin/python" -c 'import os, sys; print(os.path.realpath(sys.executable))')
cat <<NOTE

Started $LABEL. Logs: $LOG_DIR
Status: launchctl print $DOMAIN/$LABEL | head -30

ONE MANUAL STEP. macOS privacy (TCC) treats a launchd service differently from your
Terminal, which is why this can work by hand and then fail as a service, usually with
a misleading empty-folder or permission error on the NAS or the download folder.
Open System Settings > Privacy & Security > Full Disk Access, press +, press
Cmd-Shift-G and add the real interpreter, not the .venv symlink:

    $PYTHON

Then restart it: launchctl kickstart -k $DOMAIN/$LABEL
Recreating the venv on a new Python means granting the new binary again.
NOTE
