import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

from config.environment import inventory_mode, read_environment, require_demo_database

BASE_DIR = Path(__file__).resolve().parent.parent
APP_ENV, SECRET_KEY, DATABASE_URL, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS = read_environment(
    os.environ
)
# DJANGO_DEBUG=false hides the debug pages on a local stack the internet can reach (the Mac
# staging behind ngrok, 14 Oct 2569) while evaluations and the simulator keep the local mode.
DEBUG = APP_ENV == "local" and os.environ.get("DJANGO_DEBUG", "").lower() != "false"
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
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "operations.middleware.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "operations.middleware.StaffSessionMiddleware",
    "operations.middleware.FormOnceMiddleware",
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
                "operations.web.navigation",
                "operations.web.environment",
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
# M10: WhiteNoise serves the locked CSS/HTMX/fonts; hashed names (collectstatic at image build)
# outside local/CI, straight from the app directories otherwise.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if SECURE_ENVIRONMENT
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}
WHITENOISE_USE_FINDERS = not SECURE_ENVIRONMENT
WHITENOISE_AUTOREFRESH = DEBUG
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
# M12: Caddy terminates TLS on the host and is the only way in, so its X-Forwarded-Proto is
# trusted there; never set this where clients can reach Gunicorn directly.
if os.environ.get("BEHIND_TLS_PROXY", "").lower() == "true":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 31536000 if SECURE_ENVIRONMENT else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_ENVIRONMENT
SECURE_HSTS_PRELOAD = SECURE_ENVIRONMENT
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "start"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_AGE = 10 * 60 * 60

# --- LINE intake (M03) ------------------------------------------------------------------------
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
# Outside production, replies go only to these LINE user IDs (one OA serves all environments)
# unless ตัวเลือกระบบ line_audience opens the bot to everyone.
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
AI_MCP_ENABLED = os.environ.get("AI_MCP_ENABLED", "true").lower() == "true"
AI_ENABLED = os.environ.get("AI_ENABLED", "false").lower() == "true"
# Fernet key for API keys stored in the DB; kept outside the database.
AI_SECRET_KEY = os.environ.get("AI_SECRET_KEY", "")
# Fernet key that seals roster e-mails and staged roster files (15 ต.ค. 2569 Q16, Q36); kept on
# the server, its standby and the owner's password manager, never in the database. Local and CI
# derive one from SECRET_KEY (operations/sealing.py) so demos work without it.
PII_SECRET_KEY = os.environ.get("PII_SECRET_KEY", "")
AI_ALLOWED_HOSTS = frozenset(
    h.strip().lower()
    for h in os.environ.get("AI_ALLOWED_HOSTS", "thaillm.or.th,api.opentyphoon.ai").split(",")
    if h.strip()
)
# Providers that enforce response_format json_schema (ThaiLLM's vLLM, checked 6 Oct 2569). Any
# other host also gets the schema in the prompt, because OpenTyphoon accepts and ignores it.
AI_SCHEMA_ENFORCED_HOSTS = frozenset(
    h.strip().lower()
    for h in os.environ.get("AI_SCHEMA_ENFORCED_HOSTS", "thaillm.or.th").split(",")
    if h.strip()
)
# Public halves (base64, comma-separated) of the Ed25519 keys that sign evaluation evidence on the
# machine that evaluates; production imports only evidence signed by one of them (24 Oct 2569).
AI_EVIDENCE_PUBLIC_KEYS = tuple(
    k.strip() for k in os.environ.get("AI_EVIDENCE_PUBLIC_KEYS", "").split(",") if k.strip()
)
AI_TOTAL_BUDGET_SECONDS = 25
AI_REPLY_FALLBACK_SECONDS = 35
# Harness pipeline: start the next model in parallel when the current one is this slow.
AI_HEDGE_AFTER_SECONDS = float(os.environ.get("AI_HEDGE_AFTER_SECONDS", "6"))

# --- Inventory seam (M07, ADR 0003) ----------------------------------------------------------
# fake: the synthetic inventory stands in for the registry (never in production);
# manual_reference: staff issue in the registry first and record its document number here.
INVENTORY_MODE = inventory_mode(APP_ENV, os.environ.get("INVENTORY_MODE", ""))
# Imported registry figures older than this are shown as stale; shortage becomes unknown.
INVENTORY_STALE_HOURS = int(os.environ.get("INVENTORY_STALE_HOURS") or 24)
# A delivery nobody confirmed may be closed by the owner division after this many days (Q6).
RECEIPT_NO_RESPONSE_DAYS = 7

# --- Staff web and staff email (M10) -------------------------------------------------------------
# การแจ้งเตือนเจ้าหน้าที่: off until addresses and SMTP are agreed (round 1 Q10). Locally the
# file backend writes each message under reports/emails instead of sending it.
STAFF_EMAIL_ENABLED = os.environ.get("STAFF_EMAIL_ENABLED", "false").lower() == "true"
STAFF_WEB_BASE_URL = os.environ.get("STAFF_WEB_BASE_URL", "") or (
    CSRF_TRUSTED_ORIGINS[0] if CSRF_TRUSTED_ORIGINS else ""
)
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND") or (
    "django.core.mail.backends.smtp.EmailBackend"
    if SECURE_ENVIRONMENT
    else "django.core.mail.backends.filebased.EmailBackend"
)
EMAIL_FILE_PATH = BASE_DIR / "reports" / "emails"
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT") or 587)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").lower() == "true"
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "faoj-noreply@example.invalid")
# CSV export (round 1 Q12) and closed-case window of the queues.
EXPORT_MAX_ROWS = 5000
