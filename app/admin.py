from flask import Blueprint, current_app, request, jsonify
from pymongo.errors import DuplicateKeyError
import os
import secrets
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash
from .decorators import admin_required
from .models import users, drivers, rides, settings
from .utils import (
    now, oid, serialize, point, resolve_location,
    send_mail
)
from .config import ACTIVE_STATUSES
from .config import ADMIN_USERNAME, IMAGE_TYPES, ALLOWED_IMAGE_EXTENSIONS, UPLOAD_DIR

admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")

# ---------- Overview ----------
@admin_bp.route("/overview", methods=["GET"])
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

# ---------- Settings ----------
@admin_bp.route("/settings", methods=["GET", "POST"])
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

# ---------- Create driver ----------
@admin_bp.route("/drivers", methods=["POST"])
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
              <p style="margin:0 0 20px;color:#54555c;font-size:15px;line-height:1.6;">Hi {(name)}, welcome to RaniCab! Use the credentials below to log in to the Driver app.</p>
              <div style="background:#fbe7c6;padding:18px;border-radius:6px;">
                <p style="margin:0 0 8px;"><b>Username:</b> {(username)}</p>
                <p style="margin:0 0 8px;"><b>Email:</b> {(email)}</p>
                <p style="margin:0;"><b>Password:</b> <span style="font-size:18px;font-weight:bold;letter-spacing:2px;">{(password)}</span></p>
              </div>
              <p style="margin:20px 0 0;color:#54555c;font-size:13px;">Vehicle: {(vehicle_model)} | Plate: {(license_plate)}</p>
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

# ---------- Driver detail ----------
@admin_bp.route("/drivers/<driver_id>", methods=["GET"])
@admin_required
def admin_get_driver_detail(driver_id):
    driver_oid = oid(driver_id)
    driver = drivers.find_one({"_id": driver_oid})
    if not driver:
        return jsonify({"error": "Driver not found"}), 404

    user = users.find_one({"_id": driver["user_id"]}) or {}
    loc = driver.get("current_location", {}).get("coordinates", [0, 0])

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

# ---------- Upload driver image ----------
@admin_bp.route("/drivers/<driver_id>/upload", methods=["POST"])
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

# ---------- Update driver ----------
@admin_bp.route("/drivers/<driver_id>", methods=["PUT"])
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

# ---------- Delete driver ----------
@admin_bp.route("/drivers/<driver_id>", methods=["DELETE"])
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

# ---------- Send custom email to driver ----------
@admin_bp.route("/drivers/<driver_id>/send-mail", methods=["POST"])
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
          <h1 style="margin:0 0 12px;font-size:24px;">{(subject)}</h1>
          <p style="margin:0 0 16px;color:#54555c;font-size:15px;">Hi {(user.get('name', 'Driver'))},</p>
          <div style="background:#f5f3ed;padding:20px;border-left:4px solid #e8a33d;">
            <p style="margin:0;white-space:pre-line;line-height:1.7;color:#333;">{(body)}</p>
          </div>
          <p style="margin:20px 0 0;color:#999;font-size:12px;">— RaniCab Admin Team</p>
        </div>
      </div>
    </body></html>"""

    if not send_mail(user["email"], subject, body, html_body):
        return jsonify({"error": "Failed to send email. Check SMTP settings."}), 503

    return {"message": f"Email sent to {user['email']}"}

# ---------- Re-send credentials ----------
@admin_bp.route("/drivers/<driver_id>/send-credentials", methods=["POST"])
@admin_required
def admin_resend_credentials(driver_id):
    driver_oid = oid(driver_id)
    driver = drivers.find_one({"_id": driver_oid})
    if not driver:
        return jsonify({"error": "Driver not found"}), 404

    user = users.find_one({"_id": driver["user_id"]})
    if not user:
        return jsonify({"error": "Driver user account not found"}), 404

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
          <p style="margin:0 0 20px;color:#54555c;font-size:15px;">Hi {(user.get('name', 'Driver'))}, your password has been reset.</p>
          <div style="background:#fbe7c6;padding:18px;">
            <p style="margin:0 0 8px;"><b>Username:</b> {(user.get('username', ''))}</p>
            <p style="margin:0 0 8px;"><b>Email:</b> {(user.get('email', ''))}</p>
            <p style="margin:0;"><b>New Password:</b> <span style="font-size:18px;font-weight:bold;">{(new_password)}</span></p>
          </div>
          <p style="margin:16px 0 0;color:#999;font-size:12px;">Please change your password after logging in.</p>
        </div>
      </div>
    </body></html>"""

    sent = send_mail(user.get("email", ""), subject, body, html_body)
    if not sent:
        return jsonify({"error": "Failed to send email"}), 503

    return {"message": "New credentials sent to driver's email", "temp_password": new_password}

# ---------- Analytics ----------
@admin_bp.route("/analytics", methods=["GET"])
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

# ---------- All rides ----------
@admin_bp.route("/rides", methods=["GET"])
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