import re
from flask_cors import CORS
from flask_socketio import SocketIO
from flask_compress import Compress
from pymongo import MongoClient, GEOSPHERE
from redis import Redis
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import MONGODB_URI, REDIS_URL, CORS_ORIGIN_PATTERNS, IS_PROD

# MongoDB
mongo = MongoClient(
    MONGODB_URI or "mongodb://localhost:27017/rani_cab",
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=10000,
    maxPoolSize=10
)
db = mongo.get_default_database()

# Redis (fallback in-memory dict)
_DRIVER_LOCATIONS = {}
try:
    redis_client = Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=1)
except Exception:
    redis_client = None

# SocketIO
socketio = SocketIO()

# Compress
compress = Compress()

# CORS instance (will be applied in create_app)
cors = CORS()