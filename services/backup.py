import os
import glob
import csv
import json
import zipfile
import shutil
import tempfile
import time
import datetime
import threading
import sqlite3
import logging
from config import Config

logger = logging.getLogger(__name__)

MAX_BACKUPS_RETAINED = 7
_scheduler_started = False
_scheduler_lock = threading.Lock()

def get_db_direct():
    conn = sqlite3.connect(Config.DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=DELETE;")
    except Exception:
        pass
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    return conn

def format_file_size(size_in_bytes):
    if size_in_bytes < 1024:
        return f"{size_in_bytes} B"
    elif size_in_bytes < 1024 * 1024:
        return f"{size_in_bytes / 1024:.1f} KB"
    else:
        return f"{size_in_bytes / (1024 * 1024):.2f} MB"

def get_backup_config():
    try:
        conn = get_db_direct()
        cursor = conn.execute("SELECT key, value FROM settings WHERE key LIKE 'auto_backup_%'")
        rows = dict(cursor.fetchall())
        conn.close()
        
        try:
            r_val = int(rows.get("auto_backup_retention_val", "7"))
        except (ValueError, TypeError):
            r_val = 7
        if r_val < 1:
            r_val = 1

        r_unit = rows.get("auto_backup_retention_unit", "days")
        if r_unit not in ("days", "weeks"):
            r_unit = "days"

        inc_archives = rows.get("auto_backup_include_archives", "1") in ("1", "true", "True")

        return {
            "enabled": rows.get("auto_backup_enabled", "0") in ("1", "true", "True"),
            "frequency": rows.get("auto_backup_frequency", "daily"),
            "last_run": rows.get("auto_backup_last_run", ""),
            "retention_val": r_val,
            "retention_unit": r_unit,
            "include_archives": inc_archives
        }
    except Exception as e:
        logger.error(f"Error reading backup config: {e}")
        return {"enabled": False, "frequency": "daily", "last_run": "", "retention_val": 7, "retention_unit": "days", "include_archives": True}

def save_backup_config(enabled, frequency, retention_val=7, retention_unit="days", include_archives=True):
    val_enabled = "1" if enabled else "0"
    freq = "weekly" if frequency == "weekly" else "daily"
    inc_arch = "1" if include_archives else "0"
    
    try:
        r_val = int(retention_val)
    except (ValueError, TypeError):
        r_val = 7
    if r_val < 1:
        r_val = 1

    r_unit = "weeks" if str(retention_unit).lower() == "weeks" else "days"

    def _save():
        conn = get_db_direct()
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_backup_enabled', ?)", (val_enabled,))
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_backup_frequency', ?)", (freq,))
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_backup_retention_val', ?)", (str(r_val),))
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_backup_retention_unit', ?)", (r_unit,))
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_backup_include_archives', ?)", (inc_arch,))
        conn.commit()
        conn.close()
    
    _save()
    prune_old_backups()

def clean_orphaned_archives():
    """Scans data/archives and removes files/directories not referenced by any active link."""
    import re
    import stat
    db_dir = os.path.dirname(os.path.abspath(Config.DB_PATH))
    archives_dir = os.path.join(db_dir, 'archives')
    if not os.path.exists(archives_dir):
        return {"deleted_folders": 0, "deleted_files": 0, "freed_bytes": 0, "freed_formatted": "0 B"}

    def _onerror_readonly(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    conn = get_db_direct()
    rows = conn.execute("SELECT archive_path FROM links WHERE archive_path IS NOT NULL AND length(archive_path) > 0").fetchall()
    conn.close()

    active_names = set()
    for r in rows:
        p = r["archive_path"].strip('/').replace('\\', '/')
        parts = p.split('/')
        if len(parts) >= 2:
            active_names.add(parts[1])

    deleted_folders = 0
    deleted_files = 0
    freed_bytes = 0

    # Scan top-level items in data/archives
    for item in os.listdir(archives_dir):
        if item == 'images':
            continue
        item_path = os.path.join(archives_dir, item)
        if item not in active_names:
            try:
                if os.path.isdir(item_path):
                    for root, dirs, files in os.walk(item_path):
                        for f in files:
                            fp = os.path.join(root, f)
                            try:
                                freed_bytes += os.path.getsize(fp)
                                os.chmod(fp, stat.S_IWRITE)
                            except Exception:
                                pass
                    shutil.rmtree(item_path, onerror=_onerror_readonly)
                    deleted_folders += 1
                else:
                    freed_bytes += os.path.getsize(item_path)
                    try:
                        os.chmod(item_path, stat.S_IWRITE)
                    except Exception:
                        pass
                    os.remove(item_path)
                    deleted_files += 1
            except Exception as e:
                logger.error(f"Error removing orphaned archive item {item_path}: {e}")

    # Also clean orphaned images in data/archives/images
    images_dir = os.path.join(archives_dir, 'images')
    if os.path.exists(images_dir):
        referenced_images = set()
        for root, dirs, files in os.walk(archives_dir):
            if root == images_dir:
                continue
            for f in files:
                if f.endswith('.html'):
                    try:
                        with open(os.path.join(root, f), 'r', encoding='utf-8', errors='ignore') as hf:
                            content = hf.read()
                            for img_match in re.findall(r'/archives/images/([^"\'\s>]+)', content):
                                referenced_images.add(img_match)
                    except Exception:
                        pass
        for img in os.listdir(images_dir):
            if img not in referenced_images:
                img_path = os.path.join(images_dir, img)
                try:
                    freed_bytes += os.path.getsize(img_path)
                    os.remove(img_path)
                    deleted_files += 1
                except Exception as e:
                    logger.error(f"Error removing orphaned image {img_path}: {e}")

    logger.info(f"Cleaned orphaned archives: removed {deleted_folders} folders, {deleted_files} files, freed {format_file_size(freed_bytes)}")
    return {
        "deleted_folders": deleted_folders,
        "deleted_files": deleted_files,
        "freed_bytes": freed_bytes,
        "freed_formatted": format_file_size(freed_bytes)
    }

def get_archives_storage_stats():
    """Calculates storage footprint of the active archives directory."""
    db_dir = os.path.dirname(os.path.abspath(Config.DB_PATH))
    archives_dir = os.path.join(db_dir, 'archives')
    if not os.path.exists(archives_dir):
        return {"total_files": 0, "total_bytes": 0, "total_formatted": "0 B", "article_count": 0, "image_count": 0}

    total_files = 0
    total_bytes = 0
    article_count = 0
    image_count = 0

    for root, dirs, files in os.walk(archives_dir):
        for f in files:
            fp = os.path.join(root, f)
            try:
                sz = os.path.getsize(fp)
                total_bytes += sz
                total_files += 1
                if f.endswith('.html'):
                    article_count += 1
                elif f.lower().endswith(('.webp', '.jpg', '.png', '.jpeg', '.gif')):
                    image_count += 1
            except Exception:
                pass

    return {
        "total_files": total_files,
        "total_bytes": total_bytes,
        "total_formatted": format_file_size(total_bytes),
        "article_count": article_count,
        "image_count": image_count
    }

def optimize_existing_archives():
    """Downscales oversized images (>900px) and re-encodes with WebP 65 to reclaim space."""
    import stat
    from PIL import Image
    import io

    db_dir = os.path.dirname(os.path.abspath(Config.DB_PATH))
    archives_dir = os.path.join(db_dir, 'archives')
    if not os.path.exists(archives_dir):
        return {"processed": 0, "optimized": 0, "saved_bytes": 0, "saved_formatted": "0 B", "stats": get_archives_storage_stats()}

    processed = 0
    optimized = 0
    saved_bytes = 0

    for root, dirs, files in os.walk(archives_dir):
        for f in files:
            if f.lower().endswith('.webp'):
                fp = os.path.join(root, f)
                processed += 1
                try:
                    sz_before = os.path.getsize(fp)
                    with Image.open(fp) as img:
                        orig_w, orig_h = img.size
                        needs_downscale = orig_w > 900 or orig_h > 900
                        if needs_downscale:
                            img.thumbnail((900, 900), Image.Resampling.LANCZOS)
                        if img.mode != 'RGB':
                            img = img.convert('RGB')
                        
                        buf = io.BytesIO()
                        img.save(buf, format='WEBP', quality=65, method=6)
                        sz_after = buf.tell()

                        # Only overwrite if we actually reduced the size
                        if sz_after < sz_before:
                            try:
                                os.chmod(fp, stat.S_IWRITE)
                            except Exception:
                                pass
                            with open(fp, 'wb') as f_out:
                                f_out.write(buf.getvalue())
                            saved_bytes += (sz_before - sz_after)
                            optimized += 1
                except Exception as e:
                    logger.warning(f"Could not optimize image {fp}: {e}")

    logger.info(f"Archive optimization complete: processed {processed}, optimized {optimized}, saved {format_file_size(saved_bytes)}")
    return {
        "processed": processed,
        "optimized": optimized,
        "saved_bytes": saved_bytes,
        "saved_formatted": format_file_size(saved_bytes),
        "stats": get_archives_storage_stats()
    }

def prune_aged_archive_images(days_old=60):
    """Removes heavy image binaries from articles older than days_old while keeping full HTML text intact."""
    import stat
    db_dir = os.path.dirname(os.path.abspath(Config.DB_PATH))
    archives_dir = os.path.join(db_dir, 'archives')
    if not os.path.exists(archives_dir):
        return {"pruned_articles": 0, "removed_images": 0, "freed_bytes": 0, "freed_formatted": "0 B", "stats": get_archives_storage_stats()}

    conn = get_db_direct()
    query = """
        SELECT id, archive_path FROM links 
        WHERE archive_path IS NOT NULL 
          AND length(archive_path) > 0 
          AND date_added < datetime('now', ?)
    """
    rows = conn.execute(query, (f"-{days_old} days",)).fetchall()
    conn.close()

    pruned_articles = 0
    removed_images = 0
    freed_bytes = 0

    for r in rows:
        p = r["archive_path"].strip('/').replace('\\', '/')
        parts = p.split('/')
        if len(parts) >= 2:
            folder_name = parts[1]
            folder_path = os.path.join(archives_dir, folder_name)
            if os.path.isdir(folder_path):
                has_pruned = False
                for f in os.listdir(folder_path):
                    if f.lower().endswith(('.webp', '.png', '.jpg', '.jpeg', '.gif')):
                        img_path = os.path.join(folder_path, f)
                        try:
                            freed_bytes += os.path.getsize(img_path)
                            os.chmod(img_path, stat.S_IWRITE)
                            os.remove(img_path)
                            removed_images += 1
                            has_pruned = True
                        except Exception as e:
                            logger.error(f"Error removing aged image {img_path}: {e}")
                if has_pruned:
                    pruned_articles += 1

    return {
        "pruned_articles": pruned_articles,
        "removed_images": removed_images,
        "freed_bytes": freed_bytes,
        "freed_formatted": format_file_size(freed_bytes),
        "stats": get_archives_storage_stats()
    }

def prune_old_backups():
    try:
        cfg = get_backup_config()
        r_val = cfg["retention_val"]
        r_unit = cfg["retention_unit"]
        
        if r_unit == "weeks":
            max_age_seconds = r_val * 7 * 86400
        else:
            max_age_seconds = r_val * 86400

        patterns = [
            os.path.join(Config.BACKUP_DIR, "linkforge_auto_backup_*.zip"),
            os.path.join(Config.BACKUP_DIR, "dashforge_auto_backup_*.csv"),
            os.path.join(Config.BACKUP_DIR, "dashforge_auto_backup_*.db")
        ]
        
        files = []
        for pat in patterns:
            files.extend(glob.glob(pat))
            
        now = time.time()
        for f in files:
            mtime = os.path.getmtime(f)
            file_age = now - mtime
            if file_age > max_age_seconds:
                try:
                    os.remove(f)
                    logger.info(f"Pruned expired backup file: {f}")
                except Exception as pe:
                    logger.error(f"Failed to remove expired backup {f}: {pe}")
    except Exception as e:
        logger.error(f"Error pruning backups: {e}")

def create_auto_backup():
    """Create a complete system backup containing the full SQLite database, multi-table exports, and optional archives in a ZIP archive."""
    os.makedirs(Config.BACKUP_DIR, exist_ok=True)
    cfg = get_backup_config()
    include_archives = cfg.get("include_archives", True)

    # Prune orphaned archives first to ensure minimal footprint
    clean_orphaned_archives()

    timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_filename = f"linkforge_auto_backup_{timestamp_str}.zip"
    zip_filepath = os.path.join(Config.BACKUP_DIR, zip_filename)

    with tempfile.TemporaryDirectory() as tmp_dir:
        # 1. Live consistent SQLite online backup with WAL flush
        src_conn = get_db_direct()
        try:
            src_conn.execute("PRAGMA wal_checkpoint(FULL);")
        except Exception:
            pass

        raw_db_path = os.path.join(tmp_dir, "linkforge.db")
        dest_conn = sqlite3.connect(raw_db_path)
        src_conn.backup(dest_conn)
        dest_conn.close()

        # 2. Extract and export all database tables to readable CSVs & JSON
        links_rows = src_conn.execute("SELECT * FROM links ORDER BY date_added DESC").fetchall()
        vid_rows = src_conn.execute("SELECT * FROM video_bookmarks ORDER BY date_added DESC").fetchall()
        cat_rows = src_conn.execute("SELECT * FROM video_categories ORDER BY display_order ASC, name ASC").fetchall()
        hp_rows = src_conn.execute("SELECT * FROM homepage_bookmarks ORDER BY group_name ASC, display_order ASC").fetchall()
        notes_rows = src_conn.execute("SELECT * FROM notes ORDER BY date_added DESC").fetchall()
        settings_rows = src_conn.execute("SELECT key, value FROM settings ORDER BY key ASC").fetchall()
        
        try:
            hp_groups = src_conn.execute("SELECT name, display_order FROM homepage_groups ORDER BY display_order ASC").fetchall()
        except Exception:
            hp_groups = []

        try:
            config_rows = src_conn.execute("SELECT key, value FROM config ORDER BY key ASC").fetchall()
        except Exception:
            config_rows = []

        try:
            denied_rows = src_conn.execute("SELECT * FROM denied_urls ORDER BY created_at DESC").fetchall()
        except Exception:
            denied_rows = []
            
        try:
            route_history = src_conn.execute("SELECT * FROM routing_history ORDER BY created_at DESC").fetchall()
        except Exception:
            route_history = []
            
        try:
            summaries = src_conn.execute("SELECT * FROM timeline_summaries ORDER BY created_at DESC").fetchall()
        except Exception:
            summaries = []

        try:
            book_rows = src_conn.execute("SELECT * FROM downloaded_books ORDER BY date_added DESC").fetchall()
        except Exception:
            book_rows = []

        # Helper to export any SQLite Row list to a CSV with full headers
        def export_rows_to_csv(rows, path, fallback_headers=None):
            with open(path, 'w', newline='', encoding='utf-8') as f:
                cw = csv.writer(f)
                if rows and len(rows) > 0:
                    keys = list(rows[0].keys())
                    cw.writerow(keys)
                    for r in rows:
                        cw.writerow([r[k] for k in keys])
                elif fallback_headers:
                    cw.writerow(fallback_headers)

        # Export all 12 tables to CSVs
        links_csv_path = os.path.join(tmp_dir, "links.csv")
        export_rows_to_csv(links_rows, links_csv_path, ['id', 'url', 'title', 'description', 'favicon', 'tags', 'is_read', 'date_added', 'click_count', 'image_url', 'archive_path', 'full_text'])

        vids_csv_path = os.path.join(tmp_dir, "video_bookmarks.csv")
        export_rows_to_csv(vid_rows, vids_csv_path, ['id', 'url', 'title', 'thumbnail_url', 'channel_name', 'duration', 'description', 'tags', 'category_id', 'display_order', 'date_added', 'transcript'])

        cat_csv_path = os.path.join(tmp_dir, "video_categories.csv")
        export_rows_to_csv(cat_rows, cat_csv_path, ['id', 'name', 'parent_id', 'display_order'])

        hp_csv_path = os.path.join(tmp_dir, "homepage_bookmarks.csv")
        export_rows_to_csv(hp_rows, hp_csv_path, ['id', 'link_id', 'group_name', 'display_order', 'date_added'])

        hp_grp_csv_path = os.path.join(tmp_dir, "homepage_groups.csv")
        export_rows_to_csv(hp_groups, hp_grp_csv_path, ['name', 'display_order'])

        notes_csv_path = os.path.join(tmp_dir, "notes.csv")
        export_rows_to_csv(notes_rows, notes_csv_path, ['id', 'content', 'category', 'is_done', 'date_added'])

        settings_csv_path = os.path.join(tmp_dir, "settings.csv")
        export_rows_to_csv(settings_rows, settings_csv_path, ['key', 'value'])

        config_csv_path = os.path.join(tmp_dir, "config.csv")
        export_rows_to_csv(config_rows, config_csv_path, ['key', 'custom_title', 'icon_url', 'custom_url', 'custom_color', 'hidden', 'display_order', 'is_custom', 'group_name', 'click_count'])

        denied_csv_path = os.path.join(tmp_dir, "denied_urls.csv")
        export_rows_to_csv(denied_rows, denied_csv_path, ['url', 'created_at'])

        routing_csv_path = os.path.join(tmp_dir, "routing_history.csv")
        export_rows_to_csv(route_history, routing_csv_path, ['id', 'channel_name', 'video_title', 'chosen_category_id', 'chosen_category_path', 'created_at'])

        summaries_csv_path = os.path.join(tmp_dir, "timeline_summaries.csv")
        export_rows_to_csv(summaries, summaries_csv_path, ['period_key', 'summary', 'created_at'])

        books_csv_path = os.path.join(tmp_dir, "downloaded_books.csv")
        export_rows_to_csv(book_rows, books_csv_path, ['id', 'book_key', 'title', 'author', 'year', 'cover_url', 'filename', 'file_path', 'file_size', 'date_added'])

        # Export manifest.json
        manifest = {
            "version": "2.1",
            "backup_type": "full_system_archive",
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "includes_archives": include_archives,
            "table_counts": {
                "links": len(links_rows),
                "video_bookmarks": len(vid_rows),
                "video_categories": len(cat_rows),
                "homepage_bookmarks": len(hp_rows),
                "homepage_groups": len(hp_groups),
                "notes": len(notes_rows),
                "settings": len(settings_rows),
                "config": len(config_rows),
                "denied_urls": len(denied_rows),
                "routing_history": len(route_history),
                "timeline_summaries": len(summaries),
                "downloaded_books": len(book_rows)
            }
        }
        manifest_path = os.path.join(tmp_dir, "manifest.json")
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, indent=2)

        # 3. Create compressed ZIP archive containing all files
        with zipfile.ZipFile(zip_filepath, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(raw_db_path, "linkforge.db")
            zipf.write(manifest_path, "manifest.json")
            zipf.write(links_csv_path, "links.csv")
            zipf.write(vids_csv_path, "video_bookmarks.csv")
            zipf.write(cat_csv_path, "video_categories.csv")
            zipf.write(hp_csv_path, "homepage_bookmarks.csv")
            zipf.write(hp_grp_csv_path, "homepage_groups.csv")
            zipf.write(notes_csv_path, "notes.csv")
            zipf.write(settings_csv_path, "settings.csv")
            zipf.write(config_csv_path, "config.csv")
            zipf.write(denied_csv_path, "denied_urls.csv")
            zipf.write(routing_csv_path, "routing_history.csv")
            zipf.write(summaries_csv_path, "timeline_summaries.csv")
            zipf.write(books_csv_path, "downloaded_books.csv")

            # Package active data/archives into ZIP under archives/
            db_dir = os.path.dirname(os.path.abspath(Config.DB_PATH))
            if include_archives:
                archives_dir = os.path.join(db_dir, 'archives')
                if os.path.exists(archives_dir):
                    for root, dirs, files in os.walk(archives_dir):
                        for file_name in files:
                            abs_file_path = os.path.join(root, file_name)
                            rel_arc_path = os.path.join("archives", os.path.relpath(abs_file_path, archives_dir)).replace('\\', '/')
                            zipf.write(abs_file_path, rel_arc_path)

            # Package downloaded EPUB books into ZIP under books/
            books_dir = os.path.join(db_dir, 'books')
            if os.path.exists(books_dir):
                for root, dirs, files in os.walk(books_dir):
                    for file_name in files:
                        abs_file_path = os.path.join(root, file_name)
                        rel_book_path = os.path.join("books", os.path.relpath(abs_file_path, books_dir)).replace('\\', '/')
                        zipf.write(abs_file_path, rel_book_path)

    # Update last run timestamp
    now_formatted = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    def _update_ts():
        c = get_db_direct()
        try:
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_backup_last_run', ?)", (now_formatted,))
            c.commit()
        finally:
            c.close()
    try:
        from services.db import retry_write
        retry_write(_update_ts)
    except Exception as e:
        logger.warning(f"Could not record auto_backup_last_run timestamp: {e}")

    prune_old_backups()
    logger.info(f"Created comprehensive automated backup: {zip_filepath}")
    return zip_filename

def list_auto_backups():
    os.makedirs(Config.BACKUP_DIR, exist_ok=True)
    prune_old_backups()
    
    patterns = [
        os.path.join(Config.BACKUP_DIR, "linkforge_auto_backup_*.zip"),
        os.path.join(Config.BACKUP_DIR, "dashforge_auto_backup_*.csv"),
        os.path.join(Config.BACKUP_DIR, "dashforge_auto_backup_*.db")
    ]
    
    files = []
    for pat in patterns:
        files.extend(glob.glob(pat))
    
    result = []
    for filepath in files:
        filename = os.path.basename(filepath)
        stat = os.stat(filepath)
        mod_time = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        
        # Determine backup type badge
        if filename.endswith(".zip"):
            backup_type = "Full System Archive (ZIP)"
        elif filename.endswith(".db"):
            backup_type = "Database Snapshot (DB)"
        else:
            backup_type = "Legacy Links (CSV)"

        # Inspect manifest summary if ZIP
        summary_info = ""
        if filename.endswith(".zip"):
            try:
                with zipfile.ZipFile(filepath, 'r') as z:
                    if "manifest.json" in z.namelist():
                        m_data = json.loads(z.read("manifest.json").decode('utf-8'))
                        counts = m_data.get("table_counts", {})
                        has_arch = m_data.get("includes_archives", False)
                        arch_tag = " • with Article Archives" if has_arch else ""
                        bks_cnt = counts.get('downloaded_books', 0)
                        bks_tag = f", {bks_cnt} books" if bks_cnt > 0 else ""
                        summary_info = f"{counts.get('links', 0)} links, {counts.get('video_bookmarks', 0)} videos, {counts.get('notes', 0)} notes{bks_tag}{arch_tag}"
            except Exception:
                pass

        result.append({
            "filename": filename,
            "date": mod_time,
            "size": format_file_size(stat.st_size),
            "size_bytes": stat.st_size,
            "mtime": stat.st_mtime,
            "type": backup_type,
            "summary": summary_info
        })
    
    result.sort(key=lambda x: x["mtime"], reverse=True)
    return result

def restore_backup(file_storage):
    """Restore from a full .zip archive, .db snapshot, or legacy .csv file."""
    filename = file_storage.filename.lower()
    
    # Save uploaded file to temp directory
    with tempfile.TemporaryDirectory() as tmp_dir:
        upload_path = os.path.join(tmp_dir, file_storage.filename)
        file_storage.save(upload_path)
        
        # 1. Restore from ZIP Archive
        if filename.endswith(".zip"):
            with zipfile.ZipFile(upload_path, 'r') as zf:
                namelist = zf.namelist()
                if "linkforge.db" not in namelist:
                    raise ValueError("Invalid backup archive: missing 'linkforge.db'.")
                
                extracted_db = os.path.join(tmp_dir, "extracted.db")
                with open(extracted_db, "wb") as f_out:
                    f_out.write(zf.read("linkforge.db"))
                
                # Validate SQLite integrity
                test_conn = sqlite3.connect(extracted_db)
                integrity = test_conn.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    test_conn.close()
                    raise ValueError(f"Corrupted backup database: integrity check failed ({integrity}).")
                
                links_cnt = test_conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
                try:
                    vids_cnt = test_conn.execute("SELECT COUNT(*) FROM video_bookmarks").fetchone()[0]
                except Exception:
                    vids_cnt = 0
                try:
                    notes_cnt = test_conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
                except Exception:
                    notes_cnt = 0
                try:
                    books_cnt = test_conn.execute("SELECT COUNT(*) FROM downloaded_books").fetchone()[0]
                except Exception:
                    books_cnt = 0
                test_conn.close()

                # Safety copy of current database
                backup_safety_path = os.path.join(Config.BACKUP_DIR, f"pre_restore_safety_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
                try:
                    src_conn = get_db_direct()
                    safety_conn = sqlite3.connect(backup_safety_path)
                    src_conn.backup(safety_conn)
                    safety_conn.close()
                    src_conn.close()
                except Exception as e:
                    logger.warning(f"Could not create pre-restore safety copy: {e}")

                # Live restore into active DB using SQLite backup API
                active_conn = get_db_direct()
                restore_src = sqlite3.connect(extracted_db)
                restore_src.backup(active_conn)
                restore_src.close()
                active_conn.close()

                # Restore archives folder if present in ZIP
                import stat
                restored_archives_count = 0
                db_dir = os.path.dirname(os.path.abspath(Config.DB_PATH))
                target_archives_dir = os.path.join(db_dir, 'archives')
                os.makedirs(target_archives_dir, exist_ok=True)

                for member in namelist:
                    if member.startswith("archives/") and not member.endswith('/'):
                        try:
                            zf.extract(member, tmp_dir)
                            src_file = os.path.join(tmp_dir, member)
                            rel_sub = member[len("archives/"):].replace('/', os.sep).replace('\\', os.sep)
                            dest_file = os.path.join(target_archives_dir, rel_sub)
                            os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                            
                            if os.path.exists(dest_file):
                                try:
                                    os.chmod(dest_file, stat.S_IWRITE)
                                    shutil.copy2(src_file, dest_file)
                                except Exception:
                                    pass  # File already exists on disk and is preserved
                            else:
                                shutil.copy2(src_file, dest_file)
                            restored_archives_count += 1
                        except Exception as arc_e:
                            logger.warning(f"Could not extract archive file {member}: {arc_e}")

                # Restore downloaded EPUB books if present in ZIP
                restored_books_count = 0
                target_books_dir = os.path.join(db_dir, 'books')
                os.makedirs(target_books_dir, exist_ok=True)

                for member in namelist:
                    if member.startswith("books/") and not member.endswith('/'):
                        try:
                            zf.extract(member, tmp_dir)
                            src_file = os.path.join(tmp_dir, member)
                            rel_sub = member[len("books/"):].replace('/', os.sep).replace('\\', os.sep)
                            dest_file = os.path.join(target_books_dir, rel_sub)
                            os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                            
                            if os.path.exists(dest_file):
                                try:
                                    os.chmod(dest_file, stat.S_IWRITE)
                                    shutil.copy2(src_file, dest_file)
                                except Exception:
                                    pass
                            else:
                                shutil.copy2(src_file, dest_file)
                            restored_books_count += 1
                        except Exception as book_e:
                            logger.warning(f"Could not extract book file {member}: {book_e}")

                arch_msg = f", {restored_archives_count} article archives" if restored_archives_count > 0 else ""
                books_msg = f", {books_cnt} books ({restored_books_count} EPUB files)" if books_cnt > 0 else ""
                return {
                    "status": "success",
                    "message": f"Full system restore successful! Restored {links_cnt} links, {vids_cnt} videos, {notes_cnt} notes{books_msg}{arch_msg}."
                }

        # 2. Restore from raw .db snapshot
        elif filename.endswith(".db") or filename.endswith(".sqlite"):
            test_conn = sqlite3.connect(upload_path)
            integrity = test_conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                test_conn.close()
                raise ValueError(f"Corrupted database file: integrity check failed ({integrity}).")
            links_cnt = test_conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
            test_conn.close()

            active_conn = get_db_direct()
            restore_src = sqlite3.connect(upload_path)
            restore_src.backup(active_conn)
            restore_src.close()
            active_conn.close()

            return {
                "status": "success",
                "message": f"Database restored successfully! Restored {links_cnt} links and all associated tables."
            }

        # 3. Restore / Import from CSV
        elif filename.endswith(".csv"):
            with open(upload_path, 'r', encoding='utf-8', errors='ignore') as f:
                csv_input = csv.reader(f)
                header = next(csv_input, None)
                imported_count = 0
                conn = get_db_direct()
                for row in csv_input:
                    if len(row) >= 9:
                        url, title, desc, favicon, tags = row[1], row[2], row[3], row[4], row[5]
                        is_read = int(row[6]) if str(row[6]).isdigit() else 0
                        date_added = row[7]
                        click_count = int(row[8]) if str(row[8]).isdigit() else 0
                        conn.execute("""
                            INSERT OR IGNORE INTO links 
                            (url, title, description, favicon, tags, is_read, date_added, click_count) 
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (url, title, desc, favicon, tags, is_read, date_added, click_count))
                        imported_count += 1
                conn.commit()
                conn.close()
                return {
                    "status": "success",
                    "message": f"CSV import successful! Processed {imported_count} links."
                }
        else:
            raise ValueError("Unsupported file type. Please upload a .zip, .db, or .csv backup file.")

def is_backup_due():
    cfg = get_backup_config()
    if not cfg["enabled"]:
        return False
    
    last_run_str = cfg["last_run"]
    if not last_run_str:
        return True
    
    try:
        last_run = datetime.datetime.strptime(last_run_str, "%Y-%m-%d %H:%M:%S")
        elapsed = (datetime.datetime.now() - last_run).total_seconds()
        
        if cfg["frequency"] == "weekly":
            return elapsed >= 7 * 86400  # 7 days
        else:
            return elapsed >= 86400  # 24 hours (daily)
    except Exception as e:
        logger.error(f"Error checking if backup is due: {e}")
        return True

def backup_worker_loop():
    logger.info("Backup scheduler worker thread started.")
    while True:
        try:
            if is_backup_due():
                logger.info("Automatic backup is due. Running backup...")
                create_auto_backup()
        except Exception as e:
            logger.error(f"Error in backup worker loop: {e}")
        time.sleep(300)  # Check every 5 minutes

def start_backup_scheduler(app=None):
    global _scheduler_started
    with _scheduler_lock:
        if not _scheduler_started:
            _scheduler_started = True
            thread = threading.Thread(target=backup_worker_loop, daemon=True, name="BackupScheduler")
            thread.start()
            logger.info("Started background backup scheduler thread.")
