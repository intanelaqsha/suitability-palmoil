#!/usr/bin/env python3
"""
Convert a binary suitability GeoTIFF (RI-2-9) to H3 hexagons via vectorization,
saved as Parquet.

Replaces suitability_to_h3.py's per-pixel approach, which was built for RADD's
sparse alert pixels and doesn't scale to a ~30%-dense binary mask (benchmarked
at ~1000 rows/sec on Aceh alone -> 12+ hours for one small province).

This vectorizes the raster's suitable=1 regions into polygons (large,
contiguous blobs - mountains, coastal plains), then fills each polygon with
H3 cells directly (h3.polygon_to_cells), instead of walking every pixel.
Containment is hex-center-in-polygon (h3-py v4's only mode in this version -
no full/overlapping option), so this is an approximation at polygon edges,
not the exact-area-overlap of the RADD tool. Good enough for "is this hex
suitable"; revisit if boundary precision turns out to matter.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

import geopandas as gpd
import h3
import pyarrow as pa
import pyarrow.parquet as pq
from tqdm import tqdm


def polygon_to_h3_cells(geom, res):
    """Shapely Polygon/MultiPolygon -> set of H3 cell indices (center-contained)."""
    cells = set()
    polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    for poly in polys:
        outer = [(lat, lng) for lng, lat in poly.exterior.coords]
        holes = [[(lat, lng) for lng, lat in ring.coords] for ring in poly.interiors]
        shape = h3.LatLngPoly(outer, *holes)
        cells.update(h3.polygon_to_cells(shape, res))
    return cells


def main():
    parser = argparse.ArgumentParser(
        description="Convert a suitability GeoTIFF to H3 hexagons via polygonize + polyfill",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("input_tif", help="Path to the suitability COG (e.g. suitable_cog.tif)")
    parser.add_argument("--output", "-o", default=None,
                         help="Output Parquet path (default: output/<input stem>_h3.parquet)")
    parser.add_argument("--h3-resolution", "-r", type=int, default=11, help="H3 resolution level")
    parser.add_argument("--nonzero-value", type=int, default=1, help="Pixel value to treat as suitable")
    parser.add_argument("--keep-vector", action="store_true",
                         help="Keep the intermediate polygonized GeoPackage instead of deleting it")
    args = parser.parse_args()

    input_path = Path(args.input_tif)
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output) if args.output else Path(f"./output/{input_path.stem}_h3.parquet")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    vector_path = output_path.with_suffix("")
    vector_path = Path(str(vector_path) + "-polygons.gpkg")

    print(f"Input: {input_path}")
    print(f"Intermediate vector: {vector_path}")
    print(f"Output: {output_path}")
    print(f"H3 resolution: {args.h3_resolution}")

    t0 = time.time()
    print("Polygonizing...")
    subprocess.run(
        ["gdal_polygonize.py", "-q", str(input_path), "-f", "GPKG",
         str(vector_path), "suitable", "DN"],
        check=True,
    )
    t1 = time.time()
    print(f"Polygonized in {t1 - t0:.1f}s")

    gdf = gpd.read_file(vector_path, layer="suitable")
    gdf = gdf[gdf["DN"] == args.nonzero_value]
    print(f"{len(gdf)} suitable polygon features to fill")

    all_cells = set()
    for geom in tqdm(gdf.geometry, desc="Filling polygons with H3 cells"):
        all_cells.update(polygon_to_h3_cells(geom, args.h3_resolution))
    t2 = time.time()
    print(f"Filled {len(gdf)} polygons -> {len(all_cells)} unique H3 cells in {t2 - t1:.1f}s")

    table = pa.table({
        "h3": pa.array(sorted(all_cells)),
        "hex_area_m2": pa.array([h3.cell_area(h, unit="m^2") for h in sorted(all_cells)]),
    })
    pq.write_table(table, output_path, compression="zstd")
    t3 = time.time()

    print(f"Wrote {len(all_cells)} rows to {output_path} in {t3 - t2:.1f}s")
    print(f"Total wall time: {t3 - t0:.1f}s")

    if not args.keep_vector:
        vector_path.unlink()
    print("Done.")


if __name__ == "__main__":
    main()
