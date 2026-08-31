import sqlite3
from app import create_app
from services.pulse import refresh_pulse_feed

app = create_app()
app.app_context().push()

count = refresh_pulse_feed()
print(f"Ingested {count} fresh articles.")

conn = sqlite3.connect(r"data\dashboard.db")
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT id, title, source_name, topic, image_url FROM pulse_items ORDER BY relevance_score DESC, date_fetched DESC LIMIT 20").fetchall()
print(f"\nRetrieved {len(rows)} articles:")
auth_count = 0
for r in rows:
    img = r['image_url'] or ''
    is_auth = 'unsplash' not in img
    if is_auth:
        auth_count += 1
    t = r['title'].encode('ascii', 'ignore').decode()[:40]
    src = r['source_name'].encode('ascii', 'ignore').decode()
    top = (r['topic'] or '').encode('ascii', 'ignore').decode()
    kind = "AUTHENTIC" if is_auth else "DYNAMIC"
    print(f" • [{top}] {t}... | {src}")
    print(f"   Image ({kind}): {img[:75]}")

print(f"\nAuthentic Image Ratio: {auth_count}/{len(rows)} ({round(auth_count/len(rows)*100)}%)")
