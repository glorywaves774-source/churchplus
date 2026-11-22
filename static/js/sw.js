/* ChurchPlus service worker – makes the app installable and gives a friendly offline page.
 * Privacy: pages are never cached (they hold church data). Only the app's own static files
 * (styles, scripts, fonts, icons) and the offline page are kept on the device. */
"use strict";
var VERSION = "__VERSION__";
var CACHE = "churchplus-static-" + VERSION;
var OFFLINE = "/offline";
var PRECACHE = __PRECACHE__;

self.addEventListener("install", function (event) {
  event.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(PRECACHE.concat([OFFLINE])); }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener("activate", function (event) {
  event.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k.indexOf("churchplus-") === 0 && k !== CACHE; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.method !== "GET") { return; }
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) { return; }
  if (req.mode === "navigate") {
    // Always fresh from the network; only when there is no connection show the offline page.
    event.respondWith(fetch(req).catch(function () { return caches.match(OFFLINE); }));
    return;
  }
  if (url.pathname.indexOf("/static/") === 0) {
    event.respondWith(caches.open(CACHE).then(function (cache) {
      return cache.match(req).then(function (hit) {
        var refresh = fetch(req).then(function (res) { if (res.ok) { cache.put(req, res.clone()); } return res; }).catch(function () { return hit; });
        return hit || refresh;
      });
    }));
  }
});
