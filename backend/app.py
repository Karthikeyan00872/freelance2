import os
import secrets
import smtplib
from datetime import datetime, timezone, timedelta
from functools import wraps

from dotenv import load_dotenv
from email.message import EmailMessage

from flask import Flask, jsonify, request, session
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient, GEOSPHERE
from redis import Redis
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", secrets.token_hex(32))
CORS(app, supports_credentials=True)
socketio = SocketIO(app, cors_allowed_origins=os.getenv("CORS_ORIGINS", "*"), async_mode="eventlet")

mongo = MongoClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017/rani_cab"))
db = mongo.get_default_database()
redis_client = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)

users = db.users
drivers = db.drivers
rides = db.rides
settings = db.settings
promotions = db.promotions
password_otps = db.password_otps

for collection in (drivers, rides):
    for field in ("current_location", "pickup_location", "dropoff_location"):
        try:
            collection.create_index([(field, GEOSPHERE)])
        except Exception:
            pass
users.create_index("email", unique=True)
users.create_index("username", sparse=True, unique=True)
users.create_index("role")
drivers.create_index("user_id")
drivers.create_index("is_online")
rides.create_index([("rider_id", 1), ("status", 1)])
rides.create_index([("driver_id", 1), ("status", 1)])
password_otps.create_index("email")
password_otps.create_index("expires_at", expireAfterSeconds=0)

ACTIVE_STATUSES = ["requested", "accepted", "ongoing"]


def now():
    return datetime.now(timezone.utc)


def oid(value):
    from bson import ObjectId
    return ObjectId(value) if value else None


def serialize(doc):
    if not doc:
        return None
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
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



def send_mail(to_email, subject, body):
    """Send transactional email with sender name RaniCab.

    In development, if SMTP credentials are not configured, the message is logged
    and the API still succeeds so local auth flows remain testable.
    """
    sender_email = os.getenv("SENDER_EMAIL") or os.getenv("sender_email")
    sender_password = os.getenv("SENDER_APP_PASSWORD") or os.getenv("sender_app_password")
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    if not sender_email or not sender_password or sender_email.startswith("replace-with"):
        app.logger.warning("Email not sent; SMTP credentials are not configured. To=%s Subject=%s Body=%s", to_email, subject, body)
        return False
    msg = EmailMessage()
    msg["From"] = f"RaniCab <{sender_email}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
        smtp.starttls()
        smtp.login(sender_email, sender_password)
        smtp.send_message(msg)
    return True


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
    return users.find_one({"_id": oid(user_id)}) if user_id else None


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


@app.post("/api/auth/register")
def register_rider():
    data = request.get_json(force=True)
    if data.get("role", "rider") != "rider":
        return jsonify({"error": "Drivers must be registered by admin"}), 403
    email = data["email"].lower()
    if users.find_one({"email": email}):
        return jsonify({"error": "An account with this email already exists"}), 409
    user = {
        "name": data["name"], "username": data.get("username", data["name"]), "email": email, "phone": data.get("phone", ""),
        "password_hash": generate_password_hash(data["password"]), "role": "rider", "rating": 5.0,
        "provider": "manual", "created_at": now(),
    }
    result = users.insert_one(user)
    user["_id"] = result.inserted_id
    session["user_id"] = str(result.inserted_id)
    return jsonify({"user": serialize(user)}), 201


@app.post("/api/auth/login")
def login():
    data = request.get_json(force=True)
    login_id = data.get("email") or data.get("username", "")
    user = users.find_one({"$or": [{"email": login_id.lower()}, {"username": login_id}]})
    if not user or not check_password_hash(user.get("password_hash", ""), data.get("password", "")):
        return jsonify({"error": "Invalid credentials"}), 401
    session["user_id"] = str(user["_id"])
    return {"user": serialize(user)}


@app.post("/api/auth/google")
def google_auth():
    data = request.get_json(force=True)
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
    data = request.get_json(force=True)
    email = data.get("email", "").lower().strip()
    user = users.find_one({"email": email, "provider": {"$ne": "google"}, "password_hash": {"$exists": True}})
    if not user:
        return jsonify({"error": "Forgot password works only for manually signed-up accounts"}), 404
    otp = f"{secrets.randbelow(1000000):06d}"
    password_otps.delete_many({"email": email})
    password_otps.insert_one({"email": email, "otp_hash": generate_password_hash(otp), "expires_at": now() + timedelta(minutes=10), "attempts": 0, "created_at": now()})
    send_mail(email, "Your RaniCab password reset OTP", f"Hi {user.get('name', 'RaniCab rider')},\n\nYour RaniCab password reset OTP is {otp}. It expires in 10 minutes.\n\nIf you did not request this, please ignore this email.\n\nRaniCab")
    return {"message": "OTP sent to your registered email"}


@app.post("/api/auth/forgot-password/verify")
def forgot_password_verify():
    data = request.get_json(force=True)
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
    return {"message": "Password updated"}


@app.get("/api/config")
def public_config():
    return {"google_client_id": os.getenv("GOOGLE_CLIENT_ID", "")}


@app.post("/api/admin/login")
def admin_login():
    data = request.get_json(force=True)
    if data.get("username") == os.getenv("ADMIN_USERNAME") and data.get("password") == os.getenv("ADMIN_PASSWORD"):
        session["admin"] = True
        return {"admin": True}
    return jsonify({"error": "Invalid admin credentials"}), 401


