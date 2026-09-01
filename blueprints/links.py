from flask import Blueprint, render_template, jsonify, request, Response
import os
import sqlite3
import datetime
import csv
import io
import logging
from services.db import get_db, retry_write
from services.scraper import scrape_url_data, check_link_alive, find_link_mirrors

logger = logging.getLogger(__name__)
links_bp = Blueprint('links', __name__)

def get_tag_swarm():
    conn = get_db()
    rows = conn.execute("SELECT tags FROM links WHERE tags IS NOT NULL AND tags != ''").fetchall()
    v_rows = conn.execute("SELECT tags FROM video_bookmarks WHERE tags IS NOT NULL AND tags != ''").fetchall()
    tag_counts = {}
    for row in (rows + v_rows):
        for tag in [t.strip().lower() for t in row[0].split(',') if t.strip()]:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    
    return dict(sorted(tag_counts.items(), key=lambda item: (-item[1], item[0])))



@links_bp.route("/bookmarks")
def bookmarks_page():
    conn = get_db()
    conn.row_factory = sqlite3.Row
    archived_links = conn.execute("SELECT * FROM links ORDER BY date_added DESC").fetchall()
    swarm = get_tag_swarm()
    top_tags = dict(list(swarm.items())[:25])
    return render_template("bookmarks.html", links=archived_links, tag_swarm=swarm, top_tags=top_tags, active_page='bookmarks')


@links_bp.route("/api/links/<int:link_id>/click", methods=["POST"])
def click_link(link_id):
    def _click():
        conn = get_db()
        conn.execute("UPDATE links SET click_count = COALESCE(click_count, 0) + 1 WHERE id = ?", (link_id,))
        conn.commit()
    retry_write(_click)
    return jsonify({"status": "logged"})

@links_bp.route("/api/links/top")
def top_links():
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM links WHERE click_count > 0 ORDER BY click_count DESC LIMIT 9").fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        logger.error(f"API Links Top Error: {e}")
        return jsonify([])

@links_bp.route("/api/links/add", methods=["POST"])
def api_add_link():
    url = request.json.get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    title, description, favicon, tags = scrape_url_data(url)
    
    link_id = None
    def _write_link():
        nonlocal link_id
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO links (url, title, description, favicon, tags) VALUES (?, ?, ?, ?, ?)", (url, title, description, favicon, tags))
        # If it was inserted, start the thread
        if cursor.rowcount > 0:
            link_id = cursor.lastrowid
        else:
            # Get existing ID if we ignored insert
            row = cursor.execute("SELECT id FROM links WHERE url = ?", (url,)).fetchone()
            if row:
                link_id = row[0]
        conn.commit()
    retry_write(_write_link)
    
    if link_id:
        import threading
        # We need the app context for get_db in the thread
        from flask import current_app
        app = current_app._get_current_object()
        
        def run_with_context(app, l_id, u):
            with app.app_context():
                run_background_enrichment(l_id, u)
                
        threading.Thread(target=run_with_context, args=(app, link_id, url)).start()
        
    return jsonify({"status": "success", "title": title})

@links_bp.route("/api/links/cleanup-archives", methods=["POST"])
def cleanup_archives():
    import glob, re, os
    conn = get_db()
    valid_paths = [r[0] for r in conn.execute("SELECT archive_path FROM links WHERE archive_path IS NOT NULL AND archive_path != ''").fetchall()]
    valid_html_filenames = set(os.path.basename(p) for p in valid_paths)
    archives_dir = os.path.join("data", "archives")
    images_dir = os.path.join(archives_dir, "images")
    
    deleted_html = 0
    valid_html_files = []
    for hf in glob.glob(os.path.join(archives_dir, "*.html")):
        if os.path.basename(hf) not in valid_html_filenames:
            try: os.remove(hf)
            except: pass
            deleted_html += 1
        else:
            valid_html_files.append(hf)
            
    referenced_images = set()
    img_pattern = re.compile(r"src=[\"\'\']/archives/images/([^\"\'\']+)[\"\'\']")
    for hf in valid_html_files:
        try:
            with open(hf, "r", encoding="utf-8") as f:
                matches = img_pattern.findall(f.read())
                for m in matches: referenced_images.add(m)
        except: pass
        
    deleted_images = 0
    for imf in glob.glob(os.path.join(images_dir, "*")):
        if os.path.basename(imf) not in referenced_images:
            try: os.remove(imf)
            except: pass
            deleted_images += 1
            
    return jsonify({"status": "success", "deleted_html": deleted_html, "deleted_images": deleted_images})

@links_bp.route("/api/links/<int:link_id>", methods=["POST", "DELETE"])
def update_or_delete_link(link_id):
    if request.method == "DELETE":
        def _delete():
            conn = get_db()
            row = conn.execute("SELECT archive_path FROM links WHERE id = ?", (link_id,)).fetchone()
            conn.execute("DELETE FROM links WHERE id = ?", (link_id,))
            conn.commit()
            
            if row and row[0]:
                import re, shutil
                path_val = row[0]
                if path_val.endswith("/index.html"):
                    parts = path_val.split("/")
                    if len(parts) >= 4:
                        folder = os.path.join("data", "archives", parts[2])
                        if os.path.exists(folder):
                            try: shutil.rmtree(folder)
                            except: pass
                else:
                    archive_path = os.path.join("data", "archives", os.path.basename(path_val))
                    if os.path.exists(archive_path):
                        try:
                            with open(archive_path, "r", encoding="utf-8") as f:
                                file_content = f.read()
                            img_pattern = re.compile(r"src=[\"\'\']/archives/images/([^\"\'\']+)[\"\'\']")
                            for img_name in img_pattern.findall(file_content):
                                img_path = os.path.join("data", "archives", "images", img_name)
                                if os.path.exists(img_path):
                                    os.remove(img_path)
                            os.remove(archive_path)
                        except Exception as e:
                            pass
        retry_write(_delete)
        return jsonify({"status": "deleted"})
    data = request.json
    clean_tags = ",".join([t.strip().lower() for t in data.get("tags", "").split(",") if t.strip()])
    new_url = data.get("url")
    def _update():
        conn = get_db()
        if new_url:
            conn.execute("UPDATE links SET url = ?, title = ?, description = ?, tags = ? WHERE id = ?", (new_url, data.get("title"), data.get("description"), clean_tags, link_id))
        else:
            conn.execute("UPDATE links SET title = ?, description = ?, tags = ? WHERE id = ?", (data.get("title"), data.get("description"), clean_tags, link_id))
        conn.commit()
    retry_write(_update)
    return jsonify({"status": "updated"})


