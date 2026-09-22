"""
RFID listener for RoomSync — the real hardware pathway for taps, using an
RC522 reader wired to a Raspberry Pi 4 via SPI. This replaces the
simulated tap buttons/text input on /kiosk (that page still works fine as
a fallback/demo tool) with actual card reads, POSTed to the exact same
/kiosk endpoint and the exact same rfid_tag form field the simulated kiosk
already uses — so the rest of the app needs no changes at all.

Uses SimpleMFRC522().read_id() rather than .read(): read() also attempts
a text-block auth/read on the card, which isn't needed for an ID-only tap
and can print harmless but confusing "AUTH ERROR" messages for tags that
don't have that block formatted. read_id() just returns the card's unique
ID with no text I/O.

Run as its own process, alongside the Flask app (which must already be
running — this only POSTs to it, it doesn't start it):
    python3 app.py           # in one terminal
    python3 rfid_listener.py # in another

Requires RPi.GPIO and mfrc522 on top of requirements.txt (see CLAUDE.md —
those two are Raspberry Pi-specific and deliberately left out of the
shared requirements.txt, since they don't install on a regular dev
laptop):
    pip install RPi.GPIO mfrc522

Stop with Ctrl+C — cleans up the GPIO pins before exiting.

Posts to ROOMSYNC_SERVER_URL from config.py (default: the Pi itself,
127.0.0.1). If app.py is running elsewhere instead — e.g. on a laptop for
a demo, with this reader still physically on the Pi — set
ROOMSYNC_SERVER_URL in config.py to that machine's LAN IP.
"""

import time

import requests
import RPi.GPIO as GPIO
from mfrc522 import SimpleMFRC522

from config import ROOMSYNC_SERVER_URL

KIOSK_URL = f"{ROOMSYNC_SERVER_URL}/kiosk"
DEBOUNCE_SECONDS = 2.5

reader = SimpleMFRC522()


def send_tap(card_id):
    rfid_tag = str(card_id)
    try:
        response = requests.post(KIOSK_URL, data={"rfid_tag": rfid_tag}, timeout=5)
        if response.ok:
            print(f"[rfid_listener] Tap sent: rfid_tag={rfid_tag} (status {response.status_code})")
        else:
            print(f"[rfid_listener] Tap POST returned an error: rfid_tag={rfid_tag} status={response.status_code}")
    except requests.exceptions.RequestException as exc:
        print(f"[rfid_listener] Tap POST failed: rfid_tag={rfid_tag} error={exc}")


def main():
    print("[rfid_listener] Listening for RFID taps on the RC522 reader... (Ctrl+C to stop)")
    try:
        while True:
            card_id = reader.read_id()  # blocks until a card is present
            send_tap(card_id)
            time.sleep(DEBOUNCE_SECONDS)  # debounce: ignore the same card sitting on the reader
    except KeyboardInterrupt:
        print("\n[rfid_listener] Stopping...")
    finally:
        GPIO.cleanup()
        print("[rfid_listener] GPIO cleaned up. Goodbye.")


if __name__ == "__main__":
    main()
