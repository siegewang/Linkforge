import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)
executor = ThreadPoolExecutor(max_workers=5)

def scrape_url_data(url: str):
    title, description, favicon, auto_tags = url, "", "", []
    domain = urlparse(url).netloc.lower()
    
    if 'youtube.com' in domain or 'youtu.be' in domain:
        auto_tags.append('video')
    elif 'reddit.com' in domain:
        auto_tags.extend(['social', 'reddit'])
    elif 'github.com' in domain:
        auto_tags.extend(['code', 'github'])
    elif 'news.ycombinator.com' in domain:
        auto_tags.append('news')
    elif 'twitter.com' in domain or 'x.com' in domain:
        auto_tags.append('social')
    elif 'medium.com' in domain:
        auto_tags.append('article')

    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
            
        desc_tag = soup.find('meta', attrs={'name': 'description'}) or soup.find('meta', attrs={'property': 'og:description'})
        if desc_tag and desc_tag.get('content'):
            description = desc_tag['content'].strip()
            
        icon_tag = soup.find('link', rel=lambda x: x and 'icon' in x.lower())
        if icon_tag and icon_tag.get('href'):
            favicon = urljoin(url, icon_tag['href'])
        else:
            favicon = f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
    except Exception as e:
        logger.warning(f"Error scraping data for {url}: {e}")
        if not favicon:
            favicon = f"https://www.google.com/s2/favicons?domain={domain}&sz=128"

    return title, description, favicon, ", ".join(auto_tags)

def check_link_alive(url: str) -> bool:
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        resp = requests.head(url, headers=headers, timeout=5, allow_redirects=True)
        # 401, 403, and 405 are often returned by sites blocking automated scripts
        if resp.status_code < 400 or resp.status_code in [401, 403, 405]:
            return True
        else:
            resp_get = requests.get(url, headers=headers, timeout=5, stream=True)
            if resp_get.status_code < 400 or resp_get.status_code in [401, 403, 405]:
                return True
    except Exception as e:
        logger.debug(f"Health check failed for {url}: {e}")
        return False
    return False

def find_link_mirrors(url: str, title: str = None):
    mirrors = []
    seen_urls = set()

    def add_mirror(m_url, label, source_type="Snapshot"):
        if m_url and m_url not in seen_urls and m_url != url:
            seen_urls.add(m_url)
            mirrors.append({"url": m_url, "label": label, "source": source_type})

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    # 1. Check Internet Archive (Wayback Machine)
    try:
        wayback_api = f"https://archive.org/wayback/available?url={url}"
        res = requests.get(wayback_api, headers=headers, timeout=4)
        if res.ok:
            data = res.json()
            closest = data.get("archived_snapshots", {}).get("closest", {})
            if closest.get("available") and closest.get("url"):
                add_mirror(closest["url"], "Wayback Machine Archive", "Wayback")
    except Exception as e:
        logger.debug(f"Wayback lookup error for {url}: {e}")

    # 2. Add Archive.ph / Archive.is snapshot URL option
    archive_ph = f"https://archive.ph/{url}"
    add_mirror(archive_ph, "Archive.today / Archive.ph Mirror", "Archive.ph")

    # 3. Check Alternative TLD Mirrors for Domain
    try:
        parsed = urlparse(url)
        netloc_parts = parsed.netloc.split('.')
        if len(netloc_parts) >= 2:
            base_domain = netloc_parts[-2] if netloc_parts[-1] in ('com', 'org', 'net', 'co', 'io') and len(netloc_parts) > 2 else netloc_parts[0]
            if base_domain.startswith("www."):
                base_domain = base_domain[4:]

            tlds = ['is', 'to', 'st', 'se', 'cc', 'org', 'net', 'ru', 'me', 'lol']
            candidates = []
            for tld in tlds:
                cand_netloc = f"{base_domain}.{tld}"
                if cand_netloc != parsed.netloc:
                    cand_url = f"{parsed.scheme or 'https'}://{cand_netloc}{parsed.path}"
                    candidates.append((cand_url, f"{cand_netloc} Domain Mirror"))

            def _test_candidate(cand):
                c_url, label = cand
                if check_link_alive(c_url):
                    return c_url, label
                return None

            with ThreadPoolExecutor(max_workers=5) as pool:
                results = pool.map(_test_candidate, candidates)
                for r in results:
                    if r:
                        add_mirror(r[0], r[1], "Mirror")
    except Exception as e:
        logger.debug(f"Mirror domain check error for {url}: {e}")

    return mirrors


def fetch_full_article_text(url: str, html_content: str = None) -> str:
    """Extract clean readable text from a webpage."""
    try:
        if not html_content:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            resp = requests.get(url, headers=headers, timeout=8)
            html_content = resp.text
            
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Remove non-content elements
        for el in soup(['script', 'style', 'nav', 'footer', 'aside', 'header', 'iframe', 'svg', 'form', 'button', 'noscript', 'dialog']):
            el.decompose()
            
        # Target main content containers if possible
        main_content = soup.find('article') or soup.find('main') or soup.find('div', class_=lambda c: c and any(x in c.lower() for x in ['content', 'post', 'article', 'body'])) or soup.body
        
        if main_content:
            text = main_content.get_text(separator=' ', strip=True)
        else:
            text = soup.get_text(separator=' ', strip=True)
            
        # Normalize whitespace and limit length
        import re
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:12000]
    except Exception as e:
        logger.debug(f"Error fetching full article text for {url}: {e}")
        return ""


def fetch_youtube_transcript(url: str) -> str:
    """Extract closed captions / transcript text from a YouTube video URL."""
    try:
        from urllib.parse import parse_qs, urlparse
        
        # Extract video ID
        vid_id = None
        if "youtu.be/" in url:
            vid_id = url.split("youtu.be/")[1].split("?")[0].split("&")[0]
        elif "youtube.com" in url:
            query = parse_qs(urlparse(url).query)
            vid_id = query.get("v", [None])[0]
            
        if not vid_id:
            return ""
            
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            transcript_list = YouTubeTranscriptApi().fetch(vid_id)
            chunks = [t.text for t in transcript_list if getattr(t, 'text', None)]
            if chunks:
                return " ".join(chunks)[:15000]
        except Exception as api_err:
            logger.debug(f"YouTubeTranscriptApi failed for {vid_id}, falling back: {api_err}")
            
        return ""
    except Exception as e:
        logger.debug(f"YouTube transcript extraction error for {url}: {e}")
        return ""
