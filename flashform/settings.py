"""
Django settings for the Flashform backend.

All deployment-specific values come from environment variables (PRD §14),
loaded from a `.env` file next to manage.py when present. See `.env.example`.
"""

import os
from datetime import timedelta
from pathlib import Path

import dj_database_url
from corsheaders.defaults import default_headers
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    value = os.environ.get(name, default)
    return [item.strip() for item in value.split(",") if item.strip()]


# --- Core -------------------------------------------------------------------

DEBUG = env_bool("DJANGO_DEBUG", default=False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is off.")
    SECRET_KEY = "django-insecure-local-dev-only-do-not-use-in-production"

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", "http://localhost:3000")

INSTALLED_APPS = [
    # Must be first: makes `manage.py runserver` serve ASGI (HTTP + WebSockets).
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "channels",
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    # Local
    "accounts",
    "rooms",
    "quizzes",
    "activities",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "flashform.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Project-level templates (e.g. templates/emails/).
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "flashform.wsgi.application"
ASGI_APPLICATION = "flashform.asgi.application"


# --- Database ---------------------------------------------------------------
# Postgres everywhere (docker-compose `db` service locally). SQLite is only a
# fallback when DATABASE_URL is unset; production must use Postgres (PRD §6).

# An empty DATABASE_URL counts as unset.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip() or (
    "sqlite:///" + (BASE_DIR / "db.sqlite3").as_posix()
)

DATABASES = {"default": dj_database_url.parse(DATABASE_URL, conn_health_checks=True)}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- Redis: channel layer + cache -------------------------------------------
# With REDIS_URL set (the normal path; docker-compose `redis` service), use Redis
# for both. Without it (e.g. CI), fall back to in-process backends: fine for a
# single process, not for multiple workers.

REDIS_URL = os.environ.get("REDIS_URL", "").strip()

if REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [REDIS_URL]},
        }
    }
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Throttle keys for WebSocket broadcasts (activities/broadcast.py) need millisecond
# expiry (SET NX PX), which Django's cache can't do. Empty = in-process store.
BROADCAST_REDIS_URL = REDIS_URL


# --- Celery -----------------------------------------------------------------
# The broker gets its own Redis database so it never shares keys with the cache
# or channel layer.

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "").strip() or "redis://redis:6379/1"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_ENABLE_UTC = True
# Ack after the task finishes so a crashed worker's task is redelivered (tasks
# must be idempotent). Prefetch one at a time so long tasks don't starve others.
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True


# --- Auth -------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Password reset links (default_token_generator) are valid for 1 hour (PRD T5).
PASSWORD_RESET_TIMEOUT = 3600


# --- Django REST Framework --------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    # Secure by default: public endpoints (health, student endpoints) opt out
    # explicitly with `permission_classes = [AllowAny]`.
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",),
    "DEFAULT_PARSER_CLASSES": ("rest_framework.parsers.JSONParser",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "flashform.exceptions.api_exception_handler",
    # Per-IP limits for ScopedRateThrottle views. Counters live in the default cache.
    "DEFAULT_THROTTLE_RATES": {
        "auth_register": "20/hour",
        "auth_login": "10/min",
        "auth_refresh": "30/min",
        # resend-verification and password-reset: per client IP, and (auth_email_address)
        # per target email. Each endpoint has its own counters.
        "auth_resend_verification": "10/hour",
        "auth_password_reset": "10/hour",
        "auth_email_address": "3/hour",
        # verify-email / password-reset/confirm (signed tokens; this only caps abuse).
        "auth_token": "30/min",
        # Student joins (PRD §8): per client IP and per room code. A class behind one
        # school NAT shares an IP, and students re-join for every new activity, so these
        # may need raising for real classrooms.
        "join_ip": os.environ.get("JOIN_RATE_PER_IP", "").strip() or "10/min",
        "join_room": os.environ.get("JOIN_RATE_PER_ROOM", "").strip() or "60/min",
    },
    # Reverse proxies in front of the app. 0 = ignore X-Forwarded-For (it is spoofable);
    # set to 1 behind a single trusted proxy (e.g. Render/Fly) so throttles see client IPs.
    "NUM_PROXIES": int(os.environ.get("NUM_PROXIES", "0")),
}

# PRD §8
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Flashform API",
    "DESCRIPTION": "REST API for Flashform, a classroom response app. See PRD.md §8.",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": r"/api",
    # Separate request/response components so generated frontend types are precise.
    "COMPONENT_SPLIT_REQUEST": True,
    # simplejwt's JWTAuthentication is mapped to the `jwtAuth` scheme by
    # drf-spectacular's contrib extension; declare it explicitly so the
    # Swagger "Authorize" button always shows Bearer auth.
    "APPEND_COMPONENTS": {
        "securitySchemes": {
            "jwtAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        }
    },
    "SWAGGER_UI_SETTINGS": {"persistAuthorization": True},
    # Name shared enums explicitly; the defaults (TypeEnum, ModeEnum) would collide.
    "ENUM_NAME_OVERRIDES": {
        "ActivityTypeEnum": "activities.models.ActivityType",
        "ActivityModeEnum": "activities.models.ActivityMode",
        "ActivityStatusEnum": "activities.models.ActivityStatus",
        "QuestionTypeEnum": "activities.models.QuestionType",
    },
}


# --- Email (PRD §8 "Email sending") -----------------------------------------
# Transactional email goes through Brevo's HTTP API (accounts/email_service.py).
# With BREVO_API_KEY empty, emails are logged instead of sent (see the worker logs).

APP_NAME = os.environ.get("APP_NAME", "").strip() or "Flashform"
# Where links in emails point (the Next.js app). No trailing slash.
FRONTEND_URL = (os.environ.get("FRONTEND_URL", "").strip() or "http://localhost:3000").rstrip("/")
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "").strip()
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()
BREVO_FROM_EMAIL = os.environ.get("BREVO_FROM_EMAIL", "").strip() or "no-reply@example.com"

# Only used by EmailService.send_smtp_mail; Brevo is the default transport.
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", default=True)
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = f"{APP_NAME} <{BREVO_FROM_EMAIL}>"


# --- CORS -------------------------------------------------------------------

CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "http://localhost:5000")
# Students authenticate with this header (PRD §8).
CORS_ALLOW_HEADERS = (*default_headers, "x-participant-token")


# --- I18n / time ------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# --- Static files -----------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"


# --- Logging ----------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    # Django configures its own loggers; this covers our apps.
    "loggers": {
        name: {"handlers": ["console"], "level": "INFO"}
        for name in ("flashform", "accounts", "rooms", "quizzes", "activities")
    },
}
