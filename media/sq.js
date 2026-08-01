/* Service Worker for media-library UI
 *
 * Purpose:
 * - Serve app pages/assets under /ui from the network with the browser's HTTP
 *   cache explicitly bypassed, so a deploy shows up on the next reload. The SW
 *   keeps a copy only as an offline fallback and never serves it while online.
 * - Cache GET /media (network-first with cache fallback) so library page can load when offline
 *
 * NOTE: This service worker intentionally does NOT cache POST /scan.
 */

const CACHE_NAME = "media-library-ui-v5";

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

// For app pages/assets under /ui: always go to the network with the browser's
// HTTP cache explicitly bypassed, so a deploy is picked up on the very next
// reload. We keep a copy in the SW cache ONLY as an offline fallback, and never
// serve it while the network is reachable.
async function networkOnlyWithOfflineFallback(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    // cache:"no-store" forces the SW's own fetch to skip (and not populate) the
    // browser HTTP cache — without this, a network-first SW can still be handed
    // a stale copy by the browser cache and never notice a deploy.
    const resp = await fetch(request, { cache: "no-store" });
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

  // App pages/assets: network with browser-cache bypassed, so deploys show up
  // on the next reload. SW cache is used only as an offline fallback.
  if (url.pathname.startsWith("/ui/")) {
    event.respondWith(networkOnlyWithOfflineFallback(req));
    return;
  }

  // Cache library data (network-first, fallback to cache)
  if (url.pathname === "/media") {
    event.respondWith(networkFirst(req));
    return;
  }

  // Everything else: pass through
});