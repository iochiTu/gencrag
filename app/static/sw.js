// Minimal service worker so phones can install the app ("Add to Home Screen").
// It does not cache anything: staff always see live products and prices.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
