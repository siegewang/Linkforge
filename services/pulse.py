import logging
import re
import html
import sqlite3
import datetime
import threading
from urllib.parse import urlparse, urljoin
import xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup
from services.db import get_db, retry_write

logger = logging.getLogger(__name__)

STOP_WORDS = {
    'the', 'and', 'for', 'with', 'from', 'this', 'that', 'your', 'about', 'what', 'when',
    'where', 'how', 'why', 'who', 'which', 'will', 'have', 'been', 'more', 'their', 'some',
    'into', 'than', 'them', 'then', 'http', 'https', 'www', 'com', 'org', 'net', 'video',
    'watch', 'youtube', 'post', 'view', 'read', 'article', 'reddit', 'reddit.com'
}

DEFAULT_TOPIC_FEEDS = {
    'Tech & Code': [
        'https://feeds.arstechnica.com/arstechnica/index',
        'http://feeds.bbci.co.uk/news/technology/rss.xml',
        'https://www.engadget.com/rss.xml',
    ],
    'AI & ML': [
        'https://techcrunch.com/category/artificial-intelligence/feed/',
        'https://venturebeat.com/category/ai/feed/',
        'https://www.artificialintelligence-news.com/feed/',
    ],
    'Automotive & EVs': [
        'https://electrek.co/feed/',
        'https://insideevs.com/rss/articles/all/',
        'https://www.autoblog.com/rss.xml',
    ],
    'Hardware & 3D': [
        'https://www.tomshardware.com/feeds/all',
        'https://hackaday.com/feed/',
        'https://www.space.com/feeds/all',
    ]
}

def extract_user_interest_keywords() -> list:
    """Analyze user library bookmarks, tags, and video titles to determine core interest topics."""
    try:
        conn = get_db()
        word_freq = {}
        
        # 1. Tags from links
        link_tags = conn.execute("SELECT tags FROM links WHERE tags IS NOT NULL AND tags != '' LIMIT 200").fetchall()
        for r in link_tags:
            for t in (r['tags'] or '').split(','):
                w = t.strip().lower()
                if w and len(w) > 2 and w not in STOP_WORDS:
                    word_freq[w] = word_freq.get(w, 0) + 4

        # 2. Tags & Categories from videos
        vid_tags = conn.execute("SELECT tags, title FROM video_bookmarks LIMIT 100").fetchall()
        for r in vid_tags:
            if r['tags']:
                for t in r['tags'].split(','):
                    w = t.strip().lower()
                    if w and len(w) > 2 and w not in STOP_WORDS:
                        word_freq[w] = word_freq.get(w, 0) + 4
            if r['title']:
                words = re.findall(r'[a-zA-Z0-9\-]{3,}', r['title'].lower())
                for w in words:
                    if w not in STOP_WORDS:
                        word_freq[w] = word_freq.get(w, 0) + 1

        # 3. Notes categories
        note_cats = conn.execute("SELECT category FROM notes WHERE category IS NOT NULL AND category != '' LIMIT 50").fetchall()
        for r in note_cats:
            w = (r['category'] or '').strip().lower()
            if w and len(w) > 2 and w not in STOP_WORDS:
                word_freq[w] = word_freq.get(w, 0) + 5

        # Sort top keywords
        sorted_kw = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
        top_keywords = [k for k, v in sorted_kw[:12]]
        return top_keywords or ["technology", "linux", "ai", "hardware", "software"]
    except Exception as e:
        logger.warning(f"Error extracting user interest keywords: {e}")
        return ["technology", "linux", "ai", "hardware", "software"]

def fetch_page_og_image(url: str) -> str:
    """Fetch article webpage head to extract authentic OpenGraph / Twitter featured image."""
    if not url or not url.startswith('http') or 'news.google.com' in url or 'consent.google' in url:
        return ''
    try:
        r = requests.get(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
        }, timeout=4)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text[:80000], 'html.parser')
            for prop in ['og:image', 'og:image:secure_url', 'twitter:image', 'twitter:image:src']:
                tag = soup.find('meta', property=re.compile(f'^{prop}$', re.I)) or soup.find('meta', attrs={'name': re.compile(f'^{prop}$', re.I)})
                if tag and tag.get('content'):
                    u = tag['content'].strip()
                    if u.startswith('http') and not u.endswith('.svg') and 'pixel' not in u and 'spacer' not in u:
                        return u
    except Exception:
        pass
    return ''

