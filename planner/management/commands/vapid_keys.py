"""Generate the VAPID key pair that background push reminders need.

Run it once, paste the two lines into .env, and restart. The private key
signs every push; the public one is handed to browsers when they subscribe.
Generating a new pair later invalidates every existing subscription.
"""

from cryptography.hazmat.primitives import serialization
from django.core.management.base import BaseCommand
from py_vapid import Vapid, b64urlencode


def generate_vapid_keys():
    """(public_key, private_key) as the base64url strings .env and the browser want."""
    vapid = Vapid()
    vapid.generate_keys()
    public = b64urlencode(
        vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    private = b64urlencode(vapid.private_key.private_numbers().private_value.to_bytes(32, "big"))
    return public, private


class Command(BaseCommand):
    help = "Print a fresh VAPID key pair for background push reminders, ready to paste into .env."

    def handle(self, *args, **options):
        public, private = generate_vapid_keys()
        self.stdout.write(f'VAPID_PUBLIC_KEY="{public}"')
        self.stdout.write(f'VAPID_PRIVATE_KEY="{private}"')
        self.stdout.write(self.style.WARNING("Keep the private key out of git. Set VAPID_CLAIMS_EMAIL too."))
