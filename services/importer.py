import os
import re
import time
import json
import datetime
import logging
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
import requests

from config import Config
from services.db import get_db, retry_write
from services.scraper import scrape_url_data, check_link_alive, fetch_youtube_transcript_details

logger = logging.getLogger(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
}

CLEAN_QUERY_PARAMS = {
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'fbclid', 'gclid', 'msclkid', 'mc_cid', 'mc_eid', '_ga', 'ref', 'source'
}

def clean_url_tracking(raw_url: str) -> str:
    """Strip common marketing tracking query parameters while preserving video IDs."""
    try:
        parsed = urllib.parse.urlparse(raw_url)
        query_dict = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        filtered_queries = [(k, v) for k, v in query_dict if k.lower() not in CLEAN_QUERY_PARAMS]
        new_query = urllib.parse.urlencode(filtered_queries)
        clean = urllib.parse.urlunparse((
            parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment
        ))
        return clean.rstrip('/') if parsed.path == '/' and not new_query else clean
    except Exception:
        return raw_url

def extract_youtube_video_id(url: str):
    """Extract 11-char YouTube video ID if URL points to a specific video."""
    parsed = urllib.parse.urlparse(url)
    domain = parsed.netloc.lower()
    
    if 'youtu.be' in domain:
        path_parts = parsed.path.strip('/').split('/')
        if path_parts and len(path_parts[0]) == 11:
            return path_parts[0]
    elif 'youtube.com' in domain:
        if '/watch' in parsed.path:
            q = urllib.parse.parse_qs(parsed.query)
            v_list = q.get('v')
            if v_list and len(v_list[0]) == 11:
                return v_list[0]
        elif '/shorts/' in parsed.path:
            parts = parsed.path.split('/shorts/')
            if len(parts) > 1 and len(parts[1].split('/')[0]) == 11:
                return parts[1].split('/')[0]
        elif '/embed/' in parsed.path:
            parts = parsed.path.split('/embed/')
            if len(parts) > 1 and len(parts[1].split('/')[0]) == 11:
                return parts[1].split('/')[0]
    return None

def fetch_youtube_oembed(url: str):
    """Fetch video metadata from YouTube free oEmbed API."""
    try:
        resp = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=8
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "title": data.get("title", ""),
                "thumbnail_url": data.get("thumbnail_url", ""),
                "channel_name": data.get("author_name", ""),
            }
    except Exception as e:
        logger.debug(f"YouTube oEmbed fetch failed for {url}: {e}")
    return None