@app.post("/api/admin/drivers")
@admin_required
def create_driver():
    data = request.get_json(force=True)
    username = f"driver{secrets.randbelow(900000) + 100000}"
    password = secrets.token_urlsafe(9)
    user = {"name": data["name"], "username": username, "email": data.get("email", f"{username}@rani-cab.local"), "phone": data.get("phone", ""), "password_hash": generate_password_hash(password), "role": "driver", "rating": 5.0, "created_at": now()}
    result = users.insert_one(user)
    driver = {"user_id": result.inserted_id, "vehicle_model": data["vehicle_model"], "license_plate": data["license_plate"], "is_online": False, "current_location": point(data.get("lng", 78.6569), data.get("lat", 11.1271)), "created_at": now()}
    drivers.insert_one(driver)
    return jsonify({"driver": serialize(driver), "credentials": {"username": username, "email": user["email"], "password": password}}), 201


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
        roster.append({**serialize(driver), "name": user.get("name"), "phone": user.get("phone"), "rating": user.get("rating", 5.0)})
    return {"revenue": {"today": revenue_since(today), "month": revenue_since(month), "all_time": revenue_since()}, "drivers": roster}


@app.post("/api/rides/request")
@require_role("rider")
def request_ride():
    data = request.get_json(force=True)
    pickup = point(data["pickup_lng"], data["pickup_lat"])
    nearby = list(drivers.find({"is_online": True, "current_location": {"$near": {"$geometry": pickup, "$maxDistance": int(data.get("radius_m", 3000))}}}).limit(10))
    ride = {"rider_id": request.user["_id"], "driver_id": None, "pickup_location": pickup, "dropoff_location": point(data["dropoff_lng"], data["dropoff_lat"]), "pickup_address": data.get("pickup_address", "Pickup"), "dropoff_address": data.get("dropoff_address", "Destination"), "status": "requested", "fare": float(data.get("estimated_fare", 0)), "created_at": now()}
    result = rides.insert_one(ride)
    ride["_id"] = result.inserted_id
    payload = serialize(ride)
    for driver in nearby:
        socketio.emit("ride_request", payload, room=f"driver:{driver['user_id']}")
    return {"ride": payload, "nearby_drivers": len(nearby)}


@app.post("/api/rides/<ride_id>/accept")
@require_role("driver")
def accept_ride(ride_id):
    driver = drivers.find_one({"user_id": request.user["_id"]})
    rides.update_one({"_id": oid(ride_id), "status": "requested"}, {"$set": {"driver_id": request.user["_id"], "status": "accepted", "accepted_at": now()}})
    ride = rides.find_one({"_id": oid(ride_id)})
    payload = serialize(ride)
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    return {"ride": payload, "driver": serialize(driver)}


@app.post("/api/rides/<ride_id>/complete")
@require_role("driver")
def complete_ride(ride_id):
    data = request.get_json(silent=True) or {}
    rides.update_one({"_id": oid(ride_id), "driver_id": request.user["_id"]}, {"$set": {"status": "completed", "fare": float(data.get("fare", 0)), "completed_at": now()}})
    ride = rides.find_one({"_id": oid(ride_id)})
    socketio.emit("ride_updated", serialize(ride), room=f"rider:{ride['rider_id']}")
    return {"ride": serialize(ride)}


@app.get("/api/rides/active")
@require_role("rider", "driver")
def active_ride():
    key = "rider_id" if request.user["role"] == "rider" else "driver_id"
    return {"ride": serialize(rides.find_one({key: request.user["_id"], "status": {"$in": ACTIVE_STATUSES}}, sort=[("created_at", -1)]))}


@app.get("/api/rides/history")
@require_role("rider", "driver")
def ride_history():
    key = "rider_id" if request.user["role"] == "rider" else "driver_id"
    return {"rides": [serialize(r) for r in rides.find({key: request.user["_id"]}).sort("created_at", -1).limit(50)]}


@app.get("/api/driver/performance")
@require_role("driver")
def driver_performance():
    start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    week = start - timedelta(days=start.weekday())
    completed = list(rides.find({"driver_id": request.user["_id"], "status": "completed", "completed_at": {"$gte": week}}))
    today = [r for r in completed if r.get("completed_at", week) >= start]
    by_day = {i: 0 for i in range(7)}
    for ride in completed:
        by_day[ride["completed_at"].weekday()] += 1
    return {"today": {"completed": len(today), "earnings": sum(r.get("fare", 0) for r in today)}, "week": [{"day": i, "rides": by_day[i]} for i in range(7)]}


@socketio.on("connect")
def socket_connect():
    user = current_user()
    if user:
        join_room(f"{user['role']}:{user['_id']}")
        emit("connected", {"role": user["role"], "user_id": str(user["_id"])})


@socketio.on("driver_location")
def driver_location(data):
    user = current_user()
    if not user or user.get("role") != "driver":
        return
    location = point(data["lng"], data["lat"])
    redis_client.hset(f"driver_location:{user['_id']}", mapping={"lng": location["coordinates"][0], "lat": location["coordinates"][1], "updated_at": now().isoformat()})
    drivers.update_one({"user_id": user["_id"]}, {"$set": {"is_online": bool(data.get("is_online", True)), "current_location": location, "location_updated_at": now()}})
    active = rides.find_one({"driver_id": user["_id"], "status": {"$in": ["accepted", "ongoing"]}})
    if active:
        socketio.emit("driver_location", {"ride_id": str(active["_id"]), "location": location}, room=f"rider:{active['rider_id']}")


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
