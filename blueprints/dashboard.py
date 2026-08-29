from flask import Blueprint, render_template, jsonify, request
import sqlite3
import datetime
import logging
from config import Config
from services.db import get_db, retry_write

logger = logging.getLogger(__name__)
dashboard_bp = Blueprint('dashboard', __name__)


@dashboard_bp.route("/")
def dashboard():
    return render_template("index.html", active_page='home')

@dashboard_bp.route("/tutorial")
def tutorial_page():
    return render_template("tutorial.html")

@dashboard_bp.route("/settings")
def settings():
    conn = get_db()
    cursor = conn.execute("SELECT key, value FROM settings WHERE key LIKE 'feature_%'")
    settings_dict = dict(cursor.fetchall())
    return render_template("settings.html", active_page='settings', settings=settings_dict)

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
