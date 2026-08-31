import os
import shutil
import glob
import stat
import logging
from config import Config
from services.backup import get_db_direct
from services.db import retry_write

logger = logging.getLogger(__name__)

def nuke_system_data(options):
    """
    Selectively purges data based on user toggles.
    options: dict containing boolean flags for:
      - nuke_links: bool (default True)
      - nuke_archives: bool (default True)
      - nuke_videos: bool (default True)
      - nuke_notes: bool (default True)
      - nuke_homepage: bool (default True)
      - nuke_denied: bool (default True)
      - nuke_backups: bool (default False)
      - retain_settings: bool (default True)
    """
    nuke_links = bool(options.get("nuke_links", True))
    nuke_archives = bool(options.get("nuke_archives", True))
    nuke_videos = bool(options.get("nuke_videos", True))
    nuke_notes = bool(options.get("nuke_notes", True))
    nuke_homepage = bool(options.get("nuke_homepage", True))
    nuke_denied = bool(options.get("nuke_denied", True))
    nuke_books = bool(options.get("nuke_books", True))
    nuke_backups = bool(options.get("nuke_backups", False))
    retain_settings = bool(options.get("retain_settings", True))

    report = {
        "deleted_links": 0,
        "deleted_archives": 0,
        "deleted_videos": 0,
        "deleted_categories": 0,
        "deleted_notes": 0,
        "deleted_homepage_bookmarks": 0,
        "deleted_denied_urls": 0,
        "deleted_books": 0,
        "deleted_book_files": 0,
        "deleted_backups": 0,
        "retained_settings": retain_settings
    }

    def _execute_db_nuke():
        conn = get_db_direct()
        try:
            # 1. Links & Neural Timeline
            if nuke_links:
                c = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
                report["deleted_links"] = c
                conn.execute("DELETE FROM links")
                try:
                    conn.execute("DELETE FROM timeline_summaries")
                except Exception:
                    pass

            # 2. Video Bookmarks & Categories & Routing History
            if nuke_videos:
                v_cnt = conn.execute("SELECT COUNT(*) FROM video_bookmarks").fetchone()[0]
                c_cnt = conn.execute("SELECT COUNT(*) FROM video_categories").fetchone()[0]
                report["deleted_videos"] = v_cnt
                report["deleted_categories"] = c_cnt
                conn.execute("DELETE FROM video_bookmarks")
                conn.execute("DELETE FROM video_categories")
                try:
                    conn.execute("DELETE FROM routing_history")
                except Exception:
                    pass

            # 3. Scratchpad Notes
            if nuke_notes:
                n_cnt = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
                report["deleted_notes"] = n_cnt
                conn.execute("DELETE FROM notes")

            # 4. Homepage Pinned Bookmarks & Custom Groups
            if nuke_homepage:
                hp_cnt = conn.execute("SELECT COUNT(*) FROM homepage_bookmarks").fetchone()[0]
                report["deleted_homepage_bookmarks"] = hp_cnt
                conn.execute("DELETE FROM homepage_bookmarks")
                try:
                    conn.execute("DELETE FROM homepage_groups")
                except Exception:
                    pass

            # 5. Denied URLs Blacklist
            if nuke_denied:
                try:
                    d_cnt = conn.execute("SELECT COUNT(*) FROM denied_urls").fetchone()[0]
                    report["deleted_denied_urls"] = d_cnt
                    conn.execute("DELETE FROM denied_urls")
                except Exception:
                    pass

            # 6. Book Vault Downloaded Books
            if nuke_books:
                try:
                    b_cnt = conn.execute("SELECT COUNT(*) FROM downloaded_books").fetchone()[0]
                    report["deleted_books"] = b_cnt
                    conn.execute("DELETE FROM downloaded_books")
                except Exception:
                    pass

            # 7. Settings Table
            if not retain_settings:
                # Reset settings to clean default keys
                conn.execute("DELETE FROM settings")
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('feature_smart_ingestion_master', '1')")
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('feature_full_text_fetch', '1')")
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('feature_yt_transcript_fetch', '1')")
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('feature_ai_auto_route', '1')")
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('feature_video_routing_mode', 'suggest')")
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('show_scratchpad_menu', '1')")
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('shelfmark_url', 'https://stacks.okapitek.uk/')")
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('hardcover_api_key', '')")

            conn.commit()
            
            # Reclaim SQLite storage space
            try:
                conn.execute("VACUUM;")
                conn.execute("PRAGMA wal_checkpoint(FULL);")
            except Exception:
                pass
        finally:
            conn.close()

    # Run Database Nuke with Retry
    retry_write(_execute_db_nuke)

    def _remove_readonly(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    # 8. Filesystem: Wipe Offline Article Archives
    if nuke_archives:
        db_dir = os.path.dirname(os.path.abspath(Config.DB_PATH))
        archives_dir = os.path.join(db_dir, 'archives')
        if os.path.exists(archives_dir):
            file_count = 0
            for root, dirs, files in os.walk(archives_dir):
                file_count += len(files)
            report["deleted_archives"] = file_count
            
            try:
                shutil.rmtree(archives_dir, onerror=_remove_readonly)
            except Exception as e:
                logger.error(f"Error removing archives directory: {e}")
            os.makedirs(archives_dir, exist_ok=True)

    # 9. Filesystem: Wipe Downloaded EPUB Books
    if nuke_books:
        db_dir = os.path.dirname(os.path.abspath(Config.DB_PATH))
        books_dir = os.path.join(db_dir, 'books')
        if os.path.exists(books_dir):
            bfile_count = 0
            for root, dirs, files in os.walk(books_dir):
                bfile_count += len(files)
            report["deleted_book_files"] = bfile_count
            
            try:
                shutil.rmtree(books_dir, onerror=_remove_readonly)
            except Exception as e:
                logger.error(f"Error removing books storage directory: {e}")
            os.makedirs(books_dir, exist_ok=True)

    # 10. Filesystem: Wipe Stored System Backups
    if nuke_backups:
        if os.path.exists(Config.BACKUP_DIR):
            backup_files = glob.glob(os.path.join(Config.BACKUP_DIR, "linkforge_auto_backup_*.zip"))
            backup_files.extend(glob.glob(os.path.join(Config.BACKUP_DIR, "dashforge_auto_backup_*.*")))
            backup_files.extend(glob.glob(os.path.join(Config.BACKUP_DIR, "pre_restore_safety_*.db")))
            report["deleted_backups"] = len(backup_files)
            for bf in backup_files:
                try:
                    os.remove(bf)
                except Exception:
                    pass

    logger.info(f"Nuke operation complete: {report}")
    return report
