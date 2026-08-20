"""
Orchestrator Kertas Emisi: ambil run CAMS terbaru -> render tiap langkah ->
rekonsiliasi (retensi window) -> tulis catalog.json untuk frontend.

Jalankan: python run.py
Struktur keluarannya SAMA PERSIS dengan Kertas Cuaca, jadi frontend yang diwarisi
bisa membacanya tanpa diubah.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
from pathlib import Path

import numpy as np

import config as C
from config import KEEP_PAST_HOURS, LAYERS, OUTPUT_DIR
import cams
from process import (_export_velocity_json, hitung_daya_tampung, hitung_ispu,
                     luas_sel, write_city_data, write_point_series,
                     write_scalar_frame)


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.strptime(ts, "%Y-%m-%dT%H:00:00Z").replace(tzinfo=dt.timezone.utc)


def _frame_files(m: dict) -> list[str]:
    """Berkas milik satu frame, untuk dihapus saat frame itu dibuang.

    Velocity JSON sengaja TIDAK ikut. Di Kertas Emisi satu berkas velocity dipakai
    bersama oleh SEMUA layer pada langkah yang sama, jadi menghapusnya waktu satu
    frame dibuang akan melumpuhkan layer lain di langkah itu."""
    out = [m["_path"]]
    for k in ("preview_image", "data_image"):
        nama = m.get(k)
        if nama:
            out.append(str(OUTPUT_DIR / nama))
    return out

def reconcile_and_catalog(run: dt.datetime) -> tuple[dict, int]:
    """Kumpulkan SEMUA frame di disk (lintas run), buang yang lebih tua dari
    (run - KEEP_PAST_HOURS), dedup per (layer, valid_time) pilih run terbaru,
    lalu susun catalog. Mengembalikan (catalog, jumlah_frame)."""
    cutoff = run - dt.timedelta(hours=KEEP_PAST_HOURS)

    metas: list[dict] = []
    for mp in glob.glob(str(OUTPUT_DIR / "*.json")):
        p = Path(mp)
        if p.name == "catalog.json" or p.name.endswith("_velocity.json"):
            continue
        try:
            m = json.loads(p.read_text())
        except Exception:
            continue
        if "valid_time" not in m or "layer" not in m:
            continue
        m["_path"] = mp
        metas.append(m)

    # 1) buang frame lebih tua dari cutoff (-24 jam)
    kept: list[dict] = []
    for m in metas:
        if _parse(m["valid_time"]) < cutoff:
            for f in _frame_files(m):
                Path(f).unlink(missing_ok=True)
        else:
            kept.append(m)

    # 2) dedup per (layer, valid_time) -> run_time terbaru menang; sisanya dihapus
    best: dict[tuple, dict] = {}
    losers: list[dict] = []
    for m in kept:
        k = (m["layer"], m["valid_time"])
        cur = best.get(k)
        if cur is None or _parse(m["run_time"]) > _parse(cur["run_time"]):
            if cur is not None:
                losers.append(cur)
            best[k] = m
        else:
            losers.append(m)
    for m in losers:
        for f in _frame_files(m):
            Path(f).unlink(missing_ok=True)

    # 3) susun catalog dari frame pemenang
    by_layer: dict[str, list[dict]] = {}
    for m in best.values():
        by_layer.setdefault(m["layer"], []).append(m)

    catalog = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": "GFS",
        "run_time": run.strftime("%Y-%m-%dT%H:00:00Z"),
        "region": None,
        "layers": {},
    }
    total = 0
    for layer_key in LAYERS:  # jaga urutan definisi (angin dulu, lalu hujan)
        frames = by_layer.get(layer_key)
        if not frames:
            continue
        frames.sort(key=lambda m: _parse(m["valid_time"]))
        if catalog["region"] is None:
            catalog["region"] = {"bounds": frames[0]["bounds"]}
            if frames[0].get("image_bounds"):
                catalog["region"]["image_bounds"] = frames[0]["image_bounds"]
        entry = {
            "kind": frames[0]["kind"],
            "level": frames[0]["level"],
            "units": frames[0]["units"],
            "frames": [],
        }
        if frames[0].get("unscale") is not None:
            entry["unscale"] = frames[0]["unscale"]
        for m in frames:
            fr = {
                "valid_time": m["valid_time"],
                "forecast_step_hours": m["forecast_step_hours"],
                "preview_image": m["preview_image"],
            }
            for key in ("data_image", "velocity_json", "speed_knots_max", "value_max"):
                if m.get(key) is not None:
                    fr[key] = m[key]
            entry["frames"].append(fr)
        catalog["layers"][layer_key] = entry
        total += len(frames)
    return catalog, total


def _buka(nc):
    import xarray as xr
    ds = xr.open_dataset(nc)
    lat = np.asarray(ds["latitude"].values, dtype="f8")
    lon = np.asarray(ds["longitude"].values, dtype="f8")
    utara_dulu = lat[0] > lat[-1]                 # render butuh baris-0 = utara
    latN = lat if utara_dulu else lat[::-1]
    grid = {"west": float(lon[0]), "east": float(lon[-1]),
            "north": float(latN[0]), "south": float(latN[-1]),
            "width": int(lon.size), "height": int(latN.size)}
    jam = [int(x / 3600e9) for x in ds["forecast_period"].values.astype("int64")]
    return ds, grid, utara_dulu, jam


def _ambil(ds, nama, i, utara_dulu):
    a = np.asarray(ds[nama].isel(forecast_period=i).squeeze().values, dtype="f8")
    return a if utara_dulu else a[::-1]


def _run_paksa():
    """CAMS_RUN=20260819-12 memaksa satu run tertentu, melewati penjajakan ke ADS.

    Gunanya untuk render ulang di lokal: berkas mentahnya sudah ada di cache, jadi
    perubahan palet atau perhitungan bisa diuji tanpa menunggu antrean ADS."""
    v = os.environ.get("CAMS_RUN", "").strip()
    if not v:
        return None
    tgl, jam = v.split("-")
    return dt.datetime.strptime(tgl, "%Y%m%d").date(), f"{int(jam):02d}:00"


def main() -> None:
    hari, jam_run = _run_paksa() or cams.latest_available_run()
    run = dt.datetime.combine(hari, dt.time(int(jam_run[:2])), tzinfo=dt.timezone.utc)
    print(f"Run CAMS terpilih: {run:%Y-%m-%d %H}Z")

    single = [v["cams_var"] for v in LAYERS.values() if v["src"] == "single"]
    model = [v["cams_var"] for v in LAYERS.values() if v["src"] == "model"]

    # DUA permintaan terpisah. Variabel permukaan dan variabel 3D tak bisa digabung,
    # yang 3D butuh model_level dan ADS menolak kalau dicampur.
    print("\n[1/2] variabel permukaan + angin + suhu/tekanan")
    nc_s = cams.fetch(hari, jam_run, single + [C.WIND["cams_u"], C.WIND["cams_v"]] + C.UDARA["cams"],
                      dest=C.RAW_DIR / f"cams_sfc_{hari:%Y%m%d}_{jam_run[:2]}.nc")
    print("[2/2] gas di model_level 137 (lapisan paling bawah)")
    nc_m = cams.fetch(hari, jam_run, model, dest=C.RAW_DIR / f"cams_ml_{hari:%Y%m%d}_{jam_run[:2]}.nc",
                      model_level=["137"])

    ds_s, grid, up, jam_lead = _buka(nc_s)
    ds_m, _, up_m, _ = _buka(nc_m)
    print(f"\ngrid {grid['width']}x{grid['height']}  "
          f"bujur {grid['west']:.1f}..{grid['east']:.1f}  lintang {grid['south']:.1f}..{grid['north']:.1f}")
    print(f"{len(jam_lead)} langkah, tiap {C.CAMS['leadtime_step']} jam sampai {jam_lead[-1]} jam")

    # Velocity angin: satu per langkah, ditempel ke SEMUA frame parameter apa pun.
    vel_nama = {}
    for i, fstep in enumerate(jam_lead):
        u = _ambil(ds_s, C.WIND["nc_u"], i, up)
        v = _ambil(ds_s, C.WIND["nc_v"], i, up)
        nama = f"wind_{run:%Y%m%d_%H}_f{fstep:03d}_velocity.json"
        _export_velocity_json(u, v, grid, run, fstep, OUTPUT_DIR / nama)
        vel_nama[fstep] = nama
    print(f"velocity angin: {len(vel_nama)} berkas")

    # Kerapatan udara per langkah, untuk mengubah rasio campuran gas jadi ug/m3.
    rho = [_ambil(ds_s, C.UDARA["nc_p"], i, up) / (C.R_UDARA * _ambil(ds_s, C.UDARA["nc_t"], i, up))
           for i in range(len(jam_lead))]
    print(f"kerapatan udara: rata {np.mean([r.mean() for r in rho]):.3f} kg/m3")

    point_meta = {}
    medan_semua = {}
    kota_medan = {}      # semua parameter di SATU sumbu waktu, untuk label kota
    for key, lay in LAYERS.items():
        if lay["src"] == "turunan":
            continue                          # ISPU dihitung setelah semua parameter siap
        ds = ds_s if lay["src"] == "single" else ds_m
        uu = up if lay["src"] == "single" else up_m
        medan = []
        for i in range(len(jam_lead)):
            a = _ambil(ds, lay["nc_var"], i, uu)
            if lay["conv"] == "massa":
                a = a * 1e9                       # kg/m3 -> ug/m3
            elif lay["conv"] == "rasio":
                a = a * rho[i] * 1e9              # kg/kg * kg/m3 -> ug/m3
            medan.append(a)
        medan_semua[key] = medan

        if lay["daily"]:
            n, seri, waktu = _tulis_harian(key, lay, medan, jam_lead, run, grid, vel_nama)
            # Label kota memakai satu sumbu waktu untuk semua parameter. Rata-rata
            # harian dikembalikan ke tiap langkah di hari yang sama, jadi label PM
            # menampilkan rata 24 jam hari itu, bukan angka sesaat.
            kota_medan[key] = _sebar_harian(seri, waktu, jam_lead, run)
        else:
            n = _tulis_per_langkah(key, lay, medan, jam_lead, run, grid, vel_nama)
            seri, waktu = medan, [(run + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00Z")
                                  for h in jam_lead]
            kota_medan[key] = medan
        pm = write_point_series(key, seri, waktu, grid)
        pm["units"] = lay["units"]
        pm["daily"] = bool(lay["daily"])
        point_meta[key] = pm
        rata = np.nanmean([m.mean() for m in medan])
        maks = np.nanmax([np.nanmax(m) for m in medan])
        sat = lay["units"] or "tanpa satuan"
        print(f"  {key:5} {n:>3} frame  rata {rata:9.3f}  maks {maks:10.2f}  {sat}")

    # ISPU, turunan dari enam parameter di atas. Harus SETELAH loop, karena butuh
    # semuanya sekaligus untuk mengambil yang tertinggi.
    try:
        pra = _pemanasan_ispu(hari, jam_run)
        print(f"  pemanasan ISPU: {len(pra['pm25'])} langkah dari run "
              f"{hari - dt.timedelta(days=1):%Y-%m-%d} {jam_run[:2]}Z")
    except Exception as e:
        pra = None
        print(f"  pemanasan ISPU GAGAL ({e}); ISPU mulai 24 jam setelah waktu run")
    seri_i, seri_k, waktu_i = _tulis_ispu(medan_semua, jam_lead, run, grid, vel_nama, pra)
    pm = write_point_series("ispu", seri_i, waktu_i, grid)
    pm.update({"units": "", "daily": False, "window_hours": C.ISPU_WINDOW_HOURS})
    pmk = write_point_series("ispu_kritis", seri_k, waktu_i, grid)
    pmk.update({"units": "", "daily": False})
    pm["kritis_file"] = pmk["file"]
    pm["kritis_param"] = C.ISPU_PARAM
    point_meta["ispu"] = pm
    point_meta["ispu_kritis"] = pmk
    kota_medan["ispu"] = seri_i
    rata_i = np.nanmean([m.mean() for m in seri_i])
    maks_i = np.nanmax([np.nanmax(m) for m in seri_i])
    print(f"  {'ispu':5} {len(seri_i):>3} frame  rata {rata_i:9.3f}  maks {maks_i:10.2f}  "
          f"indeks (jendela {C.ISPU_WINDOW_HOURS} jam bergulir)")
    _ringkas_kritis(seri_i, seri_k)

    # ---- Daya tampung udara (Permen LH No. 5), satu layer per parameter ----
    lsm = _muat_lsm(hari, jam_run, grid, up)
    seri_dt, vol_dt, waktu_dt, luas_darat = _tulis_daya_tampung(
        medan_semua, jam_lead, run, grid, vel_nama, pra, lsm)
    darat = luas_darat > 0
    print(f"  daratan: {darat.sum()} dari {darat.size} sel ({100*darat.mean():.1f}%), "
          f"luas total {luas_darat.sum()/1e6:,.0f} km2")
    for par in C.DT_PARAM:
        pm = write_point_series(f"dt_{par}", seri_dt[par], waktu_dt, grid)
        pm.update({"units": "ton/tahun", "daily": False, "parameter": par,
                   "bmua": C.BMUA_24JAM[par], "window_hours": C.ISPU_WINDOW_HOURS})
        point_meta[f"dt_{par}"] = pm
        kota_medan[f"dt_{par}"] = seri_dt[par]
        nilai = np.stack(seri_dt[par])[:, darat]
        n = nilai.size
        print(f"  dt_{par:5} {len(seri_dt[par]):>3} frame  "
              f"median {np.nanmedian(nilai):12,.0f}  min {np.nanmin(nilai):14,.0f}  "
              f"terlampaui {100*np.nanmean(nilai < 0):5.1f}%  "
              f"terpotong {100*np.mean(np.abs(nilai) > 32767*100):.2f}%  ton/tahun")

    # Volume udara per sel (km3). Dengan ini popup bisa memecah DT jadi BE max dan
    # BE eks tanpa perlu menyimpan konsentrasi rata 24 jam-nya sendiri:
    #   BE max = V x BMUA,  BE eks = BE max - DT.
    pv = write_point_series("dt_vol", vol_dt, waktu_dt, grid)
    pv.update({"units": "km3", "daily": False})
    point_meta["dt_vol"] = pv

    waktu_penuh = [(run + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00Z") for h in jam_lead]
    ukuran = write_city_data(kota_medan, waktu_penuh, grid)
    print(f"  nilai per kota: {len(kota_medan)} parameter, {ukuran/1e6:.2f} MB")

    ds_s.close(); ds_m.close()
    (OUTPUT_DIR / "point_meta.json").write_text(json.dumps(point_meta, indent=2))
    tot = sum((OUTPUT_DIR / v["file"]).stat().st_size for v in point_meta.values())
    print(f"deret titik: {len(point_meta)} berkas, {tot/1e6:.1f} MB")

    catalog, total = reconcile_and_catalog(run)
    (OUTPUT_DIR / "catalog.json").write_text(json.dumps(catalog, indent=2))
    print(f"\nSelesai. {total} frame, {len(catalog['layers'])} layer -> catalog.json")


def _pemanasan_ispu(hari_run, jam_run):
    """Ambil 24 jam pertama dari run KEMARIN, khusus mengisi jendela ISPU.

    Run hari ini mulai di langkah 0, sedangkan ISPU butuh rata-rata 24 jam KE
    BELAKANG. Tanpa ini, ISPU baru punya angka 24 jam setelah waktu run, dan layer
    utama aplikasi jadi kosong untuk "sekarang".

    Frame run kemarin TIDAK bisa diandalkan sebagai gantinya: `backend/data/output`
    ada di .gitignore dan GitHub Actions selalu checkout bersih, jadi di situs live
    folder itu selalu mulai kosong dan retensi KEEP_PAST_HOURS tak pernah terpakai.

    Run kemarin jam yang sama, langkah 0..21, memberi waktu berlaku T-24 sampai T-3.
    Langkah T sendiri diambil dari run hari ini yang lebih segar."""
    hari = hari_run - dt.timedelta(days=1)
    lead = [str(h) for h in range(0, C.ISPU_WINDOW_HOURS, C.CAMS["leadtime_step"])]
    perlu = C.ISPU_PARAM + ["pbl"]      # PBL ikut: jendela daya tampung memakainya juga
    single = [C.LAYERS[k]["cams_var"] for k in perlu if C.LAYERS[k]["src"] == "single"]
    model = [C.LAYERS[k]["cams_var"] for k in perlu if C.LAYERS[k]["src"] == "model"]
    nc_s = cams.fetch(hari, jam_run, single + C.UDARA["cams"], lead=lead,
                      dest=C.RAW_DIR / f"cams_pra_sfc_{hari:%Y%m%d}_{jam_run[:2]}.nc")
    nc_m = cams.fetch(hari, jam_run, model, lead=lead, model_level=["137"],
                      dest=C.RAW_DIR / f"cams_pra_ml_{hari:%Y%m%d}_{jam_run[:2]}.nc")
    ds_s, _, up, jam_l = _buka(nc_s)
    ds_m, _, up_m, _ = _buka(nc_m)
    rho = [_ambil(ds_s, C.UDARA["nc_p"], i, up) / (C.R_UDARA * _ambil(ds_s, C.UDARA["nc_t"], i, up))
           for i in range(len(jam_l))]
    out = {}
    for key in perlu:
        lay = C.LAYERS[key]
        ds, uu = (ds_s, up) if lay["src"] == "single" else (ds_m, up_m)
        arr = []
        for i in range(len(jam_l)):
            a = _ambil(ds, lay["nc_var"], i, uu)
            if lay["conv"] == "massa":
                a = a * 1e9
            elif lay["conv"] == "rasio":
                a = a * rho[i] * 1e9
            arr.append(a)
        out[key] = arr
    ds_s.close(); ds_m.close()
    return out


def _tulis_ispu(medan_semua, jam_lead, run, grid, vel_nama, pemanasan=None):
    """ISPU dari jendela BERGULIR 24 jam, bukan blok harian.

    Pasal 6 ayat 1 Permen LHK 14/2020: ISPU dihitung tiap jam dari data pemantauan
    24 jam secara terus-menerus. Cadence kita 3 jam, jadi satu jendela = 9 langkah
    (t-24 sampai t). Delapan langkah pertama tiap run karena itu TIDAK punya ISPU,
    jendelanya belum penuh. Lubang itu terisi sendiri oleh frame run sebelumnya
    yang masih tersimpan dalam retensi -24 jam."""
    nwin = C.ISPU_WINDOW_HOURS // C.CAMS["leadtime_step"]
    pakai = [p for p in C.ISPU_PARAM if p in medan_semua]
    # Deret gabungan: langkah pemanasan dari run kemarin di depan, run ini di belakang.
    gab = {p: list((pemanasan or {}).get(p, [])) + list(medan_semua[p]) for p in pakai}
    geser = len(gab[pakai[0]]) - len(jam_lead)     # berapa langkah pemanasan yang ada
    # Kalau pemanasannya kurang, langkah paling awal terpaksa dilewati daripada
    # menyajikan rata-rata jendela yang belum genap 24 jam sebagai kalau-kalau genap.
    mulai = 0 if geser >= nwin else nwin - geser
    seri, seri_kritis, waktu = [], [], []
    for i in range(mulai, len(jam_lead)):
        j = geser + i
        jendela = slice(j - nwin, j + 1)
        rata = {par: np.nanmean(np.stack(gab[par][jendela]), axis=0) for par in pakai}
        ispu, kritis = hitung_ispu(rata)
        fstep = jam_lead[i]
        valid = run + dt.timedelta(hours=fstep)
        write_scalar_frame(ispu, grid, "ispu", run, valid, "", f"f{fstep:03d}",
                           extra={"model": "CAMS", "velocity_json": vel_nama[fstep],
                                  "window_hours": C.ISPU_WINDOW_HOURS})
        seri.append(ispu)
        seri_kritis.append(kritis)
        waktu.append(valid.strftime("%Y-%m-%dT%H:00:00Z"))
    return seri, seri_kritis, waktu


def _muat_lsm(hari, jam_run, grid, up):
    """Pecahan daratan tiap sel (land_sea_mask CAMS). Medannya statis, jadi cukup
    diminta satu langkah saja dan berlaku untuk seluruh deret."""
    nc = cams.fetch(hari, jam_run, ["land_sea_mask"], lead=["0"],
                    dest=C.RAW_DIR / f"cams_lsm_{hari:%Y%m%d}_{jam_run[:2]}.nc")
    ds, _, up_l, _ = _buka(nc)
    lsm = np.clip(_ambil(ds, "lsm", 0, up_l), 0.0, 1.0)
    ds.close()
    return lsm


def _tulis_daya_tampung(medan_semua, jam_lead, run, grid, vel_nama, pemanasan, lsm):
    """Daya tampung udara per sel, ton/tahun, satu berkas per parameter.

    Jendelanya sama dengan ISPU (24 jam bergulir), karena Permen LH 5 memakai
    rata-rata harian dan BMUA harian. PBLH juga dirata-rata 24 jam supaya sepadan
    dengan konsentrasinya. Luas yang dipakai luas DARATAN saja, yaitu luas sel
    dikali pecahan daratan; volume udara di atas laut tak berarti untuk kuota emisi."""
    nwin = C.ISPU_WINDOW_HOURS // C.CAMS["leadtime_step"]
    butuh = C.DT_PARAM + ["pbl"]
    gab = {p: list((pemanasan or {}).get(p, [])) + list(medan_semua[p]) for p in butuh}
    geser = len(gab["pbl"]) - len(jam_lead)
    mulai = 0 if geser >= nwin else nwin - geser
    luas_darat = luas_sel(grid) * np.where(lsm >= C.LSM_MIN, lsm, 0.0)

    seri = {par: [] for par in C.DT_PARAM}
    vol = []          # volume udara per sel, km3, dipakai popup memecah BE max & BE eks
    waktu = []
    for i in range(mulai, len(jam_lead)):
        j = geser + i
        jendela = slice(j - nwin, j + 1)
        pblh24 = np.nanmean(np.stack(gab["pbl"][jendela]), axis=0)
        vol.append(np.where(luas_darat > 0, luas_darat * pblh24 / 1e9, np.nan))
        fstep = jam_lead[i]
        valid = run + dt.timedelta(hours=fstep)
        for par in C.DT_PARAM:
            c24 = np.nanmean(np.stack(gab[par][jendela]), axis=0)
            dtp = hitung_daya_tampung(par, c24, pblh24, luas_darat)
            write_scalar_frame(dtp, grid, f"dt_{par}", run, valid, "ton/tahun",
                               f"f{fstep:03d}",
                               extra={"model": "CAMS", "velocity_json": vel_nama[fstep],
                                      "window_hours": C.ISPU_WINDOW_HOURS,
                                      "bmua": C.BMUA_24JAM[par], "parameter": par})
            seri[par].append(dtp)
        waktu.append(valid.strftime("%Y-%m-%dT%H:00:00Z"))
    return seri, vol, waktu, luas_darat


def _ringkas_kritis(seri_i, seri_k) -> None:
    """Cetak sebaran pencemar kritis. Berguna untuk memeriksa kewajaran: di
    Indonesia PM2.5 memang biasanya yang menentukan, kalau bukan itu curigai
    konversi satuannya."""
    kode = np.stack(seri_k).ravel()
    n = kode.size
    bagian = [(C.ISPU_PARAM[k], int((kode == k).sum())) for k in range(len(C.ISPU_PARAM))]
    bagian = [(nama, c) for nama, c in bagian if c]
    bagian.sort(key=lambda x: -x[1])
    txt = ", ".join(f"{nama} {100 * c / n:.1f}%" for nama, c in bagian)
    print(f"  pencemar kritis: {txt}")


def _sebar_harian(seri, waktu, jam_lead, run):
    """Kembalikan rata-rata harian ke sumbu langkah penuh: tiap langkah memakai
    rata-rata hari WIB tempat langkah itu jatuh."""
    per_tgl = {w[:10]: a for w, a in zip(waktu, seri)}
    keluar = []
    for h in jam_lead:
        vt = run + dt.timedelta(hours=h)
        tgl = dt.datetime.combine((vt + dt.timedelta(hours=C.WIB)).date(), dt.time(12),
                                  tzinfo=dt.timezone.utc) - dt.timedelta(hours=C.WIB)
        keluar.append(per_tgl.get(tgl.strftime("%Y-%m-%d"), None))
    # hari yang tak punya rata-rata (kepotong di ujung) diisi NaN, bukan diulang
    contoh = next(a for a in keluar if a is not None) if any(a is not None for a in keluar) else None
    if contoh is None:
        return []
    return [a if a is not None else np.full_like(contoh, np.nan) for a in keluar]


def _tulis_per_langkah(key, lay, medan, jam_lead, run, grid, vel_nama) -> int:
    for i, fstep in enumerate(jam_lead):
        valid = run + dt.timedelta(hours=fstep)
        write_scalar_frame(medan[i], grid, key, run, valid, lay["units"], f"f{fstep:03d}",
                           extra={"model": "CAMS", "velocity_json": vel_nama[fstep]})
    return len(jam_lead)


def _tulis_harian(key, lay, medan, jam_lead, run, grid, vel_nama) -> int:
    """Rata-rata 24 jam per TANGGAL WIB. Baku mutu partikel memang rata-rata harian,
    dan fluktuasi per jam untuk PM lebih banyak derau daripada informasi."""
    hari = {}
    for i, fstep in enumerate(jam_lead):
        vt = run + dt.timedelta(hours=fstep)
        tgl = (vt + dt.timedelta(hours=C.WIB)).date()      # kelompokkan menurut hari WIB
        hari.setdefault(tgl, []).append(i)
    n, seri, waktu = 0, [], []
    for tgl, idx in sorted(hari.items()):
        if len(idx) < 4:            # hari yang cuma kepotong sedikit -> lewati
            continue
        rerata = np.nanmean(np.stack([medan[i] for i in idx]), axis=0)
        seri.append(rerata)
        # Waktu berlaku = tengah hari WIB, dalam UTC. Slider hanya menampilkan tanggal.
        valid = dt.datetime.combine(tgl, dt.time(12), tzinfo=dt.timezone.utc) - dt.timedelta(hours=C.WIB)
        tengah = idx[len(idx) // 2]
        write_scalar_frame(rerata, grid, key, run, valid, lay["units"], f"d{tgl:%Y%m%d}",
                           extra={"model": "CAMS", "daily": True,
                                  "n_langkah": len(idx),
                                  "velocity_json": vel_nama[jam_lead[tengah]]})
        waktu.append(valid.strftime("%Y-%m-%dT%H:00:00Z"))
        n += 1
    return n, seri, waktu


if __name__ == "__main__":
    main()
