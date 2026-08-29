from flask import Blueprint, render_template, jsonify, request
import sqlite3
import logging
import requests
from services.db import get_db, retry_write

logger = logging.getLogger(__name__)
videos_bp = Blueprint('videos', __name__)


def fetch_youtube_oembed(url):
    """Fetch video metadata from YouTube's free oEmbed API."""
    try:
        resp = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "title": data.get("title", ""),
                "thumbnail_url": data.get("thumbnail_url", ""),
                "channel_name": data.get("author_name", ""),
            }
    except Exception as e:
        logger.warning(f"YouTube oEmbed fetch failed for {url}: {e}")
    return None


@videos_bp.route("/videos")
def video_library():
    return render_template("videos.html", active_page='videos')


# --- Video Bookmarks CRUD ---

@videos_bp.route("/api/videos")
def api_videos():
    """List all video bookmarks, optionally filtered by category or search."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    
    category_id = request.args.get("category_id")
    search = request.args.get("q", "").strip()
    
    query = """
        SELECT v.*, vc.name as category_name, vc.parent_id as category_parent_id,
               pvc.name as parent_category_name
        FROM video_bookmarks v
        LEFT JOIN video_categories vc ON v.category_id = vc.id
        LEFT JOIN video_categories pvc ON vc.parent_id = pvc.id
    """
    params = []
    conditions = []
    
    # If search is present, search across the ENTIRE library (including transcripts)
    if search:
        search_term = f"%{search}%"
        conditions.append("(v.title LIKE ? OR v.tags LIKE ? OR v.channel_name LIKE ? OR v.description LIKE ? OR v.transcript LIKE ?)")
        params.extend([search_term, search_term, search_term, search_term, search_term])
    else:
        # Category filter only applies when not performing a global search
        if category_id == 'uncategorized':
            conditions.append("v.category_id IS NULL")
        elif category_id:
            conditions.append("v.category_id = ?")
            params.append(int(category_id))
    
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    
    query += " ORDER BY v.display_order ASC, v.date_added DESC"
    
    rows = conn.execute(query, tuple(params)).fetchall()
    
    results = []
    for r in rows:
        item = dict(r)
        
        # Build category path
        if item.get("parent_category_name"):
            item["category_path"] = f"{item['parent_category_name']} > {item['category_name']}"
        elif item.get("category_name"):
            item["category_path"] = item["category_name"]
        else:
            item["category_path"] = "New Videos"
            
        # If search query matches in transcript, extract contextual snippet
        if search and item.get("transcript"):
            t = item["transcript"]
            idx = t.lower().find(search.lower())
            if idx != -1:
                start = max(0, idx - 50)
                end = min(len(t), idx + len(search) + 120)
                item["transcript_match"] = "..." + t[start:end].strip() + "..."
                
        results.append(item)
        
    return jsonify(results)


@videos_bp.route("/api/videos", methods=["POST"])
def add_video():
    """Add a new video bookmark. Auto-fetches YouTube metadata via oEmbed."""
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400
    
    # Auto-fetch metadata from YouTube oEmbed
    title = data.get("title", "").strip()
    thumbnail_url = data.get("thumbnail_url", "").strip()
    channel_name = data.get("channel_name", "").strip()
    
    if not title or not thumbnail_url:
        oembed = fetch_youtube_oembed(url)
        if oembed:
            if not title:
                title = oembed["title"]
            if not thumbnail_url:
                thumbnail_url = oembed["thumbnail_url"]
            if not channel_name:
                channel_name = oembed["channel_name"]
    
    def _write():
        conn = get_db()
        conn.execute("""INSERT INTO video_bookmarks 
            (url, title, thumbnail_url, channel_name, duration, description, tags, category_id, display_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            url, title, thumbnail_url, channel_name,
            data.get("duration", ""),
            data.get("description", ""),
            data.get("tags", ""),
            data.get("category_id"),
            data.get("display_order", 999)
        ))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    
    new_id = retry_write(_write)
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    video = dict(conn.execute("SELECT * FROM video_bookmarks WHERE id=?", (new_id,)).fetchone())
    return jsonify({"status": "created", "video": video}), 201


