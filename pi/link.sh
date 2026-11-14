#!/bin/bash
# Prints the current public ngrok link (from ngrok's local API).
LINK="$(curl -s http://127.0.0.1:4040/api/tunnels | grep -o '"public_url":"https[^"]*' | cut -d'"' -f4 | head -1)"
if [ -n "$LINK" ]; then
    echo "$LINK"
    echo "Logins: $LINK/login"
else
    echo "ngrok isn't up. Check: journalctl -u roomsync-ngrok -n 20"
fi
