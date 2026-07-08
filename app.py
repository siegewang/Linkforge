from flask import Flask, render_template, jsonify, request, Response
import docker
import sqlite3
import os
import hashlib
import time
from docker.errors import DockerException
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import datetime
import csv
import io

app = Flask(__name__, template_folder='templates', static_folder='static')
HOST_IP = "192.168.0.77"
DB_PATH = "data/dashboard.db"

def init_db():
    os.makedirs("data", exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
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
        
        try:
            cursor = conn.execute("PRAGMA table_info(config)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'group_name' not in columns: conn.execute("ALTER TABLE config ADD COLUMN group_name TEXT DEFAULT 'Ungrouped'")
            if 'click_count' not in columns: conn.execute("ALTER TABLE config ADD COLUMN click_count INTEGER DEFAULT 0")
        except Exception as e:
            print(f"Migration error (config): {e}")

        conn.execute("""CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE,
            title TEXT,
            description TEXT,
            favicon TEXT,
            tags TEXT,
            is_read INTEGER DEFAULT 0,
            date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        
        try:
            cursor = conn.execute("PRAGMA table_info(links)")
            link_columns = [column[1] for column in cursor.fetchall()]
            if 'click_count' not in link_columns: 
                conn.execute("ALTER TABLE links ADD COLUMN click_count INTEGER DEFAULT 0")
        except Exception as e:
            print(f"Migration error (links): {e}")

        conn.execute("""CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT,
            category TEXT DEFAULT 'note',
            is_done INTEGER DEFAULT 0,
            date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.commit()

init_db()

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn

def retry_write(func, max_retries=3, delay=0.4):
    for attempt in range(max_retries):
        try: return func()
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
                continue
            raise
    raise RuntimeError("Max DB write retries exceeded")

def hash_color(name):
    colors = ["amber","orange","rose","fuchsia","violet","indigo","sky","teal","emerald","lime"]
    h = int(hashlib.md5(name.encode()).hexdigest(), 16)
    return colors[h % len(colors)]

def suggest_icon(name: str) -> str:
    base = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png"
    n = name.lower().replace(" ", "-").replace("_", "-").replace(".", "-")
    return f"{base}/{n}.png"


# ==========================================
# DASHBOARD & MODULAR ADMIN ROUTES
# ==========================================

@app.route("/")
def dashboard(): return render_template("index.html")
@app.route("/settings")
def settings(): return render_template("settings.html")

# The Admin Panel is now routed to its separated template files
@app.route("/admin")
def admin(): return render_template("admin.html", sub_page="apps")
@app.route("/admin/calendar")
def admin_calendar(): return render_template("admin_calendar.html", sub_page="calendar")
@app.route("/admin/data")
def admin_data(): return render_template("admin_data.html", sub_page="data")
@app.route("/admin/health")
def admin_health(): return render_template("admin_health.html", sub_page="health")

@app.route("/api/containers")
def api_containers():
    show_hidden = request.args.get('show_hidden', 'false').lower() == 'true'
    items = []
    client = None
    try: client = docker.from_env(timeout=3)
    except: pass

    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM config").fetchall()
        db_configs = {row['key']: dict(row) for row in rows}

    processed_keys = set()
    if client:
        try:
            for container in client.containers.list():
                name = container.name
                processed_keys.add(name)
                cfg = db_configs.get(name, {})
                if cfg.get('hidden') and not show_hidden: continue
                url = cfg.get('custom_url')
                if not url:
                    labels = container.attrs.get("Config", {}).get("Labels", {}) or {}
                    url = labels.get("dashboard.url")
                if not url:
                    ports = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
                    for p in ports.values():
                        if p: url = f"http://{HOST_IP}:{p[0]['HostPort']}"; break
                items.append({
                    "key": name, "name": name,
                    "title": cfg.get("custom_title") or name.capitalize(),
                    "icon": cfg.get("icon_url") or suggest_icon(name),
                    "url": url or "#", "color_class": cfg.get("custom_color") or f"bg-{hash_color(name)}-500",
                    "hidden": bool(cfg.get("hidden", 0)), "display_order": cfg.get("display_order", 999),
                    "is_custom": 0, "group": cfg.get("group_name", "Ungrouped"),
                    "click_count": cfg.get("click_count") or 0
                })
        except Exception as e: print(f"Docker API error: {e}")

    for key, cfg in db_configs.items():
        if key not in processed_keys and not key.startswith('_group_'):
            if cfg.get('hidden') and not show_hidden: continue
            items.append({
                "key": key, "name": key, "title": cfg.get("custom_title") or "Custom App",
                "icon": cfg.get("icon_url") or "https://picsum.photos/128", "url": cfg.get("custom_url") or "#",
                "color_class": cfg.get("custom_color") or "bg-zinc-700", "hidden": bool(cfg.get("hidden", 0)),
                "display_order": cfg.get("display_order", 999), "is_custom": 1, "group": cfg.get("group_name", "Ungrouped"),
                "click_count": cfg.get("click_count") or 0
            })
    items.sort(key=lambda x: (x["display_order"], x["title"].lower()))
    return jsonify(items)

@app.route("/api/click/<key>", methods=["POST"])
def register_click(key):
    def _write():
        with get_db() as conn:
            conn.execute("UPDATE config SET click_count = COALESCE(click_count, 0) + 1 WHERE key=?", (key,))
            conn.commit()
    retry_write(_write)
    return jsonify({"status": "logged"})

@app.route("/api/groups")
def api_groups():
    with get_db() as conn:
        rows = conn.execute("SELECT DISTINCT group_name FROM config WHERE group_name IS NOT NULL").fetchall()
    return jsonify(sorted(list(set([r[0] for r in rows] + ["Ungrouped"]))))

@app.route("/api/add-group", methods=["POST"])
def add_group():
    name = request.json.get("group_name", "").strip()
    if name:
        def _write():
            with get_db() as conn:
                conn.execute("INSERT OR IGNORE INTO config (key, group_name, is_custom) VALUES (?, ?, 0)", (f"_group_{name}", name))
                conn.commit()
        retry_write(_write)
    return jsonify({"status": "ok"})

@app.route("/api/config/<key>", methods=["POST", "DELETE"])
def handle_config(key):
    if request.method == "DELETE":
        def _del():
            with get_db() as conn:
                conn.execute("DELETE FROM config WHERE key=?", (key,))
                conn.commit()
        retry_write(_del)
        return jsonify({"status": "deleted"})
    data = request.json
    def _write():
        with get_db() as conn:
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

@app.route("/api/save-order", methods=["POST"])
def save_order():
    items = request.json.get("items", [])
    def _write():
        with get_db() as conn:
            for item in items:
                conn.execute("UPDATE config SET group_name=?, display_order=? WHERE key=?", (item["group"], item["order"], item["key"]))
                if conn.total_changes == 0: 
                     conn.execute("INSERT INTO config (key, group_name, display_order) VALUES (?,?,?)", (item["key"], item["group"], item["order"]))
            conn.commit()
    retry_write(_write)
    return jsonify({"status": "saved"})

# ==========================================
# CALENDAR
# ==========================================
@app.route("/api/settings/calendar", methods=["POST"])
def save_calendar_url():
    url = request.json.get("url", "")
    def _write():
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO config (key, custom_url, custom_title, is_custom) VALUES ('_setting_calendar_url', ?, 'Google Calendar URL', 1)", (url,))
            conn.commit()
    retry_write(_write)
    return jsonify({"status": "saved"})

@app.route("/api/calendar")
def api_calendar():
    try: from icalevents.icalevents import events as fetch_ical_events
    except ImportError: return jsonify({"status": "error", "message": "Missing library. Please run: pip install icalevents"})
    with get_db() as conn:
        row = conn.execute("SELECT custom_url FROM config WHERE key='_setting_calendar_url'").fetchone()
        ical_url = row[0] if row else None
    if not ical_url: return jsonify({"status": "setup_required"})
    try:
        now = datetime.datetime.now()
        evs = fetch_ical_events(url=ical_url, start=now, end=now + datetime.timedelta(days=7))
        agenda = [{"summary": e.summary, "start": e.start.isoformat(), "all_day": e.all_day} for e in evs]
        agenda.sort(key=lambda x: x['start'])
        return jsonify({"status": "success", "events": agenda[:6]})
    except Exception as e:
        return jsonify({"status": "error", "message": "Failed to parse calendar."})

# ==========================================
# LINKS & BIN FEATURE MODULE
# ==========================================
def get_tag_swarm():
    with get_db() as conn:
        rows = conn.execute("SELECT tags FROM links WHERE tags IS NOT NULL AND tags != '' AND is_read = 1").fetchall()
    tag_counts = {}
    for row in rows:
        for tag in [t.strip().lower() for t in row[0].split(',') if t.strip()]:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    return dict(sorted(tag_counts.items(), key=lambda item: (-item[1], item[0])))

@app.route("/links")
def links_page():
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        saved_links = conn.execute("SELECT * FROM links WHERE is_read = 0 ORDER BY date_added DESC").fetchall()
    return render_template("links.html", links=saved_links, tag_swarm=get_tag_swarm())

@app.route("/bookmarks")
def bookmarks_page():
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        archived_links = conn.execute("SELECT * FROM links WHERE is_read = 1 ORDER BY date_added DESC").fetchall()
    return render_template("bookmarks.html", links=archived_links, tag_swarm=get_tag_swarm())

@app.route("/bin")
def bin_page():
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        # is_read = 2 represents dead/binned links
        dead_links = conn.execute("SELECT * FROM links WHERE is_read = 2 ORDER BY date_added DESC").fetchall()
    return render_template("bin.html", links=dead_links)

@app.route("/api/links/<int:link_id>/click", methods=["POST"])
def click_link(link_id):
    def _click():
        with get_db() as conn:
            conn.execute("UPDATE links SET click_count = COALESCE(click_count, 0) + 1 WHERE id = ?", (link_id,))
            conn.commit()
    retry_write(_click)
    return jsonify({"status": "logged"})

@app.route("/api/links/top")
def top_links():
    try:
        with get_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM links WHERE click_count > 0 AND is_read != 2 ORDER BY click_count DESC LIMIT 9").fetchall()
            return jsonify([dict(r) for r in rows])
    except Exception as e:
        print(f"API Links Top Error: {e}")
        return jsonify([])

@app.route("/api/links/add", methods=["POST"])
def api_add_link():
    url = request.json.get("url")
    if not url: return jsonify({"error": "No URL provided"}), 400
    title, description, favicon, tags = scrape_url_data(url)
    def _write_link():
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO links (url, title, description, favicon, tags) VALUES (?, ?, ?, ?, ?)", (url, title, description, favicon, tags))
            conn.commit()
    retry_write(_write_link)
    return jsonify({"status": "success", "title": title})

@app.route("/api/links/<int:link_id>", methods=["POST", "DELETE"])
def update_or_delete_link(link_id):
    if request.method == "DELETE":
        def _delete():
            with get_db() as conn:
                conn.execute("DELETE FROM links WHERE id = ?", (link_id,))
                conn.commit()
        retry_write(_delete)
        return jsonify({"status": "deleted"})
    data = request.json
    clean_tags = ",".join([t.strip().lower() for t in data.get("tags", "").split(",") if t.strip()])
    def _update():
        with get_db() as conn:
            conn.execute("UPDATE links SET title = ?, description = ?, tags = ? WHERE id = ?", (data.get("title"), data.get("description"), clean_tags, link_id))
            conn.commit()
    retry_write(_update)
    return jsonify({"status": "updated"})

@app.route("/api/links/<int:link_id>/archive", methods=["POST"])
def archive_link(link_id):
    is_read = (request.json or {}).get("is_read", 1)
    def _archive():
        with get_db() as conn:
            conn.execute("UPDATE links SET is_read = ? WHERE id = ?", (is_read, link_id))
            conn.commit()
    retry_write(_archive)
    return jsonify({"status": "status_changed"})

# NEW: Link Auditor API
@app.route("/api/links/archive/ids")
def archived_link_ids():
    with get_db() as conn:
        # Fetch only items actively in the archive (is_read = 1)
        rows = conn.execute("SELECT id FROM links WHERE is_read = 1").fetchall()
    return jsonify([r[0] for r in rows])

@app.route("/api/links/<int:link_id>/check", methods=["POST"])
def check_link_health(link_id):
    with get_db() as conn:
        link = conn.execute("SELECT url FROM links WHERE id = ?", (link_id,)).fetchone()
    if not link: return jsonify({"status": "error"}), 404
    
    url = link[0]
    is_alive = False
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        resp = requests.head(url, headers=headers, timeout=5, allow_redirects=True)
        # 401, 403, and 405 are often returned by sites blocking automated scripts. We assume alive to be safe.
        if resp.status_code < 400 or resp.status_code in [401, 403, 405]:
            is_alive = True
        else:
            resp_get = requests.get(url, headers=headers, timeout=5, stream=True)
            if resp_get.status_code < 400 or resp_get.status_code in [401, 403, 405]:
                is_alive = True
    except Exception:
        is_alive = False

    if not is_alive:
        def _move_to_bin():
            with get_db() as conn:
                conn.execute("UPDATE links SET is_read = 2 WHERE id = ?", (link_id,))
                conn.commit()
        retry_write(_move_to_bin)
        return jsonify({"status": "dead", "id": link_id, "url": url})

    return jsonify({"status": "alive", "id": link_id})

@app.route("/save")
def save_via_get():
    url = request.args.get("url")
    if not url: return "No URL provided", 400
    title, description, favicon, tags = scrape_url_data(url)
    def _write_link():
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO links (url, title, description, favicon, tags) VALUES (?, ?, ?, ?, ?)", (url, title, description, favicon, tags))
            conn.commit()
    retry_write(_write_link)
    return "<html><body style='background:#09090b; color:#10b981; font-family:sans-serif; display:flex; align-items:center; justify-content:center; height:100vh; margin:0;'><div style='text-align:center; padding:20px; background:#18181b; border:1px solid #27272a; border-radius:15px;'><h2 style='margin-bottom:10px;'>✅ Saved!</h2><script>setTimeout(() => window.close(), 1200);</script></div></body></html>"

@app.route("/api/links/export")
def export_links_csv():
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM links ORDER BY date_added DESC").fetchall()
        
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['ID', 'URL', 'Title', 'Description', 'Favicon', 'Tags', 'Is Archived (1=Yes, 0=No, 2=Bin)', 'Date Added', 'Total Clicks'])
    
    for r in rows:
        cw.writerow([
            r['id'], r['url'], r['title'], r['description'], 
            r['favicon'], r['tags'], r['is_read'], r['date_added'], r['click_count']
        ])
        
    output = si.getvalue()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=dashforge_bookmarks_backup_{timestamp}.csv"}
    )

@app.route("/api/links/import", methods=["POST"])
def import_links_csv():
    if 'file' not in request.files: return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"error": "No selected file"}), 400
    if file and file.filename.endswith('.csv'):
        try:
            stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
            csv_input = csv.reader(stream)
            header = next(csv_input, None)
            def _write_imports():
                with get_db() as conn:
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
                    conn.commit()
            retry_write(_write_imports)
            return jsonify({"status": "success", "message": "CSV imported successfully!"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "Invalid file type."}), 400

