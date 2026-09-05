from flask import Blueprint, current_app, request,app, jsonify
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, quote_plus
import secrets

from .decorators import require_role
from .models import users, drivers, rides
from .utils import (
    now, oid, serialize, point, resolve_location,
    ride_is_today, ride_is_future_booking,
    send_completion_otp_email
)
from .config import ACTIVE_STATUSES, RESEND_OTP_COOLDOWN_SECONDS
from .extensions import socketio
from werkzeug.security import generate_password_hash, check_password_hash

rides_bp = Blueprint("rides", __name__, url_prefix="/api/rides")

# ---------- Request ride ----------
@rides_bp.route("/request", methods=["POST"])
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
    
    fare = float(data.get("estimated_fare") or data.get("fare") or 0)
    trip_type = data.get("trip_type", "oneway")

    if trip_type == "hourly":
        dropoff_addr = ""
        pickup = resolve_location(pickup_addr, data.get("pickup_lng"), data.get("pickup_lat"), default_city="madurai")
        dropoff = pickup
    else:
        pickup = resolve_location(pickup_addr, data.get("pickup_lng"), data.get("pickup_lat"), default_city="madurai")
        dropoff = resolve_location(dropoff_addr, data.get("dropoff_lng"), data.get("dropoff_lat"), default_city="chennai")
    
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

# ---------- Available rides (for drivers) ----------
@rides_bp.route("/available", methods=["GET"])
@require_role("driver")
def available_rides():
    open_rides = list(rides.find({"status": "requested"}).sort("created_at", -1).limit(10))
    return {"rides": [serialize(r) for r in open_rides]}

# ---------- Accept ride ----------
@rides_bp.route("/<ride_id>/accept", methods=["POST"])
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

    requested_ride = rides.find_one({"_id": ride_oid, "status": "requested"})
    if not requested_ride:
        return jsonify({"error": "Ride already accepted or no longer available"}), 409

    booked_for_future = ride_is_future_booking(requested_ride)
    initial_status = "accepted"

    result = rides.update_one(
        {"_id": ride_oid, "status": "requested"},
        {
            "$set": {
                "driver_id": request.user["_id"],
                "status": initial_status,
                "booking_state": "booked" if booked_for_future else "accepted",
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

# ---------- Verify driver (rider) ----------
@rides_bp.route("/<ride_id>/verify", methods=["POST"])
@require_role("rider")
def verify_driver(ride_id):
    ride_oid = oid(ride_id)
    if not ride_oid:
        return jsonify({"error": "Invalid ride ID"}), 400

    ride_for_verify = rides.find_one({"_id": ride_oid, "rider_id": request.user["_id"], "status": "accepted"})
    if not ride_for_verify:
        return jsonify({"error": "Ride not found or not assigned to you"}), 404
    if not ride_is_today(ride_for_verify):
        return jsonify({"error": "This ride is booked for " + str(ride_for_verify.get("scheduled_date") or "a future date") + ". Driver verification is available on the ride date."}), 400

    result = rides.update_one(
        {"_id": ride_oid, "rider_id": request.user["_id"], "status": "accepted"},
        {"$set": {"rider_verified": True, "verified_at": now(), "booking_state": "accepted"}}
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

# ---------- Start ride ----------
@rides_bp.route("/<ride_id>/start", methods=["POST"])
@require_role("driver")
def start_ride(ride_id):
    ride_oid = oid(ride_id)
    if not ride_oid:
        return jsonify({"error": "Invalid ride ID"}), 400

    ride = rides.find_one({"_id": ride_oid, "driver_id": request.user["_id"], "status": "accepted"})
    if not ride:
        return jsonify({"error": "Ride not found or not assigned to you"}), 404
    if not ride_is_today(ride):
        return jsonify({"error": "This ride is booked for " + str(ride.get("scheduled_date") or "a future date") + ". You can start it on the scheduled date."}), 400
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

# ---------- Complete ride (driver) ----------
@rides_bp.route("/<ride_id>/complete", methods=["POST"])
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
            "payment_status": "pending",
        }}
    )

    payload = issue_completion_otp(ride, ride_oid)
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    socketio.emit("ride_updated", payload, room="drivers")
    return {"ride": payload, "message": "OTP sent to rider. Waiting for verification."}

# ---------- Helper: issue OTP ----------
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

# ---------- Resend OTP ----------
@rides_bp.route("/<ride_id>/resend-otp", methods=["POST"])
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

# ---------- Complete verification (rider) ----------
@rides_bp.route("/<ride_id>/complete-verify", methods=["POST"])
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
        {"$set": {"status": "completed", "finalized_at": now(), "payment_status": "paid"},
         "$unset": {"completion_otp_hash": "", "completion_otp_expires": "", "completion_otp_sent_at": ""}}
    )

    payload = serialize(rides.find_one({"_id": ride_oid}))
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    if ride.get("driver_id"):
        socketio.emit("ride_updated", payload, room=f"driver:{ride['driver_id']}")
    socketio.emit("ride_updated", payload, room="drivers")
    return {"ride": payload, "message": "Trip completed successfully!"}

# ---------- Complete verification (driver) ----------
@rides_bp.route("/<ride_id>/complete-verify-driver", methods=["POST"])
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
        {"$set": {"status": "completed", "finalized_at": now(), "payment_status": "paid"},
         "$unset": {"completion_otp_hash": "", "completion_otp_expires": "", "completion_otp_sent_at": ""}}
    )

    payload = serialize(rides.find_one({"_id": ride_oid}))
    socketio.emit("ride_updated", payload, room=f"rider:{ride['rider_id']}")
    socketio.emit("ride_updated", payload, room=f"driver:{ride['driver_id']}")
    socketio.emit("ride_updated", payload, room="drivers")
    return {"ride": payload, "message": "Trip completed successfully!"}

# ---------- Rate driver (rider) ----------
@rides_bp.route("/<ride_id>/rate", methods=["POST"])
@require_role("rider")
def rate_driver(ride_id):
    data = request.get_json(force=True) or {}
    rating = data.get("rating")
    if rating is None or not (1 <= rating <= 5):
        return jsonify({"error": "Rating must be between 1 and 5"}), 400

    ride_oid = oid(ride_id)
    if not ride_oid:
        return jsonify({"error": "Invalid ride ID"}), 400

    ride = rides.find_one({
        "_id": ride_oid,
        "rider_id": request.user["_id"],
        "status": {"$in": ["pending_completion", "completed"]}
    })
    if not ride:
        return jsonify({"error": "Ride not found or not ready for rating"}), 404

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

# ---------- Rate rider (driver) ----------
@rides_bp.route("/<ride_id>/rate-rider", methods=["POST"])
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

# ---------- Unassign ride ----------
@rides_bp.route("/<ride_id>/unassign", methods=["POST"])
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

# ---------- Cancel ride ----------
@rides_bp.route("/<ride_id>/cancel", methods=["POST"])
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

# ---------- Active ride ----------
@rides_bp.route("/active", methods=["GET"])
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

# ---------- Ride history ----------
@rides_bp.route("/history", methods=["GET"])
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