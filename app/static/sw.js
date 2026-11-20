// Service worker: makes the app installable (PWA) and lets the UI shell open while offline.
// Network-first for the shell (so updates show up immediately); the API and PDFs are never cached.
const CACHE = "pdfm-shell-v2";
const SHELL = ["./", "index.html", "style.css", "app.js", "manifest.webmanifest",
  "icons/icon.svg", "icons/icon-192.png", "icons/icon-512.png"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

// Navigations ignore the ?query (UI state / document id): one cached copy per page.
// The list page (scope root or index.html) is stored as "./".
function cacheKey(req) {
  if (req.mode !== "navigate") return req;
  const url = new URL(req.url);
  const scope = new URL(self.registration.scope).pathname;
  if (url.pathname === scope || url.pathname === scope + "index.html") return "./";
  return url.origin + url.pathname;
}

self.addEventListener("fetch", event => {
  const req = event.request;
  const url = new URL(req.url);
  const scope = new URL(self.registration.scope);
  if (req.method !== "GET" || url.origin !== scope.origin) return;
  if (url.pathname.startsWith(scope.pathname + "api/")) return; // live data: browser default

  event.respondWith(
    fetch(req)
      .then(res => {
        if (res.ok && res.type === "basic") {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(cacheKey(req), copy));
        }
        return res;
      })
      .catch(async () => {
        const cached = await caches.match(cacheKey(req));
        return cached || Response.error();
      }),
  );
});
