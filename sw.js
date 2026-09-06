/* MiyeePDF service worker.
 *
 * The engine is about 30 MB. Left to the ordinary HTTP cache it is evicted
 * fairly readily, and the next visit pays the whole download again -- which is
 * what "it takes minutes to load" actually felt like for repeat visitors.
 * A Cache Storage entry is not evicted under the same pressure, so the second
 * visit starts from disk and the app keeps working with no connection at all.
 *
 * Two caches, deliberately separated:
 *   SHELL   small, versioned, replaced on every release
 *   ENGINE  large, immutable, keyed by URL so it survives releases untouched
 */

const VERSION = '4.25.0';
const SHELL_CACHE = `miyee-shell-${VERSION}`;
const ENGINE_CACHE = 'miyee-engine-v1';

// Same-origin assets that make up the interface.
const SHELL_ASSETS = [
    './',
    './index.html',
    `./app.js?v=${VERSION}`,
    `./style.css?v=${VERSION}`,
    `./pdf_engine.py?v=${VERSION}`,
    './logo.svg',
    './favicon.svg',
    './favicon.ico',
    './icon-192.png',
    './icon-512.png',
    './apple-touch-icon.png',
    './site.webmanifest',
];

/** Big, immutable payloads: the PyMuPDF wheel, the Pyodide runtime and the
 *  OCR assets. Cached the first time they are fetched rather than up front, so
 *  installing the worker never blocks on 30 MB. */
function isEngineAsset(url) {
    return (
        url.pathname.includes('/vendor/') ||
        url.hostname === 'cdn.jsdelivr.net' ||
        /\.(wasm|whl|traineddata|zip)(\.gz)?$/.test(url.pathname) ||
        /pyodide|tesseract/i.test(url.pathname)
    );
}

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(SHELL_CACHE)
            // Individually, so one 404 cannot fail the whole install.
            .then((cache) => Promise.all(
                SHELL_ASSETS.map((asset) => cache.add(asset).catch(() => null))
            ))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(
                keys.filter((k) => k.startsWith('miyee-shell-') && k !== SHELL_CACHE)
                    .map((k) => caches.delete(k))
            ))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const request = event.request;
    if (request.method !== 'GET') return;

    let url;
    try {
        url = new URL(request.url);
    } catch {
        return;
    }
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return;

    // Engine payloads are immutable and huge: serve from cache whenever it is
    // there, and only reach the network on a miss.
    if (isEngineAsset(url)) {
        event.respondWith(
            caches.open(ENGINE_CACHE).then(async (cache) => {
                const hit = await cache.match(request);
                if (hit) return hit;
                const response = await fetch(request);
                // Opaque cross-origin responses are cached too: they replay
                // fine, and the CDN payloads are versioned by URL.
                if (response && (response.ok || response.type === 'opaque')) {
                    cache.put(request, response.clone()).catch(() => {});
                }
                return response;
            }).catch(() => fetch(request))
        );
        return;
    }

    if (url.origin !== self.location.origin) return;

    // The interface itself: try the network so a redeploy is picked up at once,
    // fall back to the cached copy when offline.
    event.respondWith(
        fetch(request)
            .then((response) => {
                if (response && response.ok) {
                    const copy = response.clone();
                    caches.open(SHELL_CACHE).then((c) => c.put(request, copy)).catch(() => {});
                }
                return response;
            })
            .catch(async () => {
                const cached = await caches.match(request);
                if (cached) return cached;
                // A navigation with nothing cached for that exact URL still
                // gets the app shell, so deep links work offline.
                if (request.mode === 'navigate') {
                    const shell = await caches.match('./index.html');
                    if (shell) return shell;
                }
                throw new Error('offline and not cached');
            })
    );
});
