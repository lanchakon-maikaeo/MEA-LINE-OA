from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import CommandError

ENVIRONMENTS = {"local", "ci", "staging", "production"}
DEMO_DATABASES = {"mea_demo", "mea_ci", "test_mea_demo", "test_mea_ci"}
LOCAL_DB_HOSTS = {"localhost", "127.0.0.1", "::1", "db", "postgres"}


def require_demo_database(app_env, database):
    """Shared boundary for demo writes, database reset, and test database creation."""
    if (
        app_env not in {"local", "ci"}
        or database["NAME"] not in DEMO_DATABASES
        or database["HOST"] not in LOCAL_DB_HOSTS
        or database["ENGINE"] != "django.db.backends.postgresql"
    ):
        raise CommandError("Demo/reset/test operations require a local or CI demo PostgreSQL DB.")


def read_environment(env):
    app_env = env.get("APP_ENV")
    if app_env not in ENVIRONMENTS:
        raise ImproperlyConfigured("APP_ENV must be local, ci, staging, or production.")
    secret = env.get("DJANGO_SECRET_KEY", "")
    if len(secret) < 50 or len(set(secret)) < 5 or secret.startswith("django-insecure-"):
        raise ImproperlyConfigured(
            "Set a private random DJANGO_SECRET_KEY of at least 50 characters."
        )
    url = env.get("DATABASE_URL", "")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ImproperlyConfigured("Invalid PostgreSQL DATABASE_URL.") from exc
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.hostname
        or not parsed.username
        or not parsed.password
        or not parsed.path.strip("/")
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ImproperlyConfigured(
            "DATABASE_URL must identify a PostgreSQL host, user, password, DB."
        )
    hosts = [host.strip() for host in env.get("ALLOWED_HOSTS", "").split(",") if host.strip()]
    origins = [o.strip() for o in env.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]
    if not hosts or "*" in hosts:
        raise ImproperlyConfigured("ALLOWED_HOSTS must contain explicit hosts.")
    secure = app_env in {"staging", "production"}
    for origin in origins:
        try:
            origin_url = urlsplit(origin)
            valid_port = origin_url.port
        except ValueError as exc:
            raise ImproperlyConfigured("Invalid CSRF_TRUSTED_ORIGINS.") from exc
        if (
            origin_url.scheme not in ({"https"} if secure else {"http", "https"})
            or not origin_url.hostname
            or origin_url.username
            or origin_url.password
            or origin_url.path
            or origin_url.query
            or origin_url.fragment
            or "*" in origin
            or (valid_port is not None and not 1 <= valid_port <= 65535)
        ):
            raise ImproperlyConfigured("CSRF_TRUSTED_ORIGINS must contain explicit origin URLs.")
    if secure and not origins:
        raise ImproperlyConfigured("HTTPS CSRF_TRUSTED_ORIGINS are required outside local/CI.")
    return app_env, secret, url, hosts, origins
