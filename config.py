import os

class Config:
    DB_PATH = os.environ.get("DB_PATH", "data/dashboard.db")
    DEBUG = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")
    SECRET_KEY = os.environ.get("SECRET_KEY", "dashforge-dev-secret-key")
    BACKUP_DIR = os.environ.get("BACKUP_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "backups"))
    JSON_SORT_KEYS = False
