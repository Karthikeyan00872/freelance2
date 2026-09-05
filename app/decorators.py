from functools import wraps
from flask import session, jsonify ,request
from .models import users
from .utils import oid

def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return users.find_one({"_id": oid(user_id)})

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