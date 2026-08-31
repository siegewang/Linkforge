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
    
    # Trigger background transcript extraction and AI auto-routing if enabled
    import threading
    def bg_enrich_video(v_id, v_url, v_title, v_channel):
        import sqlite3
        import json
        from config import Config
        conn = sqlite3.connect(Config.DB_PATH, timeout=30)
        try:
            settings = dict(conn.execute("SELECT key, value FROM settings WHERE key LIKE 'feature_%'").fetchall())
            master_on = settings.get("feature_smart_ingestion_master") != '0'
            if master_on:
                if settings.get("feature_yt_transcript_fetch") != '0':
                    from services.scraper import fetch_youtube_transcript_details
                    t_data = fetch_youtube_transcript_details(v_url)
                    transcript = t_data.get("text", "")
                    segments = t_data.get("segments", [])
                    if transcript or segments:
                        conn.execute("UPDATE video_bookmarks SET transcript=?, transcript_json=? WHERE id=?", (transcript, json.dumps(segments) if segments else None, v_id))
                        conn.execute("UPDATE links SET full_text=? WHERE url=?", (transcript, v_url))
                        conn.commit()
                if settings.get("feature_ai_auto_route") != '0':
                    from blueprints.links import auto_route_video_ai
                    auto_route_video_ai(v_id, v_title, v_channel, v_url)
        except Exception as e:
            logger.debug(f"Background video enrichment error: {e}")
        finally:
            conn.close()
            
    threading.Thread(target=bg_enrich_video, args=(new_id, url, title, channel_name)).start()

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


@videos_bp.route("/api/videos/<int:video_id>/re-fetch-transcript", methods=["POST"])
def refetch_video_transcript(video_id):
    """Re-fetch closed captions / transcripts with timestamped segments."""
    conn = get_db()
    row = conn.execute("SELECT url FROM video_bookmarks WHERE id=?", (video_id,)).fetchone()
    if not row or not row[0]:
        return jsonify({"error": "Video not found"}), 404
        
    url = row[0]
    import json
    from services.scraper import fetch_youtube_transcript_details
    
    t_data = fetch_youtube_transcript_details(url)
    transcript = t_data.get("text", "")
    segments = t_data.get("segments", [])
    
    if not transcript and not segments:
        err_msg = t_data.get("error_msg", "No closed captions or transcript available for this video.")
        err_code = t_data.get("error", "no_transcript")
        status_code = 429 if err_code == "rate_limited" else 404
        return jsonify({"error": err_msg, "error_type": err_code}), status_code
        
    def _save():
        c = get_db()
        c.execute("UPDATE video_bookmarks SET transcript=?, transcript_json=? WHERE id=?", (transcript, json.dumps(segments) if segments else None, video_id))
        c.execute("UPDATE links SET full_text=? WHERE url=?", (transcript, url))
        c.commit()
    retry_write(_save)
    
    return jsonify({
        "status": "success",
        "transcript": transcript,
        "segments": segments
    })


