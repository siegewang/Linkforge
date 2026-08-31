import sqlite3
import os
import time
import logging
from flask import g, has_app_context
from config import Config

logger = logging.getLogger(__name__)

def _create_connection():
    for attempt in range(5):
        try:
            conn = sqlite3.connect(Config.DB_PATH, timeout=30)
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
            except Exception:
                try:
                    conn.execute("PRAGMA journal_mode=TRUNCATE;")
                except Exception:
                    pass
            try:
                conn.execute("PRAGMA busy_timeout=30000;")
                conn.execute("PRAGMA foreign_keys=ON;")
            except Exception:
                pass
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.OperationalError:
            if attempt < 4:
                time.sleep(0.3 * (attempt + 1))
                continue
            raise

def get_db():
    if has_app_context():
        if 'db' not in g:
            g.db = _create_connection()
        return g.db
    return _create_connection()

def close_db(exception=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

_db_initialized = False

def init_db(app=None):
    global _db_initialized
    if _db_initialized and app is None:
        return

    db_dir = os.path.dirname(Config.DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
        
    conn = _create_connection()
    try:
        existing = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='downloaded_books'").fetchone()
        if existing:
            _db_initialized = True
            conn.close()
            return

        conn.execute("""CREATE TABLE IF NOT EXISTS downloaded_books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE,
            title TEXT NOT NULL,
            author TEXT,
            cover_url TEXT,
            file_path TEXT,
            file_size INTEGER DEFAULT 0,
            file_format TEXT DEFAULT 'epub',
            status TEXT DEFAULT 'completed',
            progress INTEGER DEFAULT 100,
            download_url TEXT,
            date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        conn.execute("""CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            custom_title TEXT,
            icon_url TEXT,
            custom_url TEXT,
            custom_color TEXT,
            hidden INTEGER DEFAULT 0,
            display_order INTEGER DEFAULT 999,
            is_custom INTEGER DEFAULT 0,
            group_name TEXT DEFAULT 'Ungrouped'
        )""")
        
        cursor = conn.execute("PRAGMA table_info(config)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'group_name' not in columns:
            conn.execute("ALTER TABLE config ADD COLUMN group_name TEXT DEFAULT 'Ungrouped'")
        if 'click_count' not in columns:
            conn.execute("ALTER TABLE config ADD COLUMN click_count INTEGER DEFAULT 0")

        conn.execute("""CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE,
            title TEXT,
            description TEXT,
            favicon TEXT,
            image_url TEXT,
            tags TEXT,
            is_read INTEGER DEFAULT 0,
            date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        
        cursor = conn.execute("PRAGMA table_info(links)")
        link_columns = [column[1] for column in cursor.fetchall()]
        if 'click_count' not in link_columns: 
            conn.execute("ALTER TABLE links ADD COLUMN click_count INTEGER DEFAULT 0")
        if 'image_url' not in link_columns:
            conn.execute("ALTER TABLE links ADD COLUMN image_url TEXT")
        if 'full_text' not in link_columns:
            conn.execute("ALTER TABLE links ADD COLUMN full_text TEXT")

        conn.execute("""CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT,
            category TEXT DEFAULT 'note',
            is_done INTEGER DEFAULT 0,
            date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        conn.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_backup_enabled', '0')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_backup_frequency', 'daily')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_backup_last_run', '')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_backup_retention_val', '7')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_backup_retention_unit', 'days')")
        
        # Smart Features / Automation settings
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('feature_smart_ingestion_master', '1')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('feature_full_text_fetch', '1')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('feature_yt_transcript_fetch', '1')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('feature_ai_auto_route', '1')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('feature_video_routing_mode', 'suggest')")

        # Homepage curated bookmarks
        conn.execute("""CREATE TABLE IF NOT EXISTS homepage_bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id INTEGER NOT NULL,
            group_name TEXT NOT NULL DEFAULT 'Ungrouped',
            display_order INTEGER DEFAULT 999,
            date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (link_id) REFERENCES links(id) ON DELETE CASCADE,
            UNIQUE(link_id)
        )""")

        # Video library categories (hierarchical: main -> sub)
        conn.execute("""CREATE TABLE IF NOT EXISTS video_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            parent_id INTEGER,
            display_order INTEGER DEFAULT 999,
            FOREIGN KEY (parent_id) REFERENCES video_categories(id) ON DELETE CASCADE
        )""")

        # Video bookmarks
        conn.execute("""CREATE TABLE IF NOT EXISTS video_bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            title TEXT,
            thumbnail_url TEXT,
            channel_name TEXT,
            duration TEXT,
            description TEXT,
            tags TEXT,
            category_id INTEGER,
            display_order INTEGER DEFAULT 999,
            transcript TEXT,
            suggested_category_id INTEGER,
            suggested_category_name TEXT,
            suggested_reasoning TEXT,
            date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (category_id) REFERENCES video_categories(id) ON DELETE SET NULL,
            FOREIGN KEY (suggested_category_id) REFERENCES video_categories(id) ON DELETE SET NULL
        )""")

        cursor = conn.execute("PRAGMA table_info(video_bookmarks)")
        vid_columns = [column[1] for column in cursor.fetchall()]
        if 'transcript' not in vid_columns:
            conn.execute("ALTER TABLE video_bookmarks ADD COLUMN transcript TEXT")
        if 'transcript_json' not in vid_columns:
            conn.execute("ALTER TABLE video_bookmarks ADD COLUMN transcript_json TEXT")
        if 'ai_chapters' not in vid_columns:
            conn.execute("ALTER TABLE video_bookmarks ADD COLUMN ai_chapters TEXT")
        if 'suggested_category_id' not in vid_columns:
            conn.execute("ALTER TABLE video_bookmarks ADD COLUMN suggested_category_id INTEGER")
        if 'suggested_category_name' not in vid_columns:
            conn.execute("ALTER TABLE video_bookmarks ADD COLUMN suggested_category_name TEXT")
        if 'suggested_reasoning' not in vid_columns:
            conn.execute("ALTER TABLE video_bookmarks ADD COLUMN suggested_reasoning TEXT")

        # AI Routing Feedback & Learning Memory
        conn.execute("""CREATE TABLE IF NOT EXISTS routing_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_name TEXT,
            video_title TEXT,
            chosen_category_id INTEGER,
            chosen_category_path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chosen_category_id) REFERENCES video_categories(id) ON DELETE CASCADE
        )""")

        # AI Timeline Knowledge Summaries Cache
        conn.execute("""CREATE TABLE IF NOT EXISTS timeline_summaries (
            period_key TEXT PRIMARY KEY,
            summary TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        # LinkForge Pulse: AI-Powered Smart Discover Feed Items
        conn.execute("""CREATE TABLE IF NOT EXISTS pulse_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            source_name TEXT,
            source_icon TEXT,
            summary TEXT,
            image_url TEXT,
            topic TEXT,
            published_at TEXT,
            relevance_score INTEGER DEFAULT 50,
            is_saved INTEGER DEFAULT 0,
            is_dismissed INTEGER DEFAULT 0,
            date_fetched TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        # LinkForge Pulse: Configured & Custom Topics
        conn.execute("""CREATE TABLE IF NOT EXISTS pulse_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            query_keywords TEXT NOT NULL,
            feed_type TEXT DEFAULT 'google_news',
            custom_feed_url TEXT,
            is_active INTEGER DEFAULT 1,
            display_order INTEGER DEFAULT 999
        )""")

        # Seed default pulse topics
        conn.execute("INSERT OR IGNORE INTO pulse_topics (name, query_keywords, feed_type, is_active, display_order) VALUES ('✨ For You', 'auto_profile', 'profile', 1, 1)")
        conn.execute("INSERT OR IGNORE INTO pulse_topics (name, query_keywords, feed_type, is_active, display_order) VALUES ('Tech & Code', 'technology software programming linux open source', 'google_news', 1, 2)")
        conn.execute("INSERT OR IGNORE INTO pulse_topics (name, query_keywords, feed_type, is_active, display_order) VALUES ('AI & ML', 'artificial intelligence LLM machine learning neural networks', 'google_news', 1, 3)")
        conn.execute("INSERT OR IGNORE INTO pulse_topics (name, query_keywords, feed_type, is_active, display_order) VALUES ('Automotive & EVs', 'electric vehicles cars automotive EV technology', 'google_news', 1, 4)")
        conn.execute("INSERT OR IGNORE INTO pulse_topics (name, query_keywords, feed_type, is_active, display_order) VALUES ('Hardware & 3D', '3d printing computer hardware raspberry pi maker', 'google_news', 1, 5)")

        # Book Vault: Downloaded Books Library
        conn.execute("""CREATE TABLE IF NOT EXISTS downloaded_books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE,
            title TEXT NOT NULL,
            author TEXT,
            cover_url TEXT,
            file_path TEXT,
            file_size INTEGER DEFAULT 0,
            file_format TEXT DEFAULT 'epub',
            status TEXT DEFAULT 'completed',
            progress INTEGER DEFAULT 100,
            download_url TEXT,
            date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        os.makedirs(Config.BACKUP_DIR, exist_ok=True)
        conn.commit()
        _db_initialized = True
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        raise
    finally:
        conn.close()

    if app is not None:
        app.teardown_appcontext(close_db)

def retry_write(func, max_retries=5, delay=0.5):
    for attempt in range(max_retries):
        try:
            return func()
        except sqlite3.OperationalError as e:
            err_msg = str(e).lower()
            if any(term in err_msg for term in ["locked", "disk i/o error", "busy", "timeout"]) and attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
                continue
            raise
    raise RuntimeError("Max DB write retries exceeded")
