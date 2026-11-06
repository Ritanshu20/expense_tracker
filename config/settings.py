"""
Django settings for the Smart Expense Tracker.

What lives here: all project configuration. Anything that differs between
machines (secret key, debug flag, hosts, database) is read from environment
variables; locally they come from the git-ignored `.env` file.
Who uses it: Django itself (`manage.py`, `wsgi.py`, `asgi.py`).
It must never contain real secrets or passwords.
"""

import os
from pathlib import Path

import dj_database_url
from django.contrib.messages import constants as message_constants
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Reads `.env` into os.environ. Real environment variables win over `.env`.
load_dotenv(BASE_DIR / ".env")

# Without this, a stopped database makes database commands hang for minutes on Windows:
# psycopg waits 130 seconds per address by default, and `localhost` has two addresses.
DB_CONNECT_TIMEOUT_SECONDS = 10
HSTS_STARTING_SECONDS = 3600


def get_env_list(name: str) -> list[str]:
    """Return a comma-separated environment variable as a list (empty if unset)."""
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def get_env_bool(name: str, default: bool) -> bool:
    """Return a yes or no environment variable. Only "true", in any letter case, means yes."""
    return os.environ.get(name, str(default)).lower() == "true"


def get_env_int(name: str, default: int) -> int:
    """Return a whole-number environment variable, or the default if it is unset."""
    return int(os.environ.get(name, default))


def build_database_settings() -> dict:
    """
    Build the PostgreSQL settings from the environment.

    Why: local and production differ only by environment values.
    Uses DATABASE_URL when it is set, otherwise DB_NAME, DB_USER, DB_PASSWORD,
    DB_HOST and DB_PORT.
    Raises ImproperlyConfigured if DATABASE_URL points to a non-PostgreSQL database.
    """
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        database = dj_database_url.parse(database_url)
    else:
        database = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DB_NAME", "expense_tracker"),
            "USER": os.environ.get("DB_USER", "expense_tracker"),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": os.environ.get("DB_HOST", "localhost"),
            "PORT": os.environ.get("DB_PORT", "5432"),
        }
    # RULE: this project uses PostgreSQL everywhere (dev, tests, production).
    if database["ENGINE"] != "django.db.backends.postgresql":
        raise ImproperlyConfigured("Only PostgreSQL is supported. Check DATABASE_URL in .env.")
    database.setdefault("OPTIONS", {})["connect_timeout"] = DB_CONNECT_TIMEOUT_SECONDS
    return database


# SAFETY: the secret key has no default, so it can never silently fall back to a public one.
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured("SECRET_KEY is not set. Copy .env.example to .env and fill it in.")

# SAFETY: debug is off unless it is switched on explicitly.
DEBUG = get_env_bool("DEBUG", default=False)

ALLOWED_HOSTS = get_env_list("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = get_env_list("CSRF_TRUSTED_ORIGINS")

# SAFETY: in production the site only works over HTTPS and its cookies are never sent without it.
# These stay off with DEBUG on, because the development server has no HTTPS.
SECURE_SSL_REDIRECT = get_env_bool("SECURE_SSL_REDIRECT", default=not DEBUG)
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
# HSTS makes browsers refuse plain HTTP for this long, and it cannot be undone from the server.
# So it starts small. Raise it with the environment variable once HTTPS is known to work.
SECURE_HSTS_SECONDS = get_env_int(
    "SECURE_HSTS_SECONDS", default=0 if DEBUG else HSTS_STARTING_SECONDS
)


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "drf_spectacular",
    "accounts",
    "expenses",
    "dashboard",
    "reports",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # SAFETY: every view needs a login unless it is marked with @login_not_required.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
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

WSGI_APPLICATION = "config.wsgi.application"


# Database

DATABASES = {"default": build_database_settings()}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Password validation

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "home"

# RULE: the budget warning starts when this share of the budget has been used.
BUDGET_WARNING_THRESHOLD_PERCENT = get_env_int("BUDGET_WARNING_THRESHOLD_PERCENT", 80)


# Internationalization

LANGUAGE_CODE = "en-us"

# RULE: dates and week/month boundaries follow Indian time. Expense dates are local dates.
TIME_ZONE = "Asia/Kolkata"

USE_I18N = True

USE_TZ = True


# Static files

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


# Bootstrap has no "alert-error" class; its name for errors is "danger".
MESSAGE_TAGS = {message_constants.ERROR: "danger"}


# REST API

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    # SAFETY: every API view needs a login unless it says otherwise.
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Smart Expense Tracker API",
    "DESCRIPTION": "Personal and shared expenses, settlements and reports.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAuthenticated"],
}


# Email (printed to the console; the app does not send real email)

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