def extract_authentic_image(entry_elem, raw_link: str, title: str, topic_name: str) -> str:
    """Extract authentic article image from XML tags, enclosures, HTML descriptions, webpage og:image, or dynamic unique visual."""
    # 1. Look for XML media tags: media:content, media:thumbnail, enclosure, media:group
    for c in entry_elem:
        tag = c.tag.lower()
        if any(k in tag for k in ['thumbnail', 'content', 'enclosure', 'image', 'media']):
            u = c.get('url') or c.get('href') or c.attrib.get('url') or c.attrib.get('href')
            if u and u.startswith('http') and not u.endswith('.svg') and 'spacer' not in u and 'pixel' not in u:
                return u
        for sub in c:
            u = sub.get('url') or sub.attrib.get('url')
            if u and u.startswith('http') and not u.endswith('.svg') and 'pixel' not in u and 'spacer' not in u:
                return u

    # 2. Check HTML inside description, summary, or content:encoded
    for elem in entry_elem:
        if elem.text:
            unescaped = html.unescape(elem.text)
            match = re.search(r'<img[^>]+src=["\'](https?://[^"\'>\s]+)["\']', unescaped, re.I)
            if match:
                img_url = match.group(1).strip()
                if not img_url.endswith('.svg') and 'pixel' not in img_url and 'spacer' not in img_url and 'icon' not in img_url:
                    return img_url

    # 3. Fetch from original article webpage OpenGraph tags
    og_img = fetch_page_og_image(raw_link)
    if og_img:
        return og_img

    # 4. Unique dynamic fallback (unique per URL hash so no two cards share duplicate photos)
    url_seed = abs(hash(raw_link or title)) % 9999
    return f"https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=800&q=80&sig={url_seed}"

