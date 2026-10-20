// Offline copy of the app shell: the CSS, the scripts, the icons, the manifest and the
// Google Fonts, in one cache per app version - a new version replaces the old cache.
// Served from the root by the /sw.js route (__init__.py), which fills in VERSION and
// SHELL (the shell's URLs exactly as the pages ask for them, ?v=<mtime> included).
//
// Pages: network first, the last copy only when offline. Never cached: /api/* (search,
// playback, transcoding, subtitles...), VTT, video and TorrServer - nor anything else.
"use strict";

const VERSION = "dev";
const SHELL = [];
const PREFIX = "streamfinder-";
const CACHE = PREFIX + VERSION;
const FONTS = ["https://fonts.googleapis.com", "https://fonts.gstatic.com"];

self.addEventListener("install", (event) => {
  // One file that doesn't load must not leave the rest uncached.
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) =>
        Promise.all(SHELL.map((url) => cache.add(url).catch(() => {}))),
      )
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key.startsWith(PREFIX) && key !== CACHE)
            .map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

// Static files and fonts: from the cache, else the network (and kept). A changed file
// has another ?v=, so one URL never needs refreshing.
async function cacheFirst(request) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(request);
  if (hit) return hit;
  const response = await fetch(request);
  // The fonts' stylesheet is a no-cors request: its answer is opaque (status unknown).
  if (response.ok || response.type === "opaque")
    cache.put(request, response.clone());
  return response;
}

// Pages: always the current one, the copy only when the network is gone.
async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    const html = (response.headers.get("Content-Type") || "").startsWith(
      "text/html",
    );
    if (response.ok && html && !response.redirected)
      cache.put(request, response.clone());
    return response;
  } catch (error) {
    const hit = await cache.match(request);
    if (hit) return hit;
    throw error;
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  // Not answered here = the browser fetches it as if there were no service worker.
  if (request.method !== "GET" || request.headers.has("Range")) return;
  if (["video", "audio", "track"].includes(request.destination)) return;
  const url = new URL(request.url);
  if (url.origin === self.location.origin) {
    const path = url.pathname;
    if (path.startsWith("/api/") || path.endsWith(".vtt") || path === "/sw.js")
      return;
    if (path.startsWith("/static/")) event.respondWith(cacheFirst(request));
    else if (request.mode === "navigate")
      event.respondWith(networkFirst(request));
    return;
  }
  if (FONTS.includes(url.origin)) event.respondWith(cacheFirst(request));
  // Everything else (TMDB images, TorrServer...) goes past.
});
