import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

from config.environment import read_environment, require_demo_database

BASE_DIR = Path(__file__).resolve().parent.parent
APP_ENV, SECRET_KEY, DATABASE_URL, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS = read_environment(
    os.environ
)
DEBUG = APP_ENV == "local"
SECURE_ENVIRONMENT = APP_ENV in {"staging", "production"}
DATABASES = {
    "default": dj_database_url.parse(DATABASE_URL, conn_max_age=0, disable_server_side_cursors=True)
}
DATABASES["default"].setdefault("OPTIONS", {}).setdefault("connect_timeout", 3)
if APP_ENV in {"local", "ci"}:
    require_demo_database(APP_ENV, DATABASES["default"])
elif DATABASES["default"]["OPTIONS"].get("sslmode") != "verify-full":
    raise ImproperlyConfigured("Staging/production DATABASE_URL requires sslmode=verify-full.")

INSTALLED_APPS = [
    "operations",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "th"
TIME_ZONE = "Asia/Bangkok"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = Path(os.environ.get("PRIVATE_MEDIA_ROOT") or BASE_DIR / "private-media")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
TEST_RUNNER = "operations.testing.NonEmptyTestRunner"
SESSION_COOKIE_SECURE = SECURE_ENVIRONMENT
CSRF_COOKIE_SECURE = SECURE_ENVIRONMENT
SECURE_SSL_REDIRECT = SECURE_ENVIRONMENT
# Only minimal health responses may be probed over HTTP inside the container network.
SECURE_REDIRECT_EXEMPT = [r"^health/(live|ready)/$"]
SECURE_HSTS_SECONDS = 31536000 if SECURE_ENVIRONMENT else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_ENVIRONMENT
SECURE_HSTS_PRELOAD = SECURE_ENVIRONMENT
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
