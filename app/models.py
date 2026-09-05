from .extensions import db
from pymongo import GEOSPHERE

users = db.users
drivers = db.drivers
rides = db.rides
settings = db.settings
promotions = db.promotions
password_otps = db.password_otps

def ensure_indexes():
    # Geo indexes
    for collection in (drivers, rides):
        for field in ("current_location", "pickup_location", "dropoff_location"):
            try:
                collection.create_index([(field, GEOSPHERE)])
            except Exception:
                pass
    # Other indexes
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

# Ensure default business settings
if not settings.find_one({"type": "pricing"}):
    from datetime import datetime, timezone
    settings.insert_one({
        "type": "pricing",
        "base_fare": 80,
        "price_per_km": 18,
        "surge_multiplier": 1.0,
        "maintenance_mode": False,
        "updated_at": datetime.now(timezone.utc)
    })