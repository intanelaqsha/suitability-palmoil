import math
import functools
import numpy as np
import rasterio
from rasterio.windows import Window
from rasterio.transform import xy
from rasterio.enums import Resampling
import pyarrow as pa, pyarrow.parquet as pq
from shapely.geometry import Polygon
from pyproj import CRS, Transformer
from tqdm import tqdm
import h3  # v3 or v4 works

EARTH_RADIUS_M = 6378137.0  # used only for fast core-pixel area

# --- H3 version-safe helpers --------------------------------------------------

def h3_boundary_lonlat(h):
    """
    Return hex boundary as [(lon, lat), ...], version-agnostic.
    v4: h3.cell_to_boundary(h) -> [[lat, lon], ...]
    v3: h3.h3_to_geo_boundary(h, geo_json=True) -> [[lat, lon], ...]
    """
    if hasattr(h3, "cell_to_boundary"):      # v4+
        latlons = h3.cell_to_boundary(h)
    else:                                    # v3
        latlons = h3.h3_to_geo_boundary(h, geo_json=True)
    return [(lon, lat) for lat, lon in latlons]

def h3_latlng_to_cell(lat, lon, res):
    if hasattr(h3, "latlng_to_cell"):        # v4+
        return h3.latlng_to_cell(lat, lon, res)
    else:                                    # v3
        return h3.geo_to_h3(lat, lon, res)

# --- Utilities ----------------------------------------------------------------

def _geodesic_row_area_m2(lat_deg, dy_deg, dx_deg, R=EARTH_RADIUS_M):
    """Fast spherical pixel-area for a pixel centered at lat_deg."""
    lat = math.radians(lat_deg); dy = math.radians(dy_deg); dx = math.radians(dx_deg)
    return (R**2) * (math.sin(lat + dy/2.0) - math.sin(lat - dy/2.0)) * dx

def _pixel_corners_lonlat(transform, r, c):
    """4 pixel corners (lon, lat) in UL, UR, LR, LL order."""
    rc = np.array([[r-0.5, c-0.5], [r-0.5, c+0.5], [r+0.5, c+0.5], [r+0.5, c-0.5]])
    xs, ys = xy(transform, rc[:,0], rc[:,1], offset="center")
    return list(zip(xs, ys))

@functools.lru_cache(maxsize=100000)
def _laea_transformer_for_hex(h):
    """Lon/lat -> local equal-area transformer centered at hex centroid (cached)."""
    ring = h3_boundary_lonlat(h)
    cx = sum(p[0] for p in ring) / len(ring)
    cy = sum(p[1] for p in ring) / len(ring)
    laea = CRS.from_proj4(f"+proj=laea +lat_0={cy} +lon_0={cx} +datum=WGS84 +units=m +no_defs")
    wgs84 = CRS.from_epsg(4326)
    return Transformer.from_crs(wgs84, laea, always_xy=True)

@functools.lru_cache(maxsize=100000)
def _hex_polygon_projected(h):
    """Return (hex polygon in meters, area_m2) in local LAEA (cached)."""
    ring_ll = h3_boundary_lonlat(h)
    fwd = _laea_transformer_for_hex(h)
    xs, ys = fwd.transform([p[0] for p in ring_ll], [p[1] for p in ring_ll])
    poly = Polygon(zip(xs, ys))
    return poly, float(poly.area)

def _polygon_project(lonlat_coords, transformer):
    xs, ys = transformer.transform([p[0] for p in lonlat_coords], [p[1] for p in lonlat_coords])
    return Polygon(zip(xs, ys))

# --- Main extractor with progress bar -----------------------------------------