def is_link_alive_fast(url: str, timeout: float = 2.5) -> bool:
    """Fast concurrent check to ensure the link is alive and not a dead 404/500/broken domain."""
    if not url or not (url.startswith('http://') or url.startswith('https://')):
        return False
        
    try:
        resp = requests.head(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code < 400 or resp.status_code in (401, 403, 405, 429):
            return True
        resp_get = requests.get(url, headers=HEADERS, timeout=timeout, stream=True, allow_redirects=True)
        if resp_get.status_code < 400 or resp_get.status_code in (401, 403, 405, 429):
            return True
        return False
    except Exception:
        return False

def parse_netscape_bookmarks(html_content: str):
    """
    Parse standard Netscape Bookmark HTML format (exported by Brave, Chrome, Edge, Firefox, Safari).
    Accurately extracts titles, clean URLs, base64 favicons, add dates, and folder hierarchy.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    bookmarks = []
    
    for a in soup.find_all('a'):
        href = (a.get('href') or '').strip()
        if not (href.startswith('http://') or href.startswith('https://')):
            continue
            
        title = a.get_text().strip() or href
        clean_href = clean_url_tracking(href)
        icon = a.get('icon', '').strip() or a.get('icon_uri', '').strip()
        add_date = a.get('add_date', '')
        
        folder_chain = []
        is_toolbar = False
        
        for p in a.parents:
            if p.name == 'dl':
                prev_h3 = p.find_previous_sibling('dt')
                h3_tag = None
                if prev_h3:
                    h3_tag = prev_h3.find('h3')
                if not h3_tag:
                    h3_tag = p.find_previous('h3')
                    
                if h3_tag and h3_tag.get_text().strip():
                    h_name = h3_tag.get_text().strip()
                    if h_name not in folder_chain:
                        folder_chain.append(h_name)
                    if h3_tag.get('personal_toolbar_folder', '').lower() == 'true' or 'bookmarks bar' in h_name.lower() or 'bookmarks toolbar' in h_name.lower() or 'favorites bar' in h_name.lower():
                        is_toolbar = True
                        
        folder_chain.reverse()
        
        filtered_tags = [
            f for f in folder_chain 
            if f.lower() not in ('bookmarks bar', 'bookmarks toolbar', 'other bookmarks', 'mobile bookmarks', 'favorites bar', 'bookmarks')
        ]
        
        bookmarks.append({
            'url': clean_href,
            'raw_url': href,
            'title': title,
            'icon': icon,
            'add_date': add_date,
            'folder_path': folder_chain,
            'folder_tags': filtered_tags,
            'is_in_toolbar': is_toolbar
        })

    unique_bookmarks = []
    seen = set()
    for b in bookmarks:
        norm = b['url'].lower().rstrip('/')
        if norm not in seen:
            seen.add(norm)
            unique_bookmarks.append(b)
            
    return unique_bookmarks

def get_or_create_video_category(category_path: list, conn) -> int:
    """
    Ensure category hierarchy exists in video_categories and return the final category ID.
    e.g. ['Woodworking', 'Joinery'] creates parent 'Woodworking' and subcategory 'Joinery'.
    """
    if not category_path:
        return None
        
    parent_id = None
    for cat_name in category_path:
        clean_name = cat_name.strip()
        if not clean_name:
            continue
            
        if parent_id is None:
            row = conn.execute(
                "SELECT id FROM video_categories WHERE LOWER(name) = LOWER(?) AND parent_id IS NULL",
                (clean_name,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM video_categories WHERE LOWER(name) = LOWER(?) AND parent_id = ?",
                (clean_name, parent_id)
            ).fetchone()
            
        if row:
            parent_id = row[0]
        else:
            cur = conn.execute(
                "INSERT INTO video_categories (name, parent_id, display_order) VALUES (?, ?, 0)",
                (clean_name, parent_id)
            )
            parent_id = cur.lastrowid
            
    return parent_id

def process_browser_bookmarks_import(file_storage, filter_dead_links=True, pin_bookmarks_bar=True, route_youtube_videos=True):
    """
    Execute full bookmark import pipeline:
    1. Parse Netscape HTML.
    2. Concurrently filter dead/404 links (if enabled).
    3. Route YouTube videos to video_bookmarks with auto-created folder categories.
    4. Store regular web bookmarks in links with folder tags.
    5. Pin Bookmarks Bar links to homepage_bookmarks.
    6. Queue background article & YouTube transcript ingestion worker.
    """
    content = file_storage.read().decode('utf-8', errors='ignore')
    raw_bookmarks = parse_netscape_bookmarks(content)
    
    total_found = len(raw_bookmarks)
    if total_found == 0:
        return {
            "status": "error",
            "message": "No valid bookmarks found in the uploaded file. Please upload a standard Netscape bookmarks.html file exported from Brave, Chrome, Firefox, or Edge."
        }

    # Step 2: Validate live links concurrently
    valid_bookmarks = []
    dead_links_count = 0
    
    if filter_dead_links:
        with ThreadPoolExecutor(max_workers=12) as executor:
            future_to_bm = {
                executor.submit(is_link_alive_fast, bm['url'], 2.5): bm 
                for bm in raw_bookmarks
            }
            for future in as_completed(future_to_bm):
                bm = future_to_bm[future]
                try:
                    if future.result():
                        valid_bookmarks.append(bm)
                    else:
                        dead_links_count += 1
                except Exception:
                    dead_links_count += 1
    else:
        valid_bookmarks = raw_bookmarks

    url_to_order = {bm['url']: idx for idx, bm in enumerate(raw_bookmarks)}
    valid_bookmarks.sort(key=lambda b: url_to_order.get(b['url'], 0))

    report = {
        "total_found": total_found,
        "valid_count": len(valid_bookmarks),
        "dead_skipped": dead_links_count,
        "links_imported": 0,
        "homepage_pinned": 0,
        "videos_routed": 0,
        "categories_created": 0,
        "archiving_queued": 0
    }

    links_to_background_archive = []
    videos_to_background_ingest = []

    def _execute_import_db():
        conn = get_db()
        existing_link_urls = {
            r[0].lower().rstrip('/'): r[1] 
            for r in conn.execute("SELECT url, id FROM links").fetchall()
        }
        existing_video_urls = {
            r[0].lower().rstrip('/'): r[1] 
            for r in conn.execute("SELECT url, id FROM video_bookmarks").fetchall()
        }
        
        for bm in valid_bookmarks:
            url = bm['url']
            title = bm['title']
            norm_url = url.lower().rstrip('/')
            yt_id = extract_youtube_video_id(url) if route_youtube_videos else None
            
            # 1. YouTube Video Routing
            if yt_id:
                if norm_url in existing_video_urls:
                    continue
                    
                cat_id = None
                if bm['folder_tags']:
                    cat_id = get_or_create_video_category(bm['folder_tags'], conn)
                    
                thumb = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"
                tags_str = ", ".join(bm['folder_tags']) if bm['folder_tags'] else "Imported, Video"
                
                cur = conn.execute("""
                    INSERT INTO video_bookmarks (url, title, thumbnail_url, channel_name, category_id, tags, description, date_added, display_order)
                    VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), 0)
                """, (url, title, thumb, "YouTube", cat_id, tags_str, f"Imported from browser folder: {' > '.join(bm['folder_path'])}"))
                v_id = cur.lastrowid
                
                report["videos_routed"] += 1
                videos_to_background_ingest.append((v_id, url, title))
                
            # 2. Regular Web Link Import
            else:
                link_id = existing_link_urls.get(norm_url)
                folder_tags = bm['folder_tags']
                tags_str = ", ".join(folder_tags) if folder_tags else "Imported"
                favicon = bm['icon'] or f"https://www.google.com/s2/favicons?domain={urllib.parse.urlparse(url).netloc}&sz=128"
                
                if not link_id:
                    cur = conn.execute("""
                        INSERT INTO links (url, title, description, tags, is_read, date_added, favicon, click_count)
                        VALUES (?, ?, ?, ?, 1, datetime('now'), ?, 0)
                    """, (url, title, "", tags_str, favicon))
                    link_id = cur.lastrowid
                    existing_link_urls[norm_url] = link_id
                    report["links_imported"] += 1
                    links_to_background_archive.append((link_id, url))
                
                if pin_bookmarks_bar and bm.get('is_in_toolbar'):
                    already_pinned = conn.execute("SELECT id FROM homepage_bookmarks WHERE link_id = ?", (link_id,)).fetchone()
                    if not already_pinned:
                        group_name = folder_tags[0] if folder_tags else "Bookmarks Bar"
                        conn.execute("""
                            INSERT INTO homepage_bookmarks (link_id, group_name, display_order)
                            VALUES (?, ?, 0)
                        """, (link_id, group_name))
                        report["homepage_pinned"] += 1

        conn.commit()

    retry_write(_execute_import_db)
    
    # Step 6: Spawn unified background thread for offline article reader & YouTube transcripts
    total_queued = len(links_to_background_archive) + len(videos_to_background_ingest)
    report["archiving_queued"] = total_queued
    
    if total_queued > 0:
        import threading
        from services.task_queue import start_task, update_progress, complete_task
        
        def _background_archive_worker(link_batch, video_batch):
            total_items = len(link_batch) + len(video_batch)
            logger.info(f"Starting background ingestion for {len(link_batch)} articles and {len(video_batch)} YouTube videos...")
            
            start_task(
                task_id="bookmark_ingest",
                name="Ingesting Bookmarks",
                total=total_items,
                icon="fa-bookmark"
            )
            
            processed_count = 0
            
            # 1. Ingest YouTube Video Metadata & Transcripts
            for v_id, v_url, v_title in video_batch:
                processed_count += 1
                update_progress(
                    task_id="bookmark_ingest",
                    current=processed_count,
                    current_item=f"Video: {v_title[:32]}..." if len(v_title) > 32 else f"Video: {v_title}"
                )
                try:
                    time.sleep(0.4)
                    oembed = fetch_youtube_oembed(v_url)
                    channel_name = oembed.get('channel_name', 'YouTube') if oembed else 'YouTube'
                    clean_title = oembed.get('title') if oembed and oembed.get('title') else v_title
                    
                    t_data = fetch_youtube_transcript_details(v_url)
                    transcript = t_data.get("text", "")
                    segments = t_data.get("segments", [])
                    
                    def _update_vid():
                        c = get_db()
                        c.execute("""
                            UPDATE video_bookmarks 
                            SET title = ?, channel_name = ?, transcript = ?, transcript_json = ? 
                            WHERE id = ?
                        """, (clean_title, channel_name, transcript, json.dumps(segments) if segments else None, v_id))
                        c.commit()
                    retry_write(_update_vid)
                except Exception as ve:
                    logger.debug(f"YouTube background ingestion error for video {v_id} ({v_url}): {ve}")

            # 2. Ingest Web Articles
            for l_id, l_url in link_batch:
                processed_count += 1
                domain = urllib.parse.urlparse(l_url).netloc
                update_progress(
                    task_id="bookmark_ingest",
                    current=processed_count,
                    current_item=f"Article: {domain}"
                )
                try:
                    time.sleep(0.5)
                    scraped_title, scraped_desc, scraped_fav, auto_tags = scrape_url_data(l_url)
                    
                    def _update_link():
                        c = get_db()
                        row = c.execute("SELECT title, description, tags FROM links WHERE id = ?", (l_id,)).fetchone()
                        if row:
                            curr_title, curr_desc, curr_tags = row
                            new_title = curr_title if curr_title and curr_title != l_url else scraped_title
                            new_desc = curr_desc if curr_desc else scraped_desc
                            combined_tags = ", ".join(filter(None, [curr_tags, auto_tags]))
                            c.execute("""
                                UPDATE links SET title = ?, description = ?, tags = ? WHERE id = ?
                            """, (new_title, new_desc, combined_tags, l_id))
                            c.commit()
                    retry_write(_update_link)
                except Exception as e:
                    logger.debug(f"Background archiving error for link {l_id} ({l_url}): {e}")
                    
            complete_task("bookmark_ingest", final_message=f"All {total_items} items ingested")

        threading.Thread(target=_background_archive_worker, args=(links_to_background_archive, videos_to_background_ingest), daemon=True, name="ImportArchiver").start()

    logger.info(f"Browser bookmark import complete: {report}")
    return {
        "status": "success",
        "report": report
    }
