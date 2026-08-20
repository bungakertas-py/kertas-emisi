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
from pathlib import Path

import numpy as np

import config as C
from config import KEEP_PAST_HOURS, LAYERS, OUTPUT_DIR
import cams
from process import _export_velocity_json, write_scalar_frame


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.strptime(ts, "%Y-%m-%dT%H:00:00Z").replace(tzinfo=dt.timezone.utc)

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


def main() -> None:
    hari, jam_run = cams.latest_available_run()
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

    for key, lay in LAYERS.items():
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

        if lay["daily"]:
            n = _tulis_harian(key, lay, medan, jam_lead, run, grid, vel_nama)
        else:
            n = _tulis_per_langkah(key, lay, medan, jam_lead, run, grid, vel_nama)
        rata = np.nanmean([m.mean() for m in medan])
        maks = np.nanmax([np.nanmax(m) for m in medan])
        sat = lay["units"] or "tanpa satuan"
        print(f"  {key:5} {n:>3} frame  rata {rata:9.3f}  maks {maks:10.2f}  {sat}")

    ds_s.close(); ds_m.close()
    catalog, total = reconcile_and_catalog(run)
    (OUTPUT_DIR / "catalog.json").write_text(json.dumps(catalog, indent=2))
    print(f"\nSelesai. {total} frame, {len(catalog['layers'])} layer -> catalog.json")


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
    n = 0
    for tgl, idx in sorted(hari.items()):
        if len(idx) < 4:            # hari yang cuma kepotong sedikit -> lewati
            continue
        rerata = np.nanmean(np.stack([medan[i] for i in idx]), axis=0)
        # Waktu berlaku = tengah hari WIB, dalam UTC. Slider hanya menampilkan tanggal.
        valid = dt.datetime.combine(tgl, dt.time(12), tzinfo=dt.timezone.utc) - dt.timedelta(hours=C.WIB)
        tengah = idx[len(idx) // 2]
        write_scalar_frame(rerata, grid, key, run, valid, lay["units"], f"d{tgl:%Y%m%d}",
                           extra={"model": "CAMS", "daily": True,
                                  "n_langkah": len(idx),
                                  "velocity_json": vel_nama[jam_lead[tengah]]})
        n += 1
    return n


if __name__ == "__main__":
    main()
