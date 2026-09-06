import os
import re
from flask import Flask, send_from_directory, request, redirect
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import (
    SECRET_KEY, IS_PROD, FRONTEND_DIR, ASSETS_DIR, UPLOAD_DIR,
    CORS_ORIGIN_PATTERNS, PROJECT_DIR
)
from .extensions import socketio, compress, cors, db
from .models import ensure_indexes
from .auth import auth_bp
from .admin import admin_bp
from .rides import rides_bp
from .driver import driver_bp
from . import sockets  # registers socket events

def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = SECRET_KEY
    app.config["SESSION_COOKIE_SAMESITE"] = "None" if IS_PROD else "Lax"
    app.config["SESSION_COOKIE_SECURE"] = IS_PROD
    app.config["SESSION_COOKIE_HTTPONLY"] = True

    # Proxy fix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

    # Initialize extensions
    compress.init_app(app)
    cors.init_app(app, supports_credentials=True, origins=CORS_ORIGIN_PATTERNS)
    socketio.init_app(app, cors_allowed_origins="*", async_mode=os.getenv("SOCKETIO_ASYNC_MODE", "threading"))

    # Ensure DB indexes and default settings
    ensure_indexes()

    # Register blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(rides_bp)
    app.register_blueprint(driver_bp)

    # Frontend routes
    @app.after_request
    def add_cache_headers(response):
        # Prevent stale caching during development
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
   
    @app.get("/")
    def frontend_index():
        return send_from_directory(FRONTEND_DIR, "index.html")
    
    @app.get("/<string:page_name>")
    def dynamic_frontend_pages(page_name):
        html_filename = f"{page_name}.html"
        file_path = os.path.join(FRONTEND_DIR, html_filename)        
        
        if os.path.exists(file_path):
            return send_from_directory(FRONTEND_DIR, html_filename)       
        return send_from_directory(FRONTEND_DIR, "index.html")  
         
    @app.get("/frontend/<path:filename>")
    def frontend_file(filename):       
        if filename.endswith(".html"):
            clean_route = "/" + filename.replace(".html", "")
            if filename == "index.html":
                return redirect("/")
            return redirect(clean_route)            
        return send_from_directory(FRONTEND_DIR, filename)

    
    @app.get("/src/<path:filename>")
    def frontend_asset(filename):
        frontend_src = os.path.join(PROJECT_DIR, "src")
        print(f"Serving asset: {filename} from {frontend_src}")
        if os.path.exists(os.path.join(frontend_src, filename)):
            return send_from_directory(frontend_src, filename)
        return send_from_directory(ASSETS_DIR, filename)
   
    @app.get("/uploads/<path:filename>")
    def serve_upload(filename):
        return send_from_directory(UPLOAD_DIR, filename)

    # Health check
    @app.get("/api/health")
    def health():
        return {"status": "ok", "service": "rani-cab"}

    return app
