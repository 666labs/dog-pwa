// Minimal service worker — exists purely so the browser offers "Add to Home
// Screen" / installability. This app is meaningless without a live backend, so
// we intentionally do NOT cache API responses or app assets for offline use.
// A no-op fetch handler is enough to satisfy the installability requirement.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => { /* pass through to network */ });
