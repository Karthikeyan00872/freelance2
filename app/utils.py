import re
import secrets
import json
import smtplib
from html import escape
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from email.message import EmailMessage
from bson import ObjectId

from flask import current_app, request ,app
from .config import SENDER_EMAIL, SENDER_PASSWORD, SMTP_HOST, SMTP_PORT

# ---------- Tamil Nadu City Database ----------
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
    # Airports
    "madurai airport": {"lng": 78.0934, "lat": 9.8345, "name": "Madurai Airport (IXM)"},
    "madurai international airport": {"lng": 78.0934, "lat": 9.8345, "name": "Madurai Airport (IXM)"},
    "ixm": {"lng": 78.0934, "lat": 9.8345, "name": "Madurai Airport (IXM)"},
    "chennai airport": {"lng": 80.1709, "lat": 12.9941, "name": "Chennai Airport (MAA)"},
    "chennai international airport": {"lng": 80.1709, "lat": 12.9941, "name": "Chennai Airport (MAA)"},
    "maa": {"lng": 80.1709, "lat": 12.9941, "name": "Chennai Airport (MAA)"},
    "coimbatore airport": {"lng": 77.0434, "lat": 11.0298, "name": "Coimbatore Airport (CJB)"},
    "coimbatore international airport": {"lng": 77.0434, "lat": 11.0298, "name": "Coimbatore Airport (CJB)"},
    "cjb": {"lng": 77.0434, "lat": 11.0298, "name": "Coimbatore Airport (CJB)"},
    "trichy airport": {"lng": 78.7097, "lat": 10.7654, "name": "Trichy Airport (TRZ)"},
    "trichy international airport": {"lng": 78.7097, "lat": 10.7654, "name": "Trichy Airport (TRZ)"},
    "tiruchirappalli airport": {"lng": 78.7097, "lat": 10.7654, "name": "Trichy Airport (TRZ)"},
    "trz": {"lng": 78.7097, "lat": 10.7654, "name": "Trichy Airport (TRZ)"},
    "tuticorin airport": {"lng": 78.1417, "lat": 8.7291, "name": "Tuticorin Airport (TCR)"},
    "tcr": {"lng": 78.1417, "lat": 8.7291, "name": "Tuticorin Airport (TCR)"},
    "salem airport": {"lng": 78.0875, "lat": 11.7833, "name": "Salem Airport (SXV)"},
    "sxv": {"lng": 78.0875, "lat": 11.7833, "name": "Salem Airport (SXV)"},
    "vellore airport": {"lng": 79.125, "lat": 12.885, "name": "Vellore Airport (VLR)"},
    "vlr": {"lng": 79.125, "lat": 12.885, "name": "Vellore Airport (VLR)"},
}

# ---------- Time helpers ----------
INDIA_TZ = timezone(timedelta(hours=5, minutes=30))

def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)

def india_today_str():
    return datetime.now(INDIA_TZ).date().isoformat()

def ride_is_today(ride):
    scheduled = str(ride.get("scheduled_date") or "")
    return not scheduled or scheduled == india_today_str()

def ride_is_future_booking(ride):
    scheduled = str(ride.get("scheduled_date") or "")
    return bool(scheduled) and scheduled > india_today_str()

# ---------- ObjectId helper ----------
def oid(value):
    try:
        return ObjectId(value) if value else None
    except Exception:
        return None

# ---------- Serialization ----------
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

# ---------- Geo point ----------
def point(lng, lat):
    return {"type": "Point", "coordinates": [float(lng), float(lat)]}

# ---------- Resolve location ----------
def resolve_location(address, lng=None, lat=None, default_city="madurai"):
    if lng is not None and lat is not None:
        try:
            return point(float(lng), float(lat))
        except (ValueError, TypeError):
            pass

    if address:
        addr_lower = str(address).strip().lower()
        for key in sorted(TN_CITIES, key=len, reverse=True):
            city = TN_CITIES[key]
            if key in addr_lower:
                return point(city["lng"], city["lat"])

        if "airport" in addr_lower:
            for city_key, city_data in TN_CITIES.items():
                if "airport" in city_key:
                    continue
                if city_key in addr_lower:
                    airport_key = f"{city_key} airport"
                    if airport_key in TN_CITIES:
                        return point(TN_CITIES[airport_key]["lng"], TN_CITIES[airport_key]["lat"])
                    airport_key_intl = f"{city_key} international airport"
                    if airport_key_intl in TN_CITIES:
                        return point(TN_CITIES[airport_key_intl]["lng"], TN_CITIES[airport_key_intl]["lat"])
                    return point(city_data["lng"], city_data["lat"])

    def_city = TN_CITIES.get(default_city, TN_CITIES["madurai"])
    return point(def_city["lng"], def_city["lat"])

# ---------- Email ----------
def send_mail(to_email, subject, body, html_body=None):
    if not SENDER_EMAIL or not SENDER_PASSWORD or SENDER_EMAIL.startswith("replace-with"):
        app.logger.warning("Email not sent; SMTP credentials are not configured. To=%s Subject=%s Body=%s", to_email, subject, body)
        return False
    try:
        msg = EmailMessage()
        msg["From"] = f"RaniCab <{SENDER_EMAIL}>"
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(SENDER_EMAIL, SENDER_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as exc:
        app.logger.warning("SMTP failed: %s", exc)
        return False

# ---------- OTP emails ----------
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

# ---------- Google user creation ----------
def create_or_update_google_user(profile, phone=""):
    from .models import users
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

# ---------- Google redirect URI ----------
def google_redirect_uri():
    from flask import request
    from .config import GOOGLE_REDIRECT_URI
    configured = GOOGLE_REDIRECT_URI
    if configured:
        uris = [u.strip() for u in configured.split(",") if u.strip()]
        if len(uris) == 1:
            return uris[0]
        base = request.url_root.rstrip("/")
        for uri in uris:
            if uri.startswith(base):
                return uri
        return uris[0] if uris else request.url_root.rstrip("/") + "/api/auth/google/callback"
    return request.url_root.rstrip("/") + "/api/auth/google/callback"