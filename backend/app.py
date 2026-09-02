import os
import secrets
import smtplib
from html import escape
from datetime import datetime, timezone, timedelta
from functools import wraps
from urllib.parse import quote

from dotenv import load_dotenv
from email.message import EmailMessage

from flask import Flask, jsonify, request, send_from_directory, session
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient, GEOSPHERE
from pymongo.errors import DuplicateKeyError
from redis import Redis
from werkzeug.security import check_password_hash, generate_password_hash

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(PROJECT_DIR, "frontend")
ASSETS_DIR = os.path.join(PROJECT_DIR, "src")
UPLOAD_DIR = os.path.join(PROJECT_DIR, "uploads")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(FRONTEND_DIR, exist_ok=True)

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
IMAGE_TYPES = {"profile", "licence", "rc_book", "insurance", "vehicle"}

load_dotenv(os.path.join(PROJECT_DIR, ".env"))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True

cors_origins = [
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://localhost:5000",
    "http://127.0.0.1:5000",
    "https://visualstudio.com",
]
env_origins = os.getenv("CORS_ORIGINS")
if env_origins:
    for origin in env_origins.split(","):
        origin = origin.strip()
        if origin and origin not in cors_origins:
            cors_origins.append(origin)

CORS(app, supports_credentials=True, origins=cors_origins or "*")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=os.getenv("SOCKETIO_ASYNC_MODE", "threading"))

mongo = MongoClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017/rani_cab"), serverSelectionTimeoutMS=3000)
db = mongo.get_default_database()

# Redis with in-memory thread-safe fallback
_DRIVER_LOCATIONS = {}
try:
    redis_client = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True, socket_timeout=1)
except Exception:
    redis_client = None

def cache_set_driver_location(driver_id, lng, lat):
    payload = {"lng": float(lng), "lat": float(lat), "updated_at": datetime.now(timezone.utc).isoformat()}
    _DRIVER_LOCATIONS[str(driver_id)] = payload
    if redis_client:
        try:
            redis_client.hset(f"driver_location:{driver_id}", mapping=payload)
        except Exception:
            pass

def cache_get_driver_location(driver_id):
    if redis_client:
        try:
            val = redis_client.hgetall(f"driver_location:{driver_id}")
            if val:
                return {"lng": float(val["lng"]), "lat": float(val["lat"]), "updated_at": val.get("updated_at")}
        except Exception:
            pass
    return _DRIVER_LOCATIONS.get(str(driver_id))

users = db.users
drivers = db.drivers
rides = db.rides
settings = db.settings
promotions = db.promotions
password_otps = db.password_otps

# Ensure Geo Indexes
for collection in (drivers, rides):
    for field in ("current_location", "pickup_location", "dropoff_location"):
        try:
            collection.create_index([(field, GEOSPHERE)])
        except Exception:
            pass

try:
    users.create_index("email", unique=True)
    users.create_index("username", sparse=True, unique=True)
    users.create_index("role")
    drivers.create_index("user_id")
    drivers.create_index("is_online")
    rides.create_index([("rider_id", 1), ("status", 1)])
    rides.create_index([("driver_id", 1), ("status", 1)])
    password_otps.create_index("email")
    password_otps.create_index("expires_at", expireAfterSeconds=0)
except Exception:
    pass

# Ensure default business settings exist
if not settings.find_one({"type": "pricing"}):
    settings.insert_one({
        "type": "pricing",
        "base_fare": 80,
        "price_per_km": 18,
        "surge_multiplier": 1.0,
        "maintenance_mode": False,
        "updated_at": datetime.now(timezone.utc)
    })

ACTIVE_STATUSES = ["requested", "accepted", "ongoing"]
ALLOWED_COMPLETION_STATUSES = ["pending_completion", "completed"]

# ============================================================
# EXPANDED TAMIL NADU CITY / TOWN DATABASE
# ============================================================
TN_CITIES = {
    "madurai": {"lng": 78.1198, "lat": 9.9252, "name": "Madurai"},
    "chennai": {"lng": 80.2707, "lat": 13.0827, "name": "Chennai"},
    "coimbatore": {"lng": 76.9558, "lat": 11.0168, "name": "Coimbatore"},
    "trichy": {"lng": 78.7047, "lat": 10.7905, "name": "Trichy"},
    "salem": {"lng": 78.1460, "lat": 11.6643, "name": "Salem"},
    "tirupur": {"lng": 77.3411, "lat": 11.1085, "name": "Tirupur"},
    "erode": {"lng": 77.7200, "lat": 11.3410, "name": "Erode"},
    "vellore": {"lng": 79.1326, "lat": 12.9165, "name": "Vellore"},
    "kanchipuram": {"lng": 79.7031, "lat": 12.8342, "name": "Kanchipuram"},
    "thanjavur": {"lng": 79.1398, "lat": 10.7870, "name": "Thanjavur"},
    "dindigul": {"lng": 77.9804, "lat": 10.3689, "name": "Dindigul"},
    "tuticorin": {"lng": 78.1343, "lat": 8.7642, "name": "Tuticorin"},
    "kanyakumari": {"lng": 77.5385, "lat": 8.0840, "name": "Kanyakumari"},
    "tirunelveli": {"lng": 77.7005, "lat": 8.7300, "name": "Tirunelveli"},
    "karur": {"lng": 78.0800, "lat": 10.9600, "name": "Karur"},
    "namakkal": {"lng": 78.1700, "lat": 11.2300, "name": "Namakkal"},
    "hosur": {"lng": 77.8300, "lat": 12.7200, "name": "Hosur"},
    "cuddalore": {"lng": 79.7500, "lat": 11.7500, "name": "Cuddalore"},
    "melur": {"lng": 78.3393, "lat": 10.0326, "name": "Melur"},
    "avaniyapuram": {"lng": 78.1200, "lat": 9.9200, "name": "Avaniyapuram"},
    "thiruparankundram": {"lng": 78.0750, "lat": 9.8800, "name": "Thiruparankundram"},
    "alagarkoil": {"lng": 78.1400, "lat": 10.0500, "name": "Alagarkoil"},
    "vadipatti": {"lng": 78.0500, "lat": 10.0800, "name": "Vadipatti"},
    "usilampatti": {"lng": 77.9500, "lat": 10.1700, "name": "Usilampatti"},
    "t nagar": {"lng": 80.2400, "lat": 13.0400, "name": "T Nagar"},
    "adyar": {"lng": 80.2600, "lat": 13.0100, "name": "Adyar"},
    "velachery": {"lng": 80.2300, "lat": 12.9800, "name": "Velachery"},
    "tambaram": {"lng": 80.1200, "lat": 12.9300, "name": "Tambaram"},
    "mylapore": {"lng": 80.2700, "lat": 13.0400, "name": "Mylapore"},
    "egmore": {"lng": 80.2600, "lat": 13.0700, "name": "Egmore"},
    "porur": {"lng": 80.1600, "lat": 13.0400, "name": "Porur"},
    "peelamedu": {"lng": 77.0100, "lat": 11.0300, "name": "Peelamedu"},
    "saravanampatti": {"lng": 77.0000, "lat": 11.0700, "name": "Saravanampatti"},
    "gandhipuram": {"lng": 76.9600, "lat": 11.0200, "name": "Gandhipuram"},
    "ramanathapuram": {"lng": 76.9500, "lat": 11.0000, "name": "Ramanathapuram"},
    "singanallur": {"lng": 77.0200, "lat": 11.0100, "name": "Singanallur"},
    "srirangam": {"lng": 78.7000, "lat": 10.8700, "name": "Srirangam"},
    "thillai nagar": {"lng": 78.7200, "lat": 10.8000, "name": "Thillai Nagar"},
    "kajamalai": {"lng": 78.6800, "lat": 10.7600, "name": "Kajamalai"},
    "kallakudi": {"lng": 78.8400, "lat": 10.9700, "name": "Kallakudi"},
    "gugai": {"lng": 78.1500, "lat": 11.6700, "name": "Gugai"},
    "muthunaickenpatti": {"lng": 78.1300, "lat": 11.6400, "name": "Muthunaickenpatti"},
    "jarugumalai": {"lng": 78.2000, "lat": 11.6000, "name": "Jarugumalai"},
    "kitchipalayam": {"lng": 78.1000, "lat": 11.6800, "name": "Kitchipalayam"},
    "avai shanmugam nagar": {"lng": 77.3400, "lat": 11.1100, "name": "Avai Shanmugam Nagar"},
    "kangeyam": {"lng": 77.5500, "lat": 11.0200, "name": "Kangeyam"},
    "uttukkuli": {"lng": 77.4300, "lat": 11.1700, "name": "Uttukkuli"},
    "perundurai": {"lng": 77.5800, "lat": 11.2700, "name": "Perundurai"},
    "bhavani": {"lng": 77.6800, "lat": 11.4500, "name": "Bhavani"},
    "sathyamangalam": {"lng": 77.2400, "lat": 11.5100, "name": "Sathyamangalam"},
    "katpadi": {"lng": 79.1600, "lat": 12.9700, "name": "Katpadi"},
    "gudiyatham": {"lng": 79.0700, "lat": 12.9400, "name": "Gudiyatham"},
    "ambur": {"lng": 78.7100, "lat": 12.7900, "name": "Ambur"},
    "chengalpattu": {"lng": 79.7000, "lat": 12.6900, "name": "Chengalpattu"},
    "uthiramerur": {"lng": 79.7600, "lat": 12.6200, "name": "Uthiramerur"},
    "kumbakonam": {"lng": 79.3800, "lat": 10.9600, "name": "Kumbakonam"},
    "pattukkottai": {"lng": 79.3200, "lat": 10.4300, "name": "Pattukkottai"},
    "papanasam": {"lng": 79.2800, "lat": 10.9300, "name": "Papanasam"},
    "palani": {"lng": 77.5200, "lat": 10.4500, "name": "Palani"},
    "oddanchatram": {"lng": 77.7400, "lat": 10.4900, "name": "Oddanchatram"},
    "natham": {"lng": 78.2300, "lat": 10.2200, "name": "Natham"},
    "kovilpatti": {"lng": 77.8700, "lat": 9.1700, "name": "Kovilpatti"},
    "sattankulam": {"lng": 78.0300, "lat": 8.4500, "name": "Sattankulam"},
    "nagercoil": {"lng": 77.4300, "lat": 8.1700, "name": "Nagercoil"},
    "marthandam": {"lng": 77.2300, "lat": 8.3100, "name": "Marthandam"},
    "padmanabhapuram": {"lng": 77.3300, "lat": 8.2400, "name": "Padmanabhapuram"},
    "palayamkottai": {"lng": 77.7200, "lat": 8.7200, "name": "Palayamkottai"},
    "ambasamudram": {"lng": 77.4600, "lat": 8.7000, "name": "Ambasamudram"},
    "tenkasi": {"lng": 77.3000, "lat": 8.9600, "name": "Tenkasi"},
    "kulithalai": {"lng": 78.4100, "lat": 10.9300, "name": "Kulithalai"},
    "krishnarayapuram": {"lng": 78.1300, "lat": 10.8800, "name": "Krishnarayapuram"},
    "paramathi velur": {"lng": 78.0600, "lat": 11.3500, "name": "Paramathi Velur"},
    "rasipuram": {"lng": 78.1700, "lat": 11.4600, "name": "Rasipuram"},
    "denkanikottai": {"lng": 77.7800, "lat": 12.5300, "name": "Denkanikottai"},
    "thally": {"lng": 77.6900, "lat": 12.6200, "name": "Thally"},
    "chidambaram": {"lng": 79.6900, "lat": 11.4000, "name": "Chidambaram"},
    "panruti": {"lng": 79.5500, "lat": 11.7700, "name": "Panruti"},
    "neyveli": {"lng": 79.5100, "lat": 11.6100, "name": "Neyveli"},
    "madurai airport": {"lng": 78.0934, "lat": 9.8345, "name": "Madurai Airport (IXM)"},
    "madurai airport (ixm)": {"lng": 78.0934, "lat": 9.8345, "name": "Madurai Airport (IXM)"},
    "chennai airport": {"lng": 80.1709, "lat": 12.9941, "name": "Chennai Airport (MAA)"},
    "chennai airport (maa)": {"lng": 80.1709, "lat": 12.9941, "name": "Chennai Airport (MAA)"},
    "coimbatore airport": {"lng": 77.0434, "lat": 11.0298, "name": "Coimbatore Airport (CJB)"},
    "coimbatore airport (cjb)": {"lng": 77.0434, "lat": 11.0298, "name": "Coimbatore Airport (CJB)"},
    "trichy airport": {"lng": 78.7097, "lat": 10.7654, "name": "Trichy Airport (TRZ)"},
    "trichy airport (trz)": {"lng": 78.7097, "lat": 10.7654, "name": "Trichy Airport (TRZ)"},
}

