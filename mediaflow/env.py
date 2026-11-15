"""A small .env reader, so settings can come from a file without another package.

Each line is NAME=value. Blank lines and lines starting with # are skipped, an
optional `export ` prefix is ignored, and a value may be wrapped in single or
double quotes. Real environment variables always win over the file, so setting
one in the terminal still overrides whatever the file says.
"""

import os
from pathlib import Path


def _parse_value(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        # Quoted: take it literally, so a # or = inside survives.
        return raw[1:-1]
    # Unquoted: a space then # starts a comment.
    for index in range(1, len(raw)):
        if raw[index] == "#" and raw[index - 1] in " \t":
            return raw[:index].rstrip()
    return raw


def parse_env_file(path):
    """The NAME -> value pairs in a file. A missing file gives an empty dict."""
    path = Path(path)
    if not path.is_file():
        return {}

    values = {}
    # utf-8-sig drops the byte order mark some Windows editors add.
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, separator, raw = line.partition("=")
        name = name.strip()
        if not separator or not name:
            continue
        values[name] = _parse_value(raw)
    return values


def load_env_file(path):
    """Put the file's values into os.environ, without replacing any already set."""
    for name, value in parse_env_file(path).items():
        os.environ.setdefault(name, value)


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def env_bool(name, default=False):
    """A true/false setting. Unset or empty gives the default.

    Anything else that isn't clearly true or false is an error rather than a
    guess, so a typo like DJANGO_DEBUG=flase can't quietly leave debugging on.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError(f"{name} must be true or false, not {os.environ[name]!r}.")


def env_int(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, not {raw!r}.") from None


def env_list(name):
    """A comma separated setting as a list. Spaces are trimmed, blanks dropped."""
    return [part.strip() for part in os.environ.get(name, "").split(",") if part.strip()]


def clean_smtp_password(host, password):
    """Google shows an app password in groups of four, but the code has no spaces."""
    return password.replace(" ", "") if host.lower().endswith("gmail.com") else password


def database_from_url(url, base_dir=None):
    """A Django DATABASES entry from a DATABASE_URL.

    Two shapes are understood:

        postgres://user:password@host:5432/name?sslmode=require
        sqlite:///relative/path.sqlite3   or   sqlite:////absolute/path.sqlite3

    Query parameters on a PostgreSQL URL become connection OPTIONS. Anything
    else is an error at startup, not a silent fall back to SQLite.
    """
    from urllib.parse import parse_qsl, unquote, urlsplit

    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()

    if scheme == "sqlite":
        # urlsplit keeps the leading slash of the path, so three slashes give a
        # relative path and four an absolute one, as in SQLAlchemy.
        path = unquote(parts.path)
        if path.startswith("//"):
            path = path[1:]
        else:
            path = path.lstrip("/")
        if not path:
            raise ValueError("DATABASE_URL for sqlite needs a file path, like sqlite:///db.sqlite3.")
        name = Path(path)
        if not name.is_absolute() and base_dir is not None:
            name = Path(base_dir) / name
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": name}

    if scheme in ("postgres", "postgresql"):
        name = unquote(parts.path.lstrip("/"))
        if not name or not parts.hostname:
            raise ValueError("DATABASE_URL for PostgreSQL needs a host and a database name.")
        options = {key: value for key, value in parse_qsl(parts.query)}
        entry = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": name,
            "USER": unquote(parts.username or ""),
            "PASSWORD": unquote(parts.password or ""),
            "HOST": parts.hostname,
            "PORT": str(parts.port or ""),
            # Keep connections open between requests, and check them before
            # reuse so a restarted database server doesn't surface as a 500.
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
        }
        if options:
            entry["OPTIONS"] = options
        return entry

    raise ValueError(f"DATABASE_URL must start with postgres:// or sqlite://, not {parts.scheme!r}.")
