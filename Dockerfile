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
# On a host with persistent volumes (Fly.io, Render, a plain VM) mount one at
# /data and the cache survives restarts. On Heroku it will not: the dyno
# filesystem is wiped on every deploy and at least once a day, so the cache
# silently resets and the hit rate goes to zero with nothing reporting it.
# That is the known limitation of this deployment, not a bug in the service,
# and the fix is a shared cache in Postgres or Redis rather than a local file.
ENV VIN_DB_PATH=/data/vin_cache.db

# No `VOLUME ["/data"]` here on purpose. Fly derives an implicit mount name
# from a declared VOLUME path ("data"), which then collides with the mount
# fly.toml names ("vin_cache") at the same destination, and the deploy fails
# with "can't update the attached volume". fly.toml's [mounts] is what
# provisions and attaches the volume; for plain `docker run`, pass -v
# explicitly as the README shows.

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser && mkdir -p /data && chown appuser /data
USER appuser

# Documentation only. Heroku ignores EXPOSE and injects its own $PORT.
EXPOSE 8000

# Used by plain `docker run` and compose. Heroku ignores Docker healthchecks
# and does its own thing, which is why the pipeline smoke-tests /health over
# the public URL after releasing instead of relying on this.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s CMD \
  python -c "import os,urllib.request,sys; p=os.getenv('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=2).status == 200 else 1)"

# Shell form so ${PORT} is expanded at runtime. Heroku assigns the port
# dynamically and terminates any dyno that fails to bind to it, so a hardcoded
# port is the difference between a container that runs locally and one that
# also runs on a PaaS. Locally, $PORT is unset and it falls back to 8000.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
