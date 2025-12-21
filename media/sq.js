/* Service Worker for media-library UI
 *
 * Purpose:
 * - Cache static UI assets under /ui for fast loads + basic offline use
 * - Cache GET /media (network-first with cache fallback) so library page can load when offline
 *
 * NOTE: This service worker intentionally does NOT cache POST /scan.
 */

const CACHE_NAME = "media-library-ui-v3";

const PRECACHE_URLS = [
  "/ui/library.html",
  "/ui/scan.html",
  "/ui/zxing.min.js",
  "/ui/sq.js"
];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await cache.addAll(PRECACHE_URLS);
    self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.map((k) => (k !== CACHE_NAME ? caches.delete(k) : Promise.resolve()))
    );
    self.clients.claim();
  })());
});

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const resp = await fetch(request);
  const cache = await caches.open(CACHE_NAME);
  cache.put(request, resp.clone());
  return resp;
}

async function networkFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const resp = await fetch(request);
    if (resp && resp.ok) {
      cache.put(request, resp.clone());
    }
    return resp;
  } catch (e) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw e;
  }
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // Only handle same-origin requests
  if (url.origin !== self.location.origin) return;

  // Cache UI assets (cache-first)
  if (url.pathname.startsWith("/ui/")) {
    event.respondWith(cacheFirst(req));
    return;
  }

  // Cache library data (network-first, fallback to cache)
  if (url.pathname === "/media") {
    event.respondWith(networkFirst(req));
    return;
  }

  // Everything else: pass through
});
