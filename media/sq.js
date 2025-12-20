// Offline-first service worker (simple, iOS-friendly).
// Caches UI assets and provides network-first caching for /media.

const CACHE = "media-library-v1";
const ASSETS = [
  "/ui/scan.html",
  "/ui/library.html",
  "/ui/zxing.min.js",
  "/ui/sq.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Cache-first for UI assets under /ui
  if (url.pathname.startsWith("/ui/")) {
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req))
    );
    return;
  }

  // Network-first for /media (cache on success, fallback if offline)
  if (url.pathname === "/media") {
    event.respondWith(
      fetch(req)
        .then((resp) => {
          const copy = resp.clone();
          caches.open(CACHE).then((cache) => cache.put(req, copy));
          return resp;
        })
        .catch(() => caches.match(req))
    );
    return;
  }
});
