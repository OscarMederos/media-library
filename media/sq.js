const CACHE = "media-library-v2";
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

  if (url.pathname.startsWith("/ui/")) {
    event.respondWith(caches.match(req).then((cached) => cached || fetch(req)));
    return;
  }

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
