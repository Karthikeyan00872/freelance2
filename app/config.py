import os
import re
import secrets
from dotenv import load_dotenv

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(PROJECT_DIR, "frontend")
ASSETS_DIR = os.path.join(PROJECT_DIR, "src")
UPLOAD_DIR = os.path.join(PROJECT_DIR, "uploads")
ENV_PATH = os.path.join(PROJECT_DIR, ".env")

load_dotenv(ENV_PATH)

# Secret key
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)

IS_PROD = os.getenv("SESSION_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}

# CORS origins
CORS_ORIGIN_PATTERNS = [
    re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"),
    re.compile(r"^https://.*\.devtunnels\.ms(:\d+)?$"),
    re.compile(r"^https://.*\.github\.dev$"),
    re.compile(r"^https://.*\.app\.github\.dev$"),
    "https://visualstudio.com",
]
env_origins = os.getenv("CORS_ORIGINS")
if env_origins:
    for origin in env_origins.split(","):
        origin = origin.strip()
        if origin and origin not in CORS_ORIGIN_PATTERNS:
            CORS_ORIGIN_PATTERNS.append(origin)

# MongoDB
MONGODB_URI = os.getenv("MONGODB_URI")

# Redis
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Email
SENDER_EMAIL = os.getenv("SENDER_EMAIL") or os.getenv("sender_email")
SENDER_PASSWORD = os.getenv("SENDER_APP_PASSWORD") or os.getenv("sender_app_password")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

# Google OAuth
GOOGLE_CLIENT_ID = (os.getenv("GOOGLE_CLIENT_ID") or "").strip().strip('"').strip("'")
GOOGLE_CLIENT_SECRET = (os.getenv("GOOGLE_CLIENT_SECRET") or "").strip().strip('"').strip("'")
GOOGLE_REDIRECT_URI = (os.getenv("GOOGLE_REDIRECT_URI") or "").strip().strip('"').strip("'")

# Admin
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

# Constants
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
IMAGE_TYPES = {"profile", "licence", "rc_book", "insurance", "vehicle"}
ACTIVE_STATUSES = ["requested", "accepted", "ongoing"]
ALLOWED_COMPLETION_STATUSES = ["pending_completion", "completed"]
RESEND_OTP_COOLDOWN_SECONDS = 30