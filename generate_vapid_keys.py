"""
One-time setup: generates a VAPID key pair for Web Push notifications.

Run once: python generate_vapid_keys.py

Prints a VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY, both raw base64url-encoded
strings (the format pywebpush's vapid_private_key= param and the browser's
PushManager.subscribe({applicationServerKey: ...}) both expect). Paste them
into the two constants near the top of app.py. This is a dev-only shortcut
(hardcoding config directly in source), the same as app.secret_key below it
— see CLAUDE.md's "Known simplifications" for what that means before any
real deployment.

Re-running this generates a brand-new key pair. Any browser subscriptions
created under the old public key stop being deliverable once you swap in
new keys (their applicationServerKey no longer matches) — those rows in
push_subscriptions become dead weight (pywebpush will fail on them, and
send_push_notification() cleans them up automatically the first time it
tries and gets a 404/410 back).
"""

import base64

from py_vapid import Vapid02


def main():
    vapid = Vapid02()
    vapid.generate_keys()

    private_numbers = vapid.private_key.private_numbers()
    private_bytes = private_numbers.private_value.to_bytes(32, "big")
    private_b64 = base64.urlsafe_b64encode(private_bytes).rstrip(b"=").decode()

    public_numbers = vapid.public_key.public_numbers()
    x = public_numbers.x.to_bytes(32, "big")
    y = public_numbers.y.to_bytes(32, "big")
    public_bytes = b"\x04" + x + y  # uncompressed EC point, what browsers expect
    public_b64 = base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode()

    print("Paste these into app.py, replacing the existing placeholder values:\n")
    print(f'VAPID_PRIVATE_KEY = "{private_b64}"')
    print(f'VAPID_PUBLIC_KEY = "{public_b64}"')


if __name__ == "__main__":
    main()