def auto_route_link_ai(link_id, title, summary, tags, url):
    """Classify link into the best matching homepage group using AI."""
    import json
    from openai import OpenAI
    
    conn = get_db()
    try:
        settings = dict(conn.execute("SELECT key, value FROM settings WHERE key IN ('feature_smart_ingestion_master', 'feature_ai_auto_route', 'ai_api_key', 'ai_base_url', 'ai_model')").fetchall())
        if settings.get("feature_smart_ingestion_master") == '0' or settings.get("feature_ai_auto_route") == '0':
            return
            
        api_key = settings.get("ai_api_key", "")
        if not api_key:
            return
            
        groups = [r[0] for r in conn.execute("SELECT name FROM homepage_groups UNION SELECT group_name FROM homepage_bookmarks").fetchall() if r[0] and r[0] != "Ungrouped" and r[0] != "Pinned Extensions"]
        if not groups:
            return
            
        client = OpenAI(api_key=api_key, base_url=settings.get("ai_base_url", "https://api.openai.com/v1"))
        prompt = f"""You are organizing bookmarks for a personal knowledge dashboard.
Link Title: {title}
Summary: {summary}
Tags: {tags}
URL: {url}

Available User Groups:
{json.dumps(groups)}

Choose the single best matching group from the list. Return ONLY valid JSON with key "group_name" (e.g. {{"group_name": "News"}} or null if none fit)."""

        resp = client.chat.completions.create(
            model=settings.get("ai_model", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        res = json.loads(resp.choices[0].message.content)
        matched_group = res.get("group_name")
        if matched_group and matched_group in groups:
            def _update_group():
                c = get_db()
                c.execute("UPDATE homepage_bookmarks SET group_name = ? WHERE link_id = ?", (matched_group, link_id))
                c.commit()
            retry_write(_update_group)
    except Exception as e:
        logger.debug(f"Auto-route link error: {e}")


def auto_route_video_ai(video_id, title, channel, url):
    """Classify YouTube video into the best video library category with learning memory."""
    import sqlite3
    import json
    from config import Config
    from openai import OpenAI
    
    conn = sqlite3.connect(Config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        settings = dict(conn.execute("SELECT key, value FROM settings WHERE key IN ('feature_smart_ingestion_master', 'feature_ai_auto_route', 'feature_video_routing_mode', 'ai_api_key', 'ai_base_url', 'ai_model')").fetchall())
        if settings.get("feature_smart_ingestion_master") == '0' or settings.get("feature_ai_auto_route") == '0':
            return
            
        api_key = settings.get("ai_api_key", "")
        if not api_key:
            return
            
        rows = conn.execute("SELECT id, name, parent_id FROM video_categories").fetchall()
        if not rows:
            return
            
        cat_by_id = {r['id']: r for r in rows}
        def get_path(cat_id):
            curr = cat_by_id.get(cat_id)
            if not curr: return ''
            if curr['parent_id'] and curr['parent_id'] in cat_by_id:
                return get_path(curr['parent_id']) + ' > ' + curr['name']
            return curr['name']

        category_paths = {get_path(r['id']): r['id'] for r in rows}
        path_list = list(category_paths.keys())
        
        # 1. Fast Path: Check if user previously mapped this channel directly
        matched_cat_id = None
        matched_path = None
        reasoning = ""
        
        if channel:
            prev = conn.execute("""
                SELECT chosen_category_id, chosen_category_path, count(*) as cnt 
                FROM routing_history 
                WHERE LOWER(channel_name) = LOWER(?) 
                GROUP BY chosen_category_id 
                ORDER BY cnt DESC LIMIT 1
            """, (channel.strip(),)).fetchone()
            if prev and prev['chosen_category_id'] in cat_by_id:
                matched_cat_id = prev['chosen_category_id']
                matched_path = get_path(matched_cat_id)
                reasoning = f"Learned from previous videos from channel '{channel}'"

        # 2. If no direct channel rule, prompt the AI with full hierarchy and few-shot history
        if not matched_cat_id:
            # Grab recent history examples for few-shot learning
            history_rows = conn.execute("SELECT channel_name, video_title, chosen_category_path FROM routing_history ORDER BY id DESC LIMIT 10").fetchall()
            history_text = ""
            if history_rows:
                history_lines = [f"- Channel '{h['channel_name'] or 'Unknown'}' / Title '{h['video_title']}' -> Categorized as '{h['chosen_category_path']}'" for h in history_rows]
                history_text = "User's Past Categorization Decisions (Learn patterns from these):\n" + "\n".join(history_lines) + "\n\n"

            client = OpenAI(api_key=api_key, base_url=settings.get("ai_base_url", "https://api.openai.com/v1"))
            prompt = f"""You are organizing YouTube videos into a personal knowledge library.
Video Title: {title}
Channel: {channel}
URL: {url}

Available Categories (full paths):
{json.dumps(path_list)}

{history_text}Return ONLY valid JSON with two keys:
1. "category_path": The exact matching category path from the list above (or null if none fit). Prefer specific subcategories if relevant.
2. "reasoning": A brief 1-sentence explanation of why it fits this category."""

            resp = client.chat.completions.create(
                model=settings.get("ai_model", "gpt-4o-mini"),
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            res = json.loads(resp.choices[0].message.content)
            chosen_path = res.get("category_path")
            reasoning = res.get("reasoning", "")
            
            if chosen_path and chosen_path in category_paths:
                matched_cat_id = category_paths[chosen_path]
                matched_path = chosen_path

        # 3. Apply Suggestion or Instant Route based on settings
        if matched_cat_id:
            routing_mode = settings.get("feature_video_routing_mode", "suggest")
            if routing_mode == "instant":
                conn.execute("""
                    UPDATE video_bookmarks 
                    SET category_id = ?, suggested_category_id = ?, suggested_category_name = ?, suggested_reasoning = ? 
                    WHERE id = ?
                """, (matched_cat_id, matched_cat_id, matched_path, reasoning, video_id))
            else:
                # Suggest mode: Keep in "New Videos" (category_id = NULL) with suggestion badge!
                conn.execute("""
                    UPDATE video_bookmarks 
                    SET suggested_category_id = ?, suggested_category_name = ?, suggested_reasoning = ? 
                    WHERE id = ?
                """, (matched_cat_id, matched_path, reasoning, video_id))
            conn.commit()
    except Exception as e:
        logger.debug(f"Auto-route video error: {e}")
    finally:
        conn.close()


def run_background_enrichment(link_id, url, raw_text=None, html_content=""):
    from bs4 import BeautifulSoup
    import requests
    from openai import OpenAI
    import json
    from services.scraper import fetch_full_article_text
    
    try:
        import sqlite3
        from config import Config
        c_config = sqlite3.connect(Config.DB_PATH, timeout=30)
        settings = dict(c_config.execute("SELECT key, value FROM settings").fetchall())
        c_config.close()
        
        config = {
            "api_key": settings.get("ai_api_key", ""),
            "base_url": settings.get("ai_base_url", "https://api.openai.com/v1"),
            "model": settings.get("ai_model", "gpt-4o-mini")
        }

        # Check master switch
        master_enabled = settings.get("feature_smart_ingestion_master") != '0'
        full_text_enabled = settings.get("feature_full_text_fetch") != '0' and master_enabled
        auto_route_enabled = settings.get("feature_ai_auto_route") != '0' and master_enabled

        full_article_text = ""
        if full_text_enabled:
            full_article_text = fetch_full_article_text(url, html_content)

        if raw_text:
            text = (full_article_text or raw_text)[:4000]
        elif full_article_text:
            text = full_article_text[:4000]
        else:
            try:
                res = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                soup = BeautifulSoup(res.text, "html.parser")
                text = soup.get_text(separator=' ', strip=True)[:4000]
            except Exception:
                text = ""

        if not master_enabled or not config["api_key"]:
            def _set_read():
                import sqlite3
                from config import Config
                c = sqlite3.connect(Config.DB_PATH, timeout=30)
                try:
                    c.execute("UPDATE links SET is_read = 1, full_text = ? WHERE id = ?", (full_article_text or text, link_id))
                    c.commit()
                finally:
                    c.close()
            retry_write(_set_read)
            return
            
        client = OpenAI(api_key=config["api_key"], base_url=config["base_url"])
        prompt = f"Analyze this webpage text and return a JSON object with exactly two keys: 'summary' (a concise 1-2 sentence description) and 'tags'. 'tags' MUST be a comma-separated string of STRICTLY NO MORE THAN 3 lowercase tags. Webpage text: {text}"
        prompt += "\n\nReturn ONLY valid JSON. No markdown blocks."
        
        try:
            response = client.chat.completions.create(
                model=config["model"],
                messages=[{"role": "user", "content": prompt}]
            )
            raw_content = response.choices[0].message.content
            start_idx = raw_content.find('{')
            end_idx = raw_content.rfind('}')
            if start_idx != -1 and end_idx != -1:
                raw_content = raw_content[start_idx:end_idx+1]
            result = json.loads(raw_content)
            summary = result.get("summary", "")
            tags = result.get("tags", "")
        except Exception as ai_e:
            logger.error(f"AI Enrichment failed: {ai_e}")
            summary = ""
            tags = "uncategorized"
            
        archive_path = ""
        if html_content and 'youtube.com' not in url.lower() and 'youtu.be' not in url.lower():
            try:
                import os
                import uuid
                from urllib.parse import urljoin
                from bs4 import BeautifulSoup
                import requests
                from PIL import Image
                # import pillow_avif # removed to avoid container rebuild
                
                soup = BeautifulSoup(html_content, "html.parser")
                archive_name = f"archive_{link_id}"
                specific_archive_dir = os.path.join("data", "archives", archive_name)
                os.makedirs(specific_archive_dir, exist_ok=True)
                
                # --- ADVANCED PYTHON READABILITY ---
                import os
                import uuid
                import re
                from urllib.parse import urljoin
                from bs4 import BeautifulSoup
                import requests
                from PIL import Image
                
                soup = BeautifulSoup(html_content, "html.parser")
                archive_name = f"archive_{link_id}"
                specific_archive_dir = os.path.join("data", "archives", archive_name)
                os.makedirs(specific_archive_dir, exist_ok=True)
                
                # 1. Decompose absolute junk
                for junk in soup(['script', 'style', 'link', 'meta', 'nav', 'footer', 'aside', 'header', 'iframe', 'svg', 'form', 'button', 'input', 'dialog']):
                    junk.decompose()
                
                # 2. Pick a broad container so we don't lose images
                main_node = soup.find('article') or soup.find('main') or soup.find(id=re.compile(r'content|main|article', re.I)) or soup.body or soup
                if main_node and len(main_node.get_text(strip=True)) < 50:
                    main_node = soup.body
                    
                # 3. Clean up the extracted main node
                for tag in main_node.find_all(True):
                    # Remove all styling attributes to force clean layout
                    for attr in ['class', 'id', 'style', 'width', 'height', 'bgcolor', 'color']:
                        if tag.has_attr(attr): del tag[attr]
                
                # 4. Remove empty structural tags and UI spam (like "View comments 13")
                for tag in main_node.find_all(True):
                    if tag.name in ['img', 'figure', 'picture', 'video', 'source']:
                        continue
                    txt = tag.get_text(strip=True).lower()
                    
                    # Remove tiny UI spam nodes
                    if len(txt) < 100:
                        if re.search(r'(view comments|comments|share this|tweet|subscribe|newsletter|follow us|read more|advertisement|sign up)', txt):
                            tag.decompose()
                            continue
                        if txt.isdigit(): # Random numbers like "144" from comment counts
                            tag.decompose()
                            continue
                
                # Remove empty containers
                for tag in main_node.find_all(['div', 'span', 'p', 'a', 'ul', 'li', 'section']):
                    if len(tag.get_text(strip=True)) == 0 and not tag.find(['img', 'picture', 'video']):
                        tag.decompose()

                # 5. Process remaining images
                def get_best_image_url(img_tag):
                    if img_tag.parent and img_tag.parent.name == 'picture':
                        for source in img_tag.parent.find_all('source'):
                            for attr in ['data-srcset', 'srcset', 'src']:
                                if source.get(attr):
                                    url = source.get(attr).split(',')[-1].strip().split(' ')[0]
                                    if url and not url.startswith('data:'): return url
                    for attr in ['data-src', 'data-lazy-src', 'data-original', 'src']:
                        if img_tag.get(attr) and not img_tag.get(attr).startswith('data:'):
                            return img_tag.get(attr)
                    for attr in ['data-srcset', 'srcset']:
                        if img_tag.get(attr):
                            url = img_tag.get(attr).split(',')[-1].strip().split(' ')[0]
                            if url and not url.startswith('data:'): return url
                    return None

                for img in main_node.find_all('img'):
                    src = get_best_image_url(img)
                    if not src: 
                        img.decompose()
                        continue
                    
                    img_url = urljoin(url, src)
                    try:
                        res = requests.get(img_url, stream=True, timeout=5, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36", 'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8'})
                        if res.status_code == 200:
                            img_id = str(uuid.uuid4())[:8] + ".webp"
                            local_img_path = os.path.join(specific_archive_dir, img_id)
                            
                            i = Image.open(res.raw)
                            # Filter out tiny icons or tracking pixels
                            if i.width < 150 or i.height < 100:
                                img.decompose()
                                continue
                                
                            # Downscale oversized images to optimal reading width (max 900px)
                            max_dim = 900
                            if i.width > max_dim or i.height > max_dim:
                                i.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
                                
                            if i.mode != 'RGB': 
                                i = i.convert('RGB')
                            i.save(local_img_path, format='WEBP', quality=65, method=6)
                            
                            img.attrs = {}
                            img['src'] = f"/archives/{archive_name}/{img_id}"
                        else:
                            img['src'] = img_url
                    except Exception as e:
                        img['src'] = img_url
                        
                page_title = soup.title.string if soup.title else url
                
                html_filepath = os.path.join(specific_archive_dir, "index.html")
                
                # Final aggressive cleanup of empty tags and double spacing
                for tag in main_node.find_all(['p', 'div', 'span', 'section']):
                    if not tag.get_text(strip=True) and not tag.find(['img', 'picture', 'video', 'iframe']):
                        tag.decompose()
                
                # Remove consecutive <br> tags
                for br in main_node.find_all('br'):
                    next_s = br.next_sibling
                    while next_s and isinstance(next_s, str) and not next_s.strip():
                        next_s = next_s.next_sibling
                    if next_s and next_s.name == 'br':
                        next_s.decompose()

                reader_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Archived: {page_title}</title>
    <style>
        :root {{
            --bg: #121212;
            --text: #e0e0e0;
            --muted: #888888;
            --link: #4fd1c5;
            --border: #333333;
        }}
        body {{
            background-color: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            line-height: 1.8;
            margin: 0;
            padding: 0;
            font-size: 19px;
        }}
        .reader-container {{
            max-width: 760px;
            margin: 0 auto;
            padding: 50px 20px;
        }}
        .reader-header {{
            margin-bottom: 50px;
            padding-bottom: 30px;
            border-bottom: 1px solid var(--border);
            text-align: center;
        }}
        .reader-header h1 {{
            font-size: 36px;
            margin-bottom: 15px;
            line-height: 1.2;
            font-weight: 800;
        }}
        .reader-header .meta {{
            color: var(--muted);
            font-size: 15px;
        }}
        .reader-header a {{
            color: var(--link);
            text-decoration: none;
        }}
        .reader-content {{
            font-size: 19px;
        }}
        .reader-content *:empty:not(img):not(br):not(hr) {{
            display: none !important;
        }}
        .reader-content img {{
            display: block;
            max-width: 100%;
            height: auto;
            border-radius: 8px;
            margin: 40px auto;
        }}
        .reader-content a {{ color: var(--link); text-decoration: underline; }}
        .reader-content p {{ margin-bottom: 25px; }}
        .reader-content h1, .reader-content h2, .reader-content h3 {{ margin-top: 50px; margin-bottom: 20px; font-weight: 700; }}
        .reader-content blockquote {{
            border-left: 4px solid var(--border);
            padding-left: 20px;
            color: var(--muted);
            margin: 30px 0;
            font-style: italic;
        }}
        .reader-content ul, .reader-content ol {{ margin-bottom: 25px; padding-left: 30px; }}
        .reader-content li {{ margin-bottom: 10px; }}
    </style>
</head>
<body>
    <div class="reader-container">
        <div class="reader-header">
            <h1>{page_title}</h1>
            <div class="meta">
                Archived from <a href="{url}" target="_blank">{url}</a>
            </div>
        </div>
        <div class="reader-content">
            {str(main_node)}
        </div>
    </div>
</body>
</html>"""
                with open(html_filepath, "w", encoding="utf-8") as f:
                    f.write(reader_html)
                    
                archive_path = f"/archives/{archive_name}/index.html"
            except Exception as e:
                logger.error(f"Archiving failed: {e}")
                with open("error.txt", "w") as err_file:
                    err_file.write(str(e))

        def _update():
            import sqlite3
            from config import Config
            c = sqlite3.connect(Config.DB_PATH, timeout=30)
            try:
                current = c.execute("SELECT description, tags FROM links WHERE id = ?", (link_id,)).fetchone()
                if current:
                    c.execute("UPDATE links SET description = ?, tags = ?, is_read = 1, archive_path = ?, full_text = ? WHERE id = ?", (summary, tags, archive_path, full_article_text or text, link_id))
                c.commit()
            finally:
                c.close()
        retry_write(_update)
    except Exception as e:
        logger.error(f"Background AI Enrich Error for {url}: {e}")
        with open("outer_error.txt", "w") as f:
            f.write(str(e))

@links_bp.route("/api/links/<int:link_id>/ai-enrich", methods=["POST"])
def ai_enrich_link(link_id):
    conn = get_db()
    link = conn.execute("SELECT url FROM links WHERE id = ?", (link_id,)).fetchone()
    if not link:
        return jsonify({"error": "Link not found"}), 404
        
    url = link[0]
    run_background_enrichment(link_id, url)
    
    # Return current state since the background thread updates it
    link_data = conn.execute("SELECT description, tags FROM links WHERE id = ?", (link_id,)).fetchone()
    return jsonify({"status": "processing"})


@links_bp.route("/archives/<path:filename>")
def serve_archive(filename):
    import os
    from flask import send_from_directory
    return send_from_directory(os.path.join('data', 'archives'), filename)

@links_bp.route("/api/debug-path", methods=["GET"])
def debug_path():
    import os
    from config import Config
    return jsonify({
        "cwd": os.getcwd(),
        "db_path": os.path.abspath(Config.DB_PATH),
        "files_in_data": os.listdir("data") if os.path.exists("data") else []
    })

@links_bp.route("/api/links/denied", methods=["GET", "OPTIONS"])
def list_denied_urls():
    if request.method == "OPTIONS":
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type"
        }
        return "", 200, headers
    try:
        conn = get_db()
        conn.execute("CREATE TABLE IF NOT EXISTS denied_urls (url TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        rows = conn.execute("SELECT url FROM denied_urls ORDER BY created_at DESC").fetchall()
        urls = [r[0] for r in rows if r[0]]
    except Exception as e:
        logger.error(f"Error fetching denied URLs: {e}")
        urls = []
    
    headers = {"Access-Control-Allow-Origin": "*"}
    return jsonify({"status": "success", "denied_urls": urls}), 200, headers

@links_bp.route("/api/links/deny", methods=["POST", "OPTIONS"])
def deny_url_logging():
    if request.method == "OPTIONS":
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type"
        }
        return "", 200, headers

    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    def _save_denied():
        conn = get_db()
        conn.execute("CREATE TABLE IF NOT EXISTS denied_urls (url TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("INSERT OR IGNORE INTO denied_urls (url) VALUES (?)", (url,))
        conn.commit()
    
    try:
        retry_write(_save_denied)
    except Exception as e:
        logger.error(f"Failed to record denied URL {url}: {e}")

    headers = {"Access-Control-Allow-Origin": "*"}
    return jsonify({"status": "success", "denied_url": url}), 200, headers

@links_bp.route("/api/links/undeny", methods=["POST", "OPTIONS"])
def undeny_url_logging():
    if request.method == "OPTIONS":
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type"
        }
        return "", 200, headers

    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    def _delete_denied():
        conn = get_db()
        conn.execute("CREATE TABLE IF NOT EXISTS denied_urls (url TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("DELETE FROM denied_urls WHERE url = ?", (url,))
        conn.commit()
    
    try:
        retry_write(_delete_denied)
    except Exception as e:
        logger.error(f"Failed to remove denied URL {url}: {e}")

    headers = {"Access-Control-Allow-Origin": "*"}
    return jsonify({"status": "success", "removed_url": url}), 200, headers

@links_bp.route("/api/links/auto-log", methods=["POST", "OPTIONS"])
def auto_log_link():
    if request.method == "OPTIONS":
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type"
        }
        return "", 200, headers
        
    data = request.json or {}
    url = data.get("url")

    # Check if URL was explicitly denied
    if url:
        try:
            conn = get_db()
            conn.execute("CREATE TABLE IF NOT EXISTS denied_urls (url TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            is_denied = conn.execute("SELECT 1 FROM denied_urls WHERE url = ?", (url.split('#')[0],)).fetchone()
            if is_denied:
                return jsonify({"status": "ignored", "reason": "URL explicitly aborted by user"}), 200, {"Access-Control-Allow-Origin": "*"}
        except Exception:
            pass
    
    # DEBUG LOGGING to see if the endpoint is reached
    try:
        with open("data/debug_log.txt", "a") as f:
            f.write(f"Hit auto-log with URL: {url}\n")
    except:
        pass
        
    title = data.get("title", "")
    text = data.get("text", "")
    html_content = data.get("html", "")
    favicon = data.get("favicon", "")
    image_url = data.get("image_url", "")
    
    if not url:
        return jsonify({"error": "No URL provided"}), 400
        
    # Automatically route YouTube videos to the new Video Library
    if "youtube.com/watch" in url or "youtu.be/" in url:
        from blueprints.videos import fetch_youtube_oembed
        meta = fetch_youtube_oembed(url) or {}
        def _insert_vid():
            conn = get_db()
            existing = conn.execute("SELECT id FROM video_bookmarks WHERE url = ?", (url,)).fetchone()
            if existing:
                return existing[0], False
            
            # Use oEmbed metadata if available, otherwise fallback to extension metadata
            vid_title = meta.get("title") or title
            vid_thumb = meta.get("thumbnail_url") or image_url
            vid_channel = meta.get("channel_name", "")
            
            conn.execute("""
                INSERT OR IGNORE INTO video_bookmarks 
                (url, title, thumbnail_url, channel_name, date_added) 
                VALUES (?, ?, ?, ?, datetime('now'))
            """, (url, vid_title, vid_thumb, vid_channel))
            
            row = conn.execute("SELECT id FROM video_bookmarks WHERE url = ?", (url,)).fetchone()
            vid_id = row[0] if row else conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            
            # Also add it to the generic links table so it appears in the Timeline & Search
            # We set is_read=1 so it skips background enrichment
            conn.execute("""
                INSERT OR REPLACE INTO links (url, title, favicon, image_url, is_read, date_added, click_count, tags) 
                VALUES (?, ?, ?, ?, 1, datetime('now'), 0, ?)
            """, (url, vid_title, favicon, vid_thumb, "youtube,video"))
            
            conn.commit()
            return vid_id, (existing is None)
            
        try:
            vid_id, is_new = retry_write(_insert_vid)
            
            def bg_youtube_process(v_id, u, t, c):
                import sqlite3
                from config import Config
                conn = sqlite3.connect(Config.DB_PATH, timeout=30)
                try:
                    settings = dict(conn.execute("SELECT key, value FROM settings WHERE key LIKE 'feature_%'").fetchall())
                    master_on = settings.get("feature_smart_ingestion_master") != '0'
                    if master_on:
                        if settings.get("feature_yt_transcript_fetch") != '0':
                            import json
                            from services.scraper import fetch_youtube_transcript_details
                            t_data = fetch_youtube_transcript_details(u)
                            transcript = t_data.get("text", "")
                            segments = t_data.get("segments", [])
                            if transcript or segments:
                                conn.execute("UPDATE video_bookmarks SET transcript=?, transcript_json=? WHERE id=?", (transcript, json.dumps(segments) if segments else None, v_id))
                                conn.execute("UPDATE links SET full_text=? WHERE url=?", (transcript, u))
                                conn.commit()
                        if settings.get("feature_ai_auto_route") != '0':
                            auto_route_video_ai(v_id, t, c, u)
                except Exception as e:
                    logger.debug(f"Background YouTube process error: {e}")
                finally:
                    conn.close()

            import threading
            threading.Thread(target=bg_youtube_process, args=(vid_id, url, meta.get("title") or title, meta.get("channel_name", ""))).start()

            resp = jsonify({"status": "logged", "id": vid_id, "is_new": is_new, "type": "video"})
            resp.headers.add("Access-Control-Allow-Origin", "*")
            return resp
        except Exception as e:
            logger.error(f"YouTube auto-log error: {e}")
            resp = jsonify({"error": str(e)})
            resp.headers.add("Access-Control-Allow-Origin", "*")
            return resp, 500
        
    def _insert():
        conn = get_db()
        # Check if already exists
        existing = conn.execute("SELECT id FROM links WHERE url = ?", (url,)).fetchone()
        if existing:
            return existing[0], False
            
        conn.execute("INSERT INTO links (url, title, favicon, image_url, is_read, archive_path, date_added) VALUES (?, ?, ?, ?, 0, '', datetime('now'))", (url, title, favicon, image_url))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0], True
        
    try:
        link_id, is_new = retry_write(_insert)
        if is_new:
            # Spin up background thread passing the raw text
            import threading
            threading.Thread(target=run_background_enrichment, args=(link_id, url, text, html_content)).start()
            
        resp = jsonify({"status": "logged", "id": link_id, "is_new": is_new})
        resp.headers.add("Access-Control-Allow-Origin", "*")
        return resp
    except Exception as e:
        logger.error(f"Auto-log error: {e}")
        resp = jsonify({"error": str(e)})
        resp.headers.add("Access-Control-Allow-Origin", "*")
        return resp, 500

@links_bp.route("/api/links/ai-search", methods=["POST"])
def ai_search_links():
    query = request.json.get("query", "").strip()
    selected_tags = request.json.get("tags", [])
    
    if not query and not selected_tags:
        return jsonify({"error": "No search query or tags provided"}), 400
        
    conn = get_db()
    conn.row_factory = sqlite3.Row
    links_rows = conn.execute("SELECT id, title, description, tags, url, favicon, full_text FROM links").fetchall()
    vids_rows = conn.execute("SELECT id, title, description, tags, url, thumbnail_url, transcript, channel_name FROM video_bookmarks").fetchall()
    
    all_candidates = []
    
    # 1. Add web links
    for l in links_rows:
        all_candidates.append({
            "id": f"link_{l['id']}",
            "raw_id": l['id'],
            "type": "link",
            "title": l['title'] or l['url'],
            "desc": l['description'] or "",
            "tags": l['tags'] or "",
            "url": l['url'],
            "favicon": l['favicon'] or "",
            "full_text": l['full_text'] or ""
        })
        
    # 2. Add video bookmarks
    for v in vids_rows:
        all_candidates.append({
            "id": f"video_{v['id']}",
            "raw_id": v['id'],
            "type": "video",
            "title": v['title'] or v['url'],
            "desc": v['description'] or f"YouTube Video by {v['channel_name'] or 'Channel'}",
            "tags": v['tags'] or "",
            "url": v['url'],
            "favicon": v['thumbnail_url'] or "https://www.youtube.com/s/desktop/favicon.ico",
            "full_text": v['transcript'] or ""
        })
        
    # Filter by selected tags (if any)
    filtered_candidates = []
    for c in all_candidates:
        c_tags = [t.strip().lower() for t in (c["tags"] or "").split(",") if t.strip()]
        if selected_tags:
            if not all(t.lower() in c_tags for t in selected_tags):
                continue
        filtered_candidates.append(c)
        
    if not filtered_candidates:
        return jsonify({"matches": [], "full_data": {}})
        
    # If no search query, return tag-filtered results directly with 100% relevance
    if not query:
        matches = [{"id": c["id"], "relevance": 100, "reasoning": "Matched selected tags."} for c in filtered_candidates]
        full_data = {c["id"]: c for c in filtered_candidates}
        return jsonify({"matches": matches, "full_data": full_data})
        
    # Calculate intelligent hybrid keyword/semantic score
    query_lower = query.lower()
    query_keywords = [w.lower() for w in query.split() if len(w) >= 2]
    
    def score_candidate(item):
        s = 0
        t_low = (item.get("title") or "").lower()
        tags_low = (item.get("tags") or "").lower()
        desc_low = (item.get("desc") or "").lower()
        ft_low = (item.get("full_text") or "").lower()
        url_low = (item.get("url") or "").lower()
        
        # Exact full phrase matches
        if query_lower in t_low:
            s += 80
        if query_lower in tags_low:
            s += 50
        if query_lower in desc_low:
            s += 35
        if query_lower in url_low:
            s += 30
            
        # Individual keyword matches
        for kw in query_keywords:
            if kw in t_low:
                s += 30
            if kw in tags_low:
                s += 20
            if kw in desc_low:
                s += 12
            if kw in ft_low:
                s += 10
            if kw in url_low:
                s += 8
                
        return s

    for c in filtered_candidates:
        c["_score"] = score_candidate(c)
        
    # Filter only candidates that have positive relevance
    matched_candidates = [c for c in filtered_candidates if c["_score"] > 0]
    matched_candidates.sort(key=lambda x: x["_score"], reverse=True)
    
    # If no positive matches, return empty
    if not matched_candidates:
        return jsonify({"matches": [], "full_data": {}})
        
    # Check if AI is configured for conceptual LLM re-ranking
    import json
    settings_conn = get_db()
    rows = dict(settings_conn.execute("SELECT key, value FROM settings WHERE key IN ('ai_api_key', 'ai_base_url', 'ai_model')").fetchall())
    api_key = rows.get("ai_api_key", "").strip()
    base_url = rows.get("ai_base_url", "https://api.openai.com/v1").strip()
    model = rows.get("ai_model", "gpt-4o-mini").strip()
    
    # If AI key is configured and query is complex, perform LLM semantic re-ranking
    if api_key and len(query.split()) > 2:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url)
            
            top_for_ai = matched_candidates[:35]
            ai_input_data = []
            for item in top_for_ai:
                snippet = item["desc"] or item["full_text"][:250] or ""
                ai_input_data.append({
                    "id": item["id"],
                    "type": item["type"],
                    "title": item["title"],
                    "snippet": snippet[:200],
                    "tags": item["tags"]
                })
                
            prompt = f"""Given the user search query: "{query}"
Evaluate the following items (web links and videos) and return a JSON object with a single key 'matches' containing an array of objects for the items that match the query.
Each object MUST have:
- "id": (string) the exact item ID
- "relevance": (integer 0-100) match score
- "reasoning": (string) a concise 1-sentence reason why it matched.

Items:
{json.dumps(ai_input_data)}"""

            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            ai_res = json.loads(resp.choices[0].message.content)
            llm_matches = ai_res.get("matches", [])
            if llm_matches:
                llm_matches.sort(key=lambda x: x.get("relevance", 0), reverse=True)
                full_data = {c["id"]: c for c in matched_candidates}
                return jsonify({"matches": llm_matches, "full_data": full_data})
        except Exception as ai_err:
            logger.warning(f"LLM Search fallback to fast local matching: {ai_err}")

    # High-precision local fallback matching (Works 100% with 0ms latency even without AI API key)
    max_score = max(c["_score"] for c in matched_candidates) if matched_candidates else 1
    local_matches = []
    for c in matched_candidates[:60]:
        rel_percent = min(100, max(30, int((c["_score"] / max_score) * 98)))
        match_type_label = "YouTube Video" if c["type"] == "video" else "Web Link"
        reason = f"Exact match in {match_type_label} title & tags." if c["_score"] >= 40 else f"Relevant {match_type_label} keyword match."
        local_matches.append({
            "id": c["id"],
            "relevance": rel_percent,
            "reasoning": reason
        })
        
    full_data = {c["id"]: c for c in matched_candidates}
    return jsonify({"matches": local_matches, "full_data": full_data})



@links_bp.route("/api/ai/ask-library", methods=["POST"])
def ask_library_rag():
    """Unified Semantic RAG across links, video transcripts, and scratchpad notes."""
    data = request.json or {}
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"error": "No question or query provided"}), 400

    sources = data.get("sources", ["links", "videos", "notes"])
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    
    # Check AI Config
    settings = dict(conn.execute("SELECT key, value FROM settings WHERE key IN ('ai_api_key', 'ai_base_url', 'ai_model')").fetchall())
    api_key = settings.get("ai_api_key", "").strip()
    if not api_key:
        return jsonify({"error": "AI not configured. Please add your API key in Settings or Admin."}), 400

    base_url = settings.get("ai_base_url", "https://api.openai.com/v1").strip()
    model = settings.get("ai_model", "gpt-4o-mini").strip()

    import json
    import re
    query_keywords = [w.lower() for w in re.findall(r'\b\w+\b', query) if len(w) > 2]
    
    collected_candidates = []

    # 1. Search Links / Articles
    if "links" in sources:
        links = conn.execute("SELECT id, title, description, tags, url, favicon, full_text FROM links").fetchall()
        for l in links:
            t_low = (l["title"] or "").lower()
            d_low = (l["description"] or "").lower()
            tags_low = (l["tags"] or "").lower()
            ft_low = (l["full_text"] or "").lower()
            
            score = 0
            for kw in query_keywords:
                if kw in t_low: score += 20
                if kw in tags_low: score += 12
                if kw in d_low: score += 8
                if kw in ft_low: score += 6
                
            if score > 0 or not query_keywords:
                # Find best snippet
                snippet = l["description"] or ""
                if l["full_text"]:
                    ft = l["full_text"]
                    best_idx = -1
                    for kw in query_keywords:
                        idx = ft.lower().find(kw)
                        if idx != -1:
                            best_idx = idx
                            break
                    if best_idx != -1:
                        start = max(0, best_idx - 60)
                        end = min(len(ft), best_idx + 240)
                        snippet = ("..." if start > 0 else "") + ft[start:end].strip() + "..."
                
                collected_candidates.append({
                    "score": score,
                    "type": "link",
                    "id": l["id"],
                    "title": l["title"] or "Untitled Link",
                    "url": l["url"],
                    "tags": l["tags"] or "",
                    "snippet": snippet[:350]
                })

    # 2. Search Videos & Transcripts
    if "videos" in sources:
        videos = conn.execute("SELECT id, title, channel_name, tags, url, transcript, transcript_json, ai_chapters FROM video_bookmarks").fetchall()
        for v in videos:
            t_low = (v["title"] or "").lower()
            c_low = (v["channel_name"] or "").lower()
            tags_low = (v["tags"] or "").lower()
            tr_low = (v["transcript"] or "").lower()
            
            score = 0
            for kw in query_keywords:
                if kw in t_low: score += 20
                if kw in tags_low: score += 12
                if kw in c_low: score += 10
                if kw in tr_low: score += 8

            if score > 0 or not query_keywords:
                best_timestamp_str = ""
                best_seconds = 0
                snippet = v["title"] or ""
                
                # Check timestamped segments if available
                if v["transcript_json"]:
                    try:
                        segs = json.loads(v["transcript_json"])
                        for s in segs:
                            s_text = s.get("text", "")
                            if any(kw in s_text.lower() for kw in query_keywords):
                                best_seconds = int(s.get("start", 0))
                                m, s_rem = divmod(best_seconds, 60)
                                h, m = divmod(m, 60)
                                best_timestamp_str = f"{h:02d}:{m:02d}:{s_rem:02d}" if h > 0 else f"{m:02d}:{s_rem:02d}"
                                snippet = s_text
                                break
                    except Exception:
                        pass
                
                if not best_timestamp_str and v["transcript"]:
                    tr = v["transcript"]
                    best_idx = -1
                    for kw in query_keywords:
                        idx = tr.lower().find(kw)
                        if idx != -1:
                            best_idx = idx
                            break
                    if best_idx != -1:
                        start = max(0, best_idx - 60)
                        end = min(len(tr), best_idx + 240)
                        snippet = ("..." if start > 0 else "") + tr[start:end].strip() + "..."
                    else:
                        snippet = tr[:250]

                collected_candidates.append({
                    "score": score,
                    "type": "video",
                    "id": v["id"],
                    "title": v["title"] or "Untitled Video",
                    "channel": v["channel_name"] or "YouTube",
                    "url": v["url"],
                    "timestamp": best_timestamp_str,
                    "seconds": best_seconds,
                    "tags": v["tags"] or "",
                    "snippet": snippet[:350]
                })

    # 3. Search Scratchpad Notes
    if "notes" in sources:
        notes = conn.execute("SELECT id, content, category, date_added FROM notes").fetchall()
        for n in notes:
            c_low = (n["content"] or "").lower()
            score = 0
            for kw in query_keywords:
                if kw in c_low: score += 15
            if score > 0:
                collected_candidates.append({
                    "score": score,
                    "type": "note",
                    "id": n["id"],
                    "title": f"Scratchpad Note #{n['id']}",
                    "content": n["content"],
                    "category": n["category"] or "note",
                    "snippet": (n["content"] or "")[:300]
                })

    # Sort candidates by relevance
    collected_candidates.sort(key=lambda x: x["score"], reverse=True)
    top_candidates = collected_candidates[:12]

    if not top_candidates:
        return jsonify({
            "status": "success",
            "answer": f"I searched your library for **\"{query}\"**, but couldn't find any matching articles, video transcripts, or scratchpad notes. Try adding related bookmarks or videos first!",
            "sources": []
        })

    # Build RAG Context Block
    context_lines = []
    for idx, c in enumerate(top_candidates, 1):
        if c["type"] == "link":
            context_lines.append(f"[Source {idx} - Article] Title: {c['title']} | URL: {c['url']} | Tags: {c['tags']}\nContent Excerpt: {c['snippet']}")
        elif c["type"] == "video":
            ts_info = f" at {c['timestamp']}" if c.get("timestamp") else ""
            context_lines.append(f"[Source {idx} - Video] Title: {c['title']} | Channel: {c.get('channel', 'YouTube')} | URL: {c['url']}{ts_info}\nSpoken Dialogue / Excerpt: {c['snippet']}")
        elif c["type"] == "note":
            context_lines.append(f"[Source {idx} - Scratchpad Note] Content: {c.get('content', '')}")

    context_payload = "\n\n".join(context_lines)

    system_prompt = """You are LinkForge AI, an intelligent personal knowledge assistant.
Answer the user's question accurately using ONLY the provided knowledge sources from their library.
Formatting Rules:
1. Structure your answer using clear Markdown (bullet points, bold highlights, code blocks when applicable).
2. Always ground your facts in the sources and cite them using clickable markdown links:
   - For articles: [Article Title](URL)
   - For videos: [Video Title (at MM:SS)](URL#t=SECONDS) or just [Video Title](URL)
   - For notes: [Scratchpad Note #ID]
3. Keep the tone helpful, direct, and concise."""

    user_prompt = f"""User Question: {query}

Knowledge Base Sources:
{context_payload}"""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        )
        answer = response.choices[0].message.content
        return jsonify({
            "status": "success",
            "answer": answer,
            "sources": top_candidates[:6]
        })
    except Exception as e:
        logger.error(f"Ask Library RAG Error: {e}")
        return jsonify({"error": f"AI synthesis failed: {str(e)}"}), 500

@links_bp.route("/api/links/find-mirrors", methods=["POST"])
def api_find_mirrors():
    data = request.json or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    title = data.get("title")
    mirrors = find_link_mirrors(url, title)
    return jsonify({"status": "success", "mirrors": mirrors})

@links_bp.route("/api/links/<int:link_id>/archive", methods=["POST"])
def archive_link(link_id):
    is_read = (request.json or {}).get("is_read", 1)
    def _archive():
        conn = get_db()
        conn.execute("UPDATE links SET is_read = ? WHERE id = ?", (is_read, link_id))
        conn.commit()
    retry_write(_archive)
    return jsonify({"status": "status_changed"})

@links_bp.route("/save")
def save_via_get():
    url = request.args.get("url")
    if not url:
        return "No URL provided", 400
    title, description, favicon, tags = scrape_url_data(url)
    link_id = None
    
    def _write_link():
        nonlocal link_id
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO links (url, title, description, favicon, tags) VALUES (?, ?, ?, ?, ?)", (url, title, description, favicon, tags))
        if cursor.rowcount > 0:
            link_id = cursor.lastrowid
        else:
            row = cursor.execute("SELECT id FROM links WHERE url = ?", (url,)).fetchone()
            if row:
                link_id = row[0]
        conn.commit()
    retry_write(_write_link)
    
    if link_id:
        import threading
        from flask import current_app
        app = current_app._get_current_object()
        
        def run_with_context(app, l_id, u):
            with app.app_context():
                run_background_enrichment(l_id, u)
                
        threading.Thread(target=run_with_context, args=(app, link_id, url)).start()
        
    return "<html><body style='background:#09090b; color:#10b981; font-family:sans-serif; display:flex; align-items:center; justify-content:center; height:100vh; margin:0;'><div style='text-align:center; padding:20px; background:#18181b; border:1px solid #27272a; border-radius:15px;'><h2 style='margin-bottom:10px;'>✅ Saved!</h2><script>setTimeout(() => window.close(), 1200);</script></div></body></html>"

@links_bp.route("/api/links/export")
def export_links_csv():
    conn = get_db()
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

@links_bp.route("/api/links/import", methods=["POST"])
def import_links_csv():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if file and file.filename.endswith('.csv'):
        try:
            stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
            csv_input = csv.reader(stream)
            header = next(csv_input, None)
            def _write_imports():
                conn = get_db()
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
            logger.error(f"Error importing CSV: {e}")
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "Invalid file type."}), 400

@links_bp.route("/api/links/timeline")
def get_timeline():
    conn = get_db()
    import sqlite3
    conn.row_factory = sqlite3.Row
    # Group links by date (YYYY-MM-DD)
    # We only want read links
    rows = conn.execute("SELECT id, title, description, url, tags, image_url, favicon, archive_path, date(date_added) as dte, date_added FROM links WHERE is_read = 1 ORDER BY date_added DESC").fetchall()
    
    timeline = {}
    for r in rows:
        dte = r["dte"]
        if dte not in timeline:
            timeline[dte] = []
        timeline[dte].append({
            "id": r["id"],
            "title": r["title"],
            "description": r["description"],
            "url": r["url"],
            "tags": r["tags"],
            "image_url": r["image_url"],
            "favicon": r["favicon"],
            "archive_path": r["archive_path"] if "archive_path" in r.keys() else "",
            "date_added": r["date_added"]
        })
    return jsonify(timeline)

@links_bp.route("/api/links/timeline-insights", methods=["POST"])
def get_timeline_insights():
    data = request.json or {}
    start_date = data.get("start")
    end_date = data.get("end")
    force_refresh = data.get("force", False)
    
    if not start_date or not end_date:
        return jsonify({"insights": "No date range specified."}), 400

    period_key = f"{start_date}_{end_date}"
    
    conn = get_db()
    import sqlite3
    conn.row_factory = sqlite3.Row

    # Check cache unless force refresh requested
    if not force_refresh:
        try:
            cached = conn.execute("SELECT summary FROM timeline_summaries WHERE period_key = ?", (period_key,)).fetchone()
            if cached and cached["summary"]:
                return jsonify({"insights": cached["summary"], "cached": True})
        except Exception as e:
            logger.debug(f"Cache lookup exception: {e}")
    
    rows = conn.execute("SELECT title, description, tags FROM links WHERE is_read = 1 AND date(date_added) >= ? AND date(date_added) <= ?", (start_date, end_date)).fetchall()
    
    if not rows:
        return jsonify({"insights": "No activity captured in this period."})
        
    # Compile a prompt
    articles = [f"Title: {r['title']}, Tags: {r['tags']}" for r in rows[:50]]
    text_data = "\n".join(articles)
    
    def get_ai_config():
        import sqlite3
        from config import Config
        conn = sqlite3.connect(Config.DB_PATH)
        rows = dict(conn.execute("SELECT key, value FROM settings WHERE key IN ('ai_api_key', 'ai_base_url', 'ai_model')").fetchall())
        conn.close()
        return {
            "api_key": rows.get("ai_api_key", ""),
            "base_url": rows.get("ai_base_url", "https://api.openai.com/v1"),
            "model": rows.get("ai_model", "gpt-4o-mini")
        }
    from openai import OpenAI
    
    config = get_ai_config()
    if not config["api_key"]:
        return jsonify({"insights": "AI not configured."})
        
    client = OpenAI(api_key=config["api_key"], base_url=config["base_url"])
    prompt = f"Based on the following articles the user read between {start_date} and {end_date}, write a single, elegant 2-sentence summary of their primary learning interests or themes during this time. Speak directly to the user (e.g. 'You focused heavily on...').\n\nArticles:\n{text_data}"
    
    try:
        res = client.chat.completions.create(
            model=config["model"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150
        )
        summary_text = res.choices[0].message.content.strip()
        
        # Cache in database
        def _save_summary():
            c = get_db()
            c.execute("CREATE TABLE IF NOT EXISTS timeline_summaries (period_key TEXT PRIMARY KEY, summary TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            c.execute("INSERT OR REPLACE INTO timeline_summaries (period_key, summary) VALUES (?, ?)", (period_key, summary_text))
            c.commit()
        retry_write(_save_summary)
        
        return jsonify({"insights": summary_text, "cached": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Homepage Bookmark Integration ---

@links_bp.route("/api/links/pin", methods=["POST", "OPTIONS"])
def pin_page():
    if request.method == "OPTIONS":
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type"
        }
        return "", 200, headers

    data = request.json or {}
    url = data.get("url")
    title = data.get("title", "")
    favicon = data.get("favicon", "")
    image_url = data.get("image_url", "")
    
    if not url:
        return jsonify({"error": "No URL"}), 400

    def _pin():
        conn = get_db()
        # Ensure it exists in links
        existing = conn.execute("SELECT id, title, description, tags FROM links WHERE url = ?", (url,)).fetchone()
        if existing:
            link_id = existing[0]
            link_title = title or existing[1] or ""
            link_desc = existing[2] or ""
            link_tags = existing[3] or ""
        else:
            conn.execute("""
                INSERT INTO links (url, title, favicon, image_url, is_read, date_added, click_count) 
                VALUES (?, ?, ?, ?, 1, datetime('now'), 0)
            """, (url, title, favicon, image_url))
            link_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            link_title = title or ""
            link_desc = ""
            link_tags = ""
        
        # Add to homepage
        conn.execute("""INSERT OR IGNORE INTO homepage_bookmarks (link_id, group_name)
                        VALUES (?, ?)""", (link_id, "Pinned Extensions"))
        conn.commit()
        return link_id, link_title, link_desc, link_tags

    try:
        link_id, link_title, link_desc, link_tags = retry_write(_pin)
        
        # Asynchronously enrich and auto-route pinned link into the user's best matching homepage group
        def _enrich_and_route():
            try:
                run_background_enrichment(link_id, url)
                conn = get_db()
                row = conn.execute("SELECT title, description, tags FROM links WHERE id = ?", (link_id,)).fetchone()
                t = (row[0] if row and row[0] else link_title) or title
                s = (row[1] if row and row[1] else link_desc) or ""
                tags_val = (row[2] if row and row[2] else link_tags) or ""
                auto_route_link_ai(link_id, t, s, tags_val, url)
            except Exception as e:
                logger.error(f"Pin background route error: {e}")

        import threading
        threading.Thread(target=_enrich_and_route, daemon=True).start()

        resp = jsonify({"status": "pinned", "link_id": link_id})
        resp.headers.add("Access-Control-Allow-Origin", "*")
        return resp
    except Exception as e:
        logger.error(f"Pin error: {e}")
        resp = jsonify({"error": str(e)})
        resp.headers.add("Access-Control-Allow-Origin", "*")
        return resp, 500

@links_bp.route("/api/links/<int:link_id>/add-to-homepage", methods=["POST"])
def add_link_to_homepage(link_id):
    """Add a link to the homepage as a curated bookmark in a group."""
    group_name = request.json.get("group", "Ungrouped").strip()
    if not group_name:
        group_name = "Ungrouped"
    
    def _write():
        conn = get_db()
        conn.execute("""INSERT OR REPLACE INTO homepage_bookmarks (link_id, group_name)
                        VALUES (?, ?)""", (link_id, group_name))
        conn.commit()
    retry_write(_write)
    return jsonify({"status": "added", "group": group_name})


@links_bp.route("/api/links/<int:link_id>", methods=["PUT"])
def update_link(link_id):
    """Update link metadata."""
    data = request.json
    def _write():
        conn = get_db()
        set_clauses = []
        params = []
        valid_keys = ["title", "description", "tags", "favicon", "url", "image_url"]
        for key in valid_keys:
            if key in data:
                set_clauses.append(f"{key}=?")
                params.append(data[key])
                
        if set_clauses:
            params.append(link_id)
            conn.execute(f"UPDATE links SET {', '.join(set_clauses)} WHERE id=?", tuple(params))
            conn.commit()
    retry_write(_write)
    return jsonify({"status": "updated"})

@links_bp.route("/api/links/<int:link_id>/update-tags", methods=["POST"])
def update_link_tags(link_id):
    """Inline update just the tags for a link."""
    tags = request.json.get("tags", "").strip()
    def _write():
        conn = get_db()
        conn.execute("UPDATE links SET tags=? WHERE id=?", (tags, link_id))
        conn.commit()
    retry_write(_write)
    return jsonify({"status": "updated", "tags": tags})

