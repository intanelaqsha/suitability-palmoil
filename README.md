# RI-2-9 Palm Oil Suitability — Aceh Test Pipeline

End-to-end pipeline: raw DEM/canopy tiles → binary suitability raster → H3 resolution-11
Parquet. Scoped to Aceh province, Indonesia, as a small-region validation before scaling to
the full pan-tropical belt (per `palm_oil_suitability_RI-2-9 Version 2 2026-07-20.pdf`).

**Rule:** a location is `suitable` if `(slope ≤ 25°) AND (elevation ≤ 1,000m) AND (not water)`.
Terrain-only — no climate, soil, land-cover, or economic filters beyond excluding open water.

## What you need before starting

- GDAL CLI (`gdalwarp`, `gdal_calc.py`, `gdaldem`, `gdalbuildvrt`, `gdal_translate`) — verified
  working with Homebrew GDAL 3.13.3.
- AWS CLI, `curl`.
- `python3.10` specifically for the H3 step (see [Part 2](#part-2-convert-to-h3-resolution-11)
  for why — this machine's default `python3` is 3.14, and several geospatial packages don't
  have prebuilt wheels for it yet).

---

## Part 1: Build the suitability raster

```bash
cd ~/Downloads/"Suitability Maps"
bash run_aceh_pipeline.sh
```

One command, ~10-15 min and ~2GB of downloads on a first run (re-runs skip files already on
disk). Does everything: fetches Copernicus GLO-30 DEM + Water Body Mask tiles and ETH Global
Canopy Height tiles for the Aceh bounding box, mosaics them, subtracts canopy height from the
DEM to get bare-earth, smooths it (3×3 mean, suppresses canopy-model noise), computes slope,
thresholds, excludes water, and cloud-optimizes the result.

**Output: `aceh_test/suitable_cog.tif`** — single-band Byte COG, `0`=unsuitable/water,
`1`=suitable, ~29.9% of the AOI. See that folder's pipeline stages, the NoData pitfall, and
known caveats in the [Pipeline details](#pipeline-details-part-1) section below — worth
reading before touching the script, not just running it blind.

---

## Part 2: Convert to H3 resolution 11

```bash
cd ~/Downloads/"Suitability Maps"/h3-export
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python polygon_to_h3.py ../aceh_test/suitable_cog.tif -r 11
```

Use `python3.10`, not a bare `python3` — confirmed working (same version `radd-main`'s own
venv uses); the system default 3.14 risks slow/failing from-source builds for
rasterio/pyarrow/shapely/pyproj.

**This step is slow — confirmed ~4h23m for Aceh alone**, not a bug to fix before running it.
`polygon_to_h3.py` vectorizes the raster's suitable area into polygons (fast, ~5s) then fills
each with H3 cells (`h3.polygon_to_cells`) — fine for most polygons, but H3's fill algorithm
tests every candidate hexagon individually against the polygon boundary with no shortcut for
solid interior area, so a handful of very large/complex polygons (confirmed via live process
sampling) each took over an hour by themselves. Do not use `suitability_to_h3.py` (the other
script in this folder) — it's the original per-pixel approach, benchmarked at ~1,000 rows/sec,
which extrapolates to 12+ hours on Aceh alone. Kept only for reference. See
[Known issues & next steps](#known-issues--next-steps) for what actually fixes this (not yet
built).

**Output: `h3-export/output/suitable_cog_h3.parquet`** — columns `h3` (resolution-11 cell
index, string), `hex_area_m2` (double). 21,133,020 rows for Aceh; only suitable cells are
present — unsuitable area isn't stored as rows at all.

---

## Validate the result

Six checks, run from `h3-export` with the venv active:

```bash
cd ~/Downloads/"Suitability Maps"/h3-export
source .venv/bin/activate
```

**1. File opens cleanly, basic shape:**
```bash
python3 -c "
import pyarrow.parquet as pq
t = pq.read_table('output/suitable_cog_h3.parquet')
print('rows:', t.num_rows)
print('schema:', t.schema)
print(t.slice(0,5).to_pandas())
"
```

**2. Cross-check with a different reader (DuckDB, not PyArrow) + duplicate check:**
```bash
python3 -c "
import duckdb
con = duckdb.connect()
print(con.execute(\"SELECT COUNT(*), COUNT(DISTINCT h3) FROM read_parquet('output/suitable_cog_h3.parquet')\").fetchall())
"
```
Both counts should match (no duplicates — built from a Python `set`).

**3. Every H3 index is valid and actually resolution 11:**
```bash
python3 -c "
import pyarrow.parquet as pq
import h3
t = pq.read_table('output/suitable_cog_h3.parquet')
ids = t['h3'].to_pylist()
bad = [h for h in ids if not h3.is_valid_cell(h) or h3.get_resolution(h) != 11]
print('total:', len(ids), '  invalid/wrong-resolution:', len(bad))
"
```

**4. Area values are sane (expect ~2,150-2,600 m², no zeros/negatives/outliers):**
```bash
python3 -c "
import pyarrow.parquet as pq
import pyarrow.compute as pc
t = pq.read_table('output/suitable_cog_h3.parquet')
print('min:', pc.min(t['hex_area_m2']).as_py())
print('max:', pc.max(t['hex_area_m2']).as_py())
print('mean:', pc.mean(t['hex_area_m2']).as_py())
"
```

**5. Total area roughly matches the source raster** (expect a few % difference from
hex-vs-pixel boundary approximation — not a red flag):
```bash
python3 -c "
import pyarrow.parquet as pq
import pyarrow.compute as pc
t = pq.read_table('output/suitable_cog_h3.parquet')
print(f'{pc.sum(t[\"hex_area_m2\"]).as_py()/1e6:,.1f} km^2 total (H3)')
print(f'{56_619_610 * 900 / 1e6:,.1f} km^2 total (raster pixel count)')
"
```

**6. Spot-check a known point** (flat Banda Aceh delta, validated earlier in the raster
pipeline: slope=2.0°, elevation=6.1m, raster value=1 — should print `True`):
```bash
python3 -c "
import h3, pyarrow.parquet as pq
t = pq.read_table('output/suitable_cog_h3.parquet')
h3_set = set(t['h3'].to_pylist())
cell = h3.latlng_to_cell(5.545437, 95.310726, 11)
print('known-suitable point present:', cell in h3_set)
"
```

**Visual check:** kepler.gl (native H3 hexagon rendering, handles all 21M rows) — drag in
`suitable_cog_h3.parquet`, add an H3 layer on the `h3` column. Or for a local/QGIS check on a
smaller sample, convert cells to boundary polygons and export GeoJSON (see
`h3-export/README.md` for the script).

---

## Known issues & next steps

- **Global-belt scale is not solved yet.** 4h23m for one small province, with two separate
  multi-hour stalls on individual complex polygons, is proof of correctness, not proof of a
  viable path to the full tropical belt (vastly larger). The real fix is adaptive/hierarchical
  refinement: test coarse H3 cells (e.g. resolution 6) against the suitable-area polygons —
  fully outside → discard, fully inside → generate all resolution-11 descendants via cheap
  index arithmetic (`h3.cell_to_children` / `h3.uncompact_cells`, no geometry, no per-cell
  test), only recurse into children for cells that straddle a boundary. This only pays the
  expensive point-in-polygon cost near actual edges (mountain ridgelines, coastlines) instead
  of for every cell in the solid interior of a large blob. Still uniform resolution 11 output —
  matches spec, just gets there without enumerating every interior cell one at a time. **Not
  yet built.**
- Urban/built-up areas (e.g. Banda Aceh's city core) still register as suitable, since the
  base DSM includes building rooftops and the canopy correction only removes tree height. A
  land-cover mask would be the next layer to add for a real risk product.
- AOI is a rectangular bounding box, not Aceh's actual administrative polygon.

## Pipeline details (Part 1)

<details>
<summary>Data sources, pipeline stages, the NoData pitfall, and full caveats</summary>

### Data sources

| Dataset | Role | Source | Resolution |
|---|---|---|---|
| Copernicus GLO-30 | Base elevation (DSM) | `s3://copernicus-dem-30m/` (AWS Open Data, no auth) | 30m |
| Copernicus WBM | Water body mask (0=land, 1=ocean, 2=lake, 3=river) | Same bucket, `<tile>/AUXFILES/<tile>_WBM.tif` | 30m, same grid as DEM |
| ETH Global Canopy Height 2020 | Canopy height, subtracted from DSM for bare-earth | `libdrive.ethz.ch` (Lang et al. 2023, CC BY 4.0) | 10m |

GLO-30/WBM tiles are 1°×1°, named by SW-corner lat/lon
(`Copernicus_DSM_COG_10_N05_00_E095_00_DEM.tif`). ETH canopy tiles are 3°×3°
(`ETH_GlobalCanopyHeight_10m_2020_N03E096_Map.tif`). Some GLO-30/WBM tiles legitimately don't
exist over open ocean — the fetch loop skips these (404 = expected, not an error).

### Pipeline stages

1. Fetch GLO-30 DEM tiles intersecting the Aceh bbox (`94.9, 1.8, 98.3, 6.1`).
2. Fetch matching WBM tiles (same tile names, `AUXFILES/` subfolder).
3. Fetch ETH canopy tiles covering the same area.
4. Mosaic each dataset into a VRT.
5. Clip the DEM to the AOI on its native 30m grid — the reference grid everything else aligns to.
6. Align canopy height onto that grid (10m→30m, mean-resampled) — see NoData pitfall below.
7. Bare-earth = DSM − canopy, clamped so canopy height can never exceed the DSM value.
8. Smooth bare-earth with a 3×3 mean filter before slope (PDF §5, "smooth lightly") — a small
   inline `numpy` pass, since no GDAL CLI utility does neighborhood ops directly. Also reduces
   (doesn't fully eliminate) noise in dense urban areas from DSM building-rooftop relief.
9. Slope via Horn's method on the smoothed surface, then the terrain threshold.
10. Water exclusion: align WBM to the same grid (nearest-neighbor), build a land mask
    (`WBM==0`), AND it into the terrain mask, cloud-optimize.

### NoData pitfall

ETH canopy tiles use `255` as their NoData sentinel (real canopy values run 0-64m). Tagging
the *output* of the canopy resample as NoData=255 (or worse, NoData=0, which collides with
genuine "no canopy" ground pixels) causes `gdal_calc.py` to mask its output to NoData wherever
*any* input band's pixel equals *that band's declared* NoData value — a post-processing step
that overrides whatever `--calc` actually computed. This silently dropped ~64% of the entire
AOI in an early version, invisible in histograms unless you compare classified pixel count
against the raster's total pixel count. Fix (already in the script): warp with `-srcnodata 255`
so invalid source pixels are still excluded from the average, but never tag the *output* as
NoData — zero-fill leftovers and strip the NoData flag explicitly before it reaches
`gdal_calc.py`.

### Caveats (carried over from the PDF, §9, plus what we found empirically)

- RSPO's 25° threshold is a 25ha block-average; per-pixel 30m thresholding is stricter than
  the regulatory intent. A focal-mean smoothing pass would better match it (the 3×3 filter in
  step 8 is a start, not a full fix for this specific point).
- Bare-earth correction is modelled, not measured — validate against reference lidar before
  trusting steep-edge pixels.
- Water exclusion only removes water — cities/roads/agriculture still register as suitable.
- Equatorial slope scale (`-s 111120`) is accurate to <0.5% at Aceh's latitude, would need a
  cos(latitude) correction closer to the belt's 30°N/S edges.
- AOI is a rectangular bounding box, not Aceh's real administrative polygon.

</details>
