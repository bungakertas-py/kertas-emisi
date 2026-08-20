"""
Konfigurasi pipeline peta cuaca (GFS).

Semua parameter global (region, variabel, path output) terpusat di sini agar
mudah diperluas ke multi-model / multi-level / multi-variabel di fase berikutnya.
"""
from pathlib import Path

# ---------------------------------------------------------------------------
# Domain DATA: Samudra Hindia (India) hingga Pasifik Barat, Cina Selatan hingga
# tengah Australia. Sengaja dibuat LEBIH LUAS dari area tampilan (viewing lock
# di frontend), supaya saat pan/zoom tepi domain data tidak pernah terlihat.
# Rasio domain (~118°bujur : ~66°lintang, proyeksi Mercator ~1.75) dekat dengan
# rasio layar desktop 16:9, jadi saat difit ke layar nyaris mengisi penuh.
# Cakupan inti (yang akan tampak): ~68-174E, -28..28N (India..Pasifik Barat,
# Cina Selatan..tengah Australia). Margin data ~5-6 derajat di tiap sisi.
# ---------------------------------------------------------------------------
REGION = {
    "left_lon": 62.0,     # 62E  (Laut Arab / India barat)
    "right_lon": 180.0,   # 180E (Pasifik Barat, batas antemeridian)
    "top_lat": 33.0,      # 33N  (Cina Selatan + margin)
    "bottom_lat": -33.0,  # 33S  (melewati tengah Australia)
}

# ---------------------------------------------------------------------------
# Model GFS (NOAA/NCEP), resolusi 0.25 derajat.
# Run tersedia 00/06/12/18 UTC, biasanya rilis ~3.5-5 jam setelah jam run.
# ---------------------------------------------------------------------------
GFS = {
    "resolution": "0p25",
    "run_hours": [0, 6, 12, 18],
    # jeda aman (jam) setelah jam-run sebelum data dianggap tersedia
    "availability_lag_hours": 5,
    # endpoint NOMADS "filter" untuk subset variabel + region (file kecil)
    "filter_url": "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl",
}

# ---------------------------------------------------------------------------
# Definisi layer. Fase 1: hanya angin permukaan (10 m).
# Tiap layer mendefinisikan variabel GRIB, level, tipe render, dan rentang skala.
# ---------------------------------------------------------------------------
# --- CAMS: prakiraan komposisi atmosfer global (Copernicus/ECMWF, CC-BY-4.0) ---
# Diambil lewat Atmosphere Data Store. Grid 0,4 derajat (~44 km), prakiraan 5 hari
# (0..120 jam, per jam), dua run sehari (00 dan 12 UTC).
CAMS = {
    "dataset": "cams-global-atmospheric-composition-forecasts",
    "api": "https://ads.atmosphere.copernicus.eu/api",
    # Ambil tiap 3 jam, bukan tiap jam. 121 frame terlalu berat untuk dirender tiap
    # hari, sedangkan polusi skala regional tak berubah drastis dalam 3 jam.
    "leadtime_step": 3,
    "leadtime_max": 120,
    "runs": ["00:00", "12:00"],
}

# Tujuh parameter. `single` = variabel permukaan satu level. `model` = variabel 3D,
# diambil di model_level 137 (lapisan paling bawah, ~10 m di atas tanah).
#
# PARTIKEL (pm25, pm10) dirata-ratakan jadi HARIAN. Alasannya, paparan partikel yang
# dipakai baku mutu memang rata-rata 24 jam, dan fluktuasi per jamnya lebih banyak
# derau daripada informasi. GAS tetap per langkah, karena NO2 dan O3 justru berubah
# tajam dalam sehari mengikuti jam sibuk dan sinar matahari.
LAYERS = {
    # ISPU bukan variabel CAMS, dia TURUNAN dari enam parameter di bawah. src
    # "turunan" membuatnya dilewati saat menyusun permintaan unduhan.
    "ispu": {"src": "turunan", "daily": False, "units": "", "level_label": "surface"},
    "pm25": {"cams_var": "particulate_matter_2.5um", "nc_var": "pm2p5", "src": "single",
             "conv": "massa", "daily": True, "units": "\u00b5g/m\u00b3", "level_label": "surface"},
    "pm10": {"cams_var": "particulate_matter_10um", "nc_var": "pm10", "src": "single",
             "conv": "massa", "daily": True, "units": "\u00b5g/m\u00b3", "level_label": "surface"},
    "co":   {"cams_var": "carbon_monoxide", "nc_var": "co", "src": "model",
             "conv": "rasio", "daily": False, "units": "\u00b5g/m\u00b3", "level_label": "surface"},
    "no2":  {"cams_var": "nitrogen_dioxide", "nc_var": "no2", "src": "model",
             "conv": "rasio", "daily": False, "units": "\u00b5g/m\u00b3", "level_label": "surface"},
    "so2":  {"cams_var": "sulphur_dioxide", "nc_var": "so2", "src": "model",
             "conv": "rasio", "daily": False, "units": "\u00b5g/m\u00b3", "level_label": "surface"},
    "o3":   {"cams_var": "ozone", "nc_var": "go3", "src": "model",
             "conv": "rasio", "daily": False, "units": "\u00b5g/m\u00b3", "level_label": "surface"},
    # AOD tak punya satuan, dia rasio pelemahan cahaya. Tak bisa dipaksa jadi ug/m3.
    "aod":  {"cams_var": "total_aerosol_optical_depth_550nm", "nc_var": "aod550", "src": "single",
             "conv": "apa_adanya", "daily": False, "units": "", "level_label": "surface"},
    # Tinggi lapisan batas. BUKAN polutan, melainkan penyebabnya: setinggi itulah
    # udara teraduk, jadi makin rendah makin sedikit ruang untuk mengencerkan emisi.
    # Malam hari bisa turun ke ~100 m, siang di darat bisa lebih dari 2000 m.
    "pbl":  {"cams_var": "boundary_layer_height", "nc_var": "blh", "src": "single",
             "conv": "apa_adanya", "daily": False, "units": "m", "level_label": "surface"},
    # Daya tampung, satu layer per parameter. Turunan, jadi tak ikut diunduh.
    **{f"dt_{par}": {"src": "turunan", "daily": False, "units": "ton/tahun",
                     "level_label": "surface"} for par in ("pm25", "pm10", "so2", "no2")},
}

