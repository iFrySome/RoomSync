#!/bin/bash
# One-time Raspberry Pi setup: make RoomSync start by itself on every boot.
#
#   bash pi/setup_autostart.sh            # turn autostart on
#   bash pi/setup_autostart.sh --remove   # turn it off again
#
# Installs systemd services and one desktop autostart entry:
#   roomsync.service       -> python3 app.py        (restarts if it crashes)
#   roomsync-rfid.service  -> python3 rfid_listener.py
#   roomsync-ngrok.service -> ngrok http 5000 (only if ngrok is installed and
#                             logged in; the public HTTPS link push needs)
#   ~/.config/autostart/roomsync-kiosk.desktop -> pi/kiosk.sh (Chromium full-screen)
# and switches on desktop auto-login and turns off screen blanking.
#
# Run it as the normal user (not with sudo); it asks for sudo itself.

set -e

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"
AUTOSTART="$HOME/.config/autostart/roomsync-kiosk.desktop"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user (without sudo): bash pi/setup_autostart.sh"
    exit 1
fi

if [ "$1" = "--remove" ]; then
    sudo systemctl disable --now roomsync-ngrok.service roomsync-rfid.service roomsync.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/roomsync.service /etc/systemd/system/roomsync-rfid.service \
               /etc/systemd/system/roomsync-ngrok.service /etc/sudoers.d/roomsync-power
    sudo systemctl daemon-reload
    rm -f "$AUTOSTART"
    echo "RoomSync autostart removed. Start it by hand again with: python3 app.py"
    exit 0
fi

# Use the project's virtualenv if it has one, otherwise the system Python
# (where `pip install --break-system-packages ...` put the packages).
if [ -x "$APP_DIR/.venv/bin/python" ]; then
    PYTHON="$APP_DIR/.venv/bin/python"
else
    PYTHON="$(command -v python3)"
fi

echo "RoomSync folder: $APP_DIR"
echo "User:            $USER_NAME"
echo "Python:          $PYTHON"

if [ ! -f "$APP_DIR/config.py" ]; then
    echo "config.py is missing in $APP_DIR — copy config.py.example to config.py first."
    exit 1
fi

# Stop copies started by hand in a terminal, so they don't clash with the
# services over port 5000 and the card reader.
pkill -f '^python3 app.py' 2>/dev/null || true
pkill -f '^python3 rfid_listener.py' 2>/dev/null || true
sleep 1

sudo tee /etc/systemd/system/roomsync.service >/dev/null <<EOF
[Unit]
Description=RoomSync web app
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=$PYTHON app.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/roomsync-rfid.service >/dev/null <<EOF
[Unit]
Description=RoomSync RC522 card reader
After=roomsync.service
Wants=roomsync.service

[Service]
User=$USER_NAME
SupplementaryGroups=gpio spi
WorkingDirectory=$APP_DIR
ExecStart=$PYTHON rfid_listener.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now roomsync.service roomsync-rfid.service

# Let the app power the Pi off / restart it (the touchscreen power button
# and the Admin page) without a password — those two commands only.
SUDOERS_TMP="$(mktemp)"
cat > "$SUDOERS_TMP" <<EOF
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /usr/bin/systemctl reboot
EOF
if sudo visudo -cf "$SUDOERS_TMP" >/dev/null; then
    sudo install -m 0440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/roomsync-power
else
    echo "Couldn't add the power-off permission; the power button won't work."
fi
rm -f "$SUDOERS_TMP"

# ngrok: the public HTTPS link (needed for phone push notifications). Only
# set up when ngrok is installed and logged in for this user.
NGROK="$(command -v ngrok || true)"
SERVICES="roomsync.service roomsync-rfid.service"
if [ -n "$NGROK" ] && "$NGROK" config check >/dev/null 2>&1; then
    pkill -x ngrok 2>/dev/null || true   # a copy started by hand would hold the link
    sudo tee /etc/systemd/system/roomsync-ngrok.service >/dev/null <<EOF
[Unit]
Description=RoomSync public link (ngrok)
After=network-online.target roomsync.service
Wants=network-online.target

[Service]
User=$USER_NAME
Environment=HOME=$HOME
ExecStart=$NGROK http 5000 --log=stdout
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now roomsync-ngrok.service
    SERVICES="$SERVICES roomsync-ngrok.service"
else
    echo "ngrok not installed or not logged in — skipping the public link."
fi

# Full-screen display at desktop login
chmod +x "$APP_DIR/pi/kiosk.sh"
mkdir -p "$(dirname "$AUTOSTART")"
cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=RoomSync Kiosk
Exec=$APP_DIR/pi/kiosk.sh
X-GNOME-Autostart-enabled=true
EOF

# Boot straight to the desktop (auto-login) and keep the screen on.
if command -v raspi-config >/dev/null; then
    sudo raspi-config nonint do_boot_behaviour B4 || true
    sudo raspi-config nonint do_blanking 1 || true
fi

sleep 4
echo
systemctl --no-pager --lines=0 status $SERVICES | grep -E "●|Active:" || true
echo
if [[ "$SERVICES" == *ngrok* ]]; then
    LINK="$(curl -s http://127.0.0.1:4040/api/tunnels | grep -o '"public_url":"https[^"]*' | cut -d'"' -f4 | head -1)"
    echo "Public link: ${LINK:-not up yet — check with: bash pi/link.sh}"
fi
echo
echo "Done. Reboot to try it:  sudo reboot"
echo "Logs:   journalctl -u roomsync -f        (app)"
echo "        journalctl -u roomsync-rfid -f   (card reader)"
echo "        journalctl -u roomsync-ngrok -f  (public link)"
echo "Show the public link any time:  bash pi/link.sh"
echo "Restart after a git pull or config.py change:  sudo systemctl restart roomsync roomsync-rfid"
echo "Leave the full-screen display: Alt+F4"
if ! grep -q '^SHUTDOWN_PIN' "$APP_DIR/config.py"; then
    echo
    echo "To show the touchscreen power button, add a PIN to config.py, e.g.:"
    echo "  echo 'SHUTDOWN_PIN = \"246810\"' >> $APP_DIR/config.py && sudo systemctl restart roomsync"
fi
