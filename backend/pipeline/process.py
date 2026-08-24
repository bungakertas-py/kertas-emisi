"""
Processor: GRIB2 -> aset siap-frontend.

Menghasilkan, untuk satu layer angin pada satu langkah forecast:
  1. <name>.png       : PNG data (R=u, G=v terkemas) untuk engine partikel WeatherLayers GL.
  2. <name>_preview.png: PNG pratinjau kecepatan angin berwarna (skala knots) — untuk
                         verifikasi cepat oleh manusia tanpa perlu frontend.
  3. <name>.json      : metadata (bounds, dimensi, unscale, waktu run & valid).
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import warnings
from pathlib import Path

import numpy as np
import xarray as xr
from PIL import Image, ImageDraw

from config import (BACKEND_DIR, BMUA_24JAM, DT_PARAM, HARI_PER_TAHUN, ISPU_MAKS,
                    ISPU_PARAM, ISPU_SIMPUL, ISPU_TABEL, LAYERS, OUTPUT_DIR,
                    RADIUS_BUMI, REGION, UG_PER_TON)

warnings.filterwarnings("ignore", message="Ignoring index file")

# Skala warna kecepatan angin (knots) meniru legend BMKG Signature.
# (batas_knots, (R, G, B))
_KNOTS_SCALE = [
    (0,   (0x00, 0x30, 0x50)),   # tenang - biru gelap
    (5,   (0x2b, 0x83, 0xba)),   # biru
    (10,  (0x5a, 0xa8, 0xcf)),
    (15,  (0xab, 0xdd, 0xa4)),   # hijau muda
    (20,  (0x66, 0xbd, 0x63)),   # hijau
    (25,  (0xd9, 0xef, 0x8b)),   # kuning-hijau
    (34,  (0xfe, 0xe0, 0x8b)),   # kuning
    (48,  (0xfd, 0xae, 0x61)),   # oranye
    (64,  (0xf4, 0x6d, 0x43)),   # oranye-merah
    (80,  (0xd7, 0x30, 0x27)),   # merah
    (100, (0xa5, 0x00, 0x26)),   # merah tua
    (120, (0x7a, 0x00, 0x77)),   # ungu
]

MS_TO_KNOTS = 1.943844

# Hujan PER-JAM (mm/jam) — rainbow BMKG, ambang per-jam. Kering transparan.
_RAIN_SCALE = [
    (0.0,  (0x14, 0x37, 0x8f,   0)),   # kering = transparan
    (1.0,  (0x14, 0x37, 0x8f, 140)),
    (2.0,  (0x14, 0x37, 0x8f, 225)),   # biru tua
    (4.0,  (0x23, 0x60, 0xc8, 235)),   # biru
    (8.0,  (0x22, 0xa5, 0xe0, 240)),   # cyan
    (10.0, (0x23, 0xd3, 0xc0, 240)),   # turkis
    (15.0, (0x35, 0xc8, 0x4a, 245)),   # hijau
    (20.0, (0x8e, 0xd8, 0x2a, 245)),   # hijau-kuning
    (25.0, (0xea, 0xd8, 0x21, 248)),   # kuning
    (30.0, (0xf5, 0xa9, 0x1e, 248)),   # oranye
    (35.0, (0xf2, 0x70, 0x1c, 250)),   # oranye tua
    (40.0, (0xe4, 0x23, 0x20, 250)),   # merah
    (50.0, (0xe3, 0x3b, 0xbf, 252)),   # magenta
    (60.0, (0x8a, 0x29, 0xc8, 255)),   # ungu
]

# AKUMULASI HUJAN 24 JAM (mm/hari) — rainbow sama, ambang harian (lebih tinggi).
_RAIN_ACCUM_SCALE = [
    (0.0,   (0x14, 0x37, 0x8f,   0)),
    (2.0,   (0x14, 0x37, 0x8f, 120)),
    (5.0,   (0x14, 0x37, 0x8f, 235)),
    (10.0,  (0x23, 0x60, 0xc8, 240)),
    (20.0,  (0x22, 0xa5, 0xe0, 240)),
    (40.0,  (0x23, 0xd3, 0xc0, 242)),
    (60.0,  (0x35, 0xc8, 0x4a, 245)),
    (90.0,  (0x8e, 0xd8, 0x2a, 245)),
    (120.0, (0xea, 0xd8, 0x21, 248)),
    (150.0, (0xf5, 0xa9, 0x1e, 248)),
    (200.0, (0xf2, 0x70, 0x1c, 250)),
    (300.0, (0xe4, 0x23, 0x20, 252)),
    (400.0, (0xe3, 0x3b, 0xbf, 253)),
    (500.0, (0x8a, 0x29, 0xc8, 255)),
]

# Suhu (°C) OPAQUE: biru dingin -> merah panas.
# Suhu: biru dingin -> netral -> merah panas. TANPA hijau sama sekali
# (dulu ada teal di 16 dan hijau-kuning di 22, sekarang diganti biru pucat
# dan krem netral) supaya bacanya lurus, makin merah makin panas.
_TEMP_SCALE = [
    (-10, (0x16, 0x20, 0x5e, 255)), (0, (0x24, 0x50, 0xb4, 255)),
    (8,   (0x4a, 0x97, 0xdc, 255)), (16, (0xcf, 0xe4, 0xf2, 255)),
    (22,  (0xff, 0xe0, 0x8a, 255)), (28, (0xfb, 0xaa, 0x4a, 255)),
    (32,  (0xee, 0x72, 0x33, 255)), (36, (0xd4, 0x33, 0x25, 255)),
    (42,  (0x7d, 0x0d, 0x1a, 255)),
]

# Suhu STRATOSFER 70 hPa (°C) OPAQUE. Di sana jauh lebih dingin (~-45..-75°C)
# daripada permukaan, jadi skalanya digeser ke rentang dingin sendiri.
# Suhu stratosfer, ramp yang sama dengan permukaan biar konsisten saat
# ganti Level. Hijaunya juga dibuang.
_TEMP_STRATO_SCALE = [
    (-78, (0x16, 0x20, 0x5e, 255)), (-70, (0x24, 0x50, 0xb4, 255)),
    (-64, (0x4a, 0x97, 0xdc, 255)), (-58, (0xcf, 0xe4, 0xf2, 255)),
    (-52, (0xff, 0xe0, 0x8a, 255)), (-46, (0xfb, 0xaa, 0x4a, 255)),
    (-40, (0xee, 0x72, 0x33, 255)),
]

# Kelembapan (%) OPAQUE: coklat kering -> hijau -> biru lembap.
_HUM_SCALE = [
    (0,  (0x7a, 0x45, 0x0a, 255)), (25, (0xb9, 0x84, 0x3a, 255)),
    (50, (0x88, 0xb0, 0x55, 255)), (70, (0x35, 0x9a, 0x86, 255)),
    (85, (0x21, 0x6b, 0xb0, 255)), (100, (0x12, 0x3f, 0x86, 255)),
]

# Tutupan awan (%) RGBA: cerah transparan -> abu (makin tertutup makin pekat).
_CLOUD_SCALE = [
    (0,   (0xff, 0xff, 0xff,   0)), (20, (0xc8, 0xd0, 0xd8,  70)),
    (50,  (0xaa, 0xb4, 0xbe, 150)), (80, (0x96, 0xa0, 0xac, 205)),
    (100, (0x78, 0x82, 0x8e, 235)),
]

# Tekanan MSL (hPa) OPAQUE: rendah (badai) ungu/biru -> tinggi merah.
_PRESS_SCALE = [
    (980,  (0x5e, 0x3c, 0x99, 255)), (995, (0x35, 0x6b, 0xc4, 255)),
    (1005, (0x7d, 0xc8, 0xd8, 255)), (1013, (0xf0, 0xf0, 0xe0, 255)),
    (1020, (0xf4, 0xc0, 0x60, 255)), (1030, (0xe0, 0x5a, 0x3a, 255)),
]

# CAPE (J/kg) POTENSI BADAI: stabil transparan -> hijau (sedang) -> kuning/oranye
# (tinggi) -> merah/ungu (ekstrem, potensi badai petir kuat). Ambang mirip acuan
# konvektif: <300 lemah, ~500-1000 sedang, 1000-2500 tinggi, >2500 ekstrem.
_CAPE_SCALE = [
    (0,    (0x14, 0x37, 0x8f,   0)),   # stabil: transparan
    (500,  (0x2f, 0x9e, 0x7a,   0)),   # <500: transparan
    (1000, (0x5a, 0xc8, 0x6a, 150)),   # hijau: sedang
    (1800, (0xea, 0xd8, 0x21, 195)),   # kuning
    (2600, (0xf5, 0xa9, 0x1e, 216)),   # oranye: tinggi
    (3400, (0xe4, 0x23, 0x20, 232)),   # merah
    (4200, (0x8a, 0x29, 0xc8, 246)),   # ungu: ekstrem
]


# CIN nilainya NEGATIF (J/kg), 0 berarti tak ada penghambat. Makin minus makin tebal
# tutupnya. Titik henti disusun menaik karena interpolasi warna butuh urutan naik.
_CIN_SCALE = [
    (-400, (0x4a, 0x0d, 0x67, 246)),   # tutup sangat tebal
    (-200, (0x8a, 0x29, 0xc8, 232)),
    (-100, (0xe4, 0x23, 0x20, 216)),
    (-50,  (0xf5, 0xa9, 0x1e, 195)),
    (-25,  (0xea, 0xd8, 0x21, 150)),
    (-10,  (0x5a, 0xc8, 0x6a,   0)),   # nyaris tak ada tutup: transparan
    (0,    (0x14, 0x37, 0x8f,   0)),
]


# Palet polutan. Tiap parameter punya KELUARGA WARNA SENDIRI, pilihan user, diambil
# dari colormap matplotlib lalu dibekukan jadi 5 hentian di sini. Dibekukan supaya
# matplotlib tak perlu ikut terpasang di CI cuma demi tujuh daftar warna.
#
# Alasan tiap parameter beda: satu parameter satu identitas warna, jadi pengguna tahu
# sedang melihat apa tanpa membaca label. Yang menyatukan makna tetap ada di layer
# ISPU, dan di situ warnanya justru mengikuti Lampiran II Permen LHK 14/2020.
# Ambang tiap parameter mengacu ISPU dan pedoman WHO 2021.
#
# Hentian diambil di posisi 0,20 sampai 1,00 pada colormap aslinya. Ujung paling
# pucat dilewati karena nyaris putih dan tak terbaca sebagai warna ambang.
_PALET = {
    "pm25": [(0xfe, 0xe1, 0x87), (0xfe, 0xab, 0x49), (0xfc, 0x5b, 0x2e), (0xd4, 0x10, 0x20), (0x80, 0x00, 0x26)],   # YlOrRd
    "pm10": [(0xfe, 0xeb, 0xa2), (0xfe, 0xbb, 0x47), (0xf0, 0x78, 0x18), (0xb8, 0x42, 0x03), (0x66, 0x25, 0x06)],   # YlOrBr, krem ke coklat tua
    "co":   [(0xfc, 0xd0, 0xcc), (0xfa, 0xa3, 0xb6), (0xf3, 0x5f, 0x9f), (0xc7, 0x1d, 0x8c), (0x87, 0x01, 0x79), (0x49, 0x00, 0x6a)],   # RdPu (6 pita)
    "no2":  [(0xe2, 0xe2, 0xef), (0xb6, 0xb6, 0xd8), (0x86, 0x83, 0xbd), (0x61, 0x40, 0x9b), (0x3f, 0x00, 0x7d)],   # Purples
    "so2":  [(0xe5, 0xf5, 0xac), (0xa2, 0xd8, 0x8a), (0x4c, 0xb0, 0x63), (0x15, 0x79, 0x3e), (0x00, 0x45, 0x29)],   # YlGn
    "o3":   [(0xff, 0x99, 0x33), (0xe5, 0x33, 0x00), (0x99, 0x00, 0x00), (0x4c, 0x00, 0x00), (0x00, 0x00, 0x00)],   # gist_heat dibalik
    "aod":  [(0xfc, 0x9f, 0x65), (0xbd, 0x78, 0x4c), (0x7e, 0x50, 0x33), (0x3f, 0x28, 0x19), (0x00, 0x00, 0x00)],   # copper dibalik
}
# Kepekatan naik bersama ambang: yang rendah setengah tembus supaya peta di bawahnya
# masih terbaca, yang tinggi hampir pejal supaya menonjol.
_ALPHA = [90, 170, 210, 232, 246]


def _skala(bening, ambang, palet):
    """Bangun skala warna: transparan penuh sampai `bening`, lalu ramp di `ambang`.

    Jumlah ambang boleh bukan 5 (mis. CO 6 pita). Kepekatan (_ALPHA) diregang ke
    jumlah pita lewat interpolasi supaya polanya tetap rendah->tinggi. Nilai di atas
    ambang teratas MENGIKUTI warna teratas (np.interp menjepit di ujung)."""
    n = len(ambang)
    if n == len(_ALPHA):
        alpha = _ALPHA
    else:
        xs = [j / (len(_ALPHA) - 1) for j in range(len(_ALPHA))]
        alpha = [round(float(np.interp(i / (n - 1), xs, _ALPHA))) for i in range(n)]
    out = [(0, (0x14, 0x37, 0x8f, 0)), (bening, (*palet[0], 0))]
    return out + [(v, (*c, a)) for v, c, a in zip(ambang, palet, alpha)]


# Ambang PILIHAN USER, warna per polutan tetap. Nilai di atas ambang teratas
# mengikuti warna teratas.
_PM25_SCALE = _skala(5, [15, 20, 35, 40, 55], _PALET["pm25"])
_PM10_SCALE = _skala(5, [20, 35, 40, 60, 75], _PALET["pm10"])
_CO_SCALE = _skala(100, [500, 1000, 2000, 4000, 8000, 10000], _PALET["co"])
_NO2_SCALE = _skala(2, [10, 50, 100, 150, 200], _PALET["no2"])
_SO2_SCALE = _skala(1, [5, 10, 50, 100, 150], _PALET["so2"])
_O3_SCALE = _skala(1, [5, 10, 50, 100, 150], _PALET["o3"])
# AOD 550 nm: tanpa satuan. 0,2 berkabut tipis, di atas 1 asap tebal
_AOD_SCALE = _skala(0.05, [0.2, 0.5, 1.0, 2.0, 3.0], _PALET["aod"])


# Tinggi lapisan batas (PBL). Skalanya TERBALIK dari semua layer lain: yang
# mengkhawatirkan justru nilai RENDAH, karena lapisan aduk yang tipis mengurung
# emisi di dekat tanah. Jadi yang menyala warna panas adalah yang rendah, dan yang
# tinggi dibiarkan bening karena berarti udaranya lega dan tak perlu ditandai.
# cmocean curl_pink, https://github.com/matplotlib/cmocean berkas
# cmocean/rgb/curl-pink.py. Diambil di posisi 0,20 naik ke 1,00, konvensi sama
# dengan palet polutan lain. Plum tua di nilai RENDAH lalu memudar ke nyaris
# putih di lapisan yang sudah lega, dan akhirnya bening. cmocean sengaja TIDAK
# jadi dependensi CI, nilainya dibekukan di sini seperti palet lainnya.
_PBL_WARNA = [(0x76, 0x19, 0x5d), (0xae, 0x40, 0x60), (0xd4, 0x77, 0x6a),
              (0xe6, 0xb7, 0xa2), (0xfe, 0xf6, 0xf5)]
# RATA, tidak bertingkat seperti layer polutan. Di layer polutan kepekatan boleh
# ikut naik bersama ambang, karena nilai rendah di sana memang berarti "nyaris tak
# ada polutan" jadi wajar nyaris tak terlihat. PBLH tidak begitu, setiap sel selalu
# punya nilai dan semuanya berarti. Kalau alphanya bertingkat, pita teratas di peta
# jadi abu-abu dan tak lagi cocok dengan swatch legendanya.
_PBL_ALPHA = [235, 235, 235, 235, 235]
_PBL_AMBANG = [200, 400, 700, 1000, 1500]   # meter


def _skala_terbalik(ambang, palet, alpha):
    """Skala yang pekat di nilai RENDAH lalu meredup ke atas.

    TIDAK ada ekor bening di ujung atas. Dulu ada, nilainya diturunkan ke alpha 0
    di 2500 m dengan maksud "di atas ini udaranya lega jadi tak perlu ditandai".
    Maksud itu tak sampai. Yang terlihat justru alas peta yang menembus dari
    bawah, dan di alas gelap itu terbaca HITAM. Padahal hitam di aplikasi ini
    sudah punya arti sendiri, yaitu Berbahaya di legenda ISPU. Jadi lapisan aduk
    yang paling TEBAL, yang justru paling aman, malah tampak paling gawat.

    Sekarang nilai di atas ambang teratas MENGIKUTI warna legenda paling atas,
    sama seperti semua layer lain. Pita terakhir berarti "segini ke atas".
    """
    out = [(0, (*palet[0], alpha[0]))]
    out += [(v, (*c, a)) for v, c, a in zip(ambang, palet, alpha)]
    return out


_PBL_SCALE = _skala_terbalik(_PBL_AMBANG, _PBL_WARNA, _PBL_ALPHA)


# ============ PROYEKSI: baris pratinjau harus MERCATOR, bukan lintang ============
# Leaflet menempatkan imageOverlay dengan merentangkan bitmap LINEAR di layar, dan
# layar itu Web Mercator. Kalau barisnya kita susun linear terhadap lintang
# (equirectangular), gambar melenceng sampai 81 km di lintang 20 dan 50 km di
# lintang 8. Lebih dari satu sel grid, dan paling parah persis di lintang Jawa
# sampai Maluku. Jadi baris pratinjau disusun linear di Mercator sejak dari sini.
#
# Batas gambar juga dipakai TEPI sel, bukan pusat sel. Pratinjau berisi `nx` blok
# yang masing-masing mewakili satu sel penuh; menempatkannya di kotak pusat-ke-pusat
# menggeser semuanya setengah sel di tepi domain.


def _merc(lat_deg):
    lat = np.clip(np.asarray(lat_deg, dtype="f8"), -85.05, 85.05)
    return np.log(np.tan(np.radians(45.0 + lat / 2.0)))


def _merc_balik(y):
    return np.degrees(2.0 * np.arctan(np.exp(np.asarray(y, dtype="f8"))) - np.pi / 2.0)


def tepi_gambar(grid: dict) -> tuple[float, float, float, float]:
    """Kotak TEPI sel: (barat, timur, selatan, utara). Ini yang dipakai frontend
    untuk menempatkan pratinjau, bukan kotak pusat-ke-pusat."""
    nx, ny = grid["width"], grid["height"]
    dlon = (grid["east"] - grid["west"]) / (nx - 1) if nx > 1 else 0.4
    dlat = (grid["north"] - grid["south"]) / (ny - 1) if ny > 1 else 0.4
    return (grid["west"] - dlon / 2, grid["east"] + dlon / 2,
            grid["south"] - dlat / 2, grid["north"] + dlat / 2)


def _baris_sumber(grid: dict, skala: int) -> np.ndarray:
    """Untuk tiap baris keluaran, indeks baris SUMBER (pecahan) yang benar menurut
    Mercator. Panjangnya height*skala."""
    ny = grid["height"]
    _, _, sel, uta = tepi_gambar(grid)
    dlat = (grid["north"] - grid["south"]) / (ny - 1) if ny > 1 else 0.4
    H = ny * skala
    yU, yS = float(_merc(uta)), float(_merc(sel))
    y = yU + ((np.arange(H) + 0.5) / H) * (yS - yU)
    phi = _merc_balik(y)
    return np.clip((grid["north"] - phi) / dlat, 0, ny - 1)


def _batas_baris(grid: dict, skala: int) -> np.ndarray:
    """Baris keluaran tempat BATAS antar sel jatuh (untuk garis kisi)."""
    ny = grid["height"]
    _, _, sel, uta = tepi_gambar(grid)
    dlat = (grid["north"] - grid["south"]) / (ny - 1) if ny > 1 else 0.4
    H = ny * skala
    yU, yS = float(_merc(uta)), float(_merc(sel))
    tepi_lat = grid["north"] + dlat / 2 - np.arange(ny + 1) * dlat
    baris = (_merc(tepi_lat) - yU) / (yS - yU) * H
    return np.unique(np.clip(np.round(baris), 0, H - 1).astype(int))


def _proyeksi_mercator(rgba: np.ndarray, grid: dict, skala: int,
                       kategori: bool) -> np.ndarray:
    """Perbesar `skala` kali: kolom linear di bujur, BARIS linear di Mercator.

    Layer KATEGORI digandakan apa adanya (kotak tegas). Layer MENERUS dihaluskan
    di KEDUA arah: mendatar lewat resize bilinear, tegak lewat pembauran antar
    baris sumber. Menggandakan kolom apa adanya untuk layer menerus membuat
    heatmap yang tadinya mulus jadi kotak-kotak."""
    r = _baris_sumber(grid, skala)
    if kategori:
        lebar = np.repeat(rgba, skala, axis=1)          # bujur memang linear
        return lebar[np.round(r).astype(int)]
    lebar = np.asarray(Image.fromarray(rgba, mode="RGBA").resize(
        (rgba.shape[1] * skala, rgba.shape[0]), Image.BILINEAR)).astype("f4")
    i0 = np.floor(r).astype(int)
    i1 = np.minimum(i0 + 1, grid["height"] - 1)
    t = (r - i0)[:, None, None]
    return (lebar[i0] * (1 - t) + lebar[i1] * t).round().astype("uint8")


def _skala_tangga(batas, warna):
    """Skala warna bertangga, tanpa gradasi antar kategori."""
    eps = 1e-3
    out = []
    for k, warna_k in enumerate(warna):
        lo, hi = batas[k], batas[k + 1]
        out.append((lo, warna_k))
        out.append((hi - eps, warna_k))
    out.append((batas[-1], warna[-1]))
    return out


# ============ DAYA TAMPUNG UDARA (Permen LH No. 5) ============
# Skala warna daya tampung. Peraturan cuma memberi rumus dan satu batas yang
# berarti, yaitu NOL (beban maksimum terlampaui atau belum), jadi tak ada
# penggolongan buatan; yang ada cuma angka ton/tahun.
#
# Palet bwr matplotlib DIBALIK dan sisi birunya diganti hijau, jadi merah-putih-hijau.
# Arahnya sengaja begitu: MERAH berarti beban maksimum terlampaui, HIJAU berarti
# masih ada daya tampung. Dipotong jadi PITA 10.000 ton/tahun dari -50K sampai
# +50K, bertingkat bukan gradasi mulus supaya beda antar sel terbaca. Rentang
# dirapatkan dari +-100K/20K karena sebaran nyata menumpuk dekat nol, dengan pita
# lama cuma 3 dari 10 pita terisi. Di luar rentang itu warnanya jenuh, dan itu
# disengaja: sel karhutla bisa mencapai -6,8 juta, kalau sumbunya dipaksa memuat
# itu seluruh peta lain jadi putih.
_DT_RENTANG = 50_000.0
_DT_LANGKAH = 10_000.0
_DT_ALPHA = 225
# Warna diambil di TENGAH tiap pita pada palet bwr.
_DT_PITA = [
    (0xff, 0x18, 0x18), (0xff, 0x4c, 0x4c), (0xff, 0x7e, 0x7e), (0xff, 0xb2, 0xb2),
    (0xff, 0xe6, 0xe6),
    (0xe6, 0xff, 0xe6), (0xb2, 0xff, 0xb2), (0x80, 0xff, 0x80), (0x4c, 0xff, 0x4c),
    (0x18, 0xff, 0x18),
]

_DT_TEPI = [-_DT_RENTANG + _DT_LANGKAH * k for k in range(len(_DT_PITA) + 1)]
_DT_SCALE = _skala_tangga(
    [-1e12] + _DT_TEPI[1:-1] + [1e12],
    [(*c, _DT_ALPHA) for c in _DT_PITA],
)
_DT_SCALES = {f"dt_{p}": _DT_SCALE for p in DT_PARAM}


def luas_sel(grid: dict) -> np.ndarray:
    """Luas tiap sel grid dalam m2, dihitung di bola.

    Tak boleh dianggap seragam: sel 0,4 derajat itu 1.978 km2 di ekuator tapi
    tinggal 1.713 km2 di lintang 30, selisihnya 13 persen dan langsung merambat
    ke angka ton/tahun."""
    ny, nx = grid["height"], grid["width"]
    lat = np.linspace(grid["north"], grid["south"], ny)      # baris-0 = utara
    dlat = abs(lat[1] - lat[0]) if ny > 1 else 0.4
    dlon = abs(grid["east"] - grid["west"]) / (nx - 1) if nx > 1 else 0.4
    atas = np.radians(np.minimum(lat + dlat / 2, 90.0))
    bawah = np.radians(np.maximum(lat - dlat / 2, -90.0))
    pita = RADIUS_BUMI ** 2 * np.radians(dlon) * (np.sin(atas) - np.sin(bawah))
    return np.repeat(pita[:, None], nx, axis=1)


def hitung_daya_tampung(par: str, c24: np.ndarray, pblh24: np.ndarray,
                        luas_darat: np.ndarray) -> np.ndarray:
    """Daya tampung satu parameter, ton/tahun. NaN di sel tanpa daratan.

    V = A x PBLH, BEmax = V x BMUA, BEeks = V x C, DT = BEmax - BEeks, lalu
    disetahunkan. Karena BEmax dan BEeks berbagi V yang sama, DT bisa ditulis
    ringkas sebagai V x (BMUA - C)."""
    V = luas_darat * pblh24
    dt = V * (BMUA_24JAM[par] - c24) / UG_PER_TON * HARI_PER_TAHUN
    return np.where(luas_darat > 0, dt, np.nan)


# ---- Topeng daratan untuk memotong tampilan mengikuti garis pantai ----
# DUA berkas, dan itu bukan pilihan gaya. Frontend menggambar garis pantai
# Indonesia dari idn_provinces (18.904 titik), sedangkan world_countries cuma
# punya 3.087 titik untuk Indonesia, enam kali lebih kasar. Memotong dengan yang
# kasar sementara yang digambar yang halus membuat potongannya meleset dari
# garis pantai yang terlihat. Jadi keduanya digabung.
_GEO_DARAT = [
    BACKEND_DIR.parent / "frontend" / "data" / "world_countries.geojson",
    BACKEND_DIR.parent / "frontend" / "data" / "idn_provinces.geojson",
]
# Oversampling topeng sebelum diperkecil. Tepi pantai jadi ber-alpha pecahan,
# bukan tangga keras, jadi potongannya terbaca mulus walau petanya di-zoom.
_TOPENG_HALUS = 4
_topeng_cache: dict = {}


def topeng_darat(grid: dict, skala: int) -> np.ndarray:
    """Pecahan daratan tiap piksel pratinjau (float 0..1).

    Dirasterkan dari poligon garis pantai, bukan dari kotak grid, supaya tepi
    layer mengikuti lekuk pantai. Dirasterkan `_TOPENG_HALUS` kali lebih rapat
    dari pratinjau lalu dirata-rata turun, jadi tepinya ber-antialias."""
    kunci = (grid["west"], grid["east"], grid["south"], grid["north"],
             grid["width"], grid["height"], skala)
    if kunci in _topeng_cache:
        return _topeng_cache[kunci]
    o = _TOPENG_HALUS
    W, H = grid["width"] * skala, grid["height"] * skala
    Wo, Ho = W * o, H * o
    img = Image.new("1", (Wo, Ho), 0)
    gambar = ImageDraw.Draw(img)
    bar, tim, sel, uta = tepi_gambar(grid)
    yU, yS = float(_merc(uta)), float(_merc(sel))
    sx = Wo / (tim - bar)
    sy = Ho / (yS - yU)

    def piksel(cincin):
        # Sumbu tegak WAJIB Mercator, sama dengan cara pratinjau disusun. Kalau di
        # sini linear terhadap lintang, topengnya meleset puluhan km dari datanya.
        return [((x - bar) * sx, (float(_merc(y)) - yU) * sy) for x, y in cincin]

    def poligon(p, isi):
        if not p:
            return
        gambar.polygon(piksel(p[0]), fill=isi)
        if isi:                                # lubang cuma berlaku saat mengisi
            for lubang in p[1:]:               # danau di dalam daratan
                gambar.polygon(piksel(lubang), fill=0)

    for berkas in _GEO_DARAT:
        if not berkas.exists():
            continue
        data = json.loads(berkas.read_text(encoding="utf-8"))
        for fitur in data.get("features", []):
            g = fitur.get("geometry") or {}
            if g.get("type") == "Polygon":
                poligon(g["coordinates"], 1)
            elif g.get("type") == "MultiPolygon":
                for p in g["coordinates"]:
                    poligon(p, 1)
    kasar = np.asarray(img, dtype="f4")
    hasil = kasar.reshape(H, o, W, o).mean(axis=(1, 3))     # rata-rata luas -> antialias
    _topeng_cache[kunci] = hasil
    return hasil


# ISPU. Warnanya BUKAN ramp di atas, melainkan lima warna resmi Lampiran II Permen
# LHK 14/2020: hijau, biru, kuning, merah, hitam. Sengaja menyimpang dari aturan
# "satu ramp untuk semua", karena warna ISPU sudah punya arti hukum dan orang yang
# kenal ISPU dari BMKG akan membacanya lewat warna itu.
#
# Batasnya TEGAS, bukan gradasi. Kategori ISPU itu diskret, memuluskannya bikin
# batas kategori kabur dan orang salah baca. Trik untuk memaksa tangga di
# _scalar_to_rgba yang berinterpolasi linear: taruh dua simpul rapat di tiap batas.
_ISPU_WARNA = [
    (0x35, 0xc8, 0x4a, 165),   # Baik
    (0x2b, 0x83, 0xba, 180),   # Sedang
    (0xea, 0xd8, 0x21, 205),   # Tidak Sehat
    (0xe4, 0x23, 0x20, 228),   # Sangat Tidak Sehat
    (0x0d, 0x0d, 0x0d, 242),   # Berbahaya
]
_ISPU_BATAS = [0, 50, 100, 200, 300, ISPU_MAKS]


_ISPU_SCALE = _skala_tangga(_ISPU_BATAS, _ISPU_WARNA)


def subindeks_ispu(par: str, x: np.ndarray) -> np.ndarray:
    """Konsentrasi rata 24 jam (ug/m3) -> nilai ISPU parameter itu.

    Interpolasi linear antar simpul tabel Lampiran I.A, persis rumus Lampiran I.B.
    Di atas baris terakhir tabel, kemiringan pita terakhir diteruskan lalu dipotong
    di ISPU_MAKS; aturan tak mengatur wilayah itu."""
    X = ISPU_TABEL[par]
    y = np.interp(x, X, ISPU_SIMPUL)          # np.interp memotong di ujung
    atas = X[-1]
    lewat = x > atas
    if lewat.any():
        kemiringan = (ISPU_SIMPUL[-1] - ISPU_SIMPUL[-2]) / (atas - X[-2])
        y = np.where(lewat, ISPU_SIMPUL[-1] + (x - atas) * kemiringan, y)
    return np.clip(y, 0, ISPU_MAKS)


def hitung_ispu(rata: dict) -> tuple[np.ndarray, np.ndarray]:
    """ISPU akhir + kode pencemar kritis, dari rata-rata 24 jam per parameter.

    Aturannya ISPU = nilai TERTINGGI di antara parameter, dan parameter penyebabnya
    dilaporkan sebagai pencemar kritis. Jadi ini maksimum, bukan rata-rata.
    `rata` = {parameter: array rata 24 jam}. Kode kritis = indeks di ISPU_PARAM."""
    pakai = [p for p in ISPU_PARAM if p in rata]
    tumpuk = np.stack([subindeks_ispu(p, rata[p]) for p in pakai])   # (npar, ny, nx)
    kritis_lokal = np.argmax(tumpuk, axis=0)
    ispu = np.take_along_axis(tumpuk, kritis_lokal[None], axis=0)[0]
    # petakan indeks lokal -> indeks resmi di ISPU_PARAM
    peta = np.array([ISPU_PARAM.index(p) for p in pakai])
    return ispu, peta[kritis_lokal].astype("int16")


def _load_wind(grib_path: Path) -> tuple[np.ndarray, np.ndarray, dict]:
    """Muat u/v dari GRIB, orientasikan agar baris-0 = utara, kolom-0 = barat."""
    ds = xr.open_dataset(grib_path, engine="cfgrib",
                         backend_kwargs={"indexpath": ""})
    # Komponen angin: di 10 m cfgrib menamai "u10"/"v10"; di level isobarik
    # (mis. 70 hPa) menjadi "u"/"v". Pilih yang ada agar loader dipakai kedua kasus.
    uname = "u10" if "u10" in ds else ("u" if "u" in ds else None)
    vname = "v10" if "v10" in ds else ("v" if "v" in ds else None)
    if uname is None or vname is None:
        raise RuntimeError(f"Komponen angin tak ditemukan di {grib_path.name}: {list(ds.data_vars)}")
    u = ds[uname]
    v = ds[vname]

    # pastikan longitude menaik (barat->timur)
    if float(ds.longitude[0]) > float(ds.longitude[-1]):
        u = u.isel(longitude=slice(None, None, -1))
        v = v.isel(longitude=slice(None, None, -1))
    # pastikan latitude menurun (utara di baris-0); GFS subset kita urut naik -> flip
    if float(ds.latitude[0]) < float(ds.latitude[-1]):
        u = u.isel(latitude=slice(None, None, -1))
        v = v.isel(latitude=slice(None, None, -1))

    meta = {
        "west": float(min(ds.longitude.values)),
        "east": float(max(ds.longitude.values)),
        "south": float(min(ds.latitude.values)),
        "north": float(max(ds.latitude.values)),
        "width": int(u.sizes["longitude"]),
        "height": int(u.sizes["latitude"]),
    }
    return u.values.astype("float32"), v.values.astype("float32"), meta


def _encode_vector_png(u: np.ndarray, v: np.ndarray, unscale: list[float], dest: Path) -> None:
    """Kemas u,v ke PNG RGBA: R=u, G=v (skala unscale), A=255 valid / 0 jika NaN."""
    lo, hi = unscale
    rng = hi - lo
    valid = np.isfinite(u) & np.isfinite(v)

    def enc(a):
        n = np.clip((a - lo) / rng, 0.0, 1.0)
        return (n * 255.0).round().astype("uint8")

    r = enc(np.nan_to_num(u))
    g = enc(np.nan_to_num(v))
    b = np.zeros_like(r)
    alpha = np.where(valid, 255, 0).astype("uint8")
    rgba = np.dstack([r, g, b, alpha])
    Image.fromarray(rgba, mode="RGBA").save(dest)


def _speed_to_rgb(speed_knots: np.ndarray) -> np.ndarray:
    """Petakan kecepatan (knots) ke RGB via interpolasi linear skala BMKG."""
    stops = np.array([s[0] for s in _KNOTS_SCALE], dtype="float32")
    cols = np.array([s[1] for s in _KNOTS_SCALE], dtype="float32")
    out = np.empty(speed_knots.shape + (3,), dtype="float32")
    for c in range(3):
        out[..., c] = np.interp(speed_knots, stops, cols[:, c])
    return out.round().astype("uint8")


def _save_preview(img: Image.Image, dest: Path, lossless: bool = False) -> None:
    """Simpan pratinjau (heatmap untuk mata): WebP q90 bila .webp (5-7x lebih kecil dari
    PNG, visual sama). Data image (nilai angin di piksel) TETAP PNG lossless di tempat lain.

    `lossless` untuk layer KATEGORI. Di layer menerus pergeseran warna 1-2 tingkat tak
    terlihat, tapi di layer kategori warna ITU maknanya."""
    if str(dest).lower().endswith(".webp"):
        if lossless:
            img.save(dest, "WEBP", lossless=True, method=4)
        else:
            img.save(dest, "WEBP", quality=90, method=4, alpha_quality=100)   # method 4: encode 2-3x lebih cepat, ukuran ~sama
    else:
        img.save(dest)


def _render_speed_preview(u: np.ndarray, v: np.ndarray, dest: Path, scale: int = 4) -> None:
    """PNG pratinjau kecepatan angin berwarna (untuk mata manusia)."""
    speed_kt = np.sqrt(u ** 2 + v ** 2) * MS_TO_KNOTS
    rgb = _speed_to_rgb(np.nan_to_num(speed_kt))
    img = Image.fromarray(rgb, mode="RGB")
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale), Image.BILINEAR)
    _save_preview(img, dest)


def _load_scalar(grib_path: Path, filter_keys: dict | None = None) -> tuple[np.ndarray, dict]:
    """Muat satu variabel skalar dari GRIB, orientasi baris-0=utara, kolom-0=barat.

    filter_keys mis. {'stepType': 'instant'} untuk memilih satu varian bila GRIB
    punya beberapa (spt PRATE instant vs avg).
    """
    backend_kwargs = {"indexpath": ""}
    if filter_keys:
        backend_kwargs["filter_by_keys"] = filter_keys
    ds = xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs=backend_kwargs)
    name = list(ds.data_vars)[0]            # subset kita hanya 1 variabel
    da = ds[name]
    if float(ds.longitude[0]) > float(ds.longitude[-1]):
        da = da.isel(longitude=slice(None, None, -1))
    if float(ds.latitude[0]) < float(ds.latitude[-1]):
        da = da.isel(latitude=slice(None, None, -1))
    meta = {
        "west": float(min(ds.longitude.values)),
        "east": float(max(ds.longitude.values)),
        "south": float(min(ds.latitude.values)),
        "north": float(max(ds.latitude.values)),
        "width": int(da.sizes["longitude"]),
        "height": int(da.sizes["latitude"]),
    }
    return da.values.astype("float32"), meta


def _scalar_to_rgba(values: np.ndarray, scale: list) -> np.ndarray:
    """Petakan nilai skalar ke RGBA via interpolasi linear skala warna."""
    stops = np.array([s[0] for s in scale], dtype="float32")
    cols = np.array([s[1] for s in scale], dtype="float32")   # (n, 4)
    v = np.nan_to_num(values)
    out = np.empty(v.shape + (4,), dtype="float32")
    for c in range(4):
        out[..., c] = np.interp(v, stops, cols[:, c])
    return out.round().astype("uint8")


# Layer yang nilainya KATEGORI, bukan besaran menerus. Dirender KOTAK-KOTAK:
# diperbesar tanpa pembauran, ditambah garis kisi di tiap batas sel, lalu disimpan
# lossless. Alasannya kategori ISPU itu diskret. Waktu sempat dihaluskan bilinear,
# 28 persen piksel jatuh ke warna antara (mis. olive, campuran kuning Tidak Sehat
# dan hitam Berbahaya) yang tak mewakili kategori mana pun.
_LAYER_KATEGORI = {"ispu", *(f"dt_{p}" for p in DT_PARAM)}
# Garis kisi menyesuaikan diri: sel terang digelapkan, sel gelap diterangkan. Kalau
# dipatok satu warna, kisi di sel hitam (Berbahaya) akan lenyap.
_KISI_CAMPUR = 0.34


def _garis_kisi(a: np.ndarray, langkah: int, baris: np.ndarray) -> np.ndarray:
    """Gambar garis kisi 1 piksel di tiap batas sel data.

    Batas KOLOM tetap kelipatan `langkah` (bujur linear), tapi batas BARIS harus
    diberikan, karena setelah proyeksi Mercator jaraknya tak lagi seragam."""
    lum = a[..., :3].astype("f4") @ np.array([0.299, 0.587, 0.114], dtype="f4")
    tuju = np.where(lum[..., None] > 128, 0.0, 255.0)         # terang -> hitam, gelap -> putih
    campur = a[..., :3] * (1 - _KISI_CAMPUR) + tuju * _KISI_CAMPUR
    campur = campur.round().astype("uint8")
    a[baris, :, :3] = campur[baris, :, :]
    a[:, ::langkah, :3] = campur[:, ::langkah, :]
    return a


# Layer yang hanya berlaku di DARATAN. Tampilannya dipotong mengikuti poligon
# garis pantai, bukan mengikuti kotak grid, sesuai permintaan: sel pesisir boleh
# terpotong melengkung. Volume udara di atas laut tak ada artinya untuk kuota emisi.
_LAYER_DARAT = {f"dt_{p}" for p in DT_PARAM}


# Pratinjau layer daya tampung diperbesar lebih tinggi dari layer lain. Kotak
# datanya tetap 0,4 derajat, yang bertambah rapat cuma potongan pantainya:
# 4x memberi tangga 0,1 derajat (~11 km), 8x jadi 0,05 derajat (~5,5 km).
_LAYER_SKALA = {f"dt_{p}": 8 for p in DT_PARAM}


def _render_scalar_preview(values: np.ndarray, scale: list, dest: Path, scale_up: int = 4,
                           kategori: bool = False, grid: dict | None = None,
                           potong_darat: bool = False) -> None:
    """PNG heatmap skalar berwarna (RGBA, transparan di area nilai ~0)."""
    rgba = _scalar_to_rgba(values, scale)
    # NaN berarti "tak terdefinisi di sini" (mis. sel tanpa daratan). Harus jadi
    # bening, kalau tidak nan_to_num mengubahnya jadi 0 dan 0 itu warna kategori
    # yang sah, jadi laut akan tampil seolah punya daya tampung.
    kosong = ~np.isfinite(values)
    if kosong.any():
        rgba[..., 3] = np.where(kosong, 0, rgba[..., 3])
    if scale_up > 1 and grid is not None:
        a = _proyeksi_mercator(rgba, grid, scale_up, kategori)
        if kategori:
            a = _garis_kisi(a, scale_up, _batas_baris(grid, scale_up))
        if potong_darat:
            a = a.copy()
            a[..., 3] = (a[..., 3] * topeng_darat(grid, scale_up)).round().astype("uint8")
        img = Image.fromarray(a, mode="RGBA")
    else:
        img = Image.fromarray(rgba, mode="RGBA")
        if scale_up > 1:
            img = img.resize((img.width * scale_up, img.height * scale_up),
                             Image.NEAREST if kategori else Image.BILINEAR)
    _save_preview(img, dest, lossless=kategori)


_SCALAR_SCALES = {
    "rain_surface": _RAIN_SCALE,
    "rain_accum_surface": _RAIN_ACCUM_SCALE,
    "temp_surface": _TEMP_SCALE,
    "temp_strato": _TEMP_STRATO_SCALE,
    "humidity_surface": _HUM_SCALE,
    "cloud_surface": _CLOUD_SCALE,
    "pressure_surface": _PRESS_SCALE,
    "storm_potential": _CAPE_SCALE,
    "cin_surface": _CIN_SCALE,
    "pm25": _PM25_SCALE, "pm10": _PM10_SCALE, "co": _CO_SCALE,
    "no2": _NO2_SCALE, "so2": _SO2_SCALE, "o3": _O3_SCALE, "aod": _AOD_SCALE,
    "ispu": _ISPU_SCALE, "pbl": _PBL_SCALE, **_DT_SCALES,
}


def _export_velocity_json(u: np.ndarray, v: np.ndarray, grid: dict,
                          run: dt.datetime, fstep: int, dest: Path) -> None:
    """Tulis JSON format 'velocity' (dipakai leaflet-velocity / earth wind-js).

    Urutan data: baris-major dari la1(utara) ke la2(selatan), lo1(barat) ke lo2(timur).
    u = parameterNumber 2, v = parameterNumber 3 (parameterCategory 2 = momentum).
    """
    nx, ny = grid["width"], grid["height"]
    dx = round((grid["east"] - grid["west"]) / (nx - 1), 4)
    dy = round((grid["north"] - grid["south"]) / (ny - 1), 4)
    header = {
        "lo1": grid["west"], "la1": grid["north"],
        "lo2": grid["east"], "la2": grid["south"],
        "nx": nx, "ny": ny, "dx": dx, "dy": dy,
        "parameterCategory": 2, "parameterUnit": "m.s-1",
        "refTime": run.strftime("%Y-%m-%dT%H:00:00Z"),
        "forecastTime": fstep,
    }
    u_flat = np.nan_to_num(u).astype("float32").ravel(order="C").round(2).tolist()
    v_flat = np.nan_to_num(v).astype("float32").ravel(order="C").round(2).tolist()
    payload = [
        {"header": {**header, "parameterNumber": 2, "parameterNumberName": "U-component_of_wind"}, "data": u_flat},
        {"header": {**header, "parameterNumber": 3, "parameterNumberName": "V-component_of_wind"}, "data": v_flat},
    ]
    dest.write_text(json.dumps(payload, separators=(",", ":")))


def process_wind(grib_path: Path, layer_key: str, run: dt.datetime, fstep: int,
                 out_dir: Path = OUTPUT_DIR) -> dict:
    """Proses satu file GRIB angin -> PNG data + preview + velocity JSON + metadata."""
    layer = LAYERS[layer_key]
    u, v, grid = _load_wind(grib_path)

    valid_time = run + dt.timedelta(hours=fstep)
    stamp = f"{run:%Y%m%d_%H}_f{fstep:03d}"
    base = f"{layer_key}_{stamp}"

    data_png = out_dir / f"{base}.png"
    preview_png = out_dir / f"{base}_preview.webp"
    velocity_json = out_dir / f"{base}_velocity.json"
    meta_json = out_dir / f"{base}.json"

    _encode_vector_png(u, v, layer["unscale"], data_png)
    _render_speed_preview(u, v, preview_png)
    _export_velocity_json(u, v, grid, run, fstep, velocity_json)

    speed_kt = np.sqrt(u ** 2 + v ** 2) * MS_TO_KNOTS
    meta = {
        "layer": layer_key,
        "kind": layer["kind"],
        "model": "GFS",
        "level": layer["level_label"],
        "run_time": run.strftime("%Y-%m-%dT%H:00:00Z"),
        "forecast_step_hours": fstep,
        "valid_time": valid_time.strftime("%Y-%m-%dT%H:00:00Z"),
        "bounds": [grid["west"], grid["south"], grid["east"], grid["north"]],
        "width": grid["width"],
        "height": grid["height"],
        "unscale": layer["unscale"],
        "units": "m/s",
        "data_image": data_png.name,
        "preview_image": preview_png.name,
        "velocity_json": velocity_json.name,
        "speed_knots_max": round(float(np.nanmax(speed_kt)), 1),
    }
    meta_json.write_text(json.dumps(meta, indent=2))
    return meta, {"u": u, "v": v}


def process_scalar(grib_path: Path, layer_key: str, run: dt.datetime, fstep: int,
                   out_dir: Path = OUTPUT_DIR) -> dict:
    """Proses satu file GRIB skalar (mis. hujan) -> PNG heatmap berwarna + metadata."""
    layer = LAYERS[layer_key]
    values, grid = _load_scalar(grib_path, layer.get("filter_keys"))
    # ke satuan tampilan: value * to_unit + offset (mis. K->°C, Pa->hPa).
    values = values * float(layer.get("to_unit", 1.0)) + float(layer.get("offset", 0.0))

    valid_time = run + dt.timedelta(hours=fstep)
    base = f"{layer_key}_{run:%Y%m%d_%H}_f{fstep:03d}"
    preview_png = out_dir / f"{base}_preview.webp"
    meta_json = out_dir / f"{base}.json"

    scale = _SCALAR_SCALES.get(layer_key, _RAIN_SCALE)
    _render_scalar_preview(values, scale, preview_png, kategori=layer_key in _LAYER_KATEGORI)

    meta = {
        "layer": layer_key,
        "kind": "scalar",
        "model": "GFS",
        "level": layer["level_label"],
        "run_time": run.strftime("%Y-%m-%dT%H:00:00Z"),
        "forecast_step_hours": fstep,
        "valid_time": valid_time.strftime("%Y-%m-%dT%H:00:00Z"),
        "bounds": [grid["west"], grid["south"], grid["east"], grid["north"]],
        "width": grid["width"],
        "height": grid["height"],
        "units": layer["units"],
        "preview_image": preview_png.name,
        "value_max": round(float(np.nanmax(values)), 2),
    }
    meta_json.write_text(json.dumps(meta, indent=2))
    return meta, {"values": values}


def load_prate_mmhr(grib_path: Path) -> tuple[np.ndarray, dict]:
    """Muat PRATE (instant) sebagai mm/jam + grid (untuk akumulasi harian)."""
    vals, grid = _load_scalar(grib_path, {"stepType": "instant"})
    return vals * 3600.0, grid


def write_scalar_frame(values: np.ndarray, grid: dict, layer_key: str, run: dt.datetime,
                       valid_dt: dt.datetime, units: str, base_suffix: str,
                       extra: dict | None = None, out_dir: Path = OUTPUT_DIR) -> dict:
    """Render heatmap + tulis meta untuk array skalar yang SUDAH dihitung
    (dipakai mis. akumulasi hujan harian). base_suffix jadi bagian nama file."""
    scale = _SCALAR_SCALES.get(layer_key, _RAIN_SCALE)
    base = f"{layer_key}_{run:%Y%m%d_%H}_{base_suffix}"
    preview_png = out_dir / f"{base}_preview.webp"
    meta_json = out_dir / f"{base}.json"
    _render_scalar_preview(values, scale, preview_png, scale_up=_LAYER_SKALA.get(layer_key, 4),
                           kategori=layer_key in _LAYER_KATEGORI,
                           grid=grid, potong_darat=layer_key in _LAYER_DARAT)
    meta = {
        "layer": layer_key, "kind": "scalar", "model": "CAMS",
        "level": LAYERS[layer_key]["level_label"],
        "run_time": run.strftime("%Y-%m-%dT%H:00:00Z"),
        "valid_time": valid_dt.strftime("%Y-%m-%dT%H:00:00Z"),
        "forecast_step_hours": int((valid_dt - run).total_seconds() // 3600),
        "bounds": [grid["west"], grid["south"], grid["east"], grid["north"]],
        # Kotak untuk MENEMPATKAN gambar: tepi sel, bukan pusat sel. Beda dengan
        # `bounds` yang dipakai menyampel nilai per titik.
        "image_bounds": [tepi_gambar(grid)[0], tepi_gambar(grid)[2],
                         tepi_gambar(grid)[1], tepi_gambar(grid)[3]],
        "width": grid["width"], "height": grid["height"],
        "units": units, "preview_image": preview_png.name,
        "value_max": round(float(np.nanmax(values)), 2),
    }
    if extra:
        meta.update(extra)
    meta_json.write_text(json.dumps(meta, indent=2))
    return meta


# Encoding nilai per-titik (point_data.bin.gz). value = stored*scale + offset.
_POINT_ENC = {
    "u":        {"dtype": "int16", "scale": 0.01, "offset": 0.0},
    "v":        {"dtype": "int16", "scale": 0.01, "offset": 0.0},
    "rain":     {"dtype": "int16", "scale": 0.1,  "offset": 0.0},
    "temp":     {"dtype": "int16", "scale": 0.1,  "offset": 0.0},
    "humidity": {"dtype": "uint8", "scale": 1.0,  "offset": 0.0},
    "cloud":    {"dtype": "uint8", "scale": 1.0,  "offset": 0.0},
    "pressure": {"dtype": "int16", "scale": 0.1,  "offset": 1000.0},
    "cape":     {"dtype": "int16", "scale": 1.0,  "offset": 0.0},   # CAPE J/kg (bahan bakar badai)
    "cin":      {"dtype": "int16", "scale": 1.0,  "offset": 0.0},   # CIN J/kg, NEGATIF (penghambat)
}
_NP_DTYPE = {"int16": np.int16, "uint8": np.uint8}


def write_point_data(series: dict, times: list, grid: dict, out_dir: Path = OUTPUT_DIR) -> int:
    """Emit deret-waktu semua variabel untuk lookup per-titik:
    point_data.bin.gz (biner int16/uint8 terkompresi) + point_meta.json (layout).
    series = {var: [array per waktu]} berorientasi baris-0=utara, kolom-0=barat."""
    ntime, ny, nx = len(times), grid["height"], grid["width"]
    blob = bytearray()
    layout = []
    for var, enc in _POINT_ENC.items():
        arrs = series.get(var)
        if not arrs or len(arrs) != ntime:
            continue
        stacked = np.stack([np.nan_to_num(a) for a in arrs]).astype("float32")  # (t,ny,nx)
        stored = np.round((stacked - enc["offset"]) / enc["scale"]).astype(_NP_DTYPE[enc["dtype"]])
        b = stored.tobytes(order="C")
        layout.append({"var": var, "dtype": enc["dtype"], "scale": enc["scale"],
                       "offset": enc["offset"], "byteOffset": len(blob), "byteLength": len(b)})
        blob += b
    (out_dir / "point_data.bin.gz").write_bytes(gzip.compress(bytes(blob), compresslevel=6))
    meta = {
        "bounds": [grid["west"], grid["south"], grid["east"], grid["north"]],
        "nx": nx, "ny": ny,
        "dx": round((grid["east"] - grid["west"]) / (nx - 1), 4),
        "dy": round((grid["north"] - grid["south"]) / (ny - 1), 4),
        "times": times, "vars": layout,
    }
    (out_dir / "point_meta.json").write_text(json.dumps(meta))
    return (out_dir / "point_data.bin.gz").stat().st_size


# ================= Deret waktu per TITIK (buat plot di popup) =================
# Satu berkas biner per parameter: (nwaktu, ny, nx) int16 gzip. Frontend menyampel
# bilinear di titik yang diklik lalu menggambar plot. Skalanya beda-beda karena
# rentang parameternya jauh berbeda (CO bisa 100.000, AOD cuma 6).
_PD_SCALE = {"pm25": 1.0, "pm10": 1.0, "co": 10.0, "no2": 0.1,
             "so2": 0.1, "o3": 0.1, "aod": 0.001,
             # ISPU dilaporkan sebagai bilangan bulat, jadi skala 1 sudah pas.
             # Kode pencemar kritis itu indeks 0..5, bukan besaran.
             "ispu": 1.0, "ispu_kritis": 1.0,
             # PBL dalam meter, puncaknya ~3000. Muat di int16 tanpa diskalakan.
             "pbl": 1.0,
             # Daya tampung dalam ton/tahun, rentangnya lebar (ratusan ribu sampai
             # jutaan negatif di sel karhutla). int16 dibagi 100 memberi jangkauan
             # +-3,27 juta ton/tahun dengan resolusi 100 ton/tahun.
             **{f"dt_{p}": 100.0 for p in DT_PARAM},
             # Volume udara per sel dalam km3; puncaknya ~10.000, muat di int16.
             "dt_vol": 1.0}


def write_point_series(key: str, medan: list, waktu: list, grid: dict,
                       out_dir: Path = OUTPUT_DIR) -> dict:
    """Tulis pd_<key>.bin.gz + kembalikan metanya."""
    skala = _PD_SCALE.get(key, 1.0)
    tumpuk = np.stack([np.nan_to_num(m) for m in medan]).astype("float64")
    # int16 dibatasi +-32767. Nilai di luar jangkauan dipotong, bukan dibiarkan
    # membungkus jadi angka negatif yang menyesatkan.
    stored = np.clip(np.round(tumpuk / skala), -32767, 32767).astype("<i2")
    nama = f"pd_{key}.bin.gz"
    (out_dir / nama).write_bytes(gzip.compress(stored.tobytes(order="C"), compresslevel=6))
    return {"file": nama, "scale": skala, "nt": len(waktu),
            "nx": grid["width"], "ny": grid["height"],
            "west": grid["west"], "east": grid["east"],
            "north": grid["north"], "south": grid["south"],
            "times": waktu}


# ================= Nilai per KOTA (buat label di peta) =================
# Label kota cuma butuh nilai di 514 titik, bukan seluruh grid 473x265. Kalau
# frontend memakai point_data.bin.gz (21 MB) demi angka segitu, 99,6 persen isinya
# terbuang. Jadi nilainya disampel di sini, sekali saat pipeline jalan.
CITY_PLACES = BACKEND_DIR.parent / "frontend" / "data" / "id_places.json"
# Skala penyimpanan: nilai dibagi angka ini lalu dibulatkan jadi bilangan bulat,
# supaya JSON-nya pendek. Frontend mengalikannya kembali.
_CITY_ENC = {
    "ispu": 0.1, "pm25": 0.1, "pm10": 0.1, "co": 1.0, "no2": 0.01,
    "so2": 0.01, "o3": 0.1, "aod": 0.001, "pbl": 1.0,
    # Daya tampung ton/tahun, rentangnya jutaan. Skala 100 memendekkan JSON tanpa
    # kehilangan arti, toh label kota membulatkannya ke ribuan ton.
    **{f"dt_{p}": 100.0 for p in DT_PARAM},
}


def _kota_titik(grid: dict):
    """Precompute indeks bilinear untuk 514 titik kota + fungsi penyampel satu medan.

    Dipisah supaya write_city_data, arsip harian, dan panel peringatan memakai
    penempatan titik yang PERSIS SAMA."""
    places = json.loads(CITY_PLACES.read_text(encoding="utf-8"))
    nx, ny = grid["width"], grid["height"]
    dx = (grid["east"] - grid["west"]) / (nx - 1)
    dy = (grid["north"] - grid["south"]) / (ny - 1)
    lat = np.array([p["lat"] for p in places], "f8")
    lon = np.array([p["lon"] for p in places], "f8")
    fx = np.clip((lon - grid["west"]) / dx, 0, nx - 1)
    fy = np.clip((grid["north"] - lat) / dy, 0, ny - 1)
    x0 = np.floor(fx).astype(int); x1 = np.minimum(x0 + 1, nx - 1); tx = fx - x0
    y0 = np.floor(fy).astype(int); y1 = np.minimum(y0 + 1, ny - 1); ty = fy - y0

    def samp(a):
        a = np.asarray(a, dtype="float64")
        atas = a[y0, x0] * (1 - tx) + a[y0, x1] * tx
        bawah = a[y1, x0] * (1 - tx) + a[y1, x1] * tx
        return atas * (1 - ty) + bawah * ty

    return places, samp


def sampel_kota(medan: dict, grid: dict) -> tuple[list, dict]:
    """Sampel bilinear tiap parameter di titik kota, NaN dipertahankan.

    Return (places, {kunci: array (nkota, nwaktu)}). Dipakai bersama oleh arsip
    harian dan panel peringatan, jadi penyamplingan cuma sekali."""
    if not CITY_PLACES.exists():
        return [], {}
    places, samp = _kota_titik(grid)
    out = {}
    for kunci, arr in medan.items():
        if not arr:
            continue
        out[kunci] = np.stack([samp(a) for a in arr]).T   # (nkota, nwaktu)
    return places, out


def write_city_data(medan: dict, times: list, grid: dict, out_dir: Path = OUTPUT_DIR) -> int:
    """Sampel bilinear tiap parameter di titik kota/kabupaten -> city_data.json.

    Label kota cuma butuh nilai di 514 titik, bukan seluruh grid 296x165. Kalau
    frontend memakai pd_*.bin.gz (belasan MB) demi angka segitu, hampir seluruh
    isinya terbuang. Jadi disampel di sini, sekali saat pipeline jalan.

    `medan` = {kunci_layer: daftar array}. Daftar yang lebih PENDEK dari `times`
    dianggap mulai belakangan (mis. ISPU saat pemanasan gagal) dan bagian depannya
    diisi null, bukan diulang, supaya frontend tak menampilkan angka karangan."""
    if not CITY_PLACES.exists():
        print(f"  city_data dilewati: {CITY_PLACES.name} tak ada")
        return 0
    places, samp = _kota_titik(grid)
    nt = len(times)
    data, skala = {}, {}
    for kunci, arr in medan.items():
        if kunci not in _CITY_ENC or not arr:
            continue
        s = _CITY_ENC[kunci]
        per_t = [samp(a) for a in arr]
        blok = np.stack(per_t) / s                       # (nwaktu, nkota)
        kosong = ~np.isfinite(blok)
        bulat = np.where(kosong, 0, np.round(blok)).astype("int64").T   # (nkota, nwaktu)
        kosong = kosong.T
        depan = nt - bulat.shape[1]                      # deret yang mulai belakangan
        baris = []
        for i in range(bulat.shape[0]):
            v = [None] * depan + [None if kosong[i, k] else int(bulat[i, k])
                                  for k in range(bulat.shape[1])]
            baris.append(v)
        data[kunci] = baris
        skala[kunci] = s

    doc = {"times": times, "scales": skala,
           "places": [p["n"] for p in places], "data": data}
    path = out_dir / "city_data.json"
    path.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    return path.stat().st_size


if __name__ == "__main__":
    import glob
    from config import RAW_DIR

    grib = sorted(glob.glob(str(RAW_DIR / "gfs_*_f000_10_m_above_ground.grib2")))[-1]
    # ekstrak run & fstep dari nama file
    name = Path(grib).stem  # gfs_YYYYMMDD_HH_fSSS_...
    parts = name.split("_")
    run = dt.datetime.strptime(parts[1] + parts[2], "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
    fstep = int(parts[3][1:])
    meta, _ = process_wind(Path(grib), "wind_surface", run, fstep)
    print("OK — metadata:")
    print(json.dumps(meta, indent=2))