@videos_bp.route("/api/videos/<int:video_id>", methods=["PUT"])
def update_video(video_id):
    """Update a video bookmark."""
    def _write():
        conn = get_db()
        set_clauses = []
        params = []
        valid_keys = ["title", "thumbnail_url", "channel_name", "duration", "description", "tags", "category_id", "display_order", "suggested_category_id", "suggested_category_name", "suggested_reasoning"]
        for key in valid_keys:
            if key in data:
                set_clauses.append(f"{key}=?")
                params.append(data[key])
                
        if set_clauses:
            params.append(video_id)
            query = f"UPDATE video_bookmarks SET {', '.join(set_clauses)} WHERE id=?"
            conn.execute(query, tuple(params))
            
            # If category_id was manually updated, clear suggestion and record learning history
            if "category_id" in data and data["category_id"] is not None:
                v = conn.execute("SELECT title, channel_name FROM video_bookmarks WHERE id=?", (video_id,)).fetchone()
                c = conn.execute("SELECT name, parent_id FROM video_categories WHERE id=?", (data["category_id"],)).fetchone()
                if v and c:
                    p_name = c[0]
                    if c[1]:
                        p_cat = conn.execute("SELECT name FROM video_categories WHERE id=?", (c[1],)).fetchone()
                        if p_cat:
                            p_name = f"{p_cat[0]} > {c[0]}"
                    conn.execute("""
                        INSERT INTO routing_history (channel_name, video_title, chosen_category_id, chosen_category_path)
                        VALUES (?, ?, ?, ?)
                    """, (v[1] or "", v[0] or "", data["category_id"], p_name))
            conn.commit()
    retry_write(_write)
    return jsonify({"status": "updated"})


@videos_bp.route("/api/videos/<int:video_id>", methods=["DELETE"])
def delete_video(video_id):
    """Delete a video bookmark."""
    def _write():
        conn = get_db()
        conn.execute("DELETE FROM video_bookmarks WHERE id=?", (video_id,))
        conn.commit()
    retry_write(_write)
    return jsonify({"status": "deleted"})


@videos_bp.route("/api/videos/<int:video_id>/accept-suggestion", methods=["POST"])
def accept_video_suggestion(video_id):
    """Accept AI category suggestion and move video out of New Videos."""
    def _accept():
        conn = get_db()
        conn.row_factory = sqlite3.Row
        v = conn.execute("SELECT id, title, channel_name, suggested_category_id, suggested_category_name FROM video_bookmarks WHERE id=?", (video_id,)).fetchone()
        if not v or not v['suggested_category_id']:
            return False
            
        target_cat_id = v['suggested_category_id']
        cat_path = v['suggested_category_name']
        
        conn.execute("""
            UPDATE video_bookmarks 
            SET category_id = ?, suggested_category_id = NULL 
            WHERE id = ?
        """, (target_cat_id, video_id))
        
        # Log to routing learning history
        conn.execute("""
            INSERT INTO routing_history (channel_name, video_title, chosen_category_id, chosen_category_path)
            VALUES (?, ?, ?, ?)
        """, (v['channel_name'] or "", v['title'] or "", target_cat_id, cat_path or ""))
        
        conn.commit()
        return True
        
    success = retry_write(_accept)
    return jsonify({"status": "accepted" if success else "no_suggestion"})


