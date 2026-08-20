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
}

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
