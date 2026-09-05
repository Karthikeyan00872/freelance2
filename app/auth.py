from flask import Blueprint, current_app, request, jsonify, session, redirect , app
from pymongo.errors import DuplicateKeyError
from werkzeug.security import generate_password_hash, check_password_hash
from urllib.parse import quote, urlencode
from urllib.request import Request as UrlRequest, urlopen
from datetime import timedelta, datetime
import json
import secrets
import re
import os

from .decorators import current_user, require_role
from .models import users, drivers, password_otps
from .utils import (
    now, oid, serialize, create_or_update_google_user,
    send_mail, send_reset_otp, google_redirect_uri
)
from .config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, ADMIN_USERNAME, ADMIN_PASSWORD

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")

# ---------- Public config ----------
@auth_bp.route("/config", methods=["GET"])
def public_config():
    return {
        "brand": "Rani Cab",
        "google_client_id": GOOGLE_CLIENT_ID,
        "google_oauth_enabled": bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
    }

# ---------- Me ----------
@auth_bp.route("/me", methods=["GET"])
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

# ---------- Logout ----------
@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return {"message": "Logged out"}

# ---------- Register ----------
@auth_bp.route("/register", methods=["POST"])
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

# ---------- Login ----------
@auth_bp.route("/login", methods=["POST"])
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

# ---------- Google OAuth ----------
@auth_bp.route("/google/start", methods=["GET"])
def google_auth_start():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return jsonify({"error": "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET."}), 500

    mode = (request.args.get("mode") or "login").lower()
    if mode not in {"login", "signup"}:
        mode = "login"

    state = secrets.token_urlsafe(32)
    session["google_oauth_state"] = state
    session["google_oauth_mode"] = mode

    params = urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    })
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + params)

@auth_bp.route("/google/callback", methods=["GET"])
def google_auth_callback():
    error = request.args.get("error")
    if error:
        session.pop("google_oauth_state", None)
        session.pop("google_oauth_mode", None)
        return redirect("/?google=error&message=" + quote("Google sign-in was cancelled or denied."))

    state = request.args.get("state", "")
    expected_state = session.pop("google_oauth_state", None)
    mode = session.pop("google_oauth_mode", "login")
    if not expected_state or not secrets.compare_digest(state, expected_state):
        app.logger.warning("Google OAuth state mismatch")
        return redirect("/?google=error&message=" + quote("Google sign-in session expired. Please try again."))

    code = request.args.get("code", "")
    if not code:
        return redirect("/?google=error&message=" + quote("Google did not return an authorization code."))

    redirect_uri = google_redirect_uri()

    try:
        token_payload = urlencode({
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }).encode("utf-8")
        token_request = UrlRequest(
            "https://oauth2.googleapis.com/token",
            data=token_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(token_request, timeout=15) as response:
            token_data = json.loads(response.read().decode("utf-8"))

        id_token_value = token_data.get("id_token")
        if not id_token_value:
            raise ValueError("Google token response did not contain an ID token")

        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
        profile = id_token.verify_oauth2_token(
            id_token_value, google_requests.Request(), GOOGLE_CLIENT_ID
        )

        if not profile.get("email_verified"):
            raise ValueError("Google email is not verified")

        user = create_or_update_google_user(profile)
        session["user_id"] = str(user["_id"])

        if not user.get("phone"):
            session["google_phone_pending"] = True
            return redirect("/?google=phone")

        session.pop("google_phone_pending", None)
        return redirect("/?google=success")

    except Exception as exc:
        app.logger.warning("Google OAuth callback failed: %s", exc)
        return redirect("/?google=error&message=" + quote("Google sign-in failed. Please try again."))

@auth_bp.route("/google/complete-phone", methods=["POST"])
def google_complete_phone():
    if not session.get("google_phone_pending"):
        return jsonify({"error": "No Google phone verification is pending."}), 400

    user = current_user()
    if not user:
        session.pop("google_phone_pending", None)
        return jsonify({"error": "Your Google sign-in session expired. Please sign in again."}), 401

    data = request.get_json(force=True) or {}
    phone = re.sub(r"\D", "", str(data.get("phone", "")))
    if len(phone) != 10:
        return jsonify({"error": "Enter a valid 10-digit phone number."}), 400

    users.update_one({"_id": user["_id"]}, {"$set": {"phone": phone, "updated_at": now()}})
    session.pop("google_phone_pending", None)
    updated_user = users.find_one({"_id": user["_id"]})
    return {"user": serialize(updated_user)}

@auth_bp.route("/google", methods=["POST"])
def google_auth():
    data = request.get_json(force=True) or {}
    token = data.get("credential")
    if not token:
        return jsonify({"error": "Missing Google credential"}), 400

    if not GOOGLE_CLIENT_ID:
        app.logger.error("GOOGLE_CLIENT_ID is not configured in environment variables.")
        return jsonify({"error": "Google Client ID is not configured on the server"}), 500

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
        profile = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
    except Exception as exc:
        app.logger.warning("Google sign-in verification failed: %s", exc)
        return jsonify({"error": f"Google sign-in could not be verified: {str(exc)}"}), 401

    if not profile.get("email_verified"):
        return jsonify({"error": "Google email is not verified"}), 401

    user = create_or_update_google_user(profile, data.get("phone", ""))
    session["user_id"] = str(user["_id"])
    return {"user": serialize(user)}

# ---------- Forgot password ----------
@auth_bp.route("/forgot-password/request", methods=["POST"])
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

@auth_bp.route("/forgot-password/resend", methods=["POST"])
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

@auth_bp.route("/forgot-password/verify", methods=["POST"])
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

# ---------- Admin auth ----------
@auth_bp.route("/admin/me", methods=["GET"])
def admin_me():
    if session.get("admin"):
        return {"admin": True, "username": ADMIN_USERNAME}
    return jsonify({"error": "Admin authentication required"}), 401

@auth_bp.route("/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json(force=True) or {}
    if data.get("username") == ADMIN_USERNAME and data.get("password") == ADMIN_PASSWORD:
        session["admin"] = True
        return {"admin": True, "message": "Admin logged in successfully"}
    return jsonify({"error": "Invalid admin credentials"}), 401

@auth_bp.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin", None)
    return {"message": "Admin logged out"}