def resolve_location(address, lng=None, lat=None, default_city="madurai"):
    if lng is not None and lat is not None:
        try:
            return point(float(lng), float(lat))
        except (ValueError, TypeError):
            pass
    if address:
        addr_lower = str(address).strip().lower()
        for key, city in TN_CITIES.items():
            if key in addr_lower:
                return point(city["lng"], city["lat"])
    def_city = TN_CITIES.get(default_city, TN_CITIES["madurai"])
    return point(def_city["lng"], def_city["lat"])


@app.get("/")
def frontend_index():
    return send_from_directory(FRONTEND_DIR, "index.html")

@app.get("/admin")
def admin_page():
    return send_from_directory(FRONTEND_DIR, "admin.html")

@app.get("/frontend/<path:filename>")
def frontend_file(filename):
    return send_from_directory(FRONTEND_DIR, filename)

@app.get("/src/<path:filename>")
def frontend_asset(filename):
    return send_from_directory(ASSETS_DIR, filename)

@app.get("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename)


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)

def oid(value):
    from bson import ObjectId
    try:
        return ObjectId(value) if value else None
    except Exception:
        return None

def serialize(doc):
    if not doc:
        return None
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    doc.pop("password_hash", None)
    for key, value in list(doc.items()):
        if hasattr(value, "binary"):
            doc[key] = str(value)
        elif hasattr(value, "isoformat"):
            doc[key] = value.isoformat()
        elif key.endswith("_id") and value is not None:
            doc[key] = str(value)
    return doc

def point(lng, lat):
    return {"type": "Point", "coordinates": [float(lng), float(lat)]}

def send_mail(to_email, subject, body, html_body=None):
    sender_email = os.getenv("SENDER_EMAIL") or os.getenv("sender_email")
    sender_password = os.getenv("SENDER_APP_PASSWORD") or os.getenv("sender_app_password")
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    if not sender_email or not sender_password or sender_email.startswith("replace-with"):
        app.logger.warning("Email not sent; SMTP credentials are not configured. To=%s Subject=%s Body=%s", to_email, subject, body)
        return False
    try:
        msg = EmailMessage()
        msg["From"] = f"RaniCab <{sender_email}>"
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(sender_email, sender_password)
            smtp.send_message(msg)
        return True
    except Exception as exc:
        app.logger.warning("SMTP failed: %s", exc)
        return False

def send_reset_otp(email, user, otp):
    name = user.get("name", "RaniCab rider")
    text_body = f"Hi {name},\n\nYour RaniCab password reset OTP is {otp}. It expires in 10 minutes.\n\nIf you did not request this, please ignore this email.\n\nRaniCab"
    html_body = f"""
        <!doctype html>
        <html><body style=\"margin:0;background:#faf6ee;color:#1b1b1f;font-family:Arial,sans-serif;\">
            <div style=\"max-width:560px;margin:32px auto;padding:0 20px;\">
                <div style=\"border-top:6px solid #e8a33d;background:#fff;padding:28px 30px;\">
                    <p style=\"margin:0 0 18px;color:#c9832a;font-size:12px;font-weight:bold;letter-spacing:2px;text-transform:uppercase;\">RaniCab</p>
                    <h1 style=\"margin:0 0 12px;font-size:30px;line-height:1.1;\">Reset your password</h1>
                    <p style=\"margin:0 0 24px;color:#54555c;font-size:15px;line-height:1.6;\">Hi {escape(name)}, use this one-time code to reset your RaniCab password.</p>
                    <div style=\"background:#fbe7c6;padding:18px;text-align:center;\">
                        <span style=\"font-size:32px;font-weight:bold;letter-spacing:8px;\">{otp}</span>
                    </div>
                    <p style=\"margin:20px 0 0;color:#54555c;font-size:13px;line-height:1.6;\">This code expires in 10 minutes. If you did not request a password reset, you can safely ignore this email.</p>
                </div>
                <p style=\"margin:18px 0;text-align:center;color:#6b6f76;font-size:12px;\">RaniCab Mobility Pvt Ltd - Madurai, Tamil Nadu</p>
            </div>
        </body></html>
    """
    return send_mail(email, "Your RaniCab password reset OTP", text_body, html_body)

def send_completion_otp_email(rider_email, rider_name, otp):
    subject = "RaniCab Trip Completion OTP"
    text_body = f"Hi {rider_name},\n\nYour driver has completed your trip. Please enter this OTP in your app to confirm: {otp}\n\nThis OTP expires in 10 minutes.\n\nRaniCab"
    html_body = f"""
        <!doctype html>
        <html><body style="margin:0;background:#faf6ee;color:#1b1b1f;font-family:Arial,sans-serif;">
            <div style="max-width:560px;margin:32px auto;padding:0 20px;">
                <div style="border-top:6px solid #e8a33d;background:#fff;padding:28px 30px;">
                    <p style="margin:0 0 18px;color:#c9832a;font-size:12px;font-weight:bold;letter-spacing:2px;text-transform:uppercase;">RaniCab</p>
                    <h1 style="margin:0 0 12px;font-size:30px;line-height:1.1;">Trip Completion OTP</h1>
                    <p style="margin:0 0 24px;color:#54555c;font-size:15px;line-height:1.6;">Hi {escape(rider_name)}, your driver has completed your ride. Use this OTP to confirm.</p>
                    <div style="background:#fbe7c6;padding:18px;text-align:center;">
                        <span style="font-size:32px;font-weight:bold;letter-spacing:8px;">{otp}</span>
                    </div>
                    <p style="margin:20px 0 0;color:#54555c;font-size:13px;line-height:1.6;">This OTP expires in 10 minutes. If you did not request this, please contact support.</p>
                </div>
            </div>
        </body></html>
    """
    return send_mail(rider_email, subject, text_body, html_body)

