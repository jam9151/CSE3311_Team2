from django.shortcuts import redirect

from .verification import is_verified

# Pages an unverified user still needs: the holding page, the link in the
# email, resending it, signing out, and the reminders poll (which is empty for
# them anyway and would otherwise log a redirect every minute).
EXEMPT_URL_NAMES = {"verify_pending", "verify_email", "resend_verification", "logout", "reminders_json"}


class EmailVerificationMiddleware:
    """Hold signed-in users on the "check your email" page until they confirm."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        # Runs after URL resolution, so resolver_match is set here.
        if not request.user.is_authenticated or is_verified(request.user):
            return None
        match = request.resolver_match
        if match is None or match.url_name in EXEMPT_URL_NAMES or "admin" in match.namespaces:
            return None
        return redirect("verify_pending")
