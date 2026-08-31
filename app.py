import logging
from flask import Flask
from config import Config
from services.db import init_db
from blueprints.dashboard import dashboard_bp
from blueprints.links import links_bp
from blueprints.notes import notes_bp
from blueprints.admin import admin_bp
from blueprints.videos import videos_bp

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

from services.backup import start_backup_scheduler

from werkzeug.middleware.proxy_fix import ProxyFix

def create_app():
    app = Flask(__name__, template_folder='templates', static_folder='static')
    app.config.from_object(Config)

    # Trust reverse proxy / Cloudflare Tunnel headers for HTTPS and client IPs
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # Initialize Database and teardown callbacks
    init_db(app)

    # Register Blueprints
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(links_bp)
    app.register_blueprint(notes_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(videos_bp)

    @app.context_processor
    def inject_global_settings():
        from services.db import get_db
        try:
            conn = get_db()
            cursor = conn.execute("SELECT key, value FROM settings")
            site_settings = dict(cursor.fetchall())
        except Exception:
            site_settings = {}
        return {"site_settings": site_settings}

    start_backup_scheduler(app)

    return app

app = create_app()

if __name__ == "__main__":
    logger.info(f"Starting LinkForge on port 5000 (Debug: {Config.DEBUG})...")
    app.run(host="0.0.0.0", port=5000, debug=Config.DEBUG)