def create_or_update_google_user(profile, phone=""):
    email = profile["email"].lower()
    name = profile.get("name") or email.split("@")[0]
    existing = users.find_one({"email": email})
    update = {"$set": {"name": name, "email": email, "role": "rider", "google_sub": profile.get("sub"), "provider": "google", "updated_at": now()}}
    if phone:
        update["$set"]["phone"] = phone
    if existing:
        users.update_one({"_id": existing["_id"]}, update)
        return users.find_one({"_id": existing["_id"]})
    user = {**update["$set"], "phone": phone, "rating": 5.0, "created_at": now()}
    result = users.insert_one(user)
    user["_id"] = result.inserted_id
    return user

def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = users.find_one({"_id": oid(user_id)})
    return user

def require_role(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user or user.get("role") not in roles:
                return jsonify({"error": "Unauthorized"}), 401
            request.user = user
            return fn(*args, **kwargs)
        return wrapper
    return decorator

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return jsonify({"error": "Admin authentication required"}), 401
        return fn(*args, **kwargs)
    return wrapper


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "rani-cab"}

@app.get("/api/config")
def public_config():
    return {
        "brand": "Rani Cab",
        "google_client_id": os.getenv("GOOGLE_CLIENT_ID", "")
    }

@app.get("/api/auth/me")
def auth_me():
    user = current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    user_data = serialize(user)
    if user.get("role") == "driver":
        driver_doc = drivers.find_one({"user_id": user["_id"]})
        if driver_doc:
            user_data["driver_profile"] = serialize(driver_doc)
            user_data["is_online"] = driver_doc.get("is_online", False)
    return {"user": user_data}

@app.post("/api/auth/logout")
def logout():
    session.clear()
    return {"message": "Logged out"}

@app.post("/api/auth/register")
def register_rider():
    data = request.get_json(force=True) or {}
    if data.get("role", "rider") != "rider":
        return jsonify({"error": "Drivers must be registered by admin"}), 403
    name = data.get("name", "").strip()
    username = data.get("username", name).strip().lower()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    if len(name) < 2 or len(username) < 2 or "@" not in email or len(password) < 6:
        return jsonify({"error": "Enter a valid name, username, email, and password of at least 6 characters"}), 400
    if users.find_one({"email": email}):
        return jsonify({"error": "An account with this email already exists"}), 409
    user = {
        "name": name,
        "username": username,
        "email": email,
        "phone": data.get("phone", "").strip(),
        "password_hash": generate_password_hash(password),
        "role": "rider",
        "rating": 5.0,
        "provider": "manual",
        "created_at": now(),
    }
    try:
        result = users.insert_one(user)
    except DuplicateKeyError:
        return jsonify({"error": "That username is already taken"}), 409
    user["_id"] = result.inserted_id
    session["user_id"] = str(result.inserted_id)
    try:
        send_mail(email, "Welcome to RaniCab", f"Hi {name},\n\nYour RaniCab account is ready. You can now log in and book rides across Tamil Nadu.\n\nRaniCab")
    except Exception as exc:
        app.logger.warning("Welcome email could not be sent: %s", exc)
    return jsonify({"user": serialize(user)}), 201

@app.post("/api/auth/login")
def login():
    data = request.get_json(force=True) or {}
    login_id = (data.get("email") or data.get("username") or "").strip()
    if not login_id:
        return jsonify({"error": "Please provide an email or Driver ID / username"}), 400
    user = users.find_one({
        "$or": [
            {"email": login_id.lower()},
            {"username": login_id.lower()},
            {"username": login_id}
        ]
    })
    requested_role = data.get("role")
    if (not user or (requested_role and user.get("role") != requested_role)
            or not check_password_hash(user.get("password_hash", ""), data.get("password", ""))):
        return jsonify({"error": "Invalid credentials"}), 401
    session["user_id"] = str(user["_id"])
    user_data = serialize(user)
    if user.get("role") == "driver":
        driver_doc = drivers.find_one({"user_id": user["_id"]})
        if driver_doc:
            user_data["driver_profile"] = serialize(driver_doc)
            user_data["is_online"] = driver_doc.get("is_online", False)
    return {"user": user_data}

@app.post("/api/auth/google")
def google_auth():
    data = request.get_json(force=True) or {}
    token = data.get("credential")
    if not token:
        return jsonify({"error": "Missing Google credential"}), 400
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
        profile = id_token.verify_oauth2_token(token, google_requests.Request(), os.getenv("GOOGLE_CLIENT_ID"))
    except Exception as exc:
        app.logger.warning("Google sign-in failed: %s", exc)
        return jsonify({"error": "Google sign-in could not be verified"}), 401
    if not profile.get("email_verified"):
        return jsonify({"error": "Google email is not verified"}), 401
    user = create_or_update_google_user(profile, data.get("phone", ""))
    session["user_id"] = str(user["_id"])
    return {"user": serialize(user)}

@app.post("/api/auth/forgot-password/request")
def forgot_password_request():
    data = request.get_json(force=True) or {}
    email = data.get("email", "").lower().strip()
    user = users.find_one({"email": email, "provider": {"$ne": "google"}, "password_hash": {"$exists": True}})
    if not user:
        return jsonify({"error": "Forgot password works only for manually signed-up accounts"}), 404
    existing_otp = password_otps.find_one({"email": email}, sort=[("created_at", -1)])
    existing_expiry = existing_otp.get("expires_at") if existing_otp else None
    if existing_expiry:
        if existing_expiry.tzinfo is not None:
            existing_expiry = existing_expiry.replace(tzinfo=None)
        if existing_expiry > now():
            return {"message": "OTP already sent. Use the OTP from your email."}
    otp = f"{secrets.randbelow(1000000):06d}"
    if not send_reset_otp(email, user, otp):
        return jsonify({"error": "We could not send the reset email. Please try again later."}), 503
    password_otps.delete_many({"email": email})
    password_otps.insert_one({"email": email, "otp_hash": generate_password_hash(otp), "expires_at": now() + timedelta(minutes=10), "attempts": 0, "created_at": now(), "sent_at": now()})
    return {"message": "OTP sent to your registered email"}

@app.post("/api/auth/forgot-password/resend")
def forgot_password_resend():
    data = request.get_json(force=True) or {}
    email = data.get("email", "").lower().strip()
    user = users.find_one({"email": email, "provider": {"$ne": "google"}, "password_hash": {"$exists": True}})
    if not user:
        return jsonify({"error": "Forgot password works only for manually signed-up accounts"}), 404
    existing_otp = password_otps.find_one({"email": email}, sort=[("created_at", -1)])
    if not existing_otp:
        return jsonify({"error": "Request an OTP first."}), 400
    sent_at = existing_otp.get("sent_at") or existing_otp.get("created_at")
    if sent_at and sent_at.tzinfo is not None:
        sent_at = sent_at.replace(tzinfo=None)
    remaining_seconds = max(0, 60 - int((now() - sent_at).total_seconds())) if sent_at else 0
    if remaining_seconds > 0:
        return jsonify({
            "error": f"Please wait {remaining_seconds} second(s) before requesting another OTP.",
            "retry_after_seconds": remaining_seconds
        }), 429
    otp = f"{secrets.randbelow(1000000):06d}"
    if not send_reset_otp(email, user, otp):
        return jsonify({"error": "We could not send the reset email. Please try again later."}), 503
    password_otps.delete_many({"email": email})
    password_otps.insert_one({"email": email, "otp_hash": generate_password_hash(otp), "expires_at": now() + timedelta(minutes=10), "attempts": 0, "created_at": now(), "sent_at": now()})
    return {"message": "A new OTP was sent to your email"}

