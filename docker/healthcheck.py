"""The container's health check: ask the app's own /healthz/ and exit 0 if it answers.

Docker runs this inside the container, so it talks to the app directly on
localhost. It has to say which host it is asking for, because Django turns away
any Host header that isn't in DJANGO_ALLOWED_HOSTS.
"""

import os
import sys
import urllib.request


def host_header(allowed_hosts):
    """A host name the app will accept, from the DJANGO_ALLOWED_HOSTS value."""
    for entry in (part.strip() for part in allowed_hosts.split(",")):
        if entry and entry != "*":
            # ".example.com" is a pattern that covers example.com itself.
            return entry.lstrip(".")
    return "localhost"


def main():
    port = os.environ.get("PORT", "8000")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/healthz/",
        headers={"Host": host_header(os.environ.get("DJANGO_ALLOWED_HOSTS", ""))},
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return 0 if response.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
