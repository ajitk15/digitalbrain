from datetime import timedelta

from django.core.exceptions import ImproperlyConfigured

from .configuration import ROOT, admin_identity, load_config, read_secret

BASE_DIR = ROOT
CONFIG = load_config()
PRODUCTION = CONFIG["mode"] == "production"
DOCUMENT_AUTO_CONVERT = True
DOCUMENT_SCAN_REQUIRED = PRODUCTION or CONFIG.get("scan_documents", False)
# Development only: let the bundled CLI use the operator's own Claude Code login
# instead of a mounted per-application credential. Never true in production.
CLAUDE_USE_HOST_LOGIN = bool(CONFIG.get("claude_use_host_login", False)) and not PRODUCTION
# Internal hosts an operator permits link import to reach. Everything else is
# held to the public-internet rule in platform_core/fetching.py.
FETCH_ALLOW_HOSTS = [h.strip().lower() for h in CONFIG.get("fetch_allow_hosts", [])]
DEBUG = False
SECRET_DIRECTORY = CONFIG["secret_directory"]
SECRET_KEY = read_secret(SECRET_DIRECTORY, "django_secret_key")
if len(SECRET_KEY) < 50:
    raise ImproperlyConfigured("The signing key must contain at least 50 random characters.")
SITE_ADMIN_USER_ID = admin_identity()
ALLOWED_HOSTS = CONFIG["hosts"]
CSRF_TRUSTED_ORIGINS = CONFIG.get("csrf_origins", [])
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "axes",
    "platform_core",
]
MIDDLEWARE = [
    "platform_core.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
    "platform_core.middleware.ResponsePolicyMiddleware",
]
ROOT_URLCONF = "digitalbrain.urls"
WSGI_APPLICATION = "digitalbrain.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [ROOT / "templates"],
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
if PRODUCTION:
    db = CONFIG["database"]
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": db["name"],
            "USER": db["user"],
            "HOST": db["host"],
            "PORT": db.get("port", 5432),
            "PASSWORD": read_secret(SECRET_DIRECTORY, "database_password"),
            "OPTIONS": {
                "sslmode": "verify-full",
                "sslrootcert": db["sslrootcert"],
                "connect_timeout": 5,
                "options": "-c statement_timeout=10000 -c lock_timeout=5000",
            },
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ROOT / ".runtime/local.sqlite3",
            "OPTIONS": {"timeout": 20},
        }
    }
AUTH_USER_MODEL = "platform_core.User"
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 14},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(minutes=15)
AXES_LOCKOUT_PARAMETERS = ["username", "ip_address"]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = "registration/locked.html"
AXES_SENSITIVE_PARAMETERS = ["username", "password", "ip_address"]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 1800
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_SECURE = PRODUCTION
CSRF_COOKIE_SECURE = PRODUCTION
SECURE_SSL_REDIRECT = PRODUCTION
SECURE_HSTS_SECONDS = 31536000 if PRODUCTION else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = PRODUCTION
SECURE_HSTS_PRELOAD = PRODUCTION
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
if CONFIG.get("trust_proxy", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
DATA_UPLOAD_MAX_MEMORY_SIZE = 22 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700
STATIC_URL = "/static/"
STATIC_ROOT = ROOT / "staticfiles"
STATICFILES_DIRS = [ROOT / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        "OPTIONS": {"file_permissions_mode": 0o644, "directory_permissions_mode": 0o755},
    },
}
WHITENOISE_USE_FINDERS = not PRODUCTION
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en-us"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"safe_json": {"()": "platform_core.observability.SafeJsonFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "safe_json"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "digitalbrain.requests": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "axes": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

if not PRODUCTION:
    LOGGING["handlers"]["rotating"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "formatter": "safe_json",
        "filename": str(ROOT / ".runtime/events.jsonl"),
        "maxBytes": 5 * 1024 * 1024,
        "backupCount": 3,
        "encoding": "utf-8",
    }
    LOGGING["loggers"]["digitalbrain.requests"]["handlers"] = ["rotating"]