def scrape_url_data(url):
    title, description, favicon, auto_tags = url, "", "", []
    domain = urlparse(url).netloc.lower()
    if 'youtube.com' in domain or 'youtu.be' in domain: auto_tags.append('video')
    elif 'reddit.com' in domain: auto_tags.extend(['social', 'reddit'])
    elif 'github.com' in domain: auto_tags.extend(['code', 'github'])
    elif 'news.ycombinator.com' in domain: auto_tags.append('news')
    elif 'twitter.com' in domain or 'x.com' in domain: auto_tags.append('social')
    elif 'medium.com' in domain: auto_tags.append('article')
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        if soup.title: title = soup.title.string.strip()
        desc_tag = soup.find('meta', attrs={'name': 'description'}) or soup.find('meta', attrs={'property': 'og:description'})
        if desc_tag and desc_tag.get('content'): description = desc_tag['content'].strip()
        icon_tag = soup.find('link', rel=lambda x: x and 'icon' in x.lower())
        if icon_tag and icon_tag.get('href'): favicon = urljoin(url, icon_tag['href'])
        else: favicon = f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
    except Exception: pass
    return title, description, favicon, ", ".join(auto_tags)

# ==========================================
# SCRATCHPAD / NOTES MODULE
# ==========================================
@app.route("/notes")
def notes_page(): return render_template("notes.html")

@app.route("/api/notes", methods=["GET", "POST"])
def api_notes():
    if request.method == "GET":
        with get_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM notes ORDER BY is_done ASC, date_added DESC").fetchall()
            return jsonify([dict(r) for r in rows])
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        content = str(data.get("content", "")).strip()
        category = str(data.get("category", "note"))
        if not content: return jsonify({"status": "empty"}), 400
        def _write():
            with get_db() as conn:
                conn.execute("INSERT INTO notes (content, category) VALUES (?, ?)", (content, category))
                conn.commit()
        retry_write(_write)
        return jsonify({"status": "added"})

@app.route("/api/notes/<int:note_id>", methods=["PUT", "DELETE"])
def api_note_action(note_id):
    if request.method == "DELETE":
        def _del():
            with get_db() as conn:
                conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
                conn.commit()
        retry_write(_del)
        return jsonify({"status": "deleted"})
    data = request.get_json(silent=True) or {}
    def _update():
        with get_db() as conn:
            if "is_done" in data: conn.execute("UPDATE notes SET is_done = ? WHERE id = ?", (int(data["is_done"]), note_id))
            conn.commit()
    retry_write(_update)
    return jsonify({"status": "updated"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
