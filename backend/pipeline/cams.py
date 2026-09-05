"""Unduh data CAMS dari Atmosphere Data Store (ADS).

Alurnya tiga langkah: kirim permintaan, tunggu job selesai, unduh zip lalu buka.
Kredensial dibaca dari env ADS_KEY (dipakai CI) atau berkas ~/.cdsapirc (lokal).
Berkasnya sama dengan yang dipakai Climate Data Store, satu token berlaku di
kedua layanan sejak ECMWF menyatukan akunnya.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import os
import time
import zipfile
from pathlib import Path

import requests

import config as C

RAW_DIR = C.RAW_DIR

# Server ADS sesekali memulangkan job dengan status "failed" tanpa sebab dari
# pihak kita. Sekali itu terjadi seluruh deploy mati dan data sehari hilang,
# padahal mengirim ulang job ke ADS tidak dipungut apa pun.
COBA_MAKS = 3      # berapa kali job dikirim ulang sebelum benar-benar menyerah
JEDA_COBA = 60     # detik, jeda sebelum kirim ulang


def _token() -> str:
    tok = os.environ.get("ADS_KEY", "").strip()
    if tok:
        return tok
    for p in (Path.home() / ".cdsapirc", Path("/mnt/c/Users/fsyuk/.cdsapirc")):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.startswith("key:"):
                    return line.split(":", 1)[1].strip()
    raise RuntimeError("Token ADS tak ada. Set env ADS_KEY atau sediakan ~/.cdsapirc")


def _hdr() -> dict:
    return {"PRIVATE-TOKEN": _token(), "Content-Type": "application/json"}


def leadtimes() -> list[str]:
    return [str(h) for h in range(0, C.CAMS["leadtime_max"] + 1, C.CAMS["leadtime_step"])]


def latest_available_run(variabel: list[str] | None = None,
                         max_back_days: int = 2) -> tuple[dt.date, str]:
    """Cari run terbaru yang sudah terbit LENGKAP, bukan sekadar sudah terdaftar.

    Dua hal berbeda yang dulu tertukar di sini. Run yang TERDAFTAR di katalog ADS
    belum tentu SUDAH LENGKAP di MARS. Run 12Z terdaftar beberapa jam sebelum
    langkah ramalan terakhirnya selesai ditulis. Kalau run itu yang dipilih,
    permintaan penuh di hilir mati dengan "Expected 41, got 27" dan seluruh deploy
    ikut mati. Itu yang terjadi 1 sampai 4 September 2026, empat malam beruntun.

    Makanya penjajakannya sekarang memakai langkah TERAKHIR, bukan langkah 0.
    """
    now = dt.datetime.now(dt.timezone.utc)
    kandidat = []
    for d in range(max_back_days + 1):
        hari = (now - dt.timedelta(days=d)).date()
        for jam in reversed(C.CAMS["runs"]):          # 12:00 dulu, baru 00:00
            kandidat.append((hari, jam))
    for hari, jam in kandidat:
        if _bisa(hari, jam, variabel):
            return hari, jam
    raise RuntimeError("Tak ada run CAMS yang tersedia dalam beberapa hari terakhir")


def _bisa(hari: dt.date, jam: str, variabel: list[str] | None = None) -> bool:
    """Uji murah, minta langkah TERAKHIR saja lalu tunggu job-nya benar benar jadi.

    Status HTTP tidak cukup untuk menilai ini. ADS memulangkan 400 untuk run yang
    belum terdaftar sama sekali, tapi 201 untuk run yang terdaftar walau isinya
    baru separuh. Lengkap atau tidaknya baru ketahuan setelah job-nya jalan di
    MARS. Satu langkah itu murah, sekitar setengah menit, jauh lebih murah
    daripada seluruh deploy mati dan data sehari hilang.
    """
    variabel = variabel or [C.LAYERS["pm25"]["cams_var"]]
    body = _body(hari, jam, [str(C.CAMS["leadtime_max"])], variabel)
    r = requests.post(f"{C.CAMS['api']}/retrieve/v1/processes/{C.CAMS['dataset']}/execute",
                      headers=_hdr(), json=body, timeout=120)
    if r.status_code == 400:
        return False                     # run-nya memang belum terbit
    r.raise_for_status()
    try:
        _tunggu(r.json()["jobID"], timeout_menit=15)
    except (_TakLengkap, _Sesaat) as e:
        print(f"  run {hari:%Y-%m-%d} {jam[:2]}Z dilewati, {e}")
        return False
    return True


def _body(hari: dt.date, jam: str, lead: list[str], variabel: list[str],
          model_level: list[str] | None = None) -> dict:
    R = C.REGION
    extra = {"model_level": model_level} if model_level else {}
    return {"inputs": {**extra,
        "variable": variabel,
        "date": f"{hari:%Y-%m-%d}/{hari:%Y-%m-%d}",
        "time": [jam],
        "leadtime_hour": lead,
        "type": ["forecast"],
        "data_format": "netcdf_zip",
        # urutan area = utara, barat, selatan, timur
        "area": [R["top_lat"], R["left_lon"], R["bottom_lat"], R["right_lon"]],
    }}


class _Sesaat(Exception):
    """Gagal yang pantas dicoba lagi. Server ADS ngambek, bukan permintaan kita salah."""


class _TakLengkap(Exception):
    """Run-nya belum lengkap di MARS. Ini TIDAK sembuh dalam semenit dua menit,
    jadi mengirim ulang job yang sama cuma membuang waktu. Yang benar mundur ke
    run sebelumnya."""


def _sebab_gagal(job: str) -> str:
    """Alasan job ditolak, diambil dari ADS lalu dipendekkan.

    Tanpa ini log cuma bilang "status failed" dan tak seorang pun tahu apa yang
    kurang. Empat malam deploy mati tanpa sebab yang terbaca gara gara itu.
    Balasan aslinya memuat seluruh permintaan MARS berbaris baris, jadi yang
    diambil cuma baris yang memuat pesan galatnya.
    """
    try:
        j = requests.get(f"{C.CAMS['api']}/retrieve/v1/jobs/{job}/results",
                         headers=_hdr(), timeout=60).json()
    except Exception:
        return "sebabnya tak bisa diambil dari ADS"
    tb = j.get("traceback") or j.get("detail") or ""
    inti = [b.strip() for b in tb.splitlines()
            if "Expected" in b or "has returned an error" in b or "failed with" in b]
    if not inti:
        inti = [b.strip() for b in tb.splitlines() if b.strip()][:2]
    return " | ".join(dict.fromkeys(inti))[:400] or "tanpa keterangan"


def _tak_lengkap(sebab: str) -> bool:
    """Bedakan "servernya lagi ngambek" dari "datanya memang belum ada"."""
    s = sebab.lower()
    return ("expected" in s or "mars has returned an error" in s
            or "check your selection" in s)


def _kirim(body: dict) -> str:
    """Kirim job, pulangkan id-nya."""
    try:
        r = requests.post(f"{C.CAMS['api']}/retrieve/v1/processes/{C.CAMS['dataset']}/execute",
                          headers=_hdr(), json=body, timeout=180)
        if r.status_code >= 500:
            raise _Sesaat(f"ADS balas HTTP {r.status_code} waktu job dikirim")
        r.raise_for_status()
    except requests.RequestException as e:
        raise _Sesaat(f"koneksi ke ADS putus waktu job dikirim, {type(e).__name__}") from None
    job = r.json()["jobID"]
    print(f"  job {job}")
    return job


def _tunggu(job: str, timeout_menit: int) -> None:
    """Tunggu sampai job selesai. Pulang diam diam kalau sukses, melempar kalau tidak."""
    batas = time.time() + timeout_menit * 60
    status = ""
    while time.time() < batas:
        try:
            j = requests.get(f"{C.CAMS['api']}/retrieve/v1/jobs/{job}",
                             headers=_hdr(), timeout=60).json()
        except requests.RequestException:
            # Satu kali gagal menanyakan kabar bukan alasan membuang job yang
            # mungkin sedang jalan. Tunggu lalu tanya lagi sampai batas waktu.
            time.sleep(10)
            continue
        status = j.get("status")
        if status in ("successful", "failed"):
            break
        time.sleep(10)
    if status == "successful":
        return
    if status == "failed":
        sebab = _sebab_gagal(job)
        if _tak_lengkap(sebab):
            raise _TakLengkap(f"job {job} ditolak MARS, {sebab}")
        raise _Sesaat(f"job {job} gagal di sisi ADS, {sebab}")
    # Kehabisan waktu. Job-nya kemungkinan masih mengantre, kirim ulang cuma
    # menaruh diri di ekor antrean yang sama. Jadi ini TIDAK diulang.
    raise RuntimeError(
        f"Job CAMS {job} belum selesai setelah {timeout_menit} menit (status {status or '?'})")


def _jalankan_job(body: dict, timeout_menit: int) -> bytes:
    """Kirim satu job ke ADS, tunggu, lalu pulangkan isi zip hasilnya."""
    job = _kirim(body)
    _tunggu(job, timeout_menit)
    try:
        res = requests.get(f"{C.CAMS['api']}/retrieve/v1/jobs/{job}/results",
                           headers=_hdr(), timeout=60).json()
        return requests.get(res["asset"]["value"]["href"], timeout=900).content
    except requests.RequestException as e:
        raise _Sesaat(f"job {job} sukses tapi unduhannya putus, {type(e).__name__}") from None


def fetch(hari: dt.date, jam: str, variabel: list[str], dest: Path | None = None,
          lead: list[str] | None = None, timeout_menit: int = 40,
          model_level: list[str] | None = None) -> Path:
    """Ambil satu berkas NetCDF berisi semua variabel & langkah yang diminta."""
    lead = lead or leadtimes()
    # Sidik jari daftar variabel ikut masuk nama berkas. Tanpa ini, menambah satu
    # variabel (mis. boundary_layer_height) akan DIAM-DIAM memakai unduhan lama yang
    # belum memuatnya, dan gagalnya baru ketahuan jauh di hilir sebagai KeyError.
    sidik = hashlib.sha1(",".join(sorted(variabel)).encode()).hexdigest()[:6]
    dest = dest or (RAW_DIR / f"cams_{hari:%Y%m%d}_{jam[:2]}.nc")
    if dest.suffix == ".nc" and sidik not in dest.name:
        dest = dest.with_name(f"{dest.stem}_{sidik}.nc")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Sudah pernah diunduh -> pakai lagi. Job ADS bisa antre menit-menitan, jadi
    # jangan diulang cuma karena mau ganti palet atau memperbaiki render.
    if dest.exists() and dest.stat().st_size > 1_000_000:
        print(f"  pakai unduhan lama: {dest.name} ({dest.stat().st_size/1e6:.1f} MB)")
        return dest
    body = _body(hari, jam, lead, variabel, model_level)
    print(f"  {len(variabel)} variabel x {len(lead)} langkah")
    blob = None
    for percobaan in range(1, COBA_MAKS + 1):
        try:
            blob = _jalankan_job(body, timeout_menit)
            break
        except _TakLengkap as e:
            # Bukan gangguan sesaat. Run-nya memang belum utuh, jadi berhenti di
            # sini dengan sebab yang terbaca, jangan buang 2 menit mengulang.
            raise RuntimeError(
                f"Run CAMS yang dipilih belum lengkap di MARS. {e}") from None
        except _Sesaat as e:
            if percobaan == COBA_MAKS:
                raise RuntimeError(
                    f"Job CAMS gagal {COBA_MAKS} kali berturut-turut. Terakhir: {e}") from None
            print(f"  {e}")
            print(f"  tunggu {JEDA_COBA} detik, lalu kirim ulang ({percobaan + 1}/{COBA_MAKS})")
            time.sleep(JEDA_COBA)
    # Balasannya zip berisi satu berkas nc. Dibuka ke berkas tunggal biar mudah dibaca.
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        nama = z.namelist()[0]
        dest.write_bytes(z.read(nama))
    print(f"  {dest.name}: {dest.stat().st_size/1e6:.1f} MB (dari {nama})")
    return dest
