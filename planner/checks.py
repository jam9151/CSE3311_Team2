"""Extra checks for `manage.py check --deploy`."""

from django.conf import settings
from django.core.checks import Warning, register

# Backends that never deliver anything to a real inbox.
_NOT_REAL = ("console.EmailBackend", "dummy.EmailBackend", "locmem.EmailBackend", "filebased.EmailBackend")


@register(deploy=True)
def confirmation_emails_can_be_delivered(app_configs, **kwargs):
    """New accounts can't use the app until they open a link we email them."""
    if not settings.EMAIL_BACKEND.endswith(_NOT_REAL):
        return []
    return [
        Warning(
            f"EMAIL_BACKEND is {settings.EMAIL_BACKEND}, so confirmation emails never reach anyone.",
            hint="New accounts stay stuck on the 'check your email' page. Set EMAIL_BACKEND to "
            "django.core.mail.backends.smtp.EmailBackend and fill in the EMAIL_HOST settings. See .env.example.",
            id="planner.W001",
        )
    ]