# ---------------------------------------------------------------------------
# ISPU, Indeks Standar Pencemar Udara.
# Permen LHK No. P.14/MENLHK/SETJEN/KUM.1/7/2020, berlaku 15 Juli 2020.
#
# Lampiran I.A memberi BATAS ATAS konsentrasi tiap pita, semua dalam ug/m3 dan
# semua rata-rata 24 jam. Pita ISPU-nya 0-50, 51-100, 101-200, 201-300, >300,
# jadi simpul indeksnya 50, 100, 200, 300, 400.
#
# Lampiran I.B memberi rumusnya, interpolasi linear di dalam pita:
#     I = (Ia - Ib) / (Xa - Xb) * (Xx - Xb) + Ib
#
# CATATAN. Contoh hitungan di halaman 12 aturan itu SALAH untuk CO dan SO2, angka
# yang dipakai di situ masih dari KepBapedal 107/1997 yang sudah dicabut. Lima
# parameter lain cocok persis dengan tabel di bawah. Yang mengikat tabelnya, bukan
# contohnya, jadi tabel ini yang dipakai.
#
# HIDROKARBON (HC) TIDAK ADA di sini. CAMS tak menyediakan HC sebagai satu angka,
# yang ada cuma spesies VOC satu-satu. Menjumlahkannya lalu menyebutnya HC itu
# menyesatkan. Jadi ISPU kita dari 6 parameter dari 7, dan itu ditulis di UI.
# ---------------------------------------------------------------------------
ISPU_SIMPUL = [0, 50, 100, 200, 300, 400]
ISPU_TABEL = {
    "pm10": [0, 50,   150,  350,   420,   500],
    "pm25": [0, 15.5, 55.4, 150.4, 250.4, 500],
    "so2":  [0, 52,   180,  400,   800,   1200],
    "co":   [0, 4000, 8000, 15000, 30000, 45000],
    "o3":   [0, 120,  235,  400,   800,   1000],
    "no2":  [0, 80,   200,  1130,  2260,  3000],
}
# Urutan ini yang dipakai sebagai KODE pencemar kritis di berkas deret titik.
ISPU_PARAM = ["pm25", "pm10", "co", "no2", "so2", "o3"]
# Pasal 6 ayat 1: ISPU dihitung tiap jam dari data pemantauan 24 jam terus-menerus.
# Jadi jendelanya BERGULIR 24 jam, bukan blok harian. Cadence kita 3 jam, jadi satu
# jendela = 9 langkah (t-24 sampai t).
ISPU_WINDOW_HOURS = 24
# Di atas baris terakhir tabel aturan tak mengatur apa-apa. Kemiringan pita terakhir
# diteruskan lalu dipotong di sini, supaya asap karhutla ekstrem tetap punya angka
# yang bisa dibedakan, tapi tidak melar tanpa batas.
ISPU_MAKS = 500
# Lampiran II.A, kategori dan status warna. (batas_atas, nama)
ISPU_KATEGORI = [(50, "Baik"), (100, "Sedang"), (200, "Tidak Sehat"),
                 (300, "Sangat Tidak Sehat"), (10**9, "Berbahaya")]

