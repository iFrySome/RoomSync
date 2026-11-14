#!/bin/bash
# Opens the RoomSync door display full-screen once the app is answering.
# Started at desktop login by ~/.config/autostart/roomsync-kiosk.desktop,
# which pi/setup_autostart.sh installs.

URL="http://127.0.0.1:5000/"

# Wait (up to ~2 minutes) for the roomsync service to be ready.
for _ in $(seq 1 120); do
    curl -s -o /dev/null "$URL" && break
    sleep 1
done

BROWSER="$(command -v chromium-browser || command -v chromium)"
if [ -z "$BROWSER" ]; then
    echo "[kiosk] Chromium not found" >&2
    exit 1
fi

# --password-store=basic: skips the keyring prompt that once left Chromium
#   stuck in a reload loop on this Pi.
# --incognito: no "restore pages?" bubble after a power cut.
exec "$BROWSER" --kiosk --incognito --noerrdialogs --disable-infobars \
    --disable-session-crashed-bubble --password-store=basic \
    --check-for-update-interval=31536000 "$URL"