@videos_bp.route("/api/videos/accept-all-suggestions", methods=["POST"])
def accept_all_suggestions():
    """Accept all pending AI category suggestions for videos in New Videos."""
    def _accept_all():
        conn = get_db()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, title, channel_name, suggested_category_id, suggested_category_name 
            FROM video_bookmarks 
            WHERE category_id IS NULL AND suggested_category_id IS NOT NULL
        """).fetchall()
        
        count = 0
        for r in rows:
            conn.execute("UPDATE video_bookmarks SET category_id = ?, suggested_category_id = NULL WHERE id = ?", (r['suggested_category_id'], r['id']))
            conn.execute("""
                INSERT INTO routing_history (channel_name, video_title, chosen_category_id, chosen_category_path)
                VALUES (?, ?, ?, ?)
            """, (r['channel_name'] or "", r['title'] or "", r['suggested_category_id'], r['suggested_category_name'] or ""))
            count += 1
            
        conn.commit()
        return count
        
    total = retry_write(_accept_all)
    return jsonify({"status": "success", "accepted_count": total})


@videos_bp.route("/api/videos/<int:video_id>/move", methods=["POST"])
def move_video(video_id):
    """Move a video to a different category and learn from the user's action."""
    category_id = request.json.get("category_id")
    def _write():
        conn = get_db()
        conn.execute("UPDATE video_bookmarks SET category_id=?, suggested_category_id=NULL WHERE id=?", (category_id, video_id))
        
        if category_id is not None:
            v = conn.execute("SELECT title, channel_name FROM video_bookmarks WHERE id=?", (video_id,)).fetchone()
            c = conn.execute("SELECT name, parent_id FROM video_categories WHERE id=?", (category_id,)).fetchone()
            if v and c:
                p_name = c[0]
                if c[1]:
                    p_cat = conn.execute("SELECT name FROM video_categories WHERE id=?", (c[1],)).fetchone()
                    if p_cat:
                        p_name = f"{p_cat[0]} > {c[0]}"
                conn.execute("""
                    INSERT INTO routing_history (channel_name, video_title, chosen_category_id, chosen_category_path)
                    VALUES (?, ?, ?, ?)
                """, (v[1] or "", v[0] or "", category_id, p_name))
        conn.commit()
    retry_write(_write)
    return jsonify({"status": "moved"})


@videos_bp.route("/api/video-categories")
def api_video_categories():
    """Return all categories as a tree structure with video counts."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT vc.*, 
               (SELECT COUNT(*) FROM video_bookmarks vb WHERE vb.category_id = vc.id) as video_count
        FROM video_categories vc 
        ORDER BY vc.display_order, vc.name
    """).fetchall()
    cats = [dict(r) for r in rows]
    
    # Build tree: main categories (parent_id IS NULL) with children
    main_cats = [c for c in cats if c["parent_id"] is None]
    for mc in main_cats:
        mc["children"] = [c for c in cats if c["parent_id"] == mc["id"]]
        mc["total_video_count"] = mc["video_count"] + sum(child.get("video_count", 0) for child in mc["children"])
    
    return jsonify(main_cats)


@videos_bp.route("/api/video-categories", methods=["POST"])
def create_video_category():
    """Create a new category (main or sub)."""
    data = request.json
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    
    parent_id = data.get("parent_id")
    
    def _write():
        conn = get_db()
        conn.execute("INSERT INTO video_categories (name, parent_id, display_order) VALUES (?, ?, ?)",
                     (name, parent_id, data.get("display_order", 999)))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    
    new_id = retry_write(_write)
    return jsonify({"status": "created", "id": new_id, "name": name, "parent_id": parent_id}), 201


@videos_bp.route("/api/video-categories/<int:cat_id>", methods=["PUT"])
def update_video_category(cat_id):
    """Rename or reorder a category."""
    data = request.json
    def _write():
        conn = get_db()
        conn.execute("UPDATE video_categories SET name=?, display_order=? WHERE id=?",
                     (data.get("name"), data.get("display_order", 999), cat_id))
        conn.commit()
    retry_write(_write)
    return jsonify({"status": "updated"})


@videos_bp.route("/api/video-categories/<int:cat_id>", methods=["DELETE"])
def delete_video_category(cat_id):
    """Delete a category. Videos in it become uncategorized."""
    def _write():
        conn = get_db()
        # Uncategorize videos in this category
        conn.execute("UPDATE video_bookmarks SET category_id=NULL WHERE category_id=?", (cat_id,))
        # Move sub-categories up to main level
        conn.execute("UPDATE video_categories SET parent_id=NULL WHERE parent_id=?", (cat_id,))
        conn.execute("DELETE FROM video_categories WHERE id=?", (cat_id,))
        conn.commit()
    retry_write(_write)
    return jsonify({"status": "deleted"})


# --- YouTube oEmbed proxy ---

@videos_bp.route("/api/videos/oembed")
def oembed_lookup():
    """Proxy endpoint to fetch YouTube metadata for a URL."""
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    
    result = fetch_youtube_oembed(url)
    if result:
        return jsonify(result)
    return jsonify({"error": "Could not fetch metadata"}), 404
