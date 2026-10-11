"""Django settings for MediaFlow.

Running locally needs no configuration: with nothing set, DEBUG is on and the
rest falls back to development values. Deploying means setting DJANGO_DEBUG to
false, and from then on a few settings are required and the app refuses to
start without them rather than running unsafely. Everything is read from the
environment, or from a .env file next to manage.py. See .env.example and the
Deploying section of the README for the full list.
"""

import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .env import clean_smtp_password, env_bool, env_int, env_list, load_env_file

BASE_DIR = Path(__file__).resolve().parent.parent

# Read before anything below looks at os.environ. DJANGO_ENV_FILE points at a
# different file, which the tests use so a developer's own .env can't leak in.
load_env_file(Path(os.environ.get("DJANGO_ENV_FILE") or BASE_DIR / ".env"))

# True while `manage.py test` runs. Only used to keep the HTTPS redirect from
# turning every test request into a 301 when someone runs the suite with
# production settings.
TESTING = sys.argv[1:2] == ["test"]

# On unless told otherwise, so a fresh clone just runs. Production sets it false.
DEBUG = env_bool("DJANGO_DEBUG", default=True)

# The value this project always used for development. Keeping it means nobody
# is logged out, and no email link already sent stops working, when they pull
# the change that moved the key into the environment. The "django-insecure-"
# prefix is also what makes production refuse it.
_DEV_SECRET_KEY = "django-insecure-change-me-in-production"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "").strip()
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY is not set. Generate one with: "
            'python -c "import secrets; print(secrets.token_urlsafe(50))"'
        )
    SECRET_KEY = _DEV_SECRET_KEY
elif not DEBUG and SECRET_KEY.startswith("django-insecure-"):
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY is still a placeholder (it starts with django-insecure-). Generate a real one."
    )

# Empty is fine while DEBUG is on: Django then allows localhost. With DEBUG off
# an empty list would turn away every request, so say so at startup instead.
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
if not DEBUG and not ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "DJANGO_ALLOWED_HOSTS is not set. List the host names the site is served on, comma separated."
    )
# Full origins, scheme included: https://mediaflow.example.com
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "mediaflow.apps.MediaflowStaticFilesConfig", "planner",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware", "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware", "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "planner.middleware.EmailVerificationMiddleware",
]
ROOT_URLCONF = "mediaflow.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True, "OPTIONS": {"context_processors": ["django.template.context_processors.request", "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages"]}}]
WSGI_APPLICATION = "mediaflow.wsgi.application"

# A path, so a host with a persistent disk can keep the database on it.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": Path(os.environ.get("DATABASE_PATH") or BASE_DIR / "db.sqlite3"),
    }
}

# Off in development so throwaway accounts can use any password. Real accounts
# get Django's standard four rules: not too like their details, at least eight
# characters, not a common password, not all digits.
if DEBUG:
    AUTH_PASSWORD_VALIDATORS = []
else:
    AUTH_PASSWORD_VALIDATORS = [
        {"NAME": f"django.contrib.auth.password_validation.{name}"}
        for name in (
            "UserAttributeSimilarityValidator", "MinimumLengthValidator",
            "CommonPasswordValidator", "NumericPasswordValidator",
        )
    ]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Chicago"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
# Where collectstatic gathers everything for the web server to hand out.
STATIC_ROOT = Path(os.environ.get("DJANGO_STATIC_ROOT") or BASE_DIR / "staticfiles")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "home"
LOGIN_URL = "login"

# With DEBUG off Django stops serving static files, so WhiteNoise does. The
# manifest storage gives each built file a hashed name, which also means a
# browser can never hold on to an old copy of the CSS after a deploy.
if not DEBUG:
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }

# HTTPS and cookies. Only when DEBUG is off, since local development is plain http.
if not DEBUG:
    # A browser only keeps a Secure cookie from an https site, or from localhost.
    # So over plain http to any other address, sign-in and every form fail. Set
    # DJANGO_SECURE_COOKIES=false only to run a test server on plain http, and
    # turn it back on once HTTPS is in front. `check --deploy` will keep warning
    # about it while it is off.
    _secure_cookies = env_bool("DJANGO_SECURE_COOKIES", default=True)
    SESSION_COOKIE_SECURE = _secure_cookies
    CSRF_COOKIE_SECURE = _secure_cookies

    # Send http to https. Set DJANGO_SSL_REDIRECT=false to try production
    # settings on your own machine over plain http.
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", default=True) and not TESTING
    # A host's health check usually calls over plain http inside its network.
    SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]

    # Behind a proxy or load balancer that ends HTTPS (most hosts), Django only
    # sees http and would redirect forever. Turn this on ONLY when such a proxy
    # is in front and sets X-Forwarded-Proto itself, because otherwise anyone
    # can send that header and pretend to be secure.
    if env_bool("DJANGO_BEHIND_PROXY", default=False):
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

    # HSTS makes browsers refuse plain http to the site for that many seconds,
    # and it can't be undone from the server, so it stays off until you have
    # seen HTTPS working. Try 3600 first, then a year (31536000).
    SECURE_HSTS_SECONDS = env_int("DJANGO_HSTS_SECONDS", default=0)
    # Left off on purpose, and silenced below so `check --deploy` stays clean.
    # Both are hard to reverse: includeSubdomains covers every subdomain of the
    # host, and preload puts the domain on a list browsers ship with.
    SILENCED_SYSTEM_CHECKS = (
        ["security.W005", "security.W021"] if SECURE_HSTS_SECONDS else ["security.W004"]
    )

# Where errors go. Without this a production 500 leaves no trace anywhere,
# because Django's own handlers only print while DEBUG is on.
_LOG_LEVEL = os.environ.get("DJANGO_LOG_LEVEL", "INFO").strip().upper() or "INFO"
if TESTING:
    # The tests make plenty of 404s and 500s on purpose. Keep them out of the output.
    _LOG_LEVEL = "CRITICAL"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {"django": {"handlers": ["console"], "level": _LOG_LEVEL, "propagate": False}},
    "root": {"handlers": ["console"], "level": _LOG_LEVEL},
}

# Verification emails. Printed to the terminal unless EMAIL_BACKEND says
# otherwise, so nothing needs a mail server to run locally. To send real mail,
# copy .env.example to .env and fill it in.
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = env_int("EMAIL_PORT", 587)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = clean_smtp_password(EMAIL_HOST, os.environ.get("EMAIL_HOST_PASSWORD", ""))
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "MediaFlow <noreply@mediaflow.local>")
