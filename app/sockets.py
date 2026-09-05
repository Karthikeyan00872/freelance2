from flask_socketio import join_room, emit
from flask import session
from datetime import timedelta, datetime,timezone
from .decorators import current_user
from .models import drivers, rides
from .utils import now, point
from .extensions import socketio, redis_client, _DRIVER_LOCATIONS

def cache_set_driver_location(driver_id, lng, lat):
    payload = {"lng": float(lng), "lat": float(lat), "updated_at": datetime.now(timezone.utc).isoformat()}
    _DRIVER_LOCATIONS[str(driver_id)] = payload
    if redis_client:
        try:
            redis_client.hset(f"driver_location:{driver_id}", mapping=payload)
        except Exception:
            pass

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