const CACHE_NAME = 'linkforge-cache-v1';
const STATIC_ASSETS = [
    '/',
    '/static/manifest.json',
    'https://cdn.tailwindcss.com?plugins=typography',
    'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css',
    'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap',
    'https://unpkg.com/vis-network/standalone/umd/vis-network.min.js'
];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            return cache.addAll(STATIC_ASSETS).catch(err => console.log('Asset cache partial fail', err));
        })
    );
    self.skipWaiting();
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys => {
            return Promise.all(
                keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
            );
        })
    );
    self.clients.claim();
});

self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);

    // Bypass API calls, websockets, and POST requests from cache
    if (event.request.method !== 'GET' || url.pathname.startsWith('/api/')) {
        return;
    }

    // Network-first strategy for dynamic page navigation, cache-first for CDN/static assets
    if (event.request.mode === 'navigate') {
        event.respondWith(
            fetch(event.request).catch(() => caches.match(event.request) || caches.match('/'))
        );
    } else {
        event.respondWith(
            caches.match(event.request).then(cached => {
                return cached || fetch(event.request).then(response => {
                    if (response.status === 200) {
                        const copy = response.clone();
                        caches.open(CACHE_NAME).then(cache => cache.put(event.request, copy));
                    }
                    return response;
                }).catch(() => null);
            })
        );
    }
});