@videos_bp.route("/api/videos/<int:video_id>/ai-chapters", methods=["POST"])
def generate_ai_chapters(video_id):
    """Generate structured AI chapters with timestamps and key takeaways from transcript."""
    import json
    from openai import OpenAI
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    video = conn.execute("SELECT * FROM video_bookmarks WHERE id=?", (video_id,)).fetchone()
    if not video:
        return jsonify({"error": "Video not found"}), 404
        
    req_data = request.json or {}
    force_regenerate = req_data.get("regenerate", False)
    
    # If cached and not regenerating, return cached chapters
    if video["ai_chapters"] and not force_regenerate:
        try:
            cached = json.loads(video["ai_chapters"])
            return jsonify({"status": "cached", "data": cached})
        except Exception:
            pass

    transcript = video["transcript"] or ""
    transcript_json = video["transcript_json"]
    segments = []
    if transcript_json:
        try:
            segments = json.loads(transcript_json)
        except Exception:
            pass

    # If segments missing or empty, fetch transcript details with real timestamps
    if not segments:
        from services.scraper import fetch_youtube_transcript_details
        t_data = fetch_youtube_transcript_details(video["url"])
        fetched_text = t_data.get("text", "")
        segments = t_data.get("segments", [])
        if fetched_text or segments:
            transcript = fetched_text or transcript
            def _save_t():
                c = get_db()
                c.execute("UPDATE video_bookmarks SET transcript=?, transcript_json=? WHERE id=?", (transcript, json.dumps(segments) if segments else None, video_id))
                c.commit()
            retry_write(_save_t)

    if not transcript and not segments:
        return jsonify({"error": "No transcript available for this video to generate chapters."}), 400

    # Determine maximum duration from segments or transcript
    max_duration_sec = 0
    if segments:
        last_seg = segments[-1]
        max_duration_sec = int(last_seg.get("start", 0) + last_seg.get("duration", 10))
    elif video["duration"]:
        try:
            parts = [int(p) for p in video["duration"].split(":")]
            if len(parts) == 2:
                max_duration_sec = parts[0] * 60 + parts[1]
            elif len(parts) == 3:
                max_duration_sec = parts[0] * 3600 + parts[1] * 60 + parts[2]
        except Exception:
            pass

    if max_duration_sec <= 0:
        max_duration_sec = 7200 # 2 hour upper safety bound

    m_max, s_max = divmod(max_duration_sec, 60)
    h_max, m_max = divmod(m_max, 60)
    max_dur_str = f"{h_max:02d}:{m_max:02d}:{s_max:02d}" if h_max > 0 else f"{m_max:02d}:{s_max:02d}"

    # Get AI Configuration
    settings = dict(conn.execute("SELECT key, value FROM settings WHERE key IN ('ai_api_key', 'ai_base_url', 'ai_model')").fetchall())
    api_key = settings.get("ai_api_key", "").strip()
    if not api_key:
        return jsonify({"error": "AI not configured. Please set your AI API key in Admin/Settings."}), 400

    base_url = settings.get("ai_base_url", "https://api.openai.com/v1").strip()
    model = settings.get("ai_model", "gpt-4o-mini").strip()

    # Format transcript with timestamps (sample uniformly if very long)
    formatted_lines = []
    if segments:
        # If segments > 200, sample every 1-2 segments to fit context window cleanly
        step = 1 if len(segments) <= 250 else 2
        for s in segments[::step]:
            sec = int(s.get("start", 0))
            m, sec_rem = divmod(sec, 60)
            h, m = divmod(m, 60)
            ts = f"{h:02d}:{m:02d}:{sec_rem:02d}" if h > 0 else f"{m:02d}:{sec_rem:02d}"
            formatted_lines.append(f"[{ts}] {s.get('text', '')}")
    else:
        formatted_lines.append(transcript[:15000])

    transcript_payload = "\n".join(formatted_lines)[:24000]

    system_prompt = f"""You are an expert video analyst. Analyze the timestamped transcript of a video and generate structured chapters and key takeaways.
Return ONLY valid JSON matching this exact schema:
{{
  "summary": "2-3 sentence overview summarizing the core topic and key conclusions of the video.",
  "key_takeaways": [
    "Key takeaway point 1",
    "Key takeaway point 2",
    "Key takeaway point 3"
  ],
  "chapters": [
    {{
      "timestamp": "00:00",
      "seconds": 0,
      "title": "<Concise descriptive title of topic starting at 00:00>"
    }},
    {{
      "timestamp": "MM:SS",
      "seconds": 120,
      "title": "<Concise descriptive title of next topic transition>"
    }}
  ]
}}

CRITICAL ACCURACY RULES:
1. Total Video Length: {max_dur_str} ({max_duration_sec} seconds).
2. Every chapter timestamp MUST strictly be an actual timestamp from the provided transcript and MUST NOT exceed {max_dur_str} (seconds must be <= {max_duration_sec}).
3. Chapter titles MUST be specific to what is actually discussed in this video. Do not use generic placeholders.
4. Provide between 4 to 8 natural chapter transitions."""

    user_prompt = f"""Video Title: {video['title'] or 'Untitled Video'}
Channel: {video['channel_name'] or 'Unknown'}
Total Duration: {max_dur_str}
URL: {video['url']}

Timestamped Transcript:
{transcript_payload}"""

    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"}
        )
        raw_content = response.choices[0].message.content
        result_json = json.loads(raw_content)

        # Sanitize and validate chapters against video duration
        valid_chapters = []
        for ch in result_json.get("chapters", []):
            sec = int(ch.get("seconds", 0))
            if sec <= max_duration_sec + 5: # allow 5 sec tolerance
                sec = min(sec, max_duration_sec)
                m, s_rem = divmod(sec, 60)
                h, m = divmod(m, 60)
                ts = f"{h:02d}:{m:02d}:{s_rem:02d}" if h > 0 else f"{m:02d}:{s_rem:02d}"
                valid_chapters.append({
                    "seconds": sec,
                    "timestamp": ts,
                    "title": ch.get("title", "").strip()
                })
        
        valid_chapters.sort(key=lambda c: c["seconds"])
        result_json["chapters"] = valid_chapters

        # Save to database
        def _save_chapters():
            c = get_db()
            c.execute("UPDATE video_bookmarks SET ai_chapters=? WHERE id=?", (json.dumps(result_json), video_id))
            c.commit()
        retry_write(_save_chapters)
        
        return jsonify({"status": "success", "data": result_json})
    except Exception as e:
        logger.error(f"AI Chapter generation error for video {video_id}: {e}")
        return jsonify({"error": f"Failed to generate chapters: {str(e)}"}), 500

