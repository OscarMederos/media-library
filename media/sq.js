/* Service Worker for media-library UI
 *
 * Purpose:
 * - Cache static UI assets under /ui (network-first, so deploys are picked
 *   up immediately when online; falls back to cache when offline)
 * - Cache GET /media (network-first with cache fallback) so library page can load when offline
 *
 * NOTE: This service worker intentionally does NOT cache POST /scan.
 */

const CACHE_NAME = "media-library-ui-v4";

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

  // Cache UI assets (network-first so deploys take effect immediately when online)
  if (url.pathname.startsWith("/ui/")) {
    event.respondWith(networkFirst(req));
    return;
  }

  // Cache library data (network-first, fallback to cache)
  if (url.pathname === "/media") {
    event.respondWith(networkFirst(req));
    return;
  }

  // Everything else: pass through
});