# suitability-palmoil
# RI-2-9 Palm Oil Suitability — Aceh Test Pipeline

Prototype run of the binary (suitable/unsuitable) palm oil suitability mask described in
`palm_oil_suitability_RI-2-9 Version 2 2026-07-20.pdf`, scoped to Aceh province, Indonesia,
as a small-region validation before scaling to the full pan-tropical belt.

**Rule:** a pixel is `suitable` if `(slope ≤ 25°) AND (elevation ≤ 1,000m) AND (not water)`.
Terrain-only — no climate, soil, land-cover, or economic filters beyond excluding open water.

## Run it

```bash
bash run_aceh_pipeline.sh
```

Requires: GDAL CLI (`gdalwarp`, `gdal_calc.py`, `gdaldem`, `gdalbuildvrt`, `gdal_translate`),
AWS CLI, `curl`. All verified working with Homebrew GDAL 3.13.3. Run with `bash`, not pasted
into `zsh` directly — zsh doesn't word-split unquoted multi-value variables the way bash does,
which breaks `gdalwarp -te $TE`.

Takes ~10-15 min and ~2GB of downloads on first run (re-runs skip files already on disk).

## Data sources

| Dataset | Role | Source | Resolution |
|---|---|---|---|
| Copernicus GLO-30 | Base elevation (DSM) | `s3://copernicus-dem-30m/` (AWS Open Data, no auth) | 30m |
| Copernicus WBM | Water body mask (0=land, 1=ocean, 2=lake, 3=river) | Same bucket, `<tile>/AUXFILES/<tile>_WBM.tif` | 30m, same grid as DEM |
| ETH Global Canopy Height 2020 | Canopy height, subtracted from DSM for bare-earth | `libdrive.ethz.ch` (Lang et al. 2023, CC BY 4.0) | 10m |

GLO-30 and WBM tiles are 1°×1°, named by SW-corner lat/lon
(`Copernicus_DSM_COG_10_N05_00_E095_00_DEM.tif`). ETH canopy tiles are 3°×3°
(`ETH_GlobalCanopyHeight_10m_2020_N03E096_Map.tif`). Neither dataset covers a rectangle
cleanly — some GLO-30/WBM tiles legitimately don't exist over open ocean, which the fetch
loop skips (404 = expected, not an error).

## Pipeline stages

1. **Fetch GLO-30 DEM tiles** intersecting the Aceh bounding box (`94.9, 1.8, 98.3, 6.1`).
2. **Fetch matching WBM tiles** — same tile names, `AUXFILES/` subfolder.
3. **Fetch ETH canopy tiles** covering the same area.
4. **Mosaic** each dataset into a VRT (virtual, no disk copies).
5. **Clip the DEM** to the AOI on its native 30m grid — this defines the reference grid every
   later step aligns to.
6. **Align canopy height onto that grid** (10m → 30m, mean-resampled). This step has a subtle
   trap (see [NoData pitfall](#nodata-pitfall) below) — the script's `-srcnodata 255` +
   explicit zero-fill + `--NoDataValue=none` sequence exists specifically to avoid it.
7. **Bare-earth = DSM − canopy**, clamped so canopy height can never exceed the DSM value
   (protects against noisy canopy pixels driving elevation below plausible ground).
8. **Slope** via Horn's method (GDAL/ArcGIS default, degrees), then the terrain threshold
   `(slope≤25)*(elevation≤1000)`.
9. **Water exclusion**: align WBM to the same grid (nearest-neighbor — it's categorical, not
   continuous), build a land mask (`WBM==0`), and AND it into the terrain mask. Cloud-optimize
   the result.

Output: `aceh_test/suitable_cog.tif` — single-band Byte COG, `0`=unsuitable/water,
`1`=suitable, same grid as `dem_aoi.tif`.

## NoData pitfall

Worth understanding before touching this pipeline again. ETH canopy tiles use `255` as their
NoData sentinel (real canopy values run 0–64m). Naively resampling with `-dstnodata 255` (or
worse, `-dstnodata 0`, which collides with genuine "no canopy" ground pixels) tags the *output*
raster's metadata as NoData=255. `gdal_calc.py` then masks its output to NoData wherever *any*
input band's pixel equals *that band's declared* NoData value — as a post-processing step that
overrides whatever the `--calc` expression actually computed. In the first Aceh run this
silently dropped **~64% of the entire AOI** (anywhere ETH's canopy model had low confidence —
common over urban areas, bare soil, and open water) as NoData, invisible in histograms unless
you compare the classified pixel count against the raster's total pixel count.

Fix: warp with `-srcnodata 255` (so invalid source pixels are still correctly excluded from
the average) but never tag the output itself as NoData. Explicitly zero-fill leftovers and
strip the NoData flag before the file reaches `gdal_calc.py`.

## Known caveats (carried over from the PDF, §9)

- **RSPO's 25° threshold is a 25ha block-average**; per-pixel 30m thresholding is stricter
  (less generous) than the regulatory intent. A focal-mean smoothing pass before thresholding
  would better match it — not yet implemented here.
- **Bare-earth correction is modelled, not measured** — residual canopy-model error propagates
  into slope. Validate against reference lidar before trusting steep-edge pixels.
- **Water exclusion only removes water** — it does not exclude cities/roads/agriculture, which
  will still register as trivially "suitable" (flat, low elevation) since the base DSM includes
  building rooftops and the ETH canopy correction only removes tree height, not structures. A
  land-cover mask would be the next layer to add for a real risk product, per the PDF's own
  framing ("meant to be combined with land-cover... layers").
- **Equatorial slope scale** (`-s 111120` in `gdaldem slope`) is accurate to <0.5% at Aceh's
  latitude (1.8–6.1°N) but would need a cos(latitude) correction closer to the belt's 30°N/S
  edges.
- **AOI is a rectangular bounding box**, not Aceh's actual administrative polygon — the output
  extends slightly beyond the real province boundary (harmless now that water is masked, since
  excess ocean reads as unsuitable rather than falsely suitable).

## Viewing the result

Open `aceh_test/suitable_cog.tif` in QGIS, style as Singleband Pseudocolor / Paletted with
0→red, 1→green. The Bukit Barisan mountain range running down Aceh's spine should render red
(unsuitable); flat coastal/valley terrain should render green.