def extract_nonzero_with_exact_boundary_overlap_to_parquet(
    tif_path: str,
    parquet_path: str,
    res_h3: int,
    band: int = 1,
    nonzero_value: int = 1,
    compress: str = "zstd",
    add_source: bool = True,
    show_progress: bool = True,
):
    """
    Streams a GeoTIFF, emits only non-zero pixels, and writes:
      lon, lat, h3, value, area_m2, is_boundary [, source]
    - Core pixels: area_m2 = fast geodesic per-row area
    - Boundary pixels: area_m2 = exact area of (pixel square ∩ hexagon) in a local equal-area CRS

    Returns (rows_total, windows_processed).
    """
    rows_total = 0
    windows_processed = 0

    with rasterio.Env(GDAL_CACHEMAX=512):
        with rasterio.open(tif_path) as ds:
            if not (ds.crs and ds.crs.is_geographic):
                raise ValueError("Expecting EPSG:4326 (geographic) raster.")
            t = ds.transform
            height, width = ds.height, ds.width
            dx_deg, dy_deg = t.a, -t.e
            if dx_deg <= 0 or dy_deg <= 0:
                raise ValueError("Unexpected transform; expected north-up with positive pixel sizes.")

            writer = None

            # Determine total windows for progress bar
            if ds.is_tiled:
                # blocks are (block_h, block_w) for this band
                bh, bw = ds.block_shapes[band - 1]
                nb_y = (height + bh - 1) // bh
                nb_x = (width  + bw - 1) // bw
                total_windows = nb_y * nb_x
                window_iter = ds.block_windows(band)
                use_blocks = True
            else:
                # row-chunk fallback; 4096 rows per chunk
                chunk = 4096
                total_windows = (height + chunk - 1) // chunk
                def _row_chunks():
                    for row0 in range(0, height, chunk):
                        h = min(chunk, height - row0)
                        yield (None, Window(0, row0, width, h))
                window_iter = _row_chunks()
                use_blocks = False

            prog = tqdm(total=total_windows, unit="win", disable=not show_progress, desc="Processing")
            prog.set_postfix(rows=rows_total)

            def write_batch(cols: dict):
                nonlocal writer, rows_total
                table = pa.table(cols)
                if writer is None:
                    writer = pq.ParquetWriter(parquet_path, table.schema, compression=compress)
                writer.write_table(table)
                rows_total += table.num_rows

            def process_window(win: Window):
                nonlocal windows_processed
                data = ds.read(band, window=win, resampling=Resampling.nearest)
                nz = (data == nonzero_value)
                if not np.any(nz):
                    windows_processed += 1
                    prog.update(1); prog.set_postfix(rows=rows_total)
                    return

                rr, cc = np.nonzero(nz)
                rr_abs = rr + win.row_off
                cc_abs = cc + win.col_off

                # pixel centers (lon/lat) – these are written to parquet
                xs, ys = xy(t, rr_abs, cc_abs, offset="center")
                lons = np.asarray(xs, np.float64)
                lats = np.asarray(ys, np.float64)

                # center H3 for each pixel
                h3_center = np.array([h3_latlng_to_cell(lat, lon, res_h3)
                                    for lat, lon in zip(lats, lons)], dtype=object)

                # boundary flag (if any corner maps to a different H3 than center)
                is_boundary = np.zeros(len(rr_abs), dtype=bool)
                # we will fill exact LAEA areas here
                area_exact = np.zeros(len(rr_abs), dtype=np.float64)

                for i in range(len(rr_abs)):
                    r_i = int(rr_abs[i]); c_i = int(cc_abs[i])
                    # pixel corners in lon/lat using Rasterio's corner offsets
                    ulx, uly = xy(t, r_i, c_i, offset='ul')
                    urx, ury = xy(t, r_i, c_i, offset='ur')
                    lrx, lry = xy(t, r_i, c_i, offset='lr')
                    llx, lly = xy(t, r_i, c_i, offset='ll')
                    px_ring_ll = [(ulx, uly), (urx, ury), (lrx, lry), (llx, lly)]
                    # boundary test
                    corner_h3 = [h3_latlng_to_cell(lat_, lon_, res_h3)
                                for (lon_, lat_) in px_ring_ll]
                    boundary = any(ch != h3_center[i] for ch in corner_h3)
                    is_boundary[i] = boundary

                    # local equal-area transformer + hex polygon (both cached per hex)
                    hcell = h3_center[i]
                    fwd = _laea_transformer_for_hex(hcell)
                    hex_poly_proj, _ = _hex_polygon_projected(hcell)

                    # project pixel polygon
                    px_poly_proj = _polygon_project(px_ring_ll, fwd)

                    if boundary:
                        inter = px_poly_proj.intersection(hex_poly_proj)
                        area_exact[i] = float(inter.area) if not inter.is_empty else 0.0
                    else:
                        # CORE pixel: full pixel area in LAEA
                        area_exact[i] = float(px_poly_proj.area)

                cols = {
                    "lon": pa.array(lons),
                    "lat": pa.array(lats),
                    "h3": pa.array(h3_center),
                    "value": pa.array(data[nz].astype(np.uint8)),
                    "area_m2": pa.array(area_exact),
                    "is_boundary": pa.array(is_boundary),
                }
                if add_source:
                    cols["source"] = pa.array([tif_path] * len(lons))
                write_batch(cols)

                windows_processed += 1
                prog.update(1); prog.set_postfix(rows=rows_total)

            # Iterate windows with progress
            for _, w in window_iter:
                process_window(w)

            if writer is not None:
                writer.close()
            prog.close()

    return rows_total, windows_processed
