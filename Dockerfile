# syntax=docker/dockerfile:1
#
# MediaFlow as a container: gunicorn serving the app, WhiteNoise serving the
# static files, SQLite on a volume. See "Running in Docker" in the README.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so editing the code doesn't reinstall them.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Gather the static files into the image, hashed and compressed. The three
# values exist only for this one command, because the settings insist on them
# whenever debug is off. Nothing here is used at runtime.
RUN DJANGO_DEBUG=false \
    DJANGO_SECRET_KEY=build-only-value-never-used-at-runtime-q7Zk2mXp4Nw7Rt1Ys5Uc9Hd3 \
    DJANGO_ALLOWED_HOSTS=localhost \
    DJANGO_ENV_FILE=/nonexistent \
    python manage.py collectstatic --noinput

# Not root, with a folder of its own for the database.
RUN useradd --create-home --uid 1000 app \
 && mkdir /data \
 && chown -R app:app /data /app/staticfiles
USER app

# DJANGO_SECRET_KEY and DJANGO_ALLOWED_HOSTS have no defaults on purpose. With
# debug off the app refuses to start without them.
ENV DJANGO_DEBUG=false \
    DATABASE_PATH=/data/db.sqlite3 \
    PORT=8000
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "docker/healthcheck.py"]

# Bring the database up to date, then hand the process to gunicorn.
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn mediaflow.wsgi:application --bind 0.0.0.0:${PORT} --workers ${GUNICORN_WORKERS:-3} --access-logfile - --error-logfile -"]
