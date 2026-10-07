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
    "operations.middleware.StaffSessionMiddleware",
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
SECURE_REDIRECT_EXEMPT = [
    r"^health/(live|ready)/$",
    # n8n calls Django over the internal Compose network; the proxy must not expose /internal/.
    r"^internal/v1/",
]
SECURE_HSTS_SECONDS = 31536000 if SECURE_ENVIRONMENT else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_ENVIRONMENT
SECURE_HSTS_PRELOAD = SECURE_ENVIRONMENT
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "console"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_AGE = 10 * 60 * 60

# --- LINE intake (M03) ------------------------------------------------------------------------
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
# Outside production, replies go only to these LINE user IDs (one OA serves all environments).
LINE_ALLOWED_RECIPIENTS = frozenset(
    r.strip() for r in os.environ.get("LINE_ALLOWED_RECIPIENTS", "").split(",") if r.strip()
)
LINE_REPLY_DEADLINE_SECONDS = int(os.environ.get("LINE_REPLY_DEADLINE_SECONDS") or 50)
LINE_WEBHOOK_MAX_BYTES = 1024 * 1024
LINE_API_BASE = "https://api.line.me"
LINE_DATA_API_BASE = "https://api-data.line.me"
OUTBOUND_ENABLED = os.environ.get("OUTBOUND_ENABLED", "false").lower() == "true"
ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024
ATTACHMENTS_PER_DRAFT = 10
CLAMAV_HOST = os.environ.get("CLAMAV_HOST", "127.0.0.1")
CLAMAV_PORT = int(os.environ.get("CLAMAV_PORT") or 53310)

# --- Orchestration and message budget (M04) ---------------------------------------------------
# Bearer token n8n uses for /internal/v1/ (scoped: claim/renew/complete/fail/send/maintenance).
ORCHESTRATOR_TOKEN = os.environ.get("ORCHESTRATOR_TOKEN", "")
# Push is off outside production unless explicitly enabled; one OA serves every environment.
LINE_PUSH_ENABLED = os.environ.get("LINE_PUSH_ENABLED", "").lower() == "true" or (
    APP_ENV == "production" and os.environ.get("LINE_PUSH_ENABLED", "").lower() != "false"
)
LINE_PUSH_MONTHLY_BUDGET = int(os.environ.get("LINE_PUSH_MONTHLY_BUDGET") or 300)
LINE_PUSH_NONPRODUCTION_CAP = 10
LINE_PUSH_CRITICAL_RESERVE_PERCENT = int(os.environ.get("LINE_PUSH_CRITICAL_RESERVE_PERCENT") or 20)
LINE_PUSH_DEBOUNCE_MINUTES = int(os.environ.get("LINE_PUSH_DEBOUNCE_MINUTES") or 10)

# --- AI (M05) ------------------------------------------------------------------------------
AI_ENABLED = os.environ.get("AI_ENABLED", "false").lower() == "true"
# Fernet key for API keys stored in the DB; kept outside the database.
AI_SECRET_KEY = os.environ.get("AI_SECRET_KEY", "")
AI_ALLOWED_HOSTS = frozenset(
    h.strip().lower()
    for h in os.environ.get("AI_ALLOWED_HOSTS", "thaillm.or.th,api.opentyphoon.ai").split(",")
    if h.strip()
)
AI_TOTAL_BUDGET_SECONDS = 25
AI_REPLY_FALLBACK_SECONDS = 35
# Harness pipeline: start the next model in parallel when the current one is this slow.
AI_HEDGE_AFTER_SECONDS = float(os.environ.get("AI_HEDGE_AFTER_SECONDS", "6"))
