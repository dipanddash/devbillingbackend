"""
Django settings for cafe_billing_backend project.
Online-only configuration (Neon/PostgreSQL).
"""

from datetime import timedelta
from pathlib import Path
import os

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-change-me")
DEBUG = os.getenv("DEBUG", "False").lower() in ("true", "1", "yes")
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "corsheaders",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "assets",
    "products",
    "tables",
    "orders",
    "payments",
    "inventory",
    "rest_framework",
    "reports",
    "gaming",
    "rest_framework_simplejwt",
    "sync",
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
    "cafe_billing_backend.middleware.DatabaseFailureShieldMiddleware",
    "cafe_billing_backend.middleware.OfflineAwareMiddleware",
]

ROOT_URLCONF = "cafe_billing_backend.urls"

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
            ],
        },
    },
]

WSGI_APPLICATION = "cafe_billing_backend.wsgi.application"

OFFLINE_MODE = False

_NEON_DB = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": os.getenv("DB_NAME", "neondb"),
    "USER": os.getenv("DB_USER", "neondb_owner"),
    "PASSWORD": os.getenv("DB_PASSWORD", ""),
    "HOST": os.getenv(
        "DB_HOST",
        "ep-gentle-haze-aiu8s2kt-pooler.c-4.us-east-1.aws.neon.tech",
    ),
    "PORT": os.getenv("DB_PORT", "5432"),
    "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "300")),
    "CONN_HEALTH_CHECKS": True,
    "DISABLE_SERVER_SIDE_CURSORS": os.getenv(
        "DB_DISABLE_SERVER_SIDE_CURSORS",
        "true",
    ).strip().lower() in {"1", "true", "yes"},
    "OPTIONS": {
        "sslmode": os.getenv("DB_SSLMODE", "require"),
        "channel_binding": os.getenv("DB_CHANNEL_BINDING", "require"),
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "8")),
    },
}

DATABASES = {
    "default": _NEON_DB,
    "neon": _NEON_DB,
    # Compatibility alias while offline/sqlite code paths are being retired.
    "sqlite": _NEON_DB,
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "EXCEPTION_HANDLER": "cafe_billing_backend.exception_handlers.drf_exception_handler",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=8),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "AUTH_HEADER_TYPES": ("Bearer",),
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

FAST2SMS_API_KEY = os.getenv("FAST2SMS_API_KEY", "")
FAST2SMS_WHATSAPP_TEMPLATE_ID = os.getenv("FAST2SMS_WHATSAPP_TEMPLATE_ID", "")

