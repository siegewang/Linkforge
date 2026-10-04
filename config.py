import os

class Config:
    DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "dashboard.db"))
    DEBUG = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")
    SECRET_KEY = os.environ.get("SECRET_KEY", "dashforge-dev-secret-key")
    BACKUP_DIR = os.environ.get("BACKUP_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "backups"))
    MODELS_DIR = os.environ.get("MODELS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "models"))
    TIMELAPSES_DIR = os.environ.get("TIMELAPSES_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "timelapses"))
    THUMBNAILS_DIR = os.environ.get("THUMBNAILS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "thumbnails"))
    JSON_SORT_KEYS = False