# ---------------------------------------------------------------------------
# DAYA DUKUNG & DAYA TAMPUNG UDARA.
# Permen LH No. 5 tentang Perencanaan Perlindungan dan Pengelolaan Mutu Udara,
# Lampiran "Tata Cara Perhitungan Daya Dukung dan Daya Tampung Udara".
#
#   V udara = A x PBLH                          (m3)
#   BE max  = V udara x BMUA   x 1e-12          (ton, 1 ton = 1e12 ug)
#   BE eks  = V udara x C      x 1e-12          (ton)
#   DT      = BE max - BE eks
#   %DT     = (BE max - BE eks) / BE max
#
# Diuji lawan contoh Kota X di dokumennya dan cocok: A 1.284.680.353,83 m2,
# PBLH 533,54 m, BMUA 55, C 102,23 -> BE max 37,699 dan BE eks 70,071 ton,
# DT -32,373 ton. Dokumennya menulis 37,698 / 70,071 / -32,372, beda pembulatan.
#
# CATATAN SATUAN. V x C menghasilkan MASSA, bukan laju. Dokumennya menamainya
# ton/hari, jadi angka ton/tahun di aplikasi = angka harian itu dikali 365,
# mengikuti konvensi dokumennya, BUKAN hasil integrasi emisi setahun.
#
# OZON DIKELUARKAN. Permen LH 5 menyebutnya sebagai satu dari lima parameter,
# tapi PP 22/2021 tak punya BMUA 24 jam untuk ozon, cuma 1 jam (150), 8 jam (100),
# dan 1 tahun (35). Mengadu rata-rata 24 jam dengan ambang 8 jam itu keliru.
# ---------------------------------------------------------------------------
DT_PARAM = ["pm25", "pm10", "so2", "no2"]
# Baku Mutu Udara Ambien 24 jam, PP 22/2021 Lampiran VII (ug/m3).
BMUA_24JAM = {"pm25": 55.0, "pm10": 75.0, "so2": 75.0, "no2": 65.0}
UG_PER_TON = 1e12
HARI_PER_TAHUN = 365
# Sel dengan daratan di bawah ambang ini dianggap laut sepenuhnya dan tak dihitung.
LSM_MIN = 0.02
RADIUS_BUMI = 6_371_008.8      # meter, radius rata-rata IUGG

# Angin 10 m ikut diunduh di permintaan permukaan. Bukan layer sendiri, melainkan
# overlay partikel di atas SEMUA parameter. Kontur kecepatan sengaja TIDAK dibuat.
WIND = {"cams_u": "10m_u_component_of_wind", "cams_v": "10m_v_component_of_wind",
        "nc_u": "u10", "nc_v": "v10"}

# Suhu 2 m dan tekanan permukaan dipakai menghitung kerapatan udara, supaya rasio
# campuran massa gas (kg/kg) bisa diubah jadi ug/m3. Tanpa ini angkanya tak berarti.
UDARA = {"cams": ["2m_temperature", "surface_pressure"], "nc_t": "t2m", "nc_p": "sp"}
R_UDARA = 287.05          # konstanta gas udara kering, J/(kg K)
WIB = 7                   # rata-rata harian dikelompokkan menurut tanggal WIB

# ---------------------------------------------------------------------------
# Retensi data: tiap run disimpan frame dari (run_time - KEEP_PAST_HOURS) sampai
# ujung forecast (+72 jam). Frame lebih tua dari batas itu dihapus otomatis,
# sehingga window bergeser tiap hari (-24 jam belakang ... +72 jam depan).
# ---------------------------------------------------------------------------
KEEP_PAST_HOURS = 24

# ---------------------------------------------------------------------------
# PROFIL VERTIKAL (untuk diagram Skew-T log-P per titik). Diambil T/RH/angin di
# banyak level tekanan sekaligus (1 request/waktu, multi-level), lalu di-downsample
# ke grid lebih kasar (profil atmosfer mulus, tak perlu 0.25 derajat) untuk file
# kecil yang dimuat malas saat kartu Skew-T dibuka.
# ---------------------------------------------------------------------------
PROFILE_LEVELS = [
    1000, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500,
    450, 400, 350, 300, 250, 200, 150, 100, 70, 50,
]  # hPa, permukaan -> atas
PROFILE_STRIDE = 4   # ambil tiap-4 titik grid 0.25 -> ~1 derajat

# ---------------------------------------------------------------------------
# Path output. Data ditulis sebagai file statis (PNG + JSON) yang nanti
# disajikan langsung ke frontend (tanpa database).
# ---------------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_DIR / "data"
RAW_DIR = DATA_DIR / "raw"        # file GRIB2 mentah (sementara)
OUTPUT_DIR = DATA_DIR / "output"  # PNG + JSON siap-frontend

for _d in (DATA_DIR, RAW_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)
