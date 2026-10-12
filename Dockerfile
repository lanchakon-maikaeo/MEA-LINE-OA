# Build stage: uv resolves the locked environment; neither uv nor pip reaches the runtime image.
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.10.11 && uv sync --locked --no-dev

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# OS security updates at build time; the base image's own pip is not needed at runtime (M12 scan).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y -q pip setuptools wheel \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir -p /app /data/private-media \
    && chown app:app /app /data/private-media \
    && chmod 700 /data/private-media
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY config ./config
COPY operations ./operations
COPY manage.py synthetic-data.json priority-engine-demo.json ./
# Hashed static files for WhiteNoise (M10); throwaway settings, nothing connects to a database.
RUN BUILD_KEY="$(/app/.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(64))')" \
    && APP_ENV=production ALLOWED_HOSTS=build CSRF_TRUSTED_ORIGINS=https://build \
    DATABASE_URL="postgresql://collectstatic:${BUILD_KEY}@build/build?sslmode=verify-full" \
    DJANGO_SECRET_KEY="${BUILD_KEY}" \
    /app/.venv/bin/python manage.py collectstatic --noinput
# M12: the image is the release. It carries the host stack (Compose, Caddy, operator script),
# this release's n8n workflows and release.json with the database/n8n/ClamAV/Caddy digests.
COPY deploy/server ./deploy/server
COPY deploy/n8n/import.sh ./deploy/n8n/import.sh
COPY deploy/release_manifest.py ./deploy/release_manifest.py
COPY workflows/n8n ./workflows/n8n
ARG RELEASE_COMMIT=dev
ARG DB_IMAGE=
RUN /app/.venv/bin/python deploy/release_manifest.py --commit "$RELEASE_COMMIT" --db-image "$DB_IMAGE" \
    > /app/release.json
LABEL org.opencontainers.image.revision="$RELEASE_COMMIT" \
      org.opencontainers.image.title="faoj-app"
USER 10001:10001
EXPOSE 8000
# Send an allowed Host: staging/production ALLOWED_HOSTS do not list 127.0.0.1 (Django returns 400).
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import os, urllib.request as u; host = os.environ['ALLOWED_HOSTS'].split(',')[0].strip().lstrip('.'); u.urlopen(u.Request('http://127.0.0.1:8000/health/ready/', headers={'Host': host}), timeout=3)"]
CMD ["/app/.venv/bin/gunicorn", "config.wsgi:application", "--bind=0.0.0.0:8000", "--workers=2", "--access-logfile=-", "--error-logfile=-"]