@app.post("/api/auth/forgot-password/verify")
def forgot_password_verify():
    data = request.get_json(force=True) or {}
    email = data.get("email", "").lower().strip()
    otp_doc = password_otps.find_one({"email": email})
    if not otp_doc:
        return jsonify({"error": "No OTP request found for this email"}), 400

    expires_at = otp_doc.get("expires_at")
    if not expires_at or expires_at < now():
        return jsonify({"error": "OTP expired – please request a new one"}), 400

    if otp_doc.get("attempts", 0) >= 5:
        return jsonify({"error": "Too many failed attempts"}), 400

    if not check_password_hash(otp_doc.get("otp_hash", ""), data.get("otp", "")):
        password_otps.update_one({"_id": otp_doc["_id"]}, {"$inc": {"attempts": 1}})
        return jsonify({"error": "Invalid OTP"}), 400

    new_password = data.get("password", "")
    if len(new_password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    users.update_one(
        {"email": email, "provider": {"$ne": "google"}},
        {"$set": {"password_hash": generate_password_hash(new_password), "updated_at": now()}}
    )
    password_otps.delete_many({"email": email})
    return {"message": "Password updated successfully"}


# ============ ADMIN ENDPOINTS ============

@app.get("/api/admin/me")
def admin_me():
    if session.get("admin"):
        return {"admin": True, "username": os.getenv("ADMIN_USERNAME", "admin")}
    return jsonify({"error": "Admin authentication required"}), 401

@app.post("/api/admin/login")
def admin_login():
    data = request.get_json(force=True) or {}
    if data.get("username") == os.getenv("ADMIN_USERNAME", "admin") and data.get("password") == os.getenv("ADMIN_PASSWORD", "Admin@123"):
        session["admin"] = True
        return {"admin": True, "message": "Admin logged in successfully"}
    return jsonify({"error": "Invalid admin credentials"}), 401

@app.post("/api/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return {"message": "Admin logged out"}

@app.post("/api/admin/drivers")
@admin_required
def create_driver():
    data = request.form or request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Driver name is required"}), 400

    username = f"driver{secrets.randbelow(900000) + 100000}"
    password = secrets.token_urlsafe(8)
    email = (data.get("email") or "").strip().lower() or f"{username}@rani-cab.local"
    phone = (data.get("phone") or "").strip()
    vehicle_model = (data.get("vehicle_model") or "Sedan").strip()
    license_plate = (data.get("license_plate") or f"TN-{secrets.randbelow(90)+10}-AB-{secrets.randbelow(9000)+1000}").strip()
    upi_id = (data.get("upi_id") or "").strip()
    city = data.get("city", "madurai")
    send_creds = data.get("send_credentials", True)

    loc = resolve_location(city)

    user = {
        "name": name,
        "username": username,
        "email": email,
        "phone": phone,
        "password_hash": generate_password_hash(password),
        "role": "driver",
        "rating": 5.0,
        "created_at": now()
    }
    try:
        result = users.insert_one(user)
    except DuplicateKeyError:
        return jsonify({"error": "A driver with this email already exists"}), 409

    driver = {
        "user_id": result.inserted_id,
        "vehicle_model": vehicle_model,
        "license_plate": license_plate,
        "upi_id": upi_id,
        "is_online": False,
        "current_location": loc,
        "created_at": now()
    }
    drv_result = drivers.insert_one(driver)

    # Handle image uploads
    image_urls = {}
    if request.files:
        for img_type in IMAGE_TYPES:
            file = request.files.get(img_type)
            if file and file.filename:
                ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
                if ext in ALLOWED_IMAGE_EXTENSIONS:
                    fname = f"{drv_result.inserted_id}_{img_type}_{secrets.token_hex(6)}.{ext}"
                    file.save(os.path.join(UPLOAD_DIR, fname))
                    url = f"/uploads/{fname}"
                    image_urls[f"{img_type}_image"] = url
        if image_urls:
            drivers.update_one({"_id": drv_result.inserted_id}, {"$set": image_urls})

    # Auto-send credentials email
    email_sent = False
    if send_creds and email and "@" in email:
        subject = "Your RaniCab Driver Account Credentials"
        body = f"""Hi {name},

Welcome to RaniCab! Your driver account has been created.

Here are your login credentials:

  Username : {username}
  Email    : {email}
  Password : {password}

Vehicle      : {vehicle_model}
License Plate: {license_plate}

Please log in to the RaniCab Driver app and change your password.

RaniCab Admin Team"""
        html_body = f"""
        <!doctype html><html><body style="margin:0;background:#faf6ee;font-family:Arial,sans-serif;color:#1b1b1f;">
          <div style="max-width:560px;margin:32px auto;padding:0 20px;">
            <div style="border-top:6px solid #e8a33d;background:#fff;padding:28px 30px;">
              <p style="margin:0 0 18px;color:#c9832a;font-size:12px;font-weight:bold;letter-spacing:2px;">RaniCab Admin</p>
              <h1 style="margin:0 0 12px;font-size:26px;">Driver Account Created</h1>
              <p style="margin:0 0 20px;color:#54555c;font-size:15px;line-height:1.6;">Hi {escape(name)}, welcome to RaniCab! Use the credentials below to log in to the Driver app.</p>
              <div style="background:#fbe7c6;padding:18px;border-radius:6px;">
                <p style="margin:0 0 8px;"><b>Username:</b> {escape(username)}</p>
                <p style="margin:0 0 8px;"><b>Email:</b> {escape(email)}</p>
                <p style="margin:0;"><b>Password:</b> <span style="font-size:18px;font-weight:bold;letter-spacing:2px;">{escape(password)}</span></p>
              </div>
              <p style="margin:20px 0 0;color:#54555c;font-size:13px;">Vehicle: {escape(vehicle_model)} | Plate: {escape(license_plate)}</p>
              <p style="margin:12px 0 0;color:#999;font-size:12px;">Please change your password after first login.</p>
            </div>
          </div>
        </body></html>"""
        email_sent = send_mail(email, subject, body, html_body)

    return jsonify({
        "driver": serialize(drivers.find_one({"_id": drv_result.inserted_id})),
        "credentials": {"username": username, "email": email, "password": password, "name": name},
        "email_sent": email_sent
    }), 201

@app.get("/api/admin/overview")
@admin_required
def admin_overview():
    today = now().replace(hour=0, minute=0, second=0, microsecond=0)
    month = today.replace(day=1)
    
    def revenue_since(start=None):
        match = {"status": "completed"}
        if start:
            match["completed_at"] = {"$gte": start}
        agg = list(rides.aggregate([{"$match": match}, {"$group": {"_id": None, "total": {"$sum": "$fare"}}}]))
        return agg[0]["total"] if agg else 0
    
    roster = []
    for driver in drivers.find():
        user = users.find_one({"_id": driver["user_id"]}) or {}
        loc = driver.get("current_location", {}).get("coordinates", [78.1198, 9.9252])
        roster.append({
            **serialize(driver),
            "name": user.get("name", "Driver"),
            "username": user.get("username", ""),
            "phone": user.get("phone", ""),
            "email": user.get("email", ""),
            "rating": user.get("rating", 5.0),
            "profile_image": driver.get("profile_image", ""),
            "licence_image": driver.get("licence_image", ""),
            "rc_book_image": driver.get("rc_book_image", ""),
            "insurance_image": driver.get("insurance_image", ""),
            "vehicle_image": driver.get("vehicle_image", ""),
            "lng": loc[0] if len(loc) > 0 else 78.1198,
            "lat": loc[1] if len(loc) > 1 else 9.9252,
        })
    
    recent_rides = [serialize(r) for r in rides.find().sort("created_at", -1).limit(10)]
    pricing = settings.find_one({"type": "pricing"}) or {}
    
    return {
        "revenue": {
            "today": revenue_since(today),
            "month": revenue_since(month),
            "all_time": revenue_since()
        },
        "drivers": roster,
        "recent_rides": recent_rides,
        "pricing": serialize(pricing)
    }

@app.route("/api/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        update = {
            "base_fare": float(data.get("base_fare", 80)),
            "price_per_km": float(data.get("price_per_km", 18)),
            "surge_multiplier": float(data.get("surge_multiplier", 1.0)),
            "maintenance_mode": bool(data.get("maintenance_mode", False)),
            "updated_at": now()
        }
        settings.update_one({"type": "pricing"}, {"$set": update}, upsert=True)
        return {"message": "Settings updated", "settings": update}
    pricing = settings.find_one({"type": "pricing"}) or {
        "base_fare": 80, "price_per_km": 18, "surge_multiplier": 1.0, "maintenance_mode": False
    }
    return {"settings": serialize(pricing)}


# ============ ADMIN: DRIVER DETAIL WITH IMAGES ============

@app.get("/api/admin/drivers/<driver_id>")
@admin_required
def admin_get_driver_detail(driver_id):
    driver_oid = oid(driver_id)
    driver = drivers.find_one({"_id": driver_oid})
    if not driver:
        return jsonify({"error": "Driver not found"}), 404

    user = users.find_one({"_id": driver["user_id"]}) or {}
    loc = driver.get("current_location", {}).get("coordinates", [0, 0])

    # Driver's rides
    driver_rides = list(rides.find({"driver_id": driver["user_id"]}).sort("created_at", -1).limit(30))
    completed = [r for r in driver_rides if r.get("status") == "completed"]
    total_earnings = sum(float(r.get("fare", 0)) for r in completed)

    return {
        "driver": {
            **serialize(driver),
            "name": user.get("name", ""),
            "username": user.get("username", ""),
            "email": user.get("email", ""),
            "phone": user.get("phone", ""),
            "rating": user.get("rating", 5.0),
            "ratings_count": user.get("ratings_count", 0),
            "profile_image": driver.get("profile_image", ""),
            "licence_image": driver.get("licence_image", ""),
            "rc_book_image": driver.get("rc_book_image", ""),
            "insurance_image": driver.get("insurance_image", ""),
            "vehicle_image": driver.get("vehicle_image", ""),
            "lng": loc[0] if loc else 0,
            "lat": loc[1] if loc else 0,
            "is_online": driver.get("is_online", False),
            "vehicle_model": driver.get("vehicle_model", ""),
            "license_plate": driver.get("license_plate", ""),
            "upi_id": driver.get("upi_id", ""),
        },
        "stats": {
            "total_rides": len(completed),
            "total_earnings": total_earnings,
            "avg_rating": user.get("rating", 5.0),
            "recent_rides": [serialize(r) for r in driver_rides[:10]],
        }
    }

# ============ ADMIN: UPLOAD DRIVER IMAGE ============

@app.post("/api/admin/drivers/<driver_id>/upload")
@admin_required
def admin_upload_driver_image(driver_id):
    driver_oid = oid(driver_id)
    driver = drivers.find_one({"_id": driver_oid})
    if not driver:
        return jsonify({"error": "Driver not found"}), 404

    image_type = request.form.get("type", "")
    if image_type not in IMAGE_TYPES:
        return jsonify({"error": f"Invalid image type. Must be one of: {', '.join(IMAGE_TYPES)}"}), 400

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if not file or not file.filename:
        return jsonify({"error": "Empty file"}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return jsonify({"error": "Only PNG, JPG, JPEG, WEBP files allowed"}), 400

    filename = f"{driver_id}_{image_type}_{secrets.token_hex(6)}.{ext}"
    file.save(os.path.join(UPLOAD_DIR, filename))
    url = f"/uploads/{filename}"
    field = f"{image_type}_image"
    drivers.update_one({"_id": driver_oid}, {"$set": {field: url}})

    return {"url": url, "type": image_type, "field": field}

# ============ ADMIN: UPDATE DRIVER ============

@app.put("/api/admin/drivers/<driver_id>")
@admin_required
def admin_update_driver(driver_id):
    driver_oid = oid(driver_id)
    driver = drivers.find_one({"_id": driver_oid})
    if not driver:
        return jsonify({"error": "Driver not found"}), 404

    data = request.get_json(force=True) or {}

    user_update = {}
    for field in ["name", "phone", "email"]:
        if field in data and data[field]:
            user_update[field] = data[field].strip() if isinstance(data[field], str) else data[field]
    if user_update:
        user_update["updated_at"] = now()
        users.update_one({"_id": driver["user_id"]}, {"$set": user_update})

    driver_update = {}
    for field in ["vehicle_model", "license_plate", "upi_id"]:
        if field in data and data[field] is not None:
            driver_update[field] = data[field].strip() if isinstance(data[field], str) else data[field]
    if "city" in data:
        driver_update["current_location"] = resolve_location(data["city"])
    if driver_update:
        driver_update["updated_at"] = now()
        drivers.update_one({"_id": driver_oid}, {"$set": driver_update})

    return {"message": "Driver updated successfully"}

# ============ ADMIN: DELETE DRIVER ============

@app.delete("/api/admin/drivers/<driver_id>")
@admin_required
def admin_delete_driver(driver_id):
    driver_oid = oid(driver_id)
    driver = drivers.find_one({"_id": driver_oid})
    if not driver:
        return jsonify({"error": "Driver not found"}), 404

    active = rides.find_one({"driver_id": driver["user_id"], "status": {"$in": ACTIVE_STATUSES}})
    if active:
        return jsonify({"error": "Cannot delete driver with active rides"}), 400

    drivers.delete_one({"_id": driver_oid})
    users.delete_one({"_id": driver["user_id"]})
    return {"message": "Driver deleted successfully"}

# ============ ADMIN: SEND CUSTOM EMAIL TO DRIVER ============

@app.post("/api/admin/drivers/<driver_id>/send-mail")
@admin_required
def admin_send_driver_mail(driver_id):
    driver_oid = oid(driver_id)
    driver = drivers.find_one({"_id": driver_oid})
    if not driver:
        return jsonify({"error": "Driver not found"}), 404

    user = users.find_one({"_id": driver["user_id"]})
    if not user or not user.get("email"):
        return jsonify({"error": "Driver has no email address"}), 404

    data = request.get_json(force=True) or {}
    subject = (data.get("subject") or "").strip()
    body = (data.get("body") or "").strip()

    if not subject or not body:
        return jsonify({"error": "Subject and message body are required"}), 400

    html_body = f"""
    <!doctype html><html><body style="margin:0;background:#faf6ee;font-family:Arial,sans-serif;color:#1b1b1f;">
      <div style="max-width:560px;margin:32px auto;padding:0 20px;">
        <div style="border-top:6px solid #e8a33d;background:#fff;padding:28px 30px;">
          <p style="margin:0 0 18px;color:#c9832a;font-size:12px;font-weight:bold;letter-spacing:2px;">RaniCab Admin Message</p>
          <h1 style="margin:0 0 12px;font-size:24px;">{escape(subject)}</h1>
          <p style="margin:0 0 16px;color:#54555c;font-size:15px;">Hi {escape(user.get('name', 'Driver'))},</p>
          <div style="background:#f5f3ed;padding:20px;border-left:4px solid #e8a33d;">
            <p style="margin:0;white-space:pre-line;line-height:1.7;color:#333;">{escape(body)}</p>
          </div>
          <p style="margin:20px 0 0;color:#999;font-size:12px;">— RaniCab Admin Team</p>
        </div>
      </div>
    </body></html>"""

    if not send_mail(user["email"], subject, body, html_body):
        return jsonify({"error": "Failed to send email. Check SMTP settings."}), 503

    return {"message": f"Email sent to {user['email']}"}

# ============ ADMIN: RE-SEND CREDENTIALS EMAIL ============

@app.post("/api/admin/drivers/<driver_id>/send-credentials")
@admin_required
def admin_resend_credentials(driver_id):
    driver_oid = oid(driver_id)
    driver = drivers.find_one({"_id": driver_oid})
    if not driver:
        return jsonify({"error": "Driver not found"}), 404

    user = users.find_one({"_id": driver["user_id"]})
    if not user:
        return jsonify({"error": "Driver user account not found"}), 404

    # Generate new password
    new_password = secrets.token_urlsafe(8)
    users.update_one({"_id": user["_id"]}, {
        "$set": {"password_hash": generate_password_hash(new_password), "updated_at": now()}
    })

    subject = "Your RaniCab Driver Credentials (Reset)"
    body = f"""Hi {user.get('name', 'Driver')},

Your RaniCab driver account credentials have been reset by the admin.

  Username : {user.get('username')}
  Email    : {user.get('email')}
  Password : {new_password}

Vehicle      : {driver.get('vehicle_model', 'N/A')}
License Plate: {driver.get('license_plate', 'N/A')}

Please log in and change your password.

RaniCab Admin Team"""

    html_body = f"""
    <!doctype html><html><body style="margin:0;background:#faf6ee;font-family:Arial,sans-serif;color:#1b1b1f;">
      <div style="max-width:560px;margin:32px auto;padding:0 20px;">
        <div style="border-top:6px solid #e8a33d;background:#fff;padding:28px 30px;">
          <p style="margin:0 0 18px;color:#c9832a;font-size:12px;font-weight:bold;letter-spacing:2px;">RaniCab Admin</p>
          <h1 style="margin:0 0 12px;font-size:26px;">Credentials Reset</h1>
          <p style="margin:0 0 20px;color:#54555c;font-size:15px;">Hi {escape(user.get('name', 'Driver'))}, your password has been reset.</p>
          <div style="background:#fbe7c6;padding:18px;">
            <p style="margin:0 0 8px;"><b>Username:</b> {escape(user.get('username', ''))}</p>
            <p style="margin:0 0 8px;"><b>Email:</b> {escape(user.get('email', ''))}</p>
            <p style="margin:0;"><b>New Password:</b> <span style="font-size:18px;font-weight:bold;">{escape(new_password)}</span></p>
          </div>
          <p style="margin:16px 0 0;color:#999;font-size:12px;">Please change your password after logging in.</p>
        </div>
      </div>
    </body></html>"""

    sent = send_mail(user.get("email", ""), subject, body, html_body)
    if not sent:
        return jsonify({"error": "Failed to send email"}), 503

    return {"message": "New credentials sent to driver's email", "temp_password": new_password}

# ============ ADMIN: REAL ANALYTICS ============

@app.get("/api/admin/analytics")
@admin_required
def admin_analytics():
    today = now().replace(hour=0, minute=0, second=0, microsecond=0)

    total_drivers = drivers.count_documents({})
    online_drivers_list = list(drivers.find({"is_online": True}))
    online_count = len(online_drivers_list)
    total_riders = users.count_documents({"role": "rider"})
    total_rides = rides.count_documents({})
    today_rides = rides.count_documents({"created_at": {"$gte": today}})

    today_rev_agg = list(rides.aggregate([
        {"$match": {"status": "completed", "completed_at": {"$gte": today}}},
        {"$group": {"_id": None, "total": {"$sum": "$fare"}}}
    ]))
    today_revenue = today_rev_agg[0]["total"] if today_rev_agg else 0

    month = today.replace(day=1)
    month_rev_agg = list(rides.aggregate([
        {"$match": {"status": "completed", "completed_at": {"$gte": month}}},
        {"$group": {"_id": None, "total": {"$sum": "$fare"}}}
    ]))
    month_revenue = month_rev_agg[0]["total"] if month_rev_agg else 0

    all_rev_agg = list(rides.aggregate([
        {"$match": {"status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": "$fare"}}}
    ]))
    all_time_revenue = all_rev_agg[0]["total"] if all_rev_agg else 0

    weekly_chart = []
    for i in range(6, -1, -1):
        day_start = today - timedelta(days=i)
        day_end = day_start + timedelta(days=1)
        day_rides = rides.count_documents({"created_at": {"$gte": day_start, "$lt": day_end}})
        day_rev = list(rides.aggregate([
            {"$match": {"status": "completed", "completed_at": {"$gte": day_start, "$lt": day_end}}},
            {"$group": {"_id": None, "total": {"$sum": "$fare"}}}
        ]))
        weekly_chart.append({
            "label": day_start.strftime("%a"),
            "date": day_start.strftime("%Y-%m-%d"),
            "rides": day_rides,
            "revenue": day_rev[0]["total"] if day_rev else 0
        })

    monthly_chart = []
    for i in range(29, -1, -1):
        day_start = today - timedelta(days=i)
        day_end = day_start + timedelta(days=1)
        day_rev = list(rides.aggregate([
            {"$match": {"status": "completed", "completed_at": {"$gte": day_start, "$lt": day_end}}},
            {"$group": {"_id": None, "total": {"$sum": "$fare"}}}
        ]))
        monthly_chart.append({
            "label": day_start.strftime("%d %b"),
            "revenue": day_rev[0]["total"] if day_rev else 0
        })

    status_breakdown = {}
    for status in ["requested", "accepted", "ongoing", "pending_completion", "completed", "cancelled"]:
        status_breakdown[status] = rides.count_documents({"status": status})

    top_drivers_agg = list(rides.aggregate([
        {"$match": {"status": "completed", "driver_id": {"$ne": None}}},
        {"$group": {"_id": "$driver_id", "earnings": {"$sum": "$fare"}, "rides": {"$sum": 1}}},
        {"$sort": {"earnings": -1}},
        {"$limit": 5}
    ]))
    top_drivers = []
    for td in top_drivers_agg:
        u = users.find_one({"_id": td["_id"]}) or {}
        top_drivers.append({
            "name": u.get("name", "Unknown"),
            "username": u.get("username", ""),
            "earnings": round(td["earnings"], 2),
            "rides": td["rides"]
        })

    online_drivers_detail = []
    for d in online_drivers_list:
        u = users.find_one({"_id": d["user_id"]}) or {}
        loc = d.get("current_location", {}).get("coordinates", [0, 0])
        online_drivers_detail.append({
            "id": str(d["_id"]),
            "user_id": str(d["user_id"]),
            "name": u.get("name", "Driver"),
            "username": u.get("username", ""),
            "phone": u.get("phone", ""),
            "vehicle_model": d.get("vehicle_model", "N/A"),
            "license_plate": d.get("license_plate", "N/A"),
            "rating": u.get("rating", 5.0),
            "profile_image": d.get("profile_image", ""),
            "lng": loc[0] if loc else 0,
            "lat": loc[1] if loc else 0,
            "last_seen": d.get("location_updated_at"),
        })

    hourly_chart = []
    for h in range(24):
        h_start = today + timedelta(hours=h)
        h_end = h_start + timedelta(hours=1)
        if h_start > now():
            hourly_chart.append({"hour": f"{h:02d}:00", "rides": 0})
        else:
            cnt = rides.count_documents({"created_at": {"$gte": h_start, "$lt": h_end}})
            hourly_chart.append({"hour": f"{h:02d}:00", "rides": cnt})

    return {
        "summary": {
            "total_drivers": total_drivers,
            "online_drivers": online_count,
            "offline_drivers": total_drivers - online_count,
            "total_riders": total_riders,
            "total_rides": total_rides,
            "today_rides": today_rides,
            "today_revenue": round(today_revenue, 2),
            "month_revenue": round(month_revenue, 2),
            "all_time_revenue": round(all_time_revenue, 2),
        },
        "weekly_chart": weekly_chart,
        "monthly_chart": monthly_chart,
        "hourly_chart": hourly_chart,
        "status_breakdown": status_breakdown,
        "top_drivers": top_drivers,
        "online_drivers_list": online_drivers_detail,
    }

# ============ ADMIN: ALL RIDES ============

@app.get("/api/admin/rides")
@admin_required
def admin_all_rides():
    status_filter = request.args.get("status", "")
    query = {"status": status_filter} if status_filter else {}
    all_rides = list(rides.find(query).sort("created_at", -1).limit(200))

    results = []
    for r in all_rides:
        doc = serialize(r)
        rider = users.find_one({"_id": r.get("rider_id")}) or {}
        doc["rider_name"] = rider.get("name", "Unknown")
        doc["rider_phone"] = rider.get("phone", "")
        doc["rider_email"] = rider.get("email", "")
        if r.get("driver_id"):
            drv_user = users.find_one({"_id": r["driver_id"]}) or {}
            doc["driver_name"] = drv_user.get("name", "Unknown")
            doc["driver_phone"] = drv_user.get("phone", "")
        else:
            doc["driver_name"] = "—"
            doc["driver_phone"] = ""
        results.append(doc)

    return {"rides": results}


# ============ RIDE & DISPATCH ENDPOINTS ============

def issue_completion_otp(ride, ride_oid):
    otp = f"{secrets.randbelow(1000000):06d}"
    otp_hash = generate_password_hash(otp)
    expires_at = now() + timedelta(minutes=10)

    rides.update_one(
        {"_id": ride_oid},
        {"$set": {
            "completion_otp_hash": otp_hash,
            "completion_otp_expires": expires_at,
            "completion_otp_sent_at": now(),
        }}
    )

    rider = users.find_one({"_id": ride["rider_id"]})
    if rider and rider.get("email"):
        send_completion_otp_email(rider["email"], rider.get("name", "Rider"), otp)
    else:
        app.logger.warning(f"Rider email missing for ride {ride['_id']}, OTP not sent")

    return serialize(rides.find_one({"_id": ride_oid}))


@app.post("/api/rides/request")
@require_role("rider")
def request_ride():
    existing_active = rides.find_one({
        "rider_id": request.user["_id"],
        "status": {"$in": ACTIVE_STATUSES + ["pending_completion"]}
    })
    if existing_active:
        return jsonify({
            "error": "You already have an active ride in progress. Please complete or cancel it before booking a new one.",
            "code": "ACTIVE_RIDE_EXISTS",
            "active_ride_id": str(existing_active["_id"])
        }), 409

    data = request.get_json(force=True) or {}
    pickup_addr = data.get("pickup_address", "Madurai")
    dropoff_addr = data.get("dropoff_address", "Chennai")
    
    pickup = resolve_location(pickup_addr, data.get("pickup_lng"), data.get("pickup_lat"), default_city="madurai")
    dropoff = resolve_location(dropoff_addr, data.get("dropoff_lng"), data.get("dropoff_lat"), default_city="chennai")
    
    fare = float(data.get("estimated_fare") or data.get("fare") or 0)
    trip_type = data.get("trip_type", "oneway")
    
    ride = {
        "rider_id": request.user["_id"],
        "rider_name": request.user.get("name", "Rider"),
        "rider_phone": data.get("passenger_phone") or request.user.get("phone", ""),
        "driver_id": None,
        "pickup_location": pickup,
        "dropoff_location": dropoff,
        "pickup_address": pickup_addr,
        "dropoff_address": dropoff_addr,
        "trip_type": trip_type,
        "scheduled_date": data.get("scheduled_date", ""),
        "scheduled_time": data.get("scheduled_time", ""),
        "duration_hours": data.get("duration_hours"),
        "flight_number": data.get("flight_number", ""),
        "status": "requested",
        "fare": fare,
        "created_at": now()
    }
    result = rides.insert_one(ride)
    ride["_id"] = result.inserted_id
    payload = serialize(ride)
    
    nearby_drivers = list(drivers.find({"is_online": True}))
    
    socketio.emit("ride_request", payload, room="drivers")
    for d in nearby_drivers:
        socketio.emit("ride_request", payload, room=f"driver:{d['user_id']}")
    
    return {"ride": payload, "nearby_drivers": len(nearby_drivers)}

@app.get("/api/rides/available")
@require_role("driver")
def available_rides():
    open_rides = list(rides.find({"status": "requested"}).sort("created_at", -1).limit(10))
    return {"rides": [serialize(r) for r in open_rides]}

@app.post("/api/rides/<ride_id>/accept")
@require_role("driver")
def accept_ride(ride_id):
    ride_oid = oid(ride_id)
    if not ride_oid:
        return jsonify({"error": "Invalid ride ID"}), 400

    driver = drivers.find_one({"user_id": request.user["_id"]})
    if not driver:
        return jsonify({"error": "Driver profile not found"}), 404

    driver_info = {
        "name": request.user.get("name", "Driver"),
        "phone": request.user.get("phone", ""),
        "rating": driver.get("rating", 5.0),
        "vehicle_model": driver.get("vehicle_model", "Sedan"),
        "license_plate": driver.get("license_plate", "TN-01-AB-1234"),
    }

    result = rides.update_one(
        {"_id": ride_oid, "status": "requested"},
        {
            "$set": {
                "driver_id": request.user["_id"],
                "status": "accepted",
                "accepted_at": now(),
                "driver_info": driver_info,
                "rider_verified": False,
            },
            "$unset": {"completion_otp_hash": "", "completion_otp_expires": ""},
        }
    )
    if result.modified_count == 0:
        return jsonify({"error": "Ride already accepted or no longer available"}), 409

    ride = rides.find_one({"_id": ride_oid})
    payload = serialize(ride)
    payload["driver"] = driver_info

    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    socketio.emit("ride_updated", payload, room="drivers")

    return {"ride": payload, "driver": driver_info}

@app.post("/api/rides/<ride_id>/verify")
@require_role("rider")
def verify_driver(ride_id):
    ride_oid = oid(ride_id)
    if not ride_oid:
        return jsonify({"error": "Invalid ride ID"}), 400

    result = rides.update_one(
        {"_id": ride_oid, "rider_id": request.user["_id"], "status": "accepted"},
        {"$set": {"rider_verified": True, "verified_at": now()}}
    )
    if result.modified_count == 0:
        ride = rides.find_one({"_id": ride_oid})
        if not ride or ride.get("rider_id") != request.user["_id"]:
            return jsonify({"error": "Ride not found"}), 404
        return jsonify({"error": "Driver can only be verified while the ride is accepted"}), 400

    ride = rides.find_one({"_id": ride_oid})
    payload = serialize(ride)
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    if ride.get("driver_id"):
        socketio.emit("ride_updated", payload, room=f"driver:{ride['driver_id']}")
    return {"ride": payload, "message": "Driver verified"}

@app.post("/api/rides/<ride_id>/start")
@require_role("driver")
def start_ride(ride_id):
    ride_oid = oid(ride_id)
    if not ride_oid:
        return jsonify({"error": "Invalid ride ID"}), 400

    ride = rides.find_one({"_id": ride_oid, "driver_id": request.user["_id"], "status": "accepted"})
    if not ride:
        return jsonify({"error": "Ride not found or not assigned to you"}), 404
    if not ride.get("rider_verified"):
        return jsonify({"error": "The rider has not verified you yet. Ask them to verify you in their dashboard."}), 403

    rides.update_one(
        {"_id": ride_oid},
        {"$set": {"status": "ongoing", "started_at": now()}}
    )
    ride = rides.find_one({"_id": ride_oid})
    payload = serialize(ride)
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    return {"ride": payload}

@app.post("/api/rides/<ride_id>/complete")
@require_role("driver")
def complete_ride(ride_id):
    ride_oid = oid(ride_id)
    if not ride_oid:
        return jsonify({"error": "Invalid ride ID"}), 400

    ride = rides.find_one({"_id": ride_oid, "driver_id": request.user["_id"], "status": "ongoing"})
    if not ride:
        return jsonify({"error": "Ride not found or not assigned to you"}), 404

    driver_profile = drivers.find_one({"user_id": request.user["_id"]}) or {}
    driver_upi_id = (driver_profile.get("upi_id") or "").strip()
    driver_display_name = request.user.get("name", "RaniCab Driver")
    fare_amount = round(float(ride.get("fare", 0) or 0), 2)
    payment_upi_uri = None
    if driver_upi_id:
        payment_upi_uri = (
            "upi://pay?pa=" + quote(driver_upi_id) +
            "&pn=" + quote(driver_display_name) +
            "&am=" + quote(f"{fare_amount:.2f}") +
            "&cu=INR&tn=" + quote(f"RaniCab ride {str(ride_oid)[-6:]}")
        )

    rides.update_one(
        {"_id": ride_oid},
        {"$set": {
            "status": "pending_completion",
            "completed_at": now(),
            "payment_upi_uri": payment_upi_uri,
            "payment_amount": fare_amount,
        }}
    )

    payload = issue_completion_otp(ride, ride_oid)
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    socketio.emit("ride_updated", payload, room="drivers")
    return {"ride": payload, "message": "OTP sent to rider. Waiting for verification."}


RESEND_OTP_COOLDOWN_SECONDS = 30

@app.post("/api/rides/<ride_id>/resend-otp")
@require_role("driver", "rider")
def resend_completion_otp(ride_id):
    ride_oid = oid(ride_id)
    if not ride_oid:
        return jsonify({"error": "Invalid ride ID"}), 400

    query = {"_id": ride_oid, "status": "pending_completion"}
    if request.user.get("role") == "driver":
        query["driver_id"] = request.user["_id"]
    else:
        query["rider_id"] = request.user["_id"]

    ride = rides.find_one(query)
    if not ride:
        return jsonify({"error": "Ride not found or not awaiting OTP"}), 404

    last_sent = ride.get("completion_otp_sent_at")
    if last_sent:
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - last_sent).total_seconds()
        if elapsed < RESEND_OTP_COOLDOWN_SECONDS:
            wait = int(RESEND_OTP_COOLDOWN_SECONDS - elapsed) + 1
            return jsonify({"error": f"Please wait {wait}s before requesting another OTP.", "retry_after": wait}), 429

    payload = issue_completion_otp(ride, ride_oid)
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    if ride.get("driver_id"):
        socketio.emit("ride_updated", payload, room=f"driver:{ride['driver_id']}")
    socketio.emit("ride_updated", payload, room="drivers")
    return {"ride": payload, "message": "A new OTP has been sent to the rider's email."}


@app.post("/api/rides/<ride_id>/complete-verify")
@require_role("rider")
def complete_ride_verify(ride_id):
    data = request.get_json(force=True) or {}
    otp = data.get("otp", "").strip()
    ride_oid = oid(ride_id)
    if not ride_oid or not otp:
        return jsonify({"error": "Invalid ride ID or missing OTP"}), 400

    ride = rides.find_one({"_id": ride_oid, "rider_id": request.user["_id"], "status": "pending_completion"})
    if not ride:
        return jsonify({"error": "Ride not found or not awaiting OTP"}), 404

    expires = ride.get("completion_otp_expires")
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if not expires or expires < datetime.now(timezone.utc):
        return jsonify({"error": "OTP has expired. Please ask driver to regenerate."}), 400

    if not check_password_hash(ride.get("completion_otp_hash", ""), otp):
        return jsonify({"error": "Invalid OTP"}), 400

    rides.update_one(
        {"_id": ride_oid},
        {"$set": {"status": "completed", "finalized_at": now()},
         "$unset": {"completion_otp_hash": "", "completion_otp_expires": "", "completion_otp_sent_at": ""}}
    )

    payload = serialize(rides.find_one({"_id": ride_oid}))
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    if ride.get("driver_id"):
        socketio.emit("ride_updated", payload, room=f"driver:{ride['driver_id']}")
    socketio.emit("ride_updated", payload, room="drivers")
    return {"ride": payload, "message": "Trip completed successfully!"}


@app.post("/api/rides/<ride_id>/complete-verify-driver")
@require_role("driver")
def complete_ride_verify_driver(ride_id):
    data = request.get_json(force=True) or {}
    otp = data.get("otp", "").strip()
    ride_oid = oid(ride_id)
    if not ride_oid or not otp:
        return jsonify({"error": "Invalid ride ID or missing OTP"}), 400

    ride = rides.find_one({"_id": ride_oid, "driver_id": request.user["_id"], "status": "pending_completion"})
    if not ride:
        return jsonify({"error": "Ride not found or not awaiting OTP"}), 404

    expires = ride.get("completion_otp_expires")
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if not expires or expires < datetime.now(timezone.utc):
        return jsonify({"error": "OTP has expired. Please ask the rider to check their email again, or regenerate by re-completing the ride."}), 400

    if not check_password_hash(ride.get("completion_otp_hash", ""), otp):
        return jsonify({"error": "Invalid OTP. Please double-check with the rider."}), 400

    rides.update_one(
        {"_id": ride_oid},
        {"$set": {"status": "completed", "finalized_at": now()},
         "$unset": {"completion_otp_hash": "", "completion_otp_expires": "", "completion_otp_sent_at": ""}}
    )

    payload = serialize(rides.find_one({"_id": ride_oid}))
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    socketio.emit("ride_updated", payload, room=f"driver:{ride['driver_id']}")
    socketio.emit("ride_updated", payload, room="drivers")
    return {"ride": payload, "message": "Trip completed successfully!"}

@app.post("/api/rides/<ride_id>/rate")
@require_role("rider")
def rate_driver(ride_id):
    data = request.get_json(force=True) or {}
    rating = data.get("rating")
    if rating is None or not (1 <= rating <= 5):
        return jsonify({"error": "Rating must be between 1 and 5"}), 400

    ride_oid = oid(ride_id)
    if not ride_oid:
        return jsonify({"error": "Invalid ride ID"}), 400

    ride = rides.find_one({"_id": ride_oid, "rider_id": request.user["_id"], "status": "completed"})
    if not ride:
        return jsonify({"error": "Ride not found or not completed"}), 404

    if ride.get("rider_rating") is not None:
        return jsonify({"error": "You have already rated this ride"}), 400

    rides.update_one({"_id": ride_oid}, {"$set": {"rider_rating": rating}})

    driver_id = ride["driver_id"]
    if driver_id:
        all_rides = list(rides.find({"driver_id": driver_id, "rider_rating": {"$exists": True}}))
        ratings = [r.get("rider_rating") for r in all_rides if r.get("rider_rating") is not None]
        if ratings:
            avg = round(sum(ratings) / len(ratings), 1)
            count = len(ratings)
        else:
            avg = 5.0
            count = 0
        drivers.update_one(
            {"user_id": driver_id},
            {"$set": {"rating": avg, "ratings_count": count}}
        )

    payload = serialize(rides.find_one({"_id": ride_oid}))
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    return {"ride": payload, "message": "Thank you for your rating!"}

@app.post("/api/rides/<ride_id>/rate-rider")
@require_role("driver")
def rate_rider(ride_id):
    data = request.get_json(force=True) or {}
    rating = data.get("rating")
    if rating is None or not (1 <= rating <= 5):
        return jsonify({"error": "Rating must be between 1 and 5"}), 400

    ride_oid = oid(ride_id)
    if not ride_oid:
        return jsonify({"error": "Invalid ride ID"}), 400

    ride = rides.find_one({"_id": ride_oid, "driver_id": request.user["_id"], "status": "completed"})
    if not ride:
        return jsonify({"error": "Ride not found or not completed"}), 404

    if ride.get("driver_rating") is not None:
        return jsonify({"error": "You have already rated this rider"}), 400

    rides.update_one({"_id": ride_oid}, {"$set": {"driver_rating": rating}})

    rider_id = ride["rider_id"]
    if rider_id:
        all_rides = list(rides.find({"rider_id": rider_id, "driver_rating": {"$exists": True}}))
        ratings = [r.get("driver_rating") for r in all_rides if r.get("driver_rating") is not None]
        if ratings:
            avg = round(sum(ratings) / len(ratings), 1)
            count = len(ratings)
        else:
            avg = 5.0
            count = 0
        users.update_one(
            {"_id": rider_id},
            {"$set": {"rating": avg, "ratings_count": count}}
        )

    payload = serialize(rides.find_one({"_id": ride_oid}))
    socketio.emit("ride_updated", payload, room=f"driver:{request.user['_id']}")
    return {"ride": payload, "message": "Thanks for rating your rider!"}

@app.post("/api/rides/<ride_id>/unassign")
@require_role("driver")
def unassign_ride(ride_id):
    ride_oid = oid(ride_id)
    if not ride_oid:
        return jsonify({"error": "Invalid ride ID"}), 400

    ride = rides.find_one({"_id": ride_oid, "driver_id": request.user["_id"], "status": "accepted"})
    if not ride:
        return jsonify({"error": "Ride not found, not assigned to you, or not in accepted state"}), 404

    rides.update_one(
        {"_id": ride_oid},
        {
            "$set": {"status": "requested", "driver_id": None, "accepted_at": None, "rider_verified": False},
            "$unset": {"driver_info": ""}
        }
    )

    updated_ride = rides.find_one({"_id": ride_oid})
    payload = serialize(updated_ride)

    socketio.emit("ride_updated", payload, room="drivers")
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")

    return {"ride": payload}

@app.post("/api/rides/<ride_id>/cancel")
@require_role("rider", "driver")
def cancel_ride(ride_id):
    if request.user["role"] != "rider":
        return jsonify({"error": "Only riders can permanently cancel a ride"}), 403

    ride_oid = oid(ride_id)
    if not ride_oid:
        return jsonify({"error": "Invalid ride ID"}), 400

    query = {"_id": ride_oid, "status": {"$in": ["requested", "accepted"]}, "rider_id": request.user["_id"]}
    result = rides.update_one(query, {"$set": {"status": "cancelled", "cancelled_at": now()}})
    if result.modified_count == 0:
        return jsonify({"error": "Ride cannot be cancelled"}), 400

    ride = rides.find_one({"_id": ride_oid})
    payload = serialize(ride)
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    if ride.get("driver_id"):
        socketio.emit("ride_updated", payload, room=f"driver:{ride['driver_id']}")
    socketio.emit("ride_updated", payload, room="drivers")
    return {"ride": payload}

@app.get("/api/rides/active")
@require_role("rider", "driver")
def active_ride():
    key = "rider_id" if request.user["role"] == "rider" else "driver_id"
    active = rides.find_one(
        {key: request.user["_id"], "status": {"$in": ACTIVE_STATUSES + ["pending_completion"]}},
        sort=[("created_at", -1)]
    )
    if not active:
        return {"ride": None}

    payload = serialize(active)

    if request.user["role"] == "rider" and active.get("driver_id"):
        driver_info = active.get("driver_info")
        if driver_info:
            driver_doc = drivers.find_one({"user_id": active["driver_id"]}) or {}
            driver_info["rating"] = driver_doc.get("rating", 5.0)
            payload["driver"] = driver_info
        else:
            driver_user = users.find_one({"_id": active["driver_id"]}) or {}
            driver_doc = drivers.find_one({"user_id": active["driver_id"]}) or {}
            payload["driver"] = {
                "name": driver_user.get("name", "Driver"),
                "phone": driver_user.get("phone", ""),
                "rating": driver_doc.get("rating", 5.0),
                "vehicle_model": driver_doc.get("vehicle_model", "Sedan"),
                "license_plate": driver_doc.get("license_plate", "TN-01-AB-1234"),
            }
    elif request.user["role"] == "driver" and active.get("rider_id"):
        rider_user = users.find_one({"_id": active["rider_id"]}) or {}
        payload["rider"] = {
            "name": rider_user.get("name", "Rider"),
            "phone": active.get("rider_phone") or rider_user.get("phone", ""),
            "rating": rider_user.get("rating", 5.0),
        }

    return {"ride": payload}

@app.get("/api/rides/history")
@require_role("rider", "driver")
def ride_history():
    key = "rider_id" if request.user["role"] == "rider" else "driver_id"
    history = list(rides.find({key: request.user["_id"]}).sort("created_at", -1).limit(50))
    results = []
    for r in history:
        doc = serialize(r)
        if request.user["role"] == "rider" and r.get("driver_id"):
            if r.get("driver_info"):
                doc["driver_name"] = r["driver_info"].get("name", "Driver")
            else:
                d_user = users.find_one({"_id": r["driver_id"]}) or {}
                doc["driver_name"] = d_user.get("name", "Driver")
        elif request.user["role"] == "driver" and r.get("rider_id"):
            r_user = users.find_one({"_id": r["rider_id"]}) or {}
            doc["rider_name"] = r_user.get("name", "Rider")
        results.append(doc)
    return {"rides": results}


# ============ DRIVER STATUS & PERFORMANCE ============

@app.post("/api/driver/toggle-online")
@require_role("driver")
def toggle_driver_online():
    data = request.get_json(silent=True) or {}
    driver = drivers.find_one({"user_id": request.user["_id"]})
    if not driver:
        return jsonify({"error": "Driver profile not found"}), 404
    
    current_status = driver.get("is_online", False)
    new_status = bool(data.get("is_online", not current_status))
    
    # CHECK: Prevent going offline if driver has an active ride
    if not new_status:  # Trying to go offline
        active_ride = rides.find_one({
            "driver_id": request.user["_id"],
            "status": {"$in": ["accepted", "ongoing", "pending_completion"]}
        })
        if active_ride:
            return jsonify({
                "error": "You cannot go offline while you have an active ride. Complete or unassign the ride first.",
                "active_ride": active_ride["status"]
            }), 403
    
    drivers.update_one(
        {"user_id": request.user["_id"]},
        {"$set": {"is_online": new_status, "updated_at": now()}}
    )
    
    socketio.emit("driver_status_update", {
        "driver_id": str(request.user["_id"]),
        "is_online": new_status
    }, room="admin")
    
    return {"is_online": new_status}

@app.get("/api/driver/performance")
@require_role("driver")
def driver_performance():
    start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    week = start - timedelta(days=start.weekday())
    completed = list(rides.find({"driver_id": request.user["_id"], "status": "completed"}))
    today_rides = []
    by_day = {i: 0 for i in range(7)}
    for r in completed:
        c_at = r.get("completed_at") or r.get("finalized_at")
        if c_at:
            if hasattr(c_at, "tzinfo") and c_at.tzinfo is not None:
                c_at = c_at.replace(tzinfo=None)
            if hasattr(c_at, "weekday"):
                by_day[c_at.weekday()] += 1
            if c_at >= start:
                today_rides.append(r)
    total_earnings = sum(float(r.get("fare", 0)) for r in today_rides)
    return {
        "today": {"completed": len(today_rides), "earnings": total_earnings},
        "week": [{"day": i, "rides": by_day[i]} for i in range(7)]
    }


# ============ SOCKET.IO REAL-TIME DISPATCH ============

@socketio.on("connect")
def socket_connect():
    user = current_user()
    if user:
        role = user.get("role", "rider")
        user_id_str = str(user["_id"])
        join_room(f"{role}:{user_id_str}")
        if role == "driver":
            join_room("drivers")
        emit("connected", {"role": role, "user_id": user_id_str})
    elif session.get("admin"):
        join_room("admin")
        emit("connected", {"role": "admin"})

@socketio.on("driver_location")
def driver_location_socket(data):
    user = current_user()
    if not user or user.get("role") != "driver":
        return
    try:
        lng = float(data["lng"])
        lat = float(data["lat"])
    except (KeyError, ValueError, TypeError):
        return
    
    loc = point(lng, lat)
    is_online = bool(data.get("is_online", True))
    cache_set_driver_location(user["_id"], lng, lat)
    drivers.update_one(
        {"user_id": user["_id"]},
        {"$set": {"is_online": is_online, "current_location": loc, "location_updated_at": now()}}
    )
    active = rides.find_one({"driver_id": user["_id"], "status": {"$in": ["accepted", "ongoing"]}})
    if active:
        socketio.emit(
            "driver_location",
            {"ride_id": str(active["_id"]), "location": loc, "lat": lat, "lng": lng},
            room=f"rider:{active['rider_id']}"
        )

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))