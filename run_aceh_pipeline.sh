#!/bin/bash
# RI-2-9 Palm Oil Suitability — Aceh test pipeline
# Run with: bash run_aceh_pipeline.sh
# (bash, not zsh — plain bash splits unquoted "$TE" into 4 args automatically;
#  zsh does not, and needs ${=TE} instead. Running this file with bash sidesteps that.)
set -e

WORKDIR="$HOME/Downloads/Suitability Maps/aceh_test"
mkdir -p "$WORKDIR"/{glo30,eth,wbm}
cd "$WORKDIR"

# xmin ymin xmax ymax, EPSG:4326 — rectangular box around Aceh incl. Simeulue & Sabang
TE="94.9 1.8 98.3 6.1"
# 1 arc-second (~30m) in degrees — native GLO-30 resolution
RES=0.0002777778

echo ">>> Step 1/10: fetch Copernicus GLO-30 DEM tiles (AWS Open Data, no auth)"
for lat in 01 02 03 04 05 06; do
  for lon in 094 095 096 097 098; do
    t="Copernicus_DSM_COG_10_N${lat}_00_E${lon}_00_DEM"
    if [ -f "glo30/${t}.tif" ]; then continue; fi
    aws s3 cp --no-sign-request --only-show-errors \
      "s3://copernicus-dem-30m/${t}/${t}.tif" "glo30/${t}.tif" \
      || echo "  no tile N${lat} E${lon} (open ocean) - skipping"
  done
done

echo ">>> Step 2/10: fetch matching Water Body Mask tiles (same bucket, AUXFILES/)"
for f in glo30/*.tif; do
  base=$(basename "$f" _DEM.tif)
  wbm_file="${base}_WBM.tif"
  if [ -f "wbm/${wbm_file}" ]; then continue; fi
  aws s3 cp --no-sign-request --only-show-errors \
    "s3://copernicus-dem-30m/${base}_DEM/AUXFILES/${wbm_file}" \
    "wbm/${wbm_file}"
done

echo ">>> Step 3/10: fetch ETH Global Canopy Height 2020 tiles (libdrive.ethz.ch, 3deg grid)"
for lat in N00 N03 N06; do
  for lon in E093 E096; do
    f="ETH_GlobalCanopyHeight_10m_2020_${lat}${lon}_Map.tif"
    if [ -f "eth/${f}" ]; then continue; fi
    curl -sL -o "eth/${f}" \
      "https://libdrive.ethz.ch/index.php/s/cO8or7iOe5dT2Rt/download?path=%2F3deg_cogs&files=${f}"
  done
done

echo ">>> Step 4/10: mosaic each dataset into virtual rasters (no disk copies)"
gdalbuildvrt -overwrite dem.vrt glo30/*.tif
gdalbuildvrt -overwrite wbm.vrt wbm/*.tif
gdalbuildvrt -overwrite canopy.vrt eth/*.tif

echo ">>> Step 5/10: clip DEM to the Aceh AOI on the native 30m grid"
gdalwarp -t_srs EPSG:4326 -te $TE -tr $RES $RES -tap \
  dem.vrt dem_aoi.tif -overwrite

echo ">>> Step 6/10: align canopy height onto the same grid (10m->30m, mean height)"
# Source nodata is 255 (real canopy values run 0-64). We exclude 255 from the
# average via -srcnodata, but deliberately do NOT set -dstnodata to anything -
# tagging the *output* as nodata=255 previously caused gdal_calc.py to silently
# drop ~64% of the AOI downstream (it masks output to nodata if ANY input band
# equals ITS OWN declared nodata value, overriding whatever --calc computes).
gdalwarp -t_srs EPSG:4326 -te $TE -tr $RES $RES -tap \
  -r average -srcnodata 255 -wo INIT_DEST=0 \
  canopy.vrt canopy_aligned_raw.tif -multi -wo NUM_THREADS=ALL_CPUS -overwrite

# Explicit belt-and-suspenders cleanup: zero-fill any stray NaN/near-255 value
# left over from partial-coverage averaging at tile edges, and strip any
# nodata tag so nothing downstream can silently mask on it again.
gdal_calc.py -A canopy_aligned_raw.tif \
  --calc="numpy.where((numpy.isnan(A)) | (A>250), 0, A)" \
  --outfile=canopy_aligned.tif --type=Float32 --NoDataValue=none --overwrite

echo ">>> Step 7/10: bare-earth = DSM - canopy (clamped so canopy can't exceed DSM)"
gdal_calc.py -A dem_aoi.tif -B canopy_aligned.tif \
  --calc="A-numpy.minimum(B,A)" --outfile=bare.tif --type=Float32 \
  --co COMPRESS=DEFLATE --overwrite

echo ">>> Step 8/10: light 3x3 mean smoothing on bare-earth before slope"
# PDF sec.5 "smooth lightly": canopy model carries its own error, so the
# subtracted surface has spurious micro-relief. A 3x3 box-mean suppresses it
# without flattening real terrain. No GDAL CLI utility does neighborhood
# ops directly (gdal_calc.py is pixel-wise only), so this is a small inline
# numpy pass - reads the full array into memory, fine at province scale
# (~750MB here); would need windowed/chunked processing at global-belt scale.
python3 <<'PYEOF'
from osgeo import gdal
import numpy as np

ds = gdal.Open("bare.tif")
arr = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)

padded = np.pad(arr, 1, mode="edge")
smooth = np.zeros_like(arr)
for dy in range(3):
    for dx in range(3):
        smooth += padded[dy:dy + arr.shape[0], dx:dx + arr.shape[1]]
smooth /= 9.0

driver = gdal.GetDriverByName("GTiff")
out = driver.Create("bare_smooth.tif", ds.RasterXSize, ds.RasterYSize, 1,
                     gdal.GDT_Float32, options=["COMPRESS=DEFLATE"])
out.SetGeoTransform(ds.GetGeoTransform())
out.SetProjection(ds.GetProjection())
out.GetRasterBand(1).WriteArray(smooth)
out.FlushCache()
PYEOF

echo ">>> Step 9/10: slope (Horn's method, degrees) + terrain-only threshold"
# -s 111120 assumes equatorial pixel spacing; fine at Aceh's 1.8-6.1N latitude
# (<0.5% bias), but would need a cos(lat) correction near 30N/S.
# Both slope and the elevation check use the smoothed surface (matches B's
# GEE implementation, which thresholds bareEarthSmooth for both).
gdaldem slope bare_smooth.tif slope_deg.tif -alg Horn -s 111120 -compute_edges

gdal_calc.py -A slope_deg.tif -B bare_smooth.tif \
  --calc="(A<=25)*(B<=1000)" --outfile=terrain_suitable.tif --type=Byte --overwrite

echo ">>> Step 10/10: exclude water, then finalize"
gdalwarp -t_srs EPSG:4326 -te $TE -tr $RES $RES -tap -r near \
  wbm.vrt wbm_aoi.tif -overwrite

# Copernicus WBM classes: 0=no water(land) 1=ocean 2=lake 3=river
gdal_calc.py -A wbm_aoi.tif --calc="(A==0)" --outfile=land_mask.tif --type=Byte --overwrite

gdal_calc.py -A terrain_suitable.tif -B land_mask.tif \
  --calc="A*B" --outfile=suitable.tif --type=Byte --overwrite

gdal_translate suitable.tif suitable_cog.tif -of COG -co COMPRESS=DEFLATE

echo
echo ">>> DONE. Final output: $WORKDIR/suitable_cog.tif"
gdalinfo -hist suitable_cog.tif | tail -5
