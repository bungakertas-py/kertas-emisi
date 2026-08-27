/* Service worker Kertas Emisi — cache SHELL app (berversi), data cuaca TETAP
 * online. Naikkan VERSION tiap rilis frontend agar user dapat versi terbaru
 * (cache lama dihapus saat activate). */
const VERSION = "v11";
const CACHE = "kertas-emisi-" + VERSION;

// Saat REVIEW LOKAL, jangan cache shell sama sekali. Strategi cache-first membuat
// peramban menyajikan app.js versi kemarin sementara data di /backend/ sudah versi
// hari ini. Selama keduanya tak saling bergantung itu cuma bikin telat satu muat
// ulang, tapi begitu ada perubahan yang menyeberang backend-frontend (mis. cara
// menempatkan gambar), hasilnya kode lama memakai data baru dan tampilannya
// justru lebih kacau daripada sebelum diperbaiki.
const DEV = ["127.0.0.1", "localhost", "0.0.0.0"].includes(location.hostname) ||
            /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(location.hostname);

// Shell same-origin (relatif thd scope frontend/). Data model (../backend/…)
// berada DI LUAR path frontend → sengaja tak di-cache (lihat handler fetch).
const SHELL = [
  "./", "./index.html", "./style.css", "./app.js", "./skewt.js", "./wilayah.js", "./dtw-preview.png",
  "./favicon.svg", "./manifest.webmanifest",
  "./icon-192.png", "./icon-512.png",
  "./data/world_countries.geojson", "./data/idn_provinces.geojson", "./data/id_places.json",
];

self.addEventListener("install", (e) => {
  self.skipWaiting();
  if (DEV) return;
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL).catch(() => {})));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  if (DEV) return;                                 // review lokal → selalu dari server
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;      // CDN (unpkg/fonts) → biar network
  if (url.pathname.includes("/backend/")) return;  // DATA CUACA → selalu online, jangan cache
  // Shell: sajikan dari cache dulu (cepat), sambil perbarui cache dari network.
  e.respondWith(
    caches.match(req).then((cached) => {
      const net = fetch(req)
        .then((res) => {
          if (res && res.ok) caches.open(CACHE).then((c) => c.put(req, res.clone()));
          return res;
        })
        .catch(() => cached);
      return cached || net;
    })
  );
});
