# Kertas Emisi — Peta Kualitas Udara Indonesia

Peta sebaran polutan untuk Indonesia dan sekitarnya dari model komposisi atmosfer
global CAMS. Kerangkanya diturunkan dari
[Kertas Cuaca](https://github.com/bungakertas-py/bungakertas-signature): domain,
bingkai, basemap, batas wilayah, panel, label kota, dan tata letaknya sama persis.
Yang berbeda hanya datanya.

## Parameter

| tombol | variabel CAMS | cadence | satuan |
|---|---|---|---|
| PM2.5 | `particulate_matter_2.5um` | harian | µg/m³ |
| PM10 | `particulate_matter_10um` | harian | µg/m³ |
| CO | `carbon_monoxide` | 3 jam | µg/m³ |
| NO2 | `nitrogen_dioxide` | 3 jam | µg/m³ |
| SO2 | `sulphur_dioxide` | 3 jam | µg/m³ |
| O3 | `ozone` | 3 jam | µg/m³ |
| Kabut Asap | `total_aerosol_optical_depth_550nm` | 3 jam | tanpa satuan |

Partikel dirata-ratakan 24 jam menurut tanggal WIB, karena baku mutu partikel memang
rata-rata harian dan fluktuasi per jamnya lebih banyak derau daripada informasi. Gas
tetap per langkah, karena NO2 dan O3 berubah tajam mengikuti jam sibuk dan sinar
matahari.

Angin 10 m ikut diunduh, tapi **bukan sebagai layer**. Dia jadi overlay partikel di
atas semua parameter. Kontur kecepatan angin sengaja tidak dibuat.

Timbal (Pb) tidak ada di model komposisi atmosfer mana pun. TSP juga tidak, karena
itu konvensi pengukuran alat, bukan keluaran model; PM10 pendekatan terdekatnya.

## Sumber data

CAMS global atmospheric composition forecasts (Copernicus / ECMWF), CC-BY-4.0.
Grid 0,4 derajat (~44 km), prakiraan 5 hari, dua run sehari (00 dan 12 UTC).
Diambil lewat Atmosphere Data Store.

Gas diambil di `model_level` 137 (lapisan paling bawah) dalam satuan rasio campuran
massa, lalu diubah ke µg/m³ memakai kerapatan udara yang dihitung dari suhu 2 m dan
tekanan permukaan. Tanpa konversi itu angkanya tak berarti.

## Jalankan

```
# butuh token ADS: env ADS_KEY, atau berkas ~/.cdsapirc
cd backend/pipeline && python run.py     # unduh + render, sekitar 3 menit
python dev_server.py                     # lalu buka http://127.0.0.1:8000/frontend/index.html
```
