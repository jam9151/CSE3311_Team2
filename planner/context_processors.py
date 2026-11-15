from django.conf import settings


def push(request):
    """The public VAPID key for base.html, empty when push reminders are off."""
    return {"push_public_key": settings.VAPID_PUBLIC_KEY if settings.PUSH_ENABLED else ""}
