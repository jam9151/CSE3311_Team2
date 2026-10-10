"""Email verification: signed links, sending them, and who counts as verified.

The link carries a signed, expiring token instead of a row in a table. It holds
the user id and the email address it was issued for, so it stops working if the
address changes or the account is deleted.
"""

import logging
import math
import smtplib
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .models import Profile

logger = logging.getLogger(__name__)

SALT = "mediaflow.email-verification"
TOKEN_MAX_AGE_DAYS = 3
# Stops the resend button being used to flood someone's inbox.
RESEND_COOLDOWN = timedelta(seconds=60)


def make_token(user):
    return signing.dumps({"uid": user.pk, "email": user.email}, salt=SALT)


def read_token(token):
    """The user a token was issued for, or None if it is forged, expired or stale."""
    try:
        data = signing.loads(token, salt=SALT, max_age=TOKEN_MAX_AGE_DAYS * 24 * 60 * 60)
    except signing.BadSignature:  # SignatureExpired is a subclass
        return None
    if not isinstance(data, dict):
        return None
    return get_user_model().objects.filter(pk=data.get("uid"), email__iexact=data.get("email", "")).first()


def is_verified(user):
    """Staff are exempt so an admin made with createsuperuser isn't locked out."""
    if user.is_staff:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.email_verified)


def seconds_until_resend(profile, now=None):
    if not profile.verification_sent_at:
        return 0
    remaining = profile.verification_sent_at + RESEND_COOLDOWN - (now or timezone.now())
    return max(0, math.ceil(remaining.total_seconds()))


def send_verification_email(request, user):
    """Email the confirmation link. Returns False if the mail server refused it."""
    link = request.build_absolute_uri(reverse("verify_email", args=[make_token(user)]))
    body = render_to_string(
        "registration/verify_email.txt",
        {"user": user, "link": link, "days": TOKEN_MAX_AGE_DAYS},
    )
    try:
        send_mail("Confirm your MediaFlow email", body, None, [user.email])
    except (smtplib.SMTPException, OSError):
        logger.exception("Could not send verification email to user %s", user.pk)
        return False

    # Only start the cooldown once something actually went out.
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.verification_sent_at = timezone.now()
    profile.save(update_fields=["verification_sent_at"])
    return True
