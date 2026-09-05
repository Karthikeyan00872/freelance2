from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta

from .decorators import require_role
from .models import drivers, rides, users
from .utils import now, serialize, ride_is_today
from .extensions import socketio

driver_bp = Blueprint("driver", __name__, url_prefix="/api/driver")

# ---------- Toggle online status ----------
@driver_bp.route("/toggle-online", methods=["POST"])
@require_role("driver")
def toggle_driver_online():
    data = request.get_json(silent=True) or {}
    driver = drivers.find_one({"user_id": request.user["_id"]})
    if not driver:
        return jsonify({"error": "Driver profile not found"}), 404
    
    current_status = driver.get("is_online", False)
    new_status = bool(data.get("is_online", not current_status))
    
    if not new_status:
        active_ride = rides.find_one({
            "driver_id": request.user["_id"],
            "status": {"$in": ["accepted", "ongoing", "pending_completion"]}
        })
        if active_ride and (active_ride.get("status") != "accepted" or ride_is_today(active_ride)):
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

# ---------- Performance ----------
@driver_bp.route("/performance", methods=["GET"])
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