def parse_rss_feed(feed_url: str, topic_name: str = "General") -> list:
    """Fetch and parse RSS/Atom feed items using ElementTree with authentic image extraction."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/rss+xml, application/xml, text/xml, */*'
    }
    
    items = []
    try:
        r = requests.get(feed_url, headers=headers, timeout=10)
        if r.status_code != 200:
            logger.debug(f"Feed returned status {r.status_code} for {feed_url}")
            return []

        # Safe parsing with recovery
        try:
            root = ET.fromstring(r.content)
        except Exception:
            soup = BeautifulSoup(r.content, 'html.parser')
            # Fallback to BeautifulSoup for broken XML
            entries = soup.find_all('item') or soup.find_all('entry')
            for entry in entries[:20]:
                t = entry.find('title')
                title = t.get_text().strip() if t else ''
                l = entry.find('link')
                link = l.get_text().strip() if l else ''
                if not link and l and l.get('href'):
                    link = l.get('href')
                if not title or not link:
                    continue
                
                domain = urlparse(link).netloc.replace('www.', '')
                source_icon = f"https://www.google.com/s2/favicons?domain={domain}&sz=64" if domain else ''
                
                # Image in BS4
                img = ''
                img_tag = entry.find('img')
                if img_tag and img_tag.get('src'):
                    img = img_tag['src']
                if not img:
                    img = resolve_fallback_topic_image(title, topic_name)

                d = entry.find('description') or entry.find('summary')
                desc = re.sub(r'<[^>]+>', ' ', d.get_text()).strip()[:280] if d else ''

                items.append({
                    "title": title,
                    "url": link,
                    "source_name": domain.capitalize() or 'Web',
                    "source_icon": source_icon,
                    "summary": desc,
                    "image_url": img,
                    "topic": topic_name,
                    "published_at": ''
                })
            return items

        channel = root.find('channel')
        raw_entries = channel.findall('item') if channel is not None else root.findall('{http://www.w3.org/2005/Atom}entry')
        if not raw_entries:
            raw_entries = root.findall('item')

        for entry in raw_entries[:20]:
            try:
                # Title
                title_elem = entry.find('title')
                if title_elem is None:
                    title_elem = entry.find('{http://www.w3.org/2005/Atom}title')
                title = title_elem.text.strip() if (title_elem is not None and title_elem.text) else ''
                if not title:
                    continue

                title = html.unescape(title)
                title = re.sub(r'&(?:nbsp|amp|quot|lt|gt|#039);', ' ', title)
                title = re.sub(r'\s+', ' ', title).strip()

                # Publisher Source
                source_name = 'Web'
                source_elem = entry.find('source')
                if source_elem is not None and source_elem.text:
                    source_name = source_elem.text.strip()
                elif ' - ' in title:
                    parts = title.rsplit(' - ', 1)
                    title = parts[0].strip()
                    source_name = parts[1].strip()

                # Link URL
                link = ''
                link_elem = entry.find('link')
                if link_elem is not None:
                    link = link_elem.text or link_elem.get('href') or ''
                if not link:
                    link_atom = entry.find('{http://www.w3.org/2005/Atom}link')
                    if link_atom is not None:
                        link = link_atom.get('href') or link_atom.text or ''

                if not link:
                    continue

                # Description / Summary
                desc = ''
                desc_elem = entry.find('description')
                if desc_elem is None:
                    desc_elem = entry.find('{http://www.w3.org/2005/Atom}summary')
                if desc_elem is None:
                    desc_elem = entry.find('{http://www.w3.org/2005/Atom}content')
                
                if desc_elem is not None and desc_elem.text:
                    desc_html = html.unescape(desc_elem.text)
                    desc = re.sub(r'<[^>]+>', ' ', desc_html)
                    desc = html.unescape(desc)
                    desc = re.sub(r'&(?:nbsp|amp|quot|lt|gt|#039);', ' ', desc)
                    desc = re.sub(r'\s+', ' ', desc).strip()
                    desc = desc[:280]

                # Extract authentic image
                image_url = extract_authentic_image(entry, link, title, topic_name)

                # Published date
                pub_date_str = ''
                pub_elem = entry.find('pubDate')
                if pub_elem is None:
                    pub_elem = entry.find('{http://www.w3.org/2005/Atom}published')
                if pub_elem is None:
                    pub_elem = entry.find('{http://www.w3.org/2005/Atom}updated')
                if pub_elem is not None and pub_elem.text:
                    pub_date_str = pub_elem.text.strip()

                # Source domain and icon
                domain = urlparse(link).netloc.replace('www.', '')
                source_icon = f"https://www.google.com/s2/favicons?domain={domain}&sz=64" if domain else ''

                items.append({
                    "title": title,
                    "url": link,
                    "source_name": source_name if source_name != 'Web' else (domain.capitalize() or 'Web'),
                    "source_icon": source_icon,
                    "summary": desc,
                    "image_url": image_url,
                    "topic": topic_name,
                    "published_at": pub_date_str
                })
            except Exception as item_err:
                logger.debug(f"Error parsing RSS item: {item_err}")
                continue

    except Exception as e:
        logger.warning(f"Failed to fetch feed {feed_url}: {e}")

    return items

def calculate_relevance_score(title: str, summary: str, user_keywords: list) -> int:
    """Calculate a 0-100 relevance score against user interest profile."""
    score = 50
    content = f"{title} {summary}".lower()
    
    matches = 0
    for kw in user_keywords:
        if kw in content:
            matches += 1
            score += 12

    return min(98, max(20, score))

def refresh_pulse_feed(topic_id: int = None) -> int:
    """Fetch external topic feeds and store new articles into pulse_items."""
    conn = get_db()
    user_keywords = extract_user_interest_keywords()
    
    if topic_id:
        topics = conn.execute("SELECT * FROM pulse_topics WHERE id=? AND is_active=1", (topic_id,)).fetchall()
    else:
        topics = conn.execute("SELECT * FROM pulse_topics WHERE is_active=1 ORDER BY display_order ASC").fetchall()

    new_items_count = 0

    for topic in topics:
        topic_name = topic['name']
        custom_url = topic['custom_feed_url']
        keywords = topic['query_keywords']

        urls_to_fetch = []
        if custom_url:
            urls_to_fetch.append(custom_url)
        elif topic_name == '✨ For You':
            # Curated multi-source authority feeds for authentic high-res visuals
            urls_to_fetch.extend([
                'https://feeds.arstechnica.com/arstechnica/index',
                'https://electrek.co/feed/',
                'https://www.tomshardware.com/feeds/all',
                'https://techcrunch.com/category/artificial-intelligence/feed/',
                'https://www.space.com/feeds/all',
                'https://www.engadget.com/rss.xml',
                'https://insideevs.com/rss/articles/all/'
            ])
        elif topic_name in DEFAULT_TOPIC_FEEDS:
            urls_to_fetch.extend(DEFAULT_TOPIC_FEEDS[topic_name])
        else:
            urls_to_fetch.append(f"https://news.google.com/rss/search?q={requests.utils.quote(keywords)}&hl=en-GB&gl=GB&ceid=GB:en")

        for feed_url in urls_to_fetch:
            articles = parse_rss_feed(feed_url, topic_name)
            
            for art in articles:
                relevance = calculate_relevance_score(art['title'], art['summary'], user_keywords)
                
                def _insert_item():
                    c = get_db()
                    c.execute("""
                        INSERT OR IGNORE INTO pulse_items 
                        (title, url, source_name, source_icon, summary, image_url, topic, published_at, relevance_score, is_saved, is_dismissed)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
                    """, (
                        art['title'], art['url'], art['source_name'], art['source_icon'],
                        art['summary'], art['image_url'], art['topic'], art['published_at'], relevance
                    ))
                    # If item already exists but has fallback image and now we have authentic publisher image, update it
                    if art['image_url'] and 'unsplash' not in art['image_url']:
                        c.execute("""
                            UPDATE pulse_items 
                            SET image_url = ? 
                            WHERE url = ? AND (image_url LIKE '%unsplash%' OR image_url IS NULL OR image_url = '')
                        """, (art['image_url'], art['url']))
                    c.commit()
                
                try:
                    retry_write(_insert_item)
                    new_items_count += 1
                except Exception as e:
                    logger.debug(f"Could not save pulse item {art['url']}: {e}")

    # Clean old dismissed items > 14 days
    def _clean():
        c = get_db()
        c.execute("DELETE FROM pulse_items WHERE is_dismissed=1 AND date_fetched < datetime('now', '-14 days')")
        c.commit()
    try:
        retry_write(_clean)
    except Exception:
        pass

    return new_items_count

def get_active_pulse_items(topic_name: str = None, limit: int = 30) -> list:
    """Retrieve non-dismissed pulse items for UI rendering."""
    conn = get_db()

    count = conn.execute("SELECT COUNT(*) as cnt FROM pulse_items WHERE is_dismissed=0").fetchone()['cnt']
    if count == 0:
        refresh_pulse_feed()

    query = "SELECT * FROM pulse_items WHERE is_dismissed = 0"
    params = []

    if topic_name and topic_name not in ('All', '✨ For You'):
        query += " AND topic = ?"
        params.append(topic_name)

    query += " ORDER BY is_saved ASC, relevance_score DESC, date_fetched DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]

def dismiss_pulse_item(item_id: int) -> dict:
    """Hide a pulse item from the active feed."""
    def _dismiss():
        c = get_db()
        c.execute("UPDATE pulse_items SET is_dismissed=1 WHERE id=?", (item_id,))
        c.commit()
    retry_write(_dismiss)
    return {"status": "success"}

def auto_synthesize_pulse_topics() -> dict:
    """Automatically analyze the user's library and generate personalized topics and RSS streams via AI/LLM."""
    import json
    conn = get_db()
    
    # 1. Fetch AI Configuration
    settings = dict(conn.execute("SELECT key, value FROM settings WHERE key IN ('ai_api_key', 'ai_base_url', 'ai_model')").fetchall())
    api_key = (settings.get('ai_api_key') or '').strip()
    base_url = (settings.get('ai_base_url') or '').strip() or 'https://api.openai.com/v1'
    model = (settings.get('ai_model') or '').strip() or 'gpt-4o-mini'

    # 2. Sample library items
    links = conn.execute("SELECT title, tags FROM links ORDER BY id DESC LIMIT 50").fetchall()
    vids = conn.execute("SELECT title, tags, channel_name FROM video_bookmarks ORDER BY id DESC LIMIT 50").fetchall()
    notes = conn.execute("SELECT content, category FROM notes ORDER BY id DESC LIMIT 20").fetchall()

    summary_lines = []
    for r in links:
        if r['title']:
            summary_lines.append(f"Bookmark: {r['title']} (Tags: {r['tags'] or ''})")
    for r in vids:
        if r['title']:
            summary_lines.append(f"Video: {r['title']} | Channel: {r['channel_name'] or ''} (Tags: {r['tags'] or ''})")
    for r in notes:
        if r['content']:
            summary_lines.append(f"Note: {r['content'][:80]} (Category: {r['category'] or ''})")

    library_sample = "\n".join(summary_lines[:80])
    generated_topics = []

    # If AI is configured and library has bookmarks, use AI LLM clustering
    if api_key and library_sample.strip():
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url)
            prompt = f"""You are the LinkForge AI Discover Curator.
Analyze this user's personal bookmark vault, videos, and notes:

{library_sample}

Identify their top 4 to 6 distinct, core interest categories (e.g. if they love chickens and speedboats, create topics for those).
For each topic, provide:
1. "name": Concise title with a relevant emoji (e.g. "🐔 Poultry & Farming", "🚤 Marine & Speedboats", "🦖 Paleontology")
2. "keywords": 4-6 specific search terms for discovering fresh articles on this topic
3. "custom_feed_url": Known public RSS/Atom feed URL if an authority publisher exists for this niche (e.g. https://electrek.co/feed/, https://feeds.arstechnica.com/arstechnica/index, https://hackaday.com/feed/, etc.), otherwise null.

Respond ONLY with a valid JSON array of objects:
[
  {{"name": "...", "keywords": "...", "custom_feed_url": null}}
]
"""
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3
            )
            raw_content = resp.choices[0].message.content.strip()
            # Clean markdown formatting and isolate JSON array
            clean = re.sub(r'```(?:json)?', '', raw_content).strip()
            json_match = re.search(r'\[\s*\{.*\}\s*\]', clean, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group(0))
            else:
                parsed = json.loads(clean)

            if isinstance(parsed, list) and len(parsed) > 0:
                generated_topics = parsed
        except Exception as ai_err:
            logger.warning(f"AI topic synthesis failed, falling back to keyword clustering: {ai_err}")

    # Fallback to local keyword clustering if LLM returned empty
    if not generated_topics:
        keywords = extract_user_interest_keywords()
        generated_topics = [
            {"name": f"✨ {k.capitalize()} & Trends", "keywords": f"{k} latest trends news technology", "custom_feed_url": None}
            for k in keywords[:4]
        ]

    # Update pulse_topics table in database
    if generated_topics:
        def _update_db_topics():
            c = get_db()
            c.execute("DELETE FROM pulse_topics WHERE name != '✨ For You'")
            
            for idx, top in enumerate(generated_topics[:6], start=2):
                c.execute("""
                    INSERT OR REPLACE INTO pulse_topics (name, query_keywords, feed_type, custom_feed_url, is_active, display_order)
                    VALUES (?, ?, ?, ?, 1, ?)
                """, (
                    top.get('name', 'Topic'),
                    top.get('keywords', ''),
                    'rss' if top.get('custom_feed_url') else 'google_news',
                    top.get('custom_feed_url'),
                    idx
                ))
            c.commit()

        try:
            retry_write(_update_db_topics)
            threading.Thread(target=refresh_pulse_feed, daemon=True).start()
        except Exception as db_err:
            logger.error(f"Error updating pulse topics in DB: {db_err}")

    return {"status": "success", "topics": generated_topics}

