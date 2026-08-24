"""Unduh titik panas (hotspot) VIIRS dari NASA FIRMS (LANCE).

Data near-real-time, latensi ~3 jam, lisensi terbuka. Ini PENGAMATAN satelit,
bukan ramalan. Kredensial berupa MAP_KEY gratis, dibaca dari env FIRMS_KEY (dipakai
CI) atau berkas lokal ~/.firms_key. Daftar key di
https://firms.modaps.eosdis.nasa.gov/api/map_key/
"""
from __future__ import annotations

import csv
import io
import os
from pathlib import Path

import requests

import config as C


def _map_key() -> str:
    k = os.environ.get("FIRMS_KEY", "").strip()
    if k:
        return k
    for p in (Path.home() / ".firms_key", Path("/mnt/c/Users/fsyuk/.firms_key")):
        if p.exists():
            t = p.read_text(encoding="utf-8").strip()
            if t:
                return t
    raise RuntimeError("MAP_KEY FIRMS tak ada. Set env FIRMS_KEY atau sediakan ~/.firms_key")


def _ambil_satu(key: str, source: str, area, hari: int) -> list[dict]:
    w, s, e, n = area
    url = f"{C.FIRMS['api']}/{key}/{source}/{w},{s},{e},{n}/{hari}"
    # UA wajar: server FIRMS di balik Cloudflare, UA bawaan python bisa ditolak 403.
    r = requests.get(url, timeout=120, headers={"User-Agent": "kertas-emisi/1.0"})
    r.raise_for_status()
    teks = r.text
    # FIRMS kadang balas HTTP 200 dengan PESAN GALAT berupa teks (mis. MAP_KEY salah
    # atau kuota habis), bukan CSV. Kenali dari baris header yang wajib diawali
    # "latitude,". Tanpa ini pesan galat akan diam-diam terbaca sebagai 0 titik.
    if not teks.lstrip().lower().startswith("latitude,"):
        raise RuntimeError(f"balasan bukan CSV: {teks[:120]!r}")
    return list(csv.DictReader(io.StringIO(teks)))


def ambil(area=None, hari: int | None = None) -> list[dict]:
    """Gabungan baris hotspot VIIRS dari semua satelit sumber (CSV mentah per baris).

    Satu satelit yang gagal tidak menjatuhkan yang lain: dicatat lalu dilewati."""
    area = area or C.FIRMS["area"]
    hari = hari or C.FIRMS["hari"]
    key = _map_key()
    semua: list[dict] = []
    for src in C.FIRMS["sources"]:
        try:
            baris = _ambil_satu(key, src, area, hari)
            print(f"  FIRMS {src}: {len(baris)} titik")
            semua.extend(baris)
        except Exception as ex:
            print(f"  FIRMS {src} gagal: {ex}")
    return semua
