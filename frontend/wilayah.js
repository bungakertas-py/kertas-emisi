/* ============================================================================
 * wilayah.js -- Daya Tampung Wilayah.
 * Unggah SHP (.zip) atau GeoJSON wilayah kajian -> overlay + zoom -> tombol
 * Hitung -> agregasi SEMUA variabel panel titik atas sel di dalam poligon,
 * per jam, lalu tampil di sidebar kanan (nilai + plot deret waktu).
 *
 * Mandiri: menyuntik tombol, panel, dan CSS sendiri. Menyambung ke fungsi app.js
 * yang sudah ada (loadSeries, seriesPlotSVG, bakuMutuLayer, nearestIndex, map,
 * frames, current). Tak menyentuh pipeline; nilai daya tampung per-sel (ton/tahun,
 * Permen LH No. 5) sudah ada di pd_dt_*.bin.gz, tinggal dijumlah di dalam poligon.
 * ========================================================================== */
(function () {
  "use strict";

  // --- variabel yang dihitung + aturan agregasi ---
  // sum  = kuantitas ekstensif (makin luas makin besar): daya tampung, paparan
  // mean = kuantitas intensif (konsentrasi/indeks): dirata-rata tertimbang luas
  var polyLayer = null;   // overlay Leaflet poligon
  var polygons = [];      // daftar poligon [ [ring, hole...], ... ] dalam WGS84
  var bbox = null;        // [minLon,minLat,maxLon,maxLat]

  // ---------------------------------------------------------------- util global
  // Global app.js (map, frames, current, nearestIndex, loadSeries, seriesPlotSVG,
  // bakuMutuLayer) berbagi lingkup global karena sama-sama classic script.
  function theMap() { return (typeof map !== "undefined") ? map : null; }
  function fmt(n, d) {
    if (n == null || isNaN(n)) return "-";
    return Number(n).toLocaleString("id-ID", { maximumFractionDigits: d == null ? 0 : d,
                                               minimumFractionDigits: 0 });
  }

  // ---------------------------------------------------- titik-dalam-poligon
  function ringHas(lon, lat, ring) {
    var inside = false, n = ring.length, j = n - 1;
    for (var i = 0; i < n; j = i++) {
      var xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
      if (((yi > lat) !== (yj > lat)) &&
          (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi)) inside = !inside;
    }
    return inside;
  }
  function polyHas(lon, lat, poly) {
    if (!ringHas(lon, lat, poly[0])) return false;      // di luar cincin luar
    for (var h = 1; h < poly.length; h++) if (ringHas(lon, lat, poly[h])) return false; // di lubang
    return true;
  }
  function wilayahHas(lon, lat) {
    for (var p = 0; p < polygons.length; p++) if (polyHas(lon, lat, polygons[p])) return true;
    return false;
  }

  // ------------------------------------------- kumpulkan poligon dari GeoJSON
  function ambilPoligon(gj) {
    polygons = []; bbox = [Infinity, Infinity, -Infinity, -Infinity];
    var feats = [];
    if (!gj) return;
    if (gj.type === "FeatureCollection") feats = gj.features || [];
    else if (gj.type === "Feature") feats = [gj];
    else if (gj.type) feats = [{ type: "Feature", geometry: gj }];
    function tambah(rings) { polygons.push(rings); rings.forEach(function (r) {
      r.forEach(function (c) {
        if (c[0] < bbox[0]) bbox[0] = c[0]; if (c[1] < bbox[1]) bbox[1] = c[1];
        if (c[0] > bbox[2]) bbox[2] = c[0]; if (c[1] > bbox[3]) bbox[3] = c[1];
      });
    }); }
    feats.forEach(function (f) {
      var g = (f && f.geometry) || f; if (!g) return;
      if (g.type === "Polygon") tambah(g.coordinates);
      else if (g.type === "MultiPolygon") g.coordinates.forEach(tambah);
    });
  }

  // pusat massa kasar (rata-rata simpul cincin luar) + luas poligon (km2, shoelace)
  function pusatWilayah() { return [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2]; }
  function luasWilayahKm2() {
    var latMid = (bbox[1] + bbox[3]) / 2, kx = 111.32 * Math.cos(latMid * Math.PI / 180), ky = 110.57, tot = 0;
    polygons.forEach(function (poly) {
      poly.forEach(function (ring, ri) {
        var a = 0;
        for (var i = 0, n = ring.length, j = n - 1; i < n; j = i++)
          a += (ring[j][0] * kx) * (ring[i][1] * ky) - (ring[i][0] * kx) * (ring[j][1] * ky);
        tot += (ri === 0 ? 1 : -1) * Math.abs(a / 2);   // lubang dikurangi
      });
    });
    return Math.abs(tot);
  }

  // Pecahan luas sel yang tertutup poligon (sub-sampel S x S). Jalur cepat: kalau
  // pusat + 4 sudut semua di dalam -> 1. Sel grid berpusat, membentang +-dx/2.
  function pecahanSel(lon, lat, dx, dy, S) {
    var x0 = lon - dx / 2, y0 = lat - dy / 2;
    var c = 0, uji = [[lon, lat], [x0, y0], [x0 + dx, y0], [x0, y0 + dy], [x0 + dx, y0 + dy]];
    for (var p = 0; p < 5; p++) if (wilayahHas(uji[p][0], uji[p][1])) c++;
    if (c === 5) return 1;
    var di = 0;
    for (var a = 0; a < S; a++) for (var b = 0; b < S; b++)
      if (wilayahHas(x0 + (a + 0.5) / S * dx, y0 + (b + 0.5) / S * dy)) di++;
    return di / (S * S);
  }

  // Daftar sel + PECAHAN cakupan. Semua layer berbagi grid sama (296x165).
  // Ukuran berapa pun masuk: sel dibobot pecahan luas, dan poligon lebih kecil
  // dari satu sel jatuh ke jaring pengaman (prorata luas ke sel pusat massa).
  function selDalamWilayah(meta) {
    var nx = meta.nx, ny = meta.ny, west = meta.west, north = meta.north;
    var dx = (meta.east - meta.west) / (nx - 1), dy = (meta.north - meta.south) / (ny - 1);
    var sel = [], luas = 0;
    var i0 = Math.max(0, Math.floor((bbox[0] - west) / dx) - 1);
    var i1 = Math.min(nx - 1, Math.ceil((bbox[2] - west) / dx) + 1);
    var j0 = Math.max(0, Math.floor((north - bbox[3]) / dy) - 1);
    var j1 = Math.min(ny - 1, Math.ceil((north - bbox[1]) / dy) + 1);
    var nCell = Math.max(1, (i1 - i0 + 1) * (j1 - j0 + 1));
    var S = Math.max(2, Math.min(10, Math.round(Math.sqrt(120000 / nCell))));   // budget adaptif
    for (var j = j0; j <= j1; j++) {
      var lat = north - j * dy, w = Math.cos(lat * Math.PI / 180);
      var cellKm2 = (dy * 110.57) * (dx * 111.32 * w);
      for (var i = i0; i <= i1; i++) {
        var lon = west + i * dx, frac = pecahanSel(lon, lat, dx, dy, S);
        if (frac > 0) { sel.push({ off: j * nx + i, frac: frac, w: w }); luas += frac * cellKm2; }
      }
    }
    // JAMINAN ukuran kecil: poligon lebih kecil dari satu sel -> prorata ke sel pusat massa
    if (!sel.length) {
      var c = pusatWilayah(), ci = Math.round((c[0] - west) / dx), cj = Math.round((north - c[1]) / dy);
      if (ci >= 0 && ci < nx && cj >= 0 && cj < ny) {
        var lat2 = north - cj * dy, w2 = Math.cos(lat2 * Math.PI / 180);
        var cellKm2b = (dy * 110.57) * (dx * 111.32 * w2), pa = luasWilayahKm2();
        var frac2 = Math.max(1e-4, Math.min(1, pa / cellKm2b));
        sel = [{ off: cj * nx + ci, frac: frac2, w: w2 }]; luas = pa;
      }
    }
    return { sel: sel, luas: luas, nx: nx, ny: ny, kecil: (sel.length <= 1) };
  }

  // ------------------------------------------------------ agregasi satu variabel
  function agregasi(pd, sel, mode) {
    var nx = pd.meta.nx, ny = pd.meta.ny, nt = pd.meta.nt, sc = pd.meta.scale, arr = pd.arr;
    var plane = nx * ny, out = new Array(nt);
    for (var t = 0; t < nt; t++) {
      var b = t * plane, s = 0, wsum = 0;
      for (var k = 0; k < sel.length; k++) {
        var v = arr[b + sel[k].off] * sc, fr = sel[k].frac;
        if (mode === "sum") s += v * fr;                       // ekstensif: prorata luas
        else { var wk = fr * sel[k].w; s += v * wk; wsum += wk; }   // intensif: rata2 tertimbang
      }
      out[t] = (mode === "sum") ? s : (wsum ? s / wsum : NaN);
    }
    return out;
  }
  function nilaiAktif(pd, vals) {
    var i = 0;
    if (typeof frames !== "undefined" && frames[current] && typeof nearestIndex === "function")
      i = Math.max(0, nearestIndex(pd.meta.times, frames[current].valid_time));
    return { i: i, v: vals[i] };
  }

  // =============================================================== HITUNG
  var _busy = false;
  function paramDT() {   // parameter dari layer daya tampung yang sedang aktif
    return (typeof activeLayer !== "undefined" && /^dt_/.test(activeLayer)) ? activeLayer.slice(3) : null;
  }
  function hitung() {
    if (_busy) return;
    if (!polygons.length) { setHasil('<p class="dtw-warn">Muat berkas atau gambar wilayah dulu.</p>'); return; }
    if (typeof loadSeries !== "function") { setHasil('<p class="dtw-warn">Fungsi data tak ditemukan.</p>'); return; }
    var par = paramDT();
    if (!par) { setHasil('<p class="dtw-warn">Pilih salah satu layer Daya Tampung dulu.</p>'); return; }
    _busy = true; setHasil('<p class="dtw-muted">Menghitung…</p>');
    var selInfo = null, dtPd = null;
    loadSeries("dt_" + par).then(function (pd) {
      dtPd = pd; selInfo = selDalamWilayah(pd.meta);
      if (!selInfo.sel.length) throw new Error("__luar__");
      return loadSeries("dt_vol");
    }).then(function (pv) {
      render(par, dtPd, pv, selInfo); _busy = false;
    }).catch(function (e) {
      _busy = false;
      if (e && e.message === "__luar__")
        setHasil('<p class="dtw-warn">Wilayah di luar cakupan data (62°–180° BT, 32,8° LU–32,8° LS).</p>');
      else setHasil('<p class="dtw-warn">Gagal menghitung: ' + (e && e.message ? e.message : e) + '</p>');
    });
  }

  // =============================================================== RENDER
  // Meniru panel titik dt_ PERSIS (angka + status + neraca + plot), pakai class
  // .pp-ispu/.pp-rinci/.pp-kritis milik app.js — bedanya nilainya agregat wilayah.
  function render(par, dtPd, volPd, selInfo) {
    var dtVals = agregasi(dtPd, selInfo.sel, "sum");
    var volVals = agregasi(volPd, selInfo.sel, "sum");
    var i = nilaiAktif(dtPd, dtVals).i;
    var nilai = dtVals[i], vol = volVals[i];
    var r0 = function (v) { return Math.round(v).toLocaleString("id-ID"); };
    var bg = (typeof dtWarna === "function") ? dtWarna(nilai) : "#4cff4c";
    var putih = (typeof DT_PUTIH !== "undefined" && typeof dtIndeks === "function") ? DT_PUTIH[dtIndeks(nilai)] : 0;
    var nama = nilai < 0 ? "Beban maksimum terlampaui" : "Masih ada daya tampung";

    var rinci = "", bmua = (typeof BAKU_MUTU !== "undefined" && BAKU_MUTU[par]) ? BAKU_MUTU[par]["24 jam"] : null;
    if (isFinite(vol) && vol > 0 && bmua) {
      var beMaxHari = (vol * 1e9) * bmua / 1e12, beEksHari = beMaxHari - nilai / 365;
      rinci = '<div class="pp-rinci">' +
        '<div><span>Volume udara</span><b>' + r0(vol) + ' km³</b></div>' +
        '<div><span>BE maksimum</span><b>' + r0(beMaxHari * 365) + ' ton/th</b></div>' +
        '<div><span>BE eksisting</span><b>' + r0(beEksHari * 365) + ' ton/th</b></div>' +
        '<div><span>BMUA 24 jam</span><b>' + bmua + ' µg/m³</b></div></div>';
    }
    var judul = (typeof KIMIA_HTML !== "undefined" && KIMIA_HTML["dt_" + par]) ? KIMIA_HTML["dt_" + par] : ("Daya Tampung " + par.toUpperCase());
    var kepala = '<div class="dtw-judul">' + judul + '</div>' +
      '<div class="pp-ispu' + (putih ? " putih" : "") + '" style="background:' + bg + '">' +
      '<b>' + r0(nilai) + '</b><span>' + nama + '</span></div>' +
      '<div class="pp-kritis">ton/tahun, Permen LH No. 5</div>' + rinci;

    var plotSvg = "";
    if (typeof seriesPlotSVG === "function")
      try { plotSvg = seriesPlotSVG(dtVals, dtPd.meta.times, "ton/tahun", !!dtPd.meta.daily, bg, [[0, "#ff4c4c"], [Infinity, "#4cff4c"]], null); } catch (e) {}

    var ringkas = '<div class="dtw-sum">' +
      '<div><span class="dtw-k">Luas</span><b>' + fmt(selInfo.luas) + ' km²</b></div>' +
      '<div><span class="dtw-k">Sel grid</span><b>' + fmt(selInfo.sel.length) + '</b></div></div>' +
      (selInfo.kecil ? '<p class="dtw-note" style="margin-top:0">Wilayah ≤ satu sel (~44 km); nilai diprorata dari luas, bersifat perkiraan.</p>' : '');

    setHasil(ringkas + kepala + plotSvg);
  }

  // =============================================================== UNGGAH SHP
  // Normalkan hasil (FC tunggal, array FC, Feature, geometry) jadi satu FC.
  function ratakan(gj) {
    if (Array.isArray(gj)) {
      var feats = [];
      gj.forEach(function (x) { var f = ratakan(x); (f.features || []).forEach(function (ff) { feats.push(ff); }); });
      return { type: "FeatureCollection", features: feats };
    }
    if (gj && gj.type === "FeatureCollection") return gj;
    if (gj && gj.type === "Feature") return { type: "FeatureCollection", features: [gj] };
    if (gj && gj.type) return { type: "FeatureCollection", features: [{ type: "Feature", geometry: gj }] };
    return { type: "FeatureCollection", features: [] };
  }

  // KML -> GeoJSON (poligon saja, cukup untuk batas wilayah). Tanpa pustaka.
  function kmlKeGeoJSON(text) {
    var doc = new DOMParser().parseFromString(text, "text/xml");
    function koords(el) {
      return (el.textContent || "").trim().split(/\s+/).map(function (p) {
        var a = p.split(","); return [parseFloat(a[0]), parseFloat(a[1])];
      }).filter(function (c) { return isFinite(c[0]) && isFinite(c[1]); });
    }
    var polys = doc.getElementsByTagName("Polygon"), feats = [];
    for (var i = 0; i < polys.length; i++) {
      var pg = polys[i], rings = [];
      var out = pg.getElementsByTagName("outerBoundaryIs")[0];
      if (out) { var lr = out.getElementsByTagName("coordinates")[0]; if (lr) rings.push(koords(lr)); }
      var inn = pg.getElementsByTagName("innerBoundaryIs");
      for (var h = 0; h < inn.length; h++) { var c = inn[h].getElementsByTagName("coordinates")[0]; if (c) rings.push(koords(c)); }
      if (rings.length) feats.push({ type: "Feature", geometry: { type: "Polygon", coordinates: rings } });
    }
    return { type: "FeatureCollection", features: feats };
  }

  // WKT sederhana (POLYGON / MULTIPOLYGON).
  function wktKeGeoJSON(text) {
    function ring(s) { return s.split(",").map(function (p) { var a = p.trim().split(/\s+/); return [parseFloat(a[0]), parseFloat(a[1])]; }); }
    function poly(s) { var r = [], m; var re = /\(([^()]+)\)/g; while ((m = re.exec(s))) r.push(ring(m[1])); return r; }
    var feats = [], up = text.toUpperCase();
    if (up.indexOf("MULTIPOLYGON") >= 0) {
      var body = text.slice(text.indexOf("(") + 1);
      var parts = body.match(/\(\([^]*?\)\)/g) || [];
      parts.forEach(function (p) { feats.push({ type: "Feature", geometry: { type: "Polygon", coordinates: poly(p) } }); });
    } else if (up.indexOf("POLYGON") >= 0) {
      feats.push({ type: "Feature", geometry: { type: "Polygon", coordinates: poly(text) } });
    }
    return { type: "FeatureCollection", features: feats };
  }

  // Teks apa pun -> GeoJSON (deteksi isi: JSON/TopoJSON, KML, WKT).
  function teksKeGeoJSON(t, nama) {
    var s = t.replace(/^﻿/, "").trim();
    if (s[0] === "{" || nama.endsWith(".geojson") || nama.endsWith(".json")) {
      var o = JSON.parse(s);
      if (o && o.type === "Topology") {
        if (typeof topojson !== "undefined" && topojson.feature) {
          var out = [];
          for (var k in o.objects) out.push(topojson.feature(o, o.objects[k]));
          return ratakan(out);
        }
        throw new Error("TopoJSON: konversi ke GeoJSON dulu (pustaka topojson tak dimuat).");
      }
      return o;
    }
    if (s.indexOf("<kml") >= 0 || s.indexOf("<Polygon") >= 0 || s.indexOf("<coordinates") >= 0 || nama.endsWith(".kml"))
      return kmlKeGeoJSON(s);
    if (/POLYGON/i.test(s)) return wktKeGeoJSON(s);
    throw new Error("Format tak dikenal.");
  }

  // Arsip (.zip shapefile / .kmz / zip berisi geojson/kml) -> GeoJSON.
  function arsipKeGeoJSON(buf, nama) {
    if (typeof JSZip === "undefined") {
      // tanpa JSZip: satu-satunya yang bisa = shapefile via shpjs
      if (typeof shp === "function") return shp(buf).then(ratakan);
      return Promise.reject(new Error("Pustaka arsip (JSZip) belum termuat."));
    }
    return JSZip.loadAsync(buf).then(function (zip) {
      var names = Object.keys(zip.files);
      var shpN = names.find(function (n) { return /\.shp$/i.test(n); });
      var kmlN = names.find(function (n) { return /\.kml$/i.test(n); });
      var gjN = names.find(function (n) { return /\.(geojson|json)$/i.test(n); });
      if (shpN && typeof shp === "function") return shp(buf).then(ratakan);   // shpjs baca zip utuh
      if (kmlN) return zip.files[kmlN].async("string").then(function (t) { return kmlKeGeoJSON(t); });
      if (gjN) return zip.files[gjN].async("string").then(function (t) { return JSON.parse(t); });
      throw new Error("Arsip tak berisi .shp/.kml/.geojson.");
    });
  }

  function muatFile(file) {
    var nama = (file.name || "").toLowerCase();
    setStatus("Membaca " + file.name + " …");
    var isZip = nama.endsWith(".zip") || nama.endsWith(".kmz");
    var kerja = isZip
      ? file.arrayBuffer().then(function (buf) { return arsipKeGeoJSON(buf, nama); })
      : file.text().then(function (t) { return teksKeGeoJSON(t, nama); });
    kerja.then(function (gj) { pakaiGeoJSON(ratakan(gj), file.name); })
      .catch(function (e) { setStatus("Gagal baca berkas: " + (e && e.message ? e.message : e), true); });
  }

  function pakaiGeoJSON(gj, nama) {
    ambilPoligon(gj);
    if (!polygons.length) { setStatus("Tak ada poligon di berkas ini.", true); return; }
    // sanity proyeksi: harus lat/lon
    if (Math.abs(bbox[0]) > 180 || Math.abs(bbox[2]) > 180 || Math.abs(bbox[1]) > 90 || Math.abs(bbox[3]) > 90) {
      setStatus("Koordinat di luar lat/lon — SHP-nya berproyeksi (mis. UTM) dan gagal direproyeksi. " +
        "Ekspor ulang ke WGS84, atau pakai GeoJSON WGS84.", true);
      polygons = []; return;
    }
    gambarOverlay();
    setStatus("Wilayah termuat: " + nama + " — " + polygons.length + " poligon. Klik Hitung.");
    var btn = document.getElementById("dtw-hitung"); if (btn) btn.disabled = false;
    tampilTombolWilayah(true);
  }

  function gambarOverlay() {
    var M = theMap(); if (!M || typeof L === "undefined") return;
    if (polyLayer) { M.removeLayer(polyLayer); polyLayer = null; }
    var fc = { type: "FeatureCollection", features: polygons.map(function (rings) {
      return { type: "Feature", geometry: { type: "Polygon", coordinates: rings } }; }) };
    polyLayer = L.geoJSON(fc, { style: { color: "#111", weight: 2, fillColor: "#d2ed26", fillOpacity: 0.18 } }).addTo(M);
    M.fitBounds([[bbox[1], bbox[0]], [bbox[3], bbox[2]]], { padding: [24, 24] });
  }

  // =============================================================== DIGITASI + EDIT
  // ring = daftar [lon,lat] (terbuka). Saat menggambar/edit, handler klik peta milik
  // app DILEPAS sementara (disimpan, lalu dipasang lagi) supaya popup titik/belah
  // ketupat TIDAK muncul. Handle edit = marker draggable biasa (markerPane).
  var draw = { on: false, edit: false, ring: [], prev: null, handles: null, saved: null };

  function setDrawBtn(on) {
    var b = document.getElementById("dtw-draw"); if (!b) return;
    b.classList.toggle("dtw-draw-on", on);
    b.innerHTML = on ? '<span class="material-symbols-outlined">check</span>Selesai menggambar'
                     : '<span class="material-symbols-outlined">draw</span>Gambar di peta';
  }
  function setEditBtn(on) {
    var b = document.getElementById("dtw-edit"); if (!b) return;
    b.classList.toggle("dtw-draw-on", on);
    b.innerHTML = on ? '<span class="material-symbols-outlined">check</span>Selesai edit'
                     : '<span class="material-symbols-outlined">edit</span>Edit titik';
  }
  // Lepas SEMUA handler klik/dblklik peta milik app (simpan) -> gambar/edit tak
  // memicu popup titik. Dipasang kembali saat selesai.
  function lepasAppKlik() {
    var M = theMap(); if (!M || draw.saved) return;
    var ev = M._events || {};
    draw.saved = { click: (ev.click || []).slice(), dblclick: (ev.dblclick || []).slice() };
    M.off("click"); M.off("dblclick");
  }
  function pasangAppKlik() {
    var M = theMap(); if (!M || !draw.saved) return;
    draw.saved.click.forEach(function (l) { M.on("click", l.fn, l.ctx); });
    draw.saved.dblclick.forEach(function (l) { M.on("dblclick", l.fn, l.ctx); });
    draw.saved = null;
  }
  function llArr() { return draw.ring.map(function (p) { return [p[1], p[0]]; }); }
  function bersihPreview() {
    var M = theMap();
    if (draw.prev && M) { M.removeLayer(draw.prev); draw.prev = null; }
    if (draw.handles && M) { M.removeLayer(draw.handles); draw.handles = null; }
  }
  function dotIcon(mid) {
    return L.divIcon({ className: "dtw-h", iconSize: [14, 14], iconAnchor: [7, 7],
      html: '<span class="dtw-dot' + (mid ? " dtw-mid" : "") + '"></span>' });
  }
  function tampilPreview(tutup) {
    var M = theMap(); if (!M) return;
    bersihPreview();
    var lls = llArr();
    draw.prev = (tutup ? L.polygon(lls, { color: "#111", weight: 2, fillColor: "#d2ed26", fillOpacity: 0.18 })
                       : L.polyline(lls, { color: "#111", weight: 2, dashArray: "5,5" })).addTo(M);
    draw.handles = L.layerGroup().addTo(M);
    draw.ring.forEach(function (p, k) {
      var m = L.marker([p[1], p[0]], { icon: dotIcon(false), draggable: draw.edit, keyboard: false, bubblingMouseEvents: false }).addTo(draw.handles);
      if (draw.edit) {
        m.on("drag", function (e) { var ll = e.target.getLatLng(); draw.ring[k] = [ll.lng, ll.lat]; if (draw.prev) draw.prev.setLatLngs(llArr()); });
        m.on("dragend", function () { tampilPreview(true); });
        m.on("click", function (e) { if (e && L.DomEvent) L.DomEvent.stop(e); if (draw.ring.length > 3) { draw.ring.splice(k, 1); tampilPreview(true); } });
      }
    });
    if (draw.edit && draw.ring.length >= 2) draw.ring.forEach(function (p, k) {
      var q = draw.ring[(k + 1) % draw.ring.length];
      var mm = L.marker([(p[1] + q[1]) / 2, (p[0] + q[0]) / 2], { icon: dotIcon(true), keyboard: false, bubblingMouseEvents: false }).addTo(draw.handles);
      mm.on("click", function (e) { if (e && L.DomEvent) L.DomEvent.stop(e); draw.ring.splice(k + 1, 0, [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2]); tampilPreview(true); });
    });
  }
  function onDrawClick(e) { draw.ring.push([e.latlng.lng, e.latlng.lat]); tampilPreview(false); }
  function finalisasiRing() {
    bersihPreview();
    var ring = draw.ring.slice(); ring.push(ring[0].slice());
    ambilPoligon({ type: "Polygon", coordinates: [ring] });
    gambarOverlay();
    var fn = document.getElementById("dtw-fname"); if (fn) fn.textContent = "(digitasi manual)";
    var b = document.getElementById("dtw-hitung"); if (b) b.disabled = false;
    tampilTombolWilayah(true);
  }
  function mulaiGambar() {
    var M = theMap(); if (!M || typeof L === "undefined") return;
    if (draw.on) return selesaiGambar();
    batalGambar();
    if (polyLayer) { M.removeLayer(polyLayer); polyLayer = null; }
    draw.on = true; draw.edit = false; draw.ring = [];
    if (M.doubleClickZoom) M.doubleClickZoom.disable();
    lepasAppKlik();
    M.on("click", onDrawClick); M.on("dblclick", selesaiGambar);
    M.getContainer().style.cursor = "crosshair";
    tampilPreview(false); setDrawBtn(true);
    setStatus("Klik di peta untuk menaruh titik. Klik dua kali (atau Selesai) untuk menutup.");
  }
  function selesaiGambar() {
    var M = theMap(); if (!M || !draw.on) return;
    draw.on = false; setDrawBtn(false);
    M.off("click", onDrawClick); M.off("dblclick", selesaiGambar);
    if (M.doubleClickZoom) M.doubleClickZoom.enable();
    M.getContainer().style.cursor = "";
    pasangAppKlik();
    if (draw.ring.length < 3) { setStatus("Butuh minimal 3 titik. Digitasi dibatalkan.", true); bersihPreview(); draw.ring = []; return; }
    finalisasiRing();
    setStatus("Wilayah digambar: " + draw.ring.length + " titik. Bisa Edit titik, lalu Hitung.");
  }
  function mulaiEdit() {
    var M = theMap(); if (!M) return;
    if (draw.edit) {                                   // selesai edit -> simpan
      draw.edit = false; setEditBtn(false); pasangAppKlik();
      if (draw.ring.length < 3) { setStatus("Butuh minimal 3 titik.", true); return; }
      finalisasiRing(); setStatus("Wilayah diperbarui: " + draw.ring.length + " titik. Klik Hitung.");
      return;
    }
    if (!polygons.length) { setStatus("Belum ada wilayah untuk diedit.", true); return; }
    var outer = polygons[0][0].slice();
    if (outer.length > 1) { var a = outer[0], z = outer[outer.length - 1]; if (a[0] === z[0] && a[1] === z[1]) outer.pop(); }
    draw.ring = outer.map(function (c) { return [c[0], c[1]]; });
    draw.edit = true; setEditBtn(true);
    if (polyLayer) { M.removeLayer(polyLayer); polyLayer = null; }
    lepasAppKlik();
    tampilPreview(true);
    setStatus("Seret titik untuk memindah, klik titik untuk hapus, klik titik tengah untuk menambah. Lalu Selesai edit.");
  }
  function batalGambar() {
    var M = theMap();
    if (M) {
      M.off("click", onDrawClick); M.off("dblclick", selesaiGambar);
      if (M.doubleClickZoom) M.doubleClickZoom.enable();
      M.getContainer().style.cursor = "";
    }
    pasangAppKlik();
    draw.on = false; draw.edit = false; setDrawBtn(false); setEditBtn(false);
    bersihPreview(); draw.ring = [];
  }

  // =============================================================== UI
  function setStatus(t, err) {
    var el = document.getElementById("dtw-status");
    if (el) { el.textContent = t; el.className = "dtw-status" + (err ? " err" : ""); }
  }
  function setHasil(html) { var el = document.getElementById("dtw-hasil"); if (el) el.innerHTML = html; }
  function tampilTombolWilayah(show) {
    var e = document.getElementById("dtw-edit"), h = document.getElementById("dtw-hapus");
    if (e) e.style.display = show ? "" : "none";
    if (h) h.style.display = show ? "" : "none";
  }
  function resetWilayah() {
    batalGambar();
    var M = theMap(); if (polyLayer && M) { M.removeLayer(polyLayer); polyLayer = null; }
    polygons = []; bbox = null;
    var fn = document.getElementById("dtw-fname"); if (fn) fn.textContent = "Belum ada berkas";
    var fi = document.getElementById("dtw-file"); if (fi) fi.value = "";
    var b = document.getElementById("dtw-hitung"); if (b) b.disabled = true;
    tampilTombolWilayah(false);
    setHasil(""); setStatus("");
  }

  function togglePanel() {
    var p = document.getElementById("dtw-panel"), b = document.getElementById("dtw-toggle");
    if (!p) return;
    var buka = !p.classList.contains("open");
    p.classList.toggle("open", buka);
    if (b) b.classList.toggle("active", buka);
    if (buka && polygons.length && !polyLayer) gambarOverlay();   // gambar ulang kalau sempat dihapus
    if (!buka && draw.on) batalGambar();                          // tutup panel saat menggambar -> batal
  }

  // Toggle hanya relevan di tampilan Daya Tampung (layer dt_*). Muncul saat masuk,
  // sembunyi + tutup panel + bersihkan overlay saat keluar.
  function isDT() { return typeof activeLayer !== "undefined" && /^dt_/.test(activeLayer); }
  function syncDTW() {
    var wrap = document.querySelector(".dtw-wrap"); if (!wrap) return;
    var on = isDT();
    wrap.classList.toggle("dtw-on", on);
    if (!on) {
      if (draw.on) batalGambar();
      var p = document.getElementById("dtw-panel"), b = document.getElementById("dtw-toggle");
      if (p) p.classList.remove("open");
      if (b) b.classList.remove("active");
      var M = theMap(); if (polyLayer && M) { M.removeLayer(polyLayer); polyLayer = null; }
    }
  }

  function bangunUI() {
    // CSS
    var st = document.createElement("style");
    st.textContent = [
      ".dtw-wrap{position:relative;pointer-events:auto}",
      "#dtw-panel{position:fixed;top:0;right:0;height:100%;width:380px;max-width:92vw;",
      "  background:var(--surface,#fff);color:var(--ink,#171a21);border-left:var(--bw,3px) solid var(--ink,#171a21);",
      "  box-shadow:var(--shadow,-6px 0 0 rgba(0,0,0,.12));transform:translateX(101%);",
      "  transition:transform .25s ease;z-index:1200;display:flex;flex-direction:column;font-size:14px}",
      "#dtw-panel.open{transform:translateX(0)}",
      "#dtw-panel .dtw-head{display:flex;align-items:center;justify-content:space-between;gap:8px;",
      "  padding:14px 16px;border-bottom:var(--bw,3px) solid var(--ink,#171a21)}",
      "#dtw-panel .dtw-head h3{margin:0;font:800 16px/1.2 'Archivo Black',system-ui,sans-serif}",
      "#dtw-panel .dtw-x{border:0;background:none;font-size:22px;cursor:pointer;line-height:1;color:inherit}",
      "#dtw-panel .dtw-body{overflow:auto;padding:14px 16px;flex:1}",
      ".dtw-input{display:flex;flex-direction:column;gap:8px;margin-bottom:12px}",
      ".dtw-lbl{font-weight:600;font-size:12.5px}",
      ".dtw-file-row{display:flex;align-items:center;gap:8px;min-width:0}",
      ".dtw-file-btn{display:inline-flex;align-items:center;gap:6px;border:var(--bw,3px) solid var(--ink,#171a21);",
      "  background:var(--surface,#fff);color:inherit;font:600 13px 'Space Grotesk',system-ui,sans-serif;",
      "  padding:7px 11px;cursor:pointer;box-shadow:var(--shadow,3px 3px 0 rgba(0,0,0,.12));white-space:nowrap}",
      ".dtw-file-btn:hover{background:var(--lime,#d2ed26)}",
      ".dtw-file-btn .material-symbols-outlined{font-size:18px}",
      ".dtw-fname{font-size:12px;color:var(--ink-soft,#4a5262);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}",
      ".dtw-or{font-size:10.5px;color:var(--ink-faint,#7b8494);text-align:center;text-transform:uppercase;letter-spacing:.12em}",
      ".dtw-hint{font-size:11px;color:var(--ink-soft,#4a5262);line-height:1.5}",
      ".dtw-draw-on{background:var(--lime,#d2ed26)!important}",
      ".dtw-tools{display:flex;gap:8px}.dtw-tools>button{flex:1;justify-content:center}",
      ".dtw-icon-btn{display:inline-flex;align-items:center;justify-content:center;border:var(--bw,3px) solid var(--ink,#171a21);",
      "  background:var(--surface,#fff);color:inherit;padding:6px;cursor:pointer;box-shadow:var(--shadow,3px 3px 0 rgba(0,0,0,.12));line-height:0}",
      ".dtw-icon-btn:hover{background:#ffd7d1;color:#c0392b}.dtw-icon-btn .material-symbols-outlined{font-size:18px}",
      ".dtw-judul{font:800 13px/1.2 'Archivo Black',system-ui,sans-serif;margin:6px 0 8px}",
      ".dtw-reset{width:100%;margin-top:16px;border:var(--bw,3px) solid var(--ink,#171a21);background:var(--surface,#fff);color:inherit;",
      "  font:700 14px 'Space Grotesk',system-ui,sans-serif;padding:11px;cursor:pointer;box-shadow:var(--shadow,4px 4px 0 rgba(0,0,0,.15));",
      "  display:inline-flex;align-items:center;justify-content:center;gap:8px}",
      ".dtw-reset:hover{background:#ffd7d1;color:#c0392b}",
      ".dtw-dot{display:block;width:12px;height:12px;border-radius:50%;background:#111;border:2px solid #fff;box-shadow:0 0 0 1px #111;cursor:pointer}",
      ".dtw-mid{width:10px;height:10px;background:#fff;border:2px dashed #111;box-shadow:none;opacity:.9}",
      ".leaflet-marker-icon.dtw-h{background:none;border:none}",
      ".dtw-btn{border:var(--bw,3px) solid var(--ink,#171a21);background:var(--lime,#d2ed26);",
      "  font:700 14px 'Space Grotesk',system-ui,sans-serif;padding:9px 14px;cursor:pointer;",
      "  box-shadow:var(--shadow,4px 4px 0 rgba(0,0,0,.15))}",
      ".dtw-btn:disabled{opacity:.45;cursor:not-allowed}",
      ".dtw-status{font-size:12.5px;color:var(--ink-soft,#4a5262);min-height:1.2em}",
      ".dtw-status.err{color:#c0392b}",
      ".dtw-sum{display:flex;gap:10px;margin:4px 0 10px}",
      ".dtw-sum>div{flex:1;border:1px solid var(--line,#dce0e8);border-radius:8px;padding:8px 10px}",
      ".dtw-sum .dtw-k{display:block;font-size:11px;color:var(--ink-soft,#4a5262)}",
      ".dtw-sum b{font-size:16px}",
      ".dtw-grup{margin:16px 0 6px;font:800 12px/1 'Archivo Black',sans-serif;text-transform:uppercase;",
      "  letter-spacing:.05em;border-bottom:2px solid var(--line,#dce0e8);padding-bottom:5px}",
      ".dtw-item{margin:10px 0 14px}",
      ".dtw-row{display:flex;align-items:baseline;justify-content:space-between;gap:8px}",
      ".dtw-lab{font-weight:600}.dtw-val{font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap}",
      ".dtw-val i{font-style:normal;font-weight:400;font-size:11px;color:var(--ink-soft,#4a5262)}",
      ".dtw-tag{font-size:11.5px;font-weight:700}",
      ".dtw-plot{margin-top:4px}.dtw-plot svg{max-width:100%;height:auto}",
      ".dtw-note{font-size:11.5px;color:var(--ink-soft,#4a5262);margin-top:14px;line-height:1.5}",
      ".dtw-warn{color:#c0392b;font-size:13px}.dtw-muted{color:var(--ink-soft,#4a5262)}",
      // Toggle hanya tampil saat layer daya tampung aktif (kelas .dtw-on)
      ".dtw-wrap{display:none}.dtw-wrap.dtw-on{display:block}",
      "@media(max-width:640px){.col.items-end>.dtw-wrap.dtw-on{display:none}.ctrl-open>.dtw-wrap.dtw-on{display:block}}",
      // Alarm merah MENCOLOK tapi mulus: glow kuat + denyut skala + cincin 'ping'
      // yang memancar. Bayangan neubrutalist tombol dipertahankan di keyframe.
      // Berhenti saat panel dibuka; hormati prefers-reduced-motion.
      "@keyframes dtwGlow{0%,100%{box-shadow:var(--shadow,4px 4px 0 rgba(0,0,0,.15)),0 0 8px 2px rgba(224,49,49,.55)}" +
        "50%{box-shadow:var(--shadow,4px 4px 0 rgba(0,0,0,.15)),0 0 24px 10px rgba(224,49,49,1)}}",
      "@keyframes dtwBeat{0%,100%{transform:scale(1)}50%{transform:scale(1.1)}}",
      "@keyframes dtwPing{0%{transform:scale(1);opacity:.85}70%,100%{transform:scale(2.2);opacity:0}}",
      ".dtw-wrap.dtw-on #dtw-toggle{position:relative;z-index:1;animation:dtwGlow 1.3s ease-in-out infinite,dtwBeat 1.3s ease-in-out infinite}",
      ".dtw-wrap.dtw-on #dtw-toggle::after{content:'';position:absolute;inset:-2px;border-radius:inherit;pointer-events:none;border:2.5px solid rgba(224,49,49,.85);animation:dtwPing 1.3s ease-out infinite}",
      ".dtw-wrap.dtw-on #dtw-toggle.active{animation:none}",
      ".dtw-wrap.dtw-on #dtw-toggle.active::after{animation:none;border:0}",
      // Label callout 'Hitung Daya Tampung' di samping toggle: TEKS merah polos
      // (tanpa kotak), berdenyut merah senada. Bayangan gelap tipis biar kebaca di
      // atas peta. Klik = buka panel.
      "@keyframes dtwCallout{0%,100%{opacity:.7;text-shadow:0 1px 3px rgba(0,0,0,.35),0 0 4px rgba(224,49,49,.35)}" +
        "50%{opacity:1;text-shadow:0 1px 3px rgba(0,0,0,.35),0 0 14px rgba(224,49,49,.95)}}",
      ".dtw-callout{display:none;position:absolute;top:50%;right:calc(100% + 12px);transform:translateY(-50%);",
      "  white-space:nowrap;font:800 13px 'Space Grotesk',system-ui,sans-serif;color:#e0312e;cursor:pointer;",
      "  animation:dtwCallout 1.3s ease-in-out infinite}",
      ".dtw-wrap.dtw-on .dtw-callout{display:block}",
      ".dtw-wrap.dtw-on #dtw-toggle.active ~ .dtw-callout{display:none}",
      "@media(max-width:640px){.dtw-callout{display:none!important}}",
      "@media(prefers-reduced-motion:reduce){.dtw-wrap.dtw-on #dtw-toggle,.dtw-wrap.dtw-on #dtw-toggle::after,.dtw-callout{animation:none}}"
    ].join("\n");
    document.head.appendChild(st);

    // tombol di kolom kanan
    var col = document.querySelector(".col.items-end");
    var wrap = document.createElement("div"); wrap.className = "dtw-wrap";
    wrap.innerHTML = '<button id="dtw-toggle" class="icon-btn" aria-label="Daya Tampung Wilayah" ' +
      'data-tip="Daya Tampung Wilayah"><span class="material-symbols-outlined">upload_file</span></button>' +
      '<span id="dtw-callout" class="dtw-callout">Hitung Daya Tampung</span>';
    if (col) col.appendChild(wrap); else { wrap.style.position = "fixed"; wrap.style.top = "12px"; wrap.style.right = "12px"; wrap.style.zIndex = 1100; document.body.appendChild(wrap); }

    // panel
    var panel = document.createElement("div"); panel.id = "dtw-panel";
    panel.innerHTML =
      '<div class="dtw-head"><h3>Daya Tampung Wilayah</h3><button class="dtw-x" aria-label="Tutup">×</button></div>' +
      '<div class="dtw-body">' +
      '<div class="dtw-input">' +
      '<span class="dtw-lbl">Wilayah kajian</span>' +
      '<div class="dtw-file-row">' +
      '<button id="dtw-pick" class="dtw-file-btn"><span class="material-symbols-outlined">folder_open</span>Pilih berkas</button>' +
      '<span id="dtw-fname" class="dtw-fname">Belum ada berkas</span>' +
      '<button id="dtw-hapus" class="dtw-icon-btn" title="Hapus wilayah" style="display:none"><span class="material-symbols-outlined">delete</span></button>' +
      '</div>' +
      '<input id="dtw-file" type="file" accept=".zip,.kmz,.geojson,.json,.kml,.gpx,.wkt,.txt" style="display:none">' +
      '<p class="dtw-hint">SHP (.zip), GeoJSON, KML, KMZ, WKT — terdeteksi otomatis.</p>' +
      '<div class="dtw-or">atau</div>' +
      '<div class="dtw-tools">' +
      '<button id="dtw-draw" class="dtw-file-btn"><span class="material-symbols-outlined">draw</span>Gambar di peta</button>' +
      '<button id="dtw-edit" class="dtw-file-btn" style="display:none"><span class="material-symbols-outlined">edit</span>Edit titik</button>' +
      '</div>' +
      '<button id="dtw-hitung" class="dtw-btn" disabled>Hitung</button>' +
      '<div id="dtw-status" class="dtw-status"></div>' +
      '</div>' +
      '<div id="dtw-hasil"></div>' +
      '<button id="dtw-reset" class="dtw-reset"><span class="material-symbols-outlined">restart_alt</span>Reset</button>' +
      '</div>';
    document.body.appendChild(panel);

    document.getElementById("dtw-toggle").addEventListener("click", togglePanel);
    document.getElementById("dtw-callout").addEventListener("click", togglePanel);
    panel.querySelector(".dtw-x").addEventListener("click", togglePanel);
    document.getElementById("dtw-pick").addEventListener("click", function () {
      document.getElementById("dtw-file").click();
    });
    document.getElementById("dtw-file").addEventListener("change", function (e) {
      var f = e.target.files && e.target.files[0];
      if (f) { document.getElementById("dtw-fname").textContent = f.name; muatFile(f); }
    });
    document.getElementById("dtw-draw").addEventListener("click", mulaiGambar);
    document.getElementById("dtw-edit").addEventListener("click", mulaiEdit);
    document.getElementById("dtw-hapus").addEventListener("click", resetWilayah);
    document.getElementById("dtw-reset").addEventListener("click", resetWilayah);
    document.getElementById("dtw-hitung").addEventListener("click", hitung);

    // Sambung ke pergantian layer: bungkus setActiveLayer agar syncDTW jalan tiap
    // ganti layer. setActiveLayer fungsi global app.js (classic script), bisa dibungkus.
    if (typeof setActiveLayer === "function") {
      var _sal = setActiveLayer;
      setActiveLayer = function () { var r = _sal.apply(this, arguments); try { syncDTW(); } catch (e) {} return r; };
    }
    syncDTW();   // kondisi awal (kalau app mulai di layer dt_)
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", bangunUI);
  else bangunUI();
})();
