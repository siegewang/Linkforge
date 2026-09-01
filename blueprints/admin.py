from flask import Blueprint, render_template, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename
import os
import logging
from config import Config

logger = logging.getLogger(__name__)
from services.backup import (
    get_backup_config, save_backup_config, create_auto_backup, list_auto_backups,
    restore_backup, clean_orphaned_archives, get_archives_storage_stats,
    optimize_existing_archives, prune_aged_archive_images
)

from services.nuke import nuke_system_data

admin_bp = Blueprint('admin', __name__)

@admin_bp.route("/admin")
def admin():
    return render_template("admin.html", sub_page="apps", active_page="admin")

@admin_bp.route("/admin/calendar")
def admin_calendar():
    return render_template("admin_calendar.html", sub_page="calendar", active_page="admin")

@admin_bp.route("/admin/data")
def admin_data():
    return render_template("admin_data.html", sub_page="data", active_page="admin")

@admin_bp.route("/admin/extensions")
def admin_extensions():
    return render_template("admin_extensions.html", sub_page="extensions", active_page="admin")

@admin_bp.route("/admin/nuke")
def admin_nuke():
    return render_template("admin_nuke.html", sub_page="nuke", active_page="admin")

@admin_bp.route("/api/admin/nuke", methods=["POST"])
def api_admin_nuke():
    try:
        options = request.json or {}
        report = nuke_system_data(options)
        return jsonify({"status": "success", "report": report})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@admin_bp.route("/api/admin/auto-backup/config", methods=["GET", "POST"])
def auto_backup_config():
    if request.method == "POST":
        data = request.json or {}
        enabled = bool(data.get("enabled", False))
        frequency = data.get("frequency", "daily")
        retention_val = data.get("retention_val", 7)
        retention_unit = data.get("retention_unit", "days")
        include_archives = bool(data.get("include_archives", True))
        save_backup_config(enabled, frequency, retention_val, retention_unit, include_archives)
        return jsonify({"status": "success", "config": get_backup_config()})
    return jsonify(get_backup_config())

@admin_bp.route("/api/admin/archives/stats")
def admin_archives_stats():
    return jsonify(get_archives_storage_stats())

