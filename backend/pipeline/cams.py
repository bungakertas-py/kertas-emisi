"""Unduh data CAMS dari Atmosphere Data Store (ADS).

Alurnya tiga langkah: kirim permintaan, tunggu job selesai, unduh zip lalu buka.
Kredensial dibaca dari env ADS_KEY (dipakai CI) atau berkas ~/.cdsapirc (lokal).
Berkasnya sama dengan yang dipakai Climate Data Store, satu token berlaku di
kedua layanan sejak ECMWF menyatukan akunnya.
"""
from __future__ import annotations

import datetime as dt
import io
import os
import time
import zipfile
from pathlib import Path

import requests

import config as C

RAW_DIR = C.RAW_DIR


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


def latest_available_run(max_back_days: int = 2) -> tuple[dt.date, str]:
    """Cari run terbaru yang BENAR-BENAR sudah terbit.

    Penting: run hari ini sering belum ada dan ADS menolaknya dengan 400, bukan 404.
    Jadi kita coba mundur dari yang terbaru sampai ada yang diterima.
    """
    now = dt.datetime.now(dt.timezone.utc)
    kandidat = []
    for d in range(max_back_days + 1):
        hari = (now - dt.timedelta(days=d)).date()
        for jam in reversed(C.CAMS["runs"]):          # 12:00 dulu, baru 00:00
            kandidat.append((hari, jam))
    for hari, jam in kandidat:
        if _bisa(hari, jam):
            return hari, jam
    raise RuntimeError("Tak ada run CAMS yang tersedia dalam beberapa hari terakhir")


def _bisa(hari: dt.date, jam: str) -> bool:
    """Uji murah: minta SATU langkah saja. Diterima berarti run-nya ada."""
    body = _body(hari, jam, ["0"], [C.LAYERS["pm25"]["cams_var"]])
    r = requests.post(f"{C.CAMS['api']}/retrieve/v1/processes/{C.CAMS['dataset']}/execute",
                      headers=_hdr(), json=body, timeout=120)
    if r.status_code == 201:
        return True
    if r.status_code == 400:
        return False
    r.raise_for_status()
    return False


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


def fetch(hari: dt.date, jam: str, variabel: list[str], dest: Path | None = None,
          lead: list[str] | None = None, timeout_menit: int = 40,
          model_level: list[str] | None = None) -> Path:
    """Ambil satu berkas NetCDF berisi semua variabel & langkah yang diminta."""
    lead = lead or leadtimes()
    dest = dest or (RAW_DIR / f"cams_{hari:%Y%m%d}_{jam[:2]}.nc")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Sudah pernah diunduh -> pakai lagi. Job ADS bisa antre menit-menitan, jadi
    # jangan diulang cuma karena mau ganti palet atau memperbaiki render.
    if dest.exists() and dest.stat().st_size > 1_000_000:
        print(f"  pakai unduhan lama: {dest.name} ({dest.stat().st_size/1e6:.1f} MB)")
        return dest
    body = _body(hari, jam, lead, variabel, model_level)
    r = requests.post(f"{C.CAMS['api']}/retrieve/v1/processes/{C.CAMS['dataset']}/execute",
                      headers=_hdr(), json=body, timeout=180)
    r.raise_for_status()
    job = r.json()["jobID"]
    print(f"  job {job}, {len(variabel)} variabel x {len(lead)} langkah")

    batas = time.time() + timeout_menit * 60
    status = ""
    while time.time() < batas:
        j = requests.get(f"{C.CAMS['api']}/retrieve/v1/jobs/{job}", headers=_hdr(), timeout=60).json()
        status = j.get("status")
        if status in ("successful", "failed"):
            break
        time.sleep(10)
    if status != "successful":
        raise RuntimeError(f"Job CAMS {job} berakhir dengan status {status}")

    res = requests.get(f"{C.CAMS['api']}/retrieve/v1/jobs/{job}/results", headers=_hdr(), timeout=60).json()
    url = res["asset"]["value"]["href"]
    blob = requests.get(url, timeout=900).content
    # Balasannya zip berisi satu berkas nc. Dibuka ke berkas tunggal biar mudah dibaca.
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        nama = z.namelist()[0]
        dest.write_bytes(z.read(nama))
    print(f"  {dest.name}: {dest.stat().st_size/1e6:.1f} MB (dari {nama})")
    return dest
