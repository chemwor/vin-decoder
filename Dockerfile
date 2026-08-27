# Slim runtime image. Dependencies are installed in their own layer so code
# changes do not invalidate the pip cache on every rebuild.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Where the SQLite cache lives.
#
# On Fly this path is a volume, declared in the [mounts] block of fly.toml, so
# the cache survives deploys and machine restarts. Any host with persistent
# disk (Render, a plain VM) works the same way by mounting one here.
#
# A host without persistent disk still runs the service correctly, because a
# cache miss is only a call to vPIC. What it loses is the hit rate, silently:
# nothing errors and nothing alerts, and the only symptom is latency and
# upstream call volume that nobody is watching. That is the argument for
# choosing a host with volumes rather than a limitation to document.
#
# The volume does not solve scaling. See the note at the end of fly.toml: one
# volume attaches to one machine, so N machines means N independent caches.
# The fix for that is a shared cache in Postgres or Redis, not more machines.
ENV VIN_DB_PATH=/data/vin_cache.db

# No `VOLUME ["/data"]` here. On Fly the [mounts] block in fly.toml is what
# provisions and attaches the volume, so declaring one in the image adds a
# second source of truth for the same path and nothing else. For plain
# `docker run`, pass -v explicitly as the README shows.

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser && mkdir -p /data && chown appuser /data
USER appuser

# Documentation only. Fly routes to `internal_port` from fly.toml, which is
# set to 8000 to match the CMD fallback below.
EXPOSE 8000

# Used by plain `docker run`, by compose, and by the container smoke test in
# the build pipeline. Fly runs its own check from the [[http_service.checks]]
# block instead of this one, and the deploy pipeline additionally smoke-tests
# /health over the public URL after releasing.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s CMD \
  python -c "import os,urllib.request,sys; p=os.getenv('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=2).status == 200 else 1)"

# Shell form so ${PORT} is expanded at runtime rather than baked in.
#
# Fly does not inject PORT; it routes to fly.toml's internal_port, so here the
# 8000 fallback is what actually binds and the two must agree. The variable is
# still read because platforms that do assign a port dynamically (Heroku, Cloud
# Run, App Runner) terminate any container that fails to bind to theirs, and
# supporting them costs one line. Locally, unset means 8000.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