@admin_bp.route("/api/admin/archives/clean", methods=["POST"])
def admin_clean_archives():
    try:
        res = clean_orphaned_archives()
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@admin_bp.route("/api/admin/archives/optimize", methods=["POST"])
def admin_optimize_archives():
    try:
        res = optimize_existing_archives()
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@admin_bp.route("/api/admin/archives/prune-images", methods=["POST"])
def admin_prune_images():
    try:
        data = request.json or {}
        days_old = int(data.get("days_old", 60))
        res = prune_aged_archive_images(days_old=days_old)
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@admin_bp.route("/api/admin/denied-urls", methods=["GET"])
def admin_get_denied_urls():
    from services.db import get_db
    conn = get_db()
    conn.execute("CREATE TABLE IF NOT EXISTS denied_urls (url TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    rows = conn.execute("SELECT url, created_at FROM denied_urls ORDER BY created_at DESC").fetchall()
    items = [{"url": r[0], "created_at": str(r[1]) if r[1] else ""} for r in rows]
    return jsonify({"status": "success", "items": items})

@admin_bp.route("/api/admin/denied-urls/delete", methods=["POST"])
def admin_delete_denied_urls():
    from services.db import get_db, retry_write
    data = request.json or {}
    urls = data.get("urls", [])
    if not urls:
        return jsonify({"status": "error", "message": "No URLs specified"}), 400
    
    def _delete():
        conn = get_db()
        conn.execute("CREATE TABLE IF NOT EXISTS denied_urls (url TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.executemany("DELETE FROM denied_urls WHERE url = ?", [(u,) for u in urls])
        conn.commit()
    
    try:
        retry_write(_delete)
        return jsonify({"status": "success", "deleted_count": len(urls)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@admin_bp.route("/api/admin/ai/config", methods=["GET", "POST"])
def ai_config():
    from services.db import get_db, retry_write
    if request.method == "POST":
        data = request.json or {}
        def _save():
            conn = get_db()
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ai_api_key', ?)", (data.get("api_key", ""),))
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ai_base_url', ?)", (data.get("base_url", "https://api.openai.com/v1"),))
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ai_model', ?)", (data.get("model", "gpt-4o-mini"),))
            conn.commit()
        retry_write(_save)
        return jsonify({"status": "success"})
    
    conn = get_db()
    cursor = conn.execute("SELECT key, value FROM settings WHERE key IN ('ai_api_key', 'ai_base_url', 'ai_model')")
    rows = dict(cursor.fetchall())
    return jsonify({
        "api_key": rows.get("ai_api_key", ""),
        "base_url": rows.get("ai_base_url", "https://api.openai.com/v1"),
        "model": rows.get("ai_model", "gpt-4o-mini")
    })

@admin_bp.route("/api/admin/ai/test", methods=["POST"])
def api_test_ai_connection():
    """Test connection, latency, and tool-calling capability of configured AI credentials."""
    data = request.json or {}
    api_key = data.get("api_key", "").strip()
    base_url = data.get("base_url", "https://api.openai.com/v1").strip()
    model = data.get("model", "gpt-4o-mini").strip()
    
    if not api_key:
        return jsonify({"status": "error", "message": "API Key is required to test connection."}), 400
        
    import time
    start_t = time.time()
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=12.0)
        
        # Send a lightweight test completion
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Respond with single word: OK"}],
            max_tokens=5
        )
        latency_ms = int((time.time() - start_t) * 1000)
        reply = resp.choices[0].message.content.strip()
        
        return jsonify({
            "status": "success",
            "message": f"Connected successfully to {model}!",
            "model": model,
            "latency_ms": latency_ms,
            "response": reply
        })
    except Exception as e:
        latency_ms = int((time.time() - start_t) * 1000)
        logger.warning(f"AI test connection error: {e}")
        return jsonify({
            "status": "error",
            "message": str(e),
            "latency_ms": latency_ms
        }), 400


@admin_bp.route("/api/admin/auto-backup/list")
def auto_backup_list():
    return jsonify(list_auto_backups())

@admin_bp.route("/api/admin/auto-backup/run", methods=["POST"])
def auto_backup_run():
    try:
        filename = create_auto_backup()
        return jsonify({"status": "success", "message": "Complete system backup created successfully!", "filename": filename})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@admin_bp.route("/api/admin/auto-backup/download/<filename>")
def auto_backup_download(filename):
    clean_name = secure_filename(filename)
    valid_prefix = clean_name.startswith("linkforge_auto_backup_") or clean_name.startswith("dashforge_auto_backup_")
    valid_ext = clean_name.endswith(".zip") or clean_name.endswith(".csv") or clean_name.endswith(".db")
    if not (valid_prefix and valid_ext):
        return jsonify({"error": "Invalid backup file name"}), 400
    file_path = os.path.join(Config.BACKUP_DIR, clean_name)
    if not os.path.exists(file_path):
        return jsonify({"error": "Backup file not found"}), 404
    return send_from_directory(Config.BACKUP_DIR, clean_name, as_attachment=True)

@admin_bp.route("/api/admin/auto-backup/restore", methods=["POST"])
def auto_backup_restore():
    if 'file' not in request.files:
        return jsonify({"error": "No backup file uploaded"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    try:
        res = restore_backup(file)
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@admin_bp.route("/api/admin/import-bookmarks", methods=["POST"])
def api_admin_import_bookmarks():
    """Import bookmarks from Brave, Chrome, Edge, Firefox, or Safari HTML export."""
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No bookmark HTML file uploaded"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "No selected file"}), 400
        
    filter_dead = request.form.get('filter_dead', 'true').lower() == 'true'
    pin_toolbar = request.form.get('pin_toolbar', 'true').lower() == 'true'
    route_videos = request.form.get('route_videos', 'true').lower() == 'true'
    
    try:
        from services.importer import process_browser_bookmarks_import
        res = process_browser_bookmarks_import(
            file, 
            filter_dead_links=filter_dead, 
            pin_bookmarks_bar=pin_toolbar, 
            route_youtube_videos=route_videos
        )
        return jsonify(res)
    except Exception as e:
        logger.error(f"Bookmark import exception: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

