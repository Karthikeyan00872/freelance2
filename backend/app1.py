import os
import secrets
import smtplib
from html import escape
from datetime import datetime, timezone, timedelta
from functools import wraps

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

# Tamil Nadu city coordinates mapping
TN_CITIES = {
    "madurai": {"lng": 78.1198, "lat": 9.9252, "name": "Madurai"},
    "chennai": {"lng": 80.2707, "lat": 13.0827, "name": "Chennai"},
    "coimbatore": {"lng": 76.9558, "lat": 11.0168, "name": "Coimbatore"},
    "trichy": {"lng": 78.7047, "lat": 10.7905, "name": "Trichy"},
    "salem": {"lng": 78.1460, "lat": 11.6643, "name": "Salem"},
    "tirupur": {"lng": 77.3411, "lat": 11.1085, "name": "Tirupur"},
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


@app.get("/frontend/<path:filename>")
def frontend_file(filename):
    return send_from_directory(FRONTEND_DIR, filename)


@app.get("/src/<path:filename>")
def frontend_asset(filename):
    return send_from_directory(ASSETS_DIR, filename)


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
    new_password = data.get("password", "")
    expires_at = otp_doc.get("expires_at") if otp_doc else None
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if not otp_doc or not expires_at or expires_at < now():
        return jsonify({"error": "OTP expired or invalid"}), 400
    if otp_doc.get("attempts", 0) >= 5 or not check_password_hash(otp_doc.get("otp_hash", ""), data.get("otp", "")):
        password_otps.update_one({"_id": otp_doc["_id"]}, {"$inc": {"attempts": 1}})
        return jsonify({"error": "OTP expired or invalid"}), 400
    if len(new_password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    users.update_one({"email": email, "provider": {"$ne": "google"}}, {"$set": {"password_hash": generate_password_hash(new_password), "updated_at": now()}})
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
    data = request.get_json(force=True) or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Driver name is required"}), 400
    username = f"driver{secrets.randbelow(900000) + 100000}"
    password = secrets.token_urlsafe(8)
    email = data.get("email", "").strip().lower() or f"{username}@rani-cab.local"
    phone = data.get("phone", "").strip()
    vehicle_model = data.get("vehicle_model", "Sedan").strip()
    license_plate = data.get("license_plate", f"TN-{secrets.randbelow(90)+10}-AB-{secrets.randbelow(9000)+1000}").strip()
    
    loc = resolve_location(data.get("city", "madurai"), data.get("lng"), data.get("lat"))
    
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
        return jsonify({"error": "A driver or user with this email, phone, or username already exists"}), 409
    driver = {
        "user_id": result.inserted_id,
        "vehicle_model": vehicle_model,
        "license_plate": license_plate,
        "is_online": False,
        "current_location": loc,
        "created_at": now()
    }
    drivers.insert_one(driver)
    return jsonify({
        "driver": serialize(driver),
        "credentials": {"username": username, "email": email, "password": password, "name": name}
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


# ============ RIDE & DISPATCH ENDPOINTS ============

@app.post("/api/rides/request")
@require_role("rider")
def request_ride():
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
    
    # Find online drivers
    nearby_drivers = list(drivers.find({"is_online": True}))
    
    # Broadcast to all online drivers and the dedicated drivers channel
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

    # Build driver info from the logged-in driver user and driver profile
    driver_info = {
        "name": request.user.get("name", "Driver"),
        "phone": request.user.get("phone", ""),
        "rating": request.user.get("rating", 5.0),
        "vehicle_model": driver.get("vehicle_model", "Sedan"),
        "license_plate": driver.get("license_plate", "TN-01-AB-1234"),
    }

    # Update ride: set driver_id, status, and store driver_info directly in the ride document
    result = rides.update_one(
        {"_id": ride_oid, "status": "requested"},
        {
            "$set": {
                "driver_id": request.user["_id"],
                "status": "accepted",
                "accepted_at": now(),
                "driver_info": driver_info,      # store here for good
            },
            "$unset": {"rider_verified": "", "completion_pending": "", "completion_otp": ""},
        }
    )
    if result.modified_count == 0:
        return jsonify({"error": "Ride already accepted or no longer available"}), 409

    ride = rides.find_one({"_id": ride_oid})
    payload = serialize(ride)

    # Add driver to payload (already stored in ride, but we also add explicitly)
    payload["driver"] = driver_info

    # Emit to the rider and to all drivers
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    socketio.emit("ride_updated", payload, room="drivers")

    # Optional debug log
    app.logger.info(f"Ride {ride_id} accepted by driver {request.user['_id']}, driver_info: {driver_info}")

    return {"ride": payload, "driver": driver_info}


@app.post("/api/rides/<ride_id>/verify")
@require_role("rider")
def verify_driver(ride_id):
    """Rider confirms the assigned driver before the trip may start."""
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
    if not ride:
        return jsonify({"error": "Ride not found"}), 404

    payload = serialize(ride)
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    return {"ride": payload}


@app.post("/api/rides/<ride_id>/complete")
@require_role("driver")
def complete_ride(ride_id):
    ride_oid = oid(ride_id)
    if not ride_oid:
        return jsonify({"error": "Invalid ride ID"}), 400
    
    data = request.get_json(silent=True) or {}
    existing_ride = rides.find_one({"_id": ride_oid, "driver_id": request.user["_id"]})
    if not existing_ride:
        return jsonify({"error": "Ride not found or not assigned to you"}), 404
    
    fare = float(data.get("fare") or existing_ride.get("fare") or 0)
    rides.update_one(
        {"_id": ride_oid, "driver_id": request.user["_id"]},
        {"$set": {"status": "completed", "fare": fare, "completed_at": now()}}
    )
    ride = rides.find_one({"_id": ride_oid})
    payload = serialize(ride)
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    return {"ride": payload}


@app.post("/api/rides/<ride_id>/unassign")
@require_role("driver")
def unassign_ride(ride_id):
    ride_oid = oid(ride_id)
    if not ride_oid:
        return jsonify({"error": "Invalid ride ID"}), 400

    ride = rides.find_one({"_id": ride_oid, "driver_id": request.user["_id"], "status": "accepted"})
    if not ride:
        return jsonify({"error": "Ride not found, not assigned to you, or not in accepted state"}), 404

    # Reset status and remove driver_id and driver_info
    rides.update_one(
        {"_id": ride_oid},
        {
            "$set": {"status": "requested", "driver_id": None, "accepted_at": None},
            "$unset": {"driver_info": ""}   # remove stored driver info
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
    # Only riders can permanently cancel a ride
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
    # Notify all parties
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    if ride.get("driver_id"):
        socketio.emit("ride_updated", payload, room=f"driver:{ride['driver_id']}")
    socketio.emit("ride_updated", payload, room="drivers")
    return {"ride": payload}


@app.get("/api/rides/active")
@require_role("rider", "driver")
def active_ride():
    key = "rider_id" if request.user["role"] == "rider" else "driver_id"
    active = rides.find_one({key: request.user["_id"], "status": {"$in": ACTIVE_STATUSES}}, sort=[("created_at", -1)])
    if not active:
        return {"ride": None}

    payload = serialize(active)

    if request.user["role"] == "rider" and active.get("driver_id"):
        # First, try to use stored driver_info (if present)
        driver_info = active.get("driver_info")
        if driver_info:
            payload["driver"] = driver_info
        else:
            # Fallback: look up driver user and profile (backward compatibility)
            driver_user = users.find_one({"_id": active["driver_id"]}) or {}
            driver_doc = drivers.find_one({"user_id": active["driver_id"]}) or {}
            payload["driver"] = {
                "name": driver_user.get("name", "Driver"),
                "phone": driver_user.get("phone", ""),
                "rating": driver_user.get("rating", 5.0),
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
            # Try to get driver name from stored driver_info or fallback to user lookup
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
    drivers.update_one({"user_id": request.user["_id"]}, {"$set": {"is_online": new_status, "updated_at": now()}})
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
        c_at = r.get("completed_at")
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


# ============ STATIC ASSETS & FRONTEND PAGES ============

@app.route("/")
def serve_root():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/frontend/<path:filename>")
def serve_frontend_page(filename):
    return send_from_directory(FRONTEND_DIR, filename)


@app.route("/src/<path:filename>")
def serve_src_assets(filename):
    return send_from_directory(ASSETS_DIR, filename)


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))