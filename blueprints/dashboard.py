from flask import Blueprint, render_template, jsonify, request, send_from_directory, send_file, current_app
import sqlite3
import datetime
import threading
import logging
from config import Config
from services.db import get_db, retry_write
from services import updater

logger = logging.getLogger(__name__)
dashboard_bp = Blueprint('dashboard', __name__)


@dashboard_bp.route("/favicon.ico")
def favicon():
    return send_from_directory('static', 'favicon.ico', mimetype='image/vnd.microsoft.icon')

@dashboard_bp.route("/")
def dashboard():
    return render_template("index.html", active_page='home')

@dashboard_bp.route("/tutorial")
def tutorial_page():
    return render_template("tutorial.html")

@dashboard_bp.route("/pulse")
def pulse_page():
    return render_template("pulse.html", active_page='pulse')

@dashboard_bp.route("/books")
def books_page():
    conn = get_db()
    site_settings = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    shelfmark_url = site_settings.get('shelfmark_url', 'https://stacks.okapitek.uk/').strip()
    if not shelfmark_url:
        shelfmark_url = 'https://stacks.okapitek.uk/'
    return render_template("books.html", active_page='books', shelfmark_url=shelfmark_url, site_settings=site_settings)

@dashboard_bp.route("/share-target", methods=["GET", "POST"])
def share_target():
    """Endpoint for PWA Web Share Target API on mobile/desktop OS share sheets."""
    import re
    import threading
    from flask import current_app
    
    # Collect incoming params from GET args or POST form/json
    data = {}
    if request.method == "POST":
        data = request.form.to_dict() or request.json or {}
    else:
        data = request.args.to_dict()
        
    raw_url = (data.get("url") or "").strip()
    raw_title = (data.get("title") or "").strip()
    raw_text = (data.get("text") or "").strip()
    
    # Extract URL if embedded within text (common in mobile share intents)
    target_url = raw_url
    if not target_url and raw_text:
        match = re.search(r'https?://[^\s]+', raw_text)
        if match:
            target_url = match.group(0)
    elif not target_url and raw_title:
        match = re.search(r'https?://[^\s]+', raw_title)
        if match:
            target_url = match.group(0)

    # Case 1: Video ingestion (YouTube)
    if target_url and ("youtube.com" in target_url or "youtu.be" in target_url):
        from blueprints.videos import fetch_youtube_oembed
        oembed = fetch_youtube_oembed(target_url) or {}
        vid_title = raw_title or oembed.get("title") or "YouTube Video"
        vid_thumb = oembed.get("thumbnail_url", "")
        vid_channel = oembed.get("channel_name", "")
        
        vid_id = None
        def _write_vid():
            nonlocal vid_id
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO video_bookmarks (url, title, thumbnail_url, channel_name, description)
                VALUES (?, ?, ?, ?, ?)
            """, (target_url, vid_title, vid_thumb, vid_channel, raw_text if raw_text != target_url else ""))
            vid_id = cursor.lastrowid
            conn.commit()
        retry_write(_write_vid)
        
        # Trigger background transcript & routing
        if vid_id:
            def bg_share_vid(app_obj, v_id, u, t, c):
                with app_obj.app_context():
                    from services.scraper import fetch_youtube_transcript_details
                    import json
                    from blueprints.links import auto_route_video_ai
                    c_db = get_db()
                    t_data = fetch_youtube_transcript_details(u)
                    tr = t_data.get("text", "")
                    segs = t_data.get("segments", [])
                    if tr or segs:
                        c_db.execute("UPDATE video_bookmarks SET transcript=?, transcript_json=? WHERE id=?", (tr, json.dumps(segs) if segs else None, v_id))
                        c_db.execute("UPDATE links SET full_text=? WHERE url=?", (tr, u))
                        c_db.commit()
                    auto_route_video_ai(v_id, t, c, u)
            
            app_obj = current_app._get_current_object()
            threading.Thread(target=bg_share_vid, args=(app_obj, vid_id, target_url, vid_title, vid_channel)).start()

        return render_template("share_target.html", item_type="video", item_title=vid_title, item_url=target_url, item_thumb=vid_thumb, item_channel=vid_channel)

    # Case 2: Web Article / Bookmark Ingestion
    elif target_url:
        from services.scraper import scrape_url_data
        from blueprints.links import run_background_enrichment
        
        scraped_title, scraped_desc, scraped_fav, scraped_tags = scrape_url_data(target_url)
        final_title = raw_title or scraped_title or target_url
        
        link_id = None
        def _write_link():
            nonlocal link_id
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO links (url, title, description, favicon, tags, is_read)
                VALUES (?, ?, ?, ?, ?, 1)
            """, (target_url, final_title, scraped_desc, scraped_fav, scraped_tags))
            if cursor.rowcount > 0:
                link_id = cursor.lastrowid
            else:
                row = cursor.execute("SELECT id FROM links WHERE url=?", (target_url,)).fetchone()
                if row: link_id = row[0]
            conn.commit()
        retry_write(_write_link)
        
        if link_id:
            app_obj = current_app._get_current_object()
            def run_with_ctx(a_obj, l_id, u):
                with a_obj.app_context():
                    run_background_enrichment(l_id, u)
            threading.Thread(target=run_with_ctx, args=(app_obj, link_id, target_url)).start()

        return render_template("share_target.html", item_type="link", item_title=final_title, item_url=target_url, item_thumb=scraped_fav)

    # Case 3: Pure text shared -> Save to Scratchpad
    elif raw_text or raw_title:
        note_content = (raw_title + "\n" + raw_text).strip()
        def _write_note():
            conn = get_db()
            conn.execute("INSERT INTO notes (content, category) VALUES (?, 'note')", (note_content,))
            conn.commit()
        retry_write(_write_note)
        
        return render_template("share_target.html", item_type="note", item_title="Note Saved", item_content=note_content)

    return render_template("share_target.html", item_type="link", item_title="No Content Provided", item_url="")


@dashboard_bp.route("/settings")
def settings():
    conn = get_db()
    cursor = conn.execute("SELECT key, value FROM settings WHERE key LIKE 'feature_%'")
    settings_dict = dict(cursor.fetchall())
    version_info = updater.get_version_info()
    return render_template("settings.html", active_page='settings', settings=settings_dict, version_info=version_info)

@dashboard_bp.route("/api/system/version")
def api_system_version():
    return jsonify(updater.get_version_info())

@dashboard_bp.route("/api/system/update", methods=["POST"])
def api_system_update():
    res = updater.apply_git_update()
    return jsonify(res)

@dashboard_bp.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    conn = get_db()
    if request.method == "POST":
        data = request.json or {}
        def _save():
            c = get_db()
            for k, v in data.items():
                c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
            c.commit()
        retry_write(_save)
        return jsonify({"status": "saved"})
    
    cursor = conn.execute("SELECT key, value FROM settings")
    return jsonify(dict(cursor.fetchall()))

@dashboard_bp.route("/api/icons/search")
def search_icons():
    query = request.args.get("q", "").strip().lower()
    app_url = request.args.get("url", "").strip()
    
    icons = []
    base_cdn = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png"
    
    if query:
        clean_slug = query.replace(" ", "-").replace("_", "-").replace(".", "-")
        compact_slug = query.replace(" ", "").replace("_", "").replace(".", "").replace("-", "")
        
        icons.append({"name": f"Dashboard Icon ({clean_slug})", "url": f"{base_cdn}/{clean_slug}.png"})
        if compact_slug != clean_slug:
            icons.append({"name": f"Dashboard Icon ({compact_slug})", "url": f"{base_cdn}/{compact_slug}.png"})
            
        catalog = [
            "prowlarr", "radarr", "sonarr", "lidarr", "readarr", "bazarr", "overseerr", "seerr",
            "plex", "jellyfin", "emby", "tautulli", "immich", "paperless-ngx", "paperless",
            "vaultwarden", "adguard-home", "pi-hole", "home-assistant", "portainer", "nginx-proxy-manager",
            "transmission", "qbittorrent", "deluge", "sabnzbd", "nzbget", "flaresolverr",
            "navidrome", "audiobookshelf", "calibre-web", "homarr", "homepage", "organizr",
            "grafana", "prometheus", "dozzle", "uptime-kuma", "wireguard", "rclone", "syncthing",
            "filebrowser", "glances", "netdata", "vscode", "dashy", "scrutiny", "duplicati"
        ]
        
        for cat_item in catalog:
            if cat_item in query or query in cat_item:
                candidate_url = f"{base_cdn}/{cat_item}.png"
                if candidate_url not in [i["url"] for i in icons]:
                    icons.append({"name": cat_item.replace("-", " ").title(), "url": candidate_url})

    if app_url and app_url != "#":
        try:
            from urllib.parse import urlparse
            domain = urlparse(app_url).netloc
            if domain:
                icons.append({"name": f"Website Favicon ({domain})", "url": f"https://www.google.com/s2/favicons?domain={domain}&sz=128"})
        except Exception:
            pass

    icons.append({"name": "Generic Fallback Icon", "url": f"{base_cdn}/generic.png"})

    return jsonify({"query": query, "icons": icons})


# --- Homepage Bookmark Groups ---

@dashboard_bp.route("/api/homepage-bookmarks")
def api_homepage_bookmarks():
    """Returns all homepage bookmarks grouped by group_name."""
    conn = get_db()
    conn.execute("CREATE TABLE IF NOT EXISTS homepage_groups (name TEXT PRIMARY KEY, display_order INTEGER DEFAULT 999)")
    
    # Ensure all groups from bookmarks exist in homepage_groups
    conn.execute("INSERT OR IGNORE INTO homepage_groups (name) SELECT DISTINCT group_name FROM homepage_bookmarks")
    conn.commit()
    
    # Get all distinct groups ordered by display_order
    groups = {}
    group_rows = conn.execute("SELECT name FROM homepage_groups ORDER BY display_order ASC, name ASC").fetchall()
    
    for row in group_rows:
        groups[row[0]] = []
        
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT hb.id as hb_id, hb.group_name, hb.display_order,
               l.id, l.url, l.title, l.favicon, l.tags, l.description, l.image_url, l.click_count
        FROM homepage_bookmarks hb
        JOIN links l ON hb.link_id = l.id
        ORDER BY hb.group_name, hb.display_order, hb.date_added
    """).fetchall()
    
    for row in rows:
        row = dict(row)
        gname = row['group_name']
        if gname not in groups:
            groups[gname] = []
        groups[gname].append(row)
    
    # Return ordered list of groups to guarantee display_order sequence
    result = []
    for row in group_rows:
        gname = row[0]
        result.append({
            "name": gname,
            "bookmarks": groups.get(gname, [])
        })
    
    return jsonify(result)

@dashboard_bp.route("/api/homepage-groups")
def api_homepage_groups():
    """Returns list of unique homepage group names."""
    conn = get_db()
    conn.execute("CREATE TABLE IF NOT EXISTS homepage_groups (name TEXT PRIMARY KEY, display_order INTEGER DEFAULT 999)")
    conn.execute("INSERT OR IGNORE INTO homepage_groups (name) SELECT DISTINCT group_name FROM homepage_bookmarks")
    conn.commit()
    
    rows = conn.execute("SELECT name FROM homepage_groups ORDER BY display_order ASC, name ASC").fetchall()
    groups = [r[0] for r in rows]
    if not groups:
        groups = ["Ungrouped"]
    return jsonify(groups)

@dashboard_bp.route("/api/homepage-groups/<name>", methods=["DELETE"])
def delete_homepage_group(name):
    """Delete a homepage group."""
    def _write():
        conn = get_db()
        conn.execute("DELETE FROM homepage_groups WHERE name=?", (name,))
        conn.commit()
    retry_write(_write)
    return jsonify({"status": "deleted"})

@dashboard_bp.route("/api/homepage-groups/reorder", methods=["POST"])
def reorder_homepage_groups():
    """Save display order for homepage groups."""
    groups = request.json.get("groups", [])
    def _write():
        conn = get_db()
        conn.execute("CREATE TABLE IF NOT EXISTS homepage_groups (name TEXT PRIMARY KEY, display_order INTEGER DEFAULT 999)")
        for i, name in enumerate(groups):
            name = (name or "").strip()
            if name:
                conn.execute("""
                    INSERT INTO homepage_groups (name, display_order) VALUES (?, ?)
                    ON CONFLICT(name) DO UPDATE SET display_order = ?
                """, (name, i, i))
        conn.commit()
    retry_write(_write)
    return jsonify({"status": "saved"})

@dashboard_bp.route("/api/homepage-groups", methods=["POST"])
def create_homepage_group():
    """Create a new homepage group."""
    name = request.json.get("group_name", "").strip()
    if not name:
        return jsonify({"error": "Group name required"}), 400
        
    def _write():
        conn = get_db()
        conn.execute("CREATE TABLE IF NOT EXISTS homepage_groups (name TEXT PRIMARY KEY, display_order INTEGER DEFAULT 999)")
        conn.execute("INSERT OR IGNORE INTO homepage_groups (name) VALUES (?)", (name,))
        conn.commit()
    retry_write(_write)
    return jsonify({"status": "ok", "group_name": name})

@dashboard_bp.route("/api/homepage-bookmarks/<int:hb_id>", methods=["DELETE"])
def remove_homepage_bookmark(hb_id):
    """Remove a link from the homepage (doesn't delete the link itself)."""
    def _write():
        conn = get_db()
        conn.execute("DELETE FROM homepage_bookmarks WHERE id=?", (hb_id,))
        conn.commit()
    retry_write(_write)
    return jsonify({"status": "removed"})

@dashboard_bp.route("/api/homepage-bookmarks/<int:hb_id>/move", methods=["POST"])
def move_homepage_bookmark(hb_id):
    """Move a bookmark to a different group."""
    group = request.json.get("group", "Ungrouped").strip()
    def _write():
        conn = get_db()
        conn.execute("UPDATE homepage_bookmarks SET group_name=? WHERE id=?", (group, hb_id))
        conn.commit()
    retry_write(_write)
    return jsonify({"status": "moved"})

@dashboard_bp.route("/api/homepage-bookmarks/reorder", methods=["POST"])
def reorder_homepage_bookmarks():
    """Save display order for homepage bookmarks."""
    items = request.json.get("items", [])
    def _write():
        conn = get_db()
        for item in items:
            conn.execute("UPDATE homepage_bookmarks SET group_name=?, display_order=? WHERE id=?",
                         (item["group"], item["order"], item["id"]))
        conn.commit()
    retry_write(_write)
    return jsonify({"status": "saved"})


# --- Legacy config-based groups (kept for admin) ---

@dashboard_bp.route("/api/click/<key>", methods=["POST"])
def register_click(key):
    def _write():
        conn = get_db()
        conn.execute("UPDATE config SET click_count = COALESCE(click_count, 0) + 1 WHERE key=?", (key,))
        conn.commit()
    retry_write(_write)
    return jsonify({"status": "logged"})

@dashboard_bp.route("/api/groups")
def api_groups():
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT group_name FROM config WHERE group_name IS NOT NULL").fetchall()
    return jsonify(sorted(list(set([r[0] for r in rows] + ["Ungrouped"]))))

@dashboard_bp.route("/api/add-group", methods=["POST"])
def add_group():
    name = request.json.get("group_name", "").strip()
    if name:
        def _write():
            conn = get_db()
            conn.execute("INSERT OR IGNORE INTO config (key, group_name, is_custom) VALUES (?, ?, 0)", (f"_group_{name}", name))
            conn.commit()
        retry_write(_write)
    return jsonify({"status": "ok"})

@dashboard_bp.route("/api/config/<key>", methods=["POST", "DELETE"])
def handle_config(key):
    if request.method == "DELETE":
        def _del():
            conn = get_db()
            conn.execute("DELETE FROM config WHERE key=?", (key,))
            conn.commit()
        retry_write(_del)
        return jsonify({"status": "deleted"})
    data = request.json
    def _write():
        conn = get_db()
        conn.execute("""INSERT OR REPLACE INTO config
            (key, custom_title, icon_url, custom_url, custom_color, hidden, display_order, group_name, is_custom, click_count)
            VALUES (?,?,?,?,?,?,?,?,?, COALESCE((SELECT click_count FROM config WHERE key=?), 0))""", (
            key, data.get("custom_title"), data.get("icon_url"), data.get("custom_url"),
            data.get("custom_color"), 1 if data.get("hidden") else 0,
            data.get("display_order", 999), data.get("group_name", "Ungrouped"), data.get("is_custom", 1), key
        ))
        conn.commit()
    retry_write(_write)
    return jsonify({"status": "saved"})

@dashboard_bp.route("/api/save-order", methods=["POST"])
def save_order():
    items = request.json.get("items", [])
    def _write():
        conn = get_db()
        for item in items:
            conn.execute("UPDATE config SET group_name=?, display_order=? WHERE key=?", (item["group"], item["order"], item["key"]))
            if conn.total_changes == 0: 
                conn.execute("INSERT INTO config (key, group_name, display_order) VALUES (?,?,?)", (item["key"], item["group"], item["order"]))
        conn.commit()
    retry_write(_write)
    return jsonify({"status": "saved"})

@dashboard_bp.route("/api/settings/calendar", methods=["POST"])
def save_calendar_url():
    url = request.json.get("url", "")
    def _write():
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO config (key, custom_url, custom_title, is_custom) VALUES ('_setting_calendar_url', ?, 'Google Calendar URL', 1)", (url,))
        conn.commit()
    retry_write(_write)
    return jsonify({"status": "saved"})

@dashboard_bp.route("/api/calendar")
def api_calendar():
    try:
        from icalevents.icalevents import events as fetch_ical_events
    except ImportError:
        return jsonify({"status": "error", "message": "Missing library. Please run: pip install icalevents"})
    
    conn = get_db()
    row = conn.execute("SELECT custom_url FROM config WHERE key='_setting_calendar_url'").fetchone()
    ical_url = row[0] if row else None
    
    if not ical_url:
        return jsonify({"status": "setup_required"})
    try:
        now = datetime.datetime.now()
        evs = fetch_ical_events(url=ical_url, start=now, end=now + datetime.timedelta(days=7))
        agenda = [{"summary": e.summary, "start": e.start.isoformat(), "all_day": e.all_day} for e in evs]
        agenda.sort(key=lambda x: x['start'])
        return jsonify({"status": "success", "events": agenda[:6]})
    except Exception as e:
        logger.error(f"Failed to fetch or parse calendar: {e}")
        return jsonify({"status": "error", "message": "Failed to parse calendar."})


# ================= LINKFORGE PULSE (AI DISCOVER FEED) API =================

@dashboard_bp.route("/api/pulse", methods=["GET"])
def api_get_pulse():
    """Retrieve active pulse items with optional topic filtering."""
    from services.pulse import get_active_pulse_items
    topic = request.args.get("topic")
    limit = int(request.args.get("limit", 30))
    
    conn = get_db()
    topics = conn.execute("SELECT * FROM pulse_topics WHERE is_active=1 ORDER BY display_order ASC").fetchall()
    items = get_active_pulse_items(topic_name=topic, limit=limit)
    
    return jsonify({
        "status": "success",
        "items": items,
        "topics": [dict(t) for t in topics]
    })

@dashboard_bp.route("/api/pulse/refresh", methods=["POST"])
def api_refresh_pulse():
    """Trigger background or immediate pulse feed refresh."""
    from services.pulse import refresh_pulse_feed
    topic_id = request.json.get("topic_id") if request.is_json and request.json else None
    count = refresh_pulse_feed(topic_id=topic_id)
    return jsonify({"status": "success", "added_count": count})

@dashboard_bp.route("/api/pulse/<int:item_id>/forge", methods=["POST"])
def api_forge_pulse_item(item_id):
    """1-click capture from Pulse into Neural Links library with background full-text scraping."""
    from services.pulse import forge_pulse_item_to_library
    res = forge_pulse_item_to_library(item_id)
    return jsonify(res)

@dashboard_bp.route("/api/pulse/<int:item_id>/dismiss", methods=["POST"])
def api_dismiss_pulse_item(item_id):
    """Dismiss an item so it no longer appears in the active Discover feed."""
    from services.pulse import dismiss_pulse_item
    res = dismiss_pulse_item(item_id)
    return jsonify(res)

@dashboard_bp.route("/api/pulse/topics", methods=["GET"])
def api_get_pulse_topics():
    """List all configured pulse topics and custom RSS feeds."""
    conn = get_db()
    topics = conn.execute("SELECT * FROM pulse_topics ORDER BY display_order ASC").fetchall()
    return jsonify({"status": "success", "topics": [dict(t) for t in topics]})

@dashboard_bp.route("/api/pulse/topics/add", methods=["POST"])
def api_add_pulse_topic():
    """Add a new custom topic or custom RSS feed stream."""
    data = request.json or {}
    name = (data.get("name") or "").strip()
    keywords = (data.get("keywords") or "").strip()
    custom_url = (data.get("custom_url") or "").strip()
    feed_type = "rss" if custom_url else "google_news"

    if not name:
        return jsonify({"status": "error", "error": "Topic name required"}), 400

    def _add():
        c = get_db()
        c.execute("""
            INSERT INTO pulse_topics (name, query_keywords, feed_type, custom_feed_url, is_active, display_order)
            VALUES (?, ?, ?, ?, 1, 999)
        """, (name, keywords or name, feed_type, custom_url))
        c.commit()

    try:
        retry_write(_add)
        from services.pulse import refresh_pulse_feed
        threading.Thread(target=refresh_pulse_feed, daemon=True).start()
        return jsonify({"status": "success", "message": "Topic added!"})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 400

@dashboard_bp.route("/api/pulse/topics/<int:topic_id>/delete", methods=["POST"])
def api_delete_pulse_topic(topic_id):
    """Delete a custom topic."""
    def _del():
        c = get_db()
        c.execute("DELETE FROM pulse_topics WHERE id=?", (topic_id,))
        c.commit()
    try:
        retry_write(_del)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 400

@dashboard_bp.route("/api/pulse/topics/auto-discover", methods=["POST"])
def api_auto_discover_pulse_topics():
    """Trigger AI library interest cluster synthesis to auto-generate topics and RSS streams."""
    from services.pulse import auto_synthesize_pulse_topics
    res = auto_synthesize_pulse_topics()
    return jsonify(res)

@dashboard_bp.route("/api/books/search", methods=["GET"])
def api_search_books():
    """Search books across Hardcover, OpenLibrary, and Google Books with Shelfmark deep-links."""
    from services.books import search_books
    q = request.args.get('q', '').strip()
    conn = get_db()
    settings = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    shelfmark_url = settings.get('shelfmark_url', 'https://stacks.okapitek.uk/')
    hardcover_key = settings.get('hardcover_api_key', '').strip()
    results = search_books(q, shelfmark_url, hardcover_key)
    return jsonify({
        "status": "success", 
        "query": q, 
        "books": results, 
        "shelfmark_url": shelfmark_url,
        "hardcover_configured": bool(hardcover_key)
    })

@dashboard_bp.route("/api/books/genres", methods=["GET"])
def api_get_book_genres():
    """Get pre-curated book genre topics."""
    from services.books import get_curated_book_genres
    genres = get_curated_book_genres()
    return jsonify({"status": "success", "genres": genres})

@dashboard_bp.route("/api/books/grab", methods=["POST"])
def api_grab_book():
    """Trigger background automated book grab and streaming acquisition."""
    from services.books import start_book_auto_grab
    data = request.json or {}
    title = (data.get('title') or '').strip()
    author = (data.get('author') or '').strip()
    cover_url = data.get('cover_url')
    key = data.get('key')
    ia_id = data.get('ia_id')

    if not title:
        return jsonify({"status": "error", "error": "Book title is required"}), 400

    res = start_book_auto_grab(title, author, cover_url, key, ia_id)
    return jsonify(res)

@dashboard_bp.route("/api/books/status", methods=["GET"])
def api_books_download_status():
    """Get live download progress across active tasks."""
    from services.books import get_active_downloads_status
    status = get_active_downloads_status()
    return jsonify({"status": "success", "downloads": status})

@dashboard_bp.route("/api/books/library", methods=["GET"])
def api_get_books_library():
    """Get all saved/completed books from personal Book Vault library."""
    from services.books import get_downloaded_books_library
    books = get_downloaded_books_library()
    return jsonify({"status": "success", "books": books})

@dashboard_bp.route("/api/books/download/<int:book_id>", methods=["GET"])
def api_download_book_file(book_id):
    """Serve the downloaded .epub file directly to the browser."""
    import os
    conn = get_db()
    row = conn.execute("SELECT * FROM downloaded_books WHERE id=?", (book_id,)).fetchone()
    if not row or not row['file_path'] or not os.path.exists(row['file_path']):
        return jsonify({"status": "error", "error": "Book file not found on server"}), 404

    filename = os.path.basename(row['file_path'])
    return send_file(
        row['file_path'],
        as_attachment=True,
        download_name=filename,
        mimetype='application/epub+zip'
    )

@dashboard_bp.route("/api/books/download-by-key/<path:key>", methods=["GET"])
def api_download_book_by_key(key):
    """Serve downloaded book by task key."""
    import os
    conn = get_db()
    row = conn.execute("SELECT * FROM downloaded_books WHERE key=?", (key,)).fetchone()
    if not row or not row['file_path'] or not os.path.exists(row['file_path']):
        return jsonify({"status": "error", "error": "Book file not found on server"}), 404

    filename = os.path.basename(row['file_path'])
    return send_file(
        row['file_path'],
        as_attachment=True,
        download_name=filename,
        mimetype='application/epub+zip'
    )

@dashboard_bp.route("/api/books/<int:book_id>/delete", methods=["POST"])
def api_delete_book(book_id):
    """Delete book from local library."""
    from services.books import delete_downloaded_book
    delete_downloaded_book(book_id)
    return jsonify({"status": "success"})

@dashboard_bp.route("/api/system/background-status")
def api_background_status():
    """Return live status of all background worker tasks, progress percentages, and queue lengths."""
    from services.task_queue import get_tasks_status
    return jsonify(get_tasks_status())




