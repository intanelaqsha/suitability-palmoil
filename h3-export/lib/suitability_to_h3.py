#!/usr/bin/env python3
"""
Convert a binary suitability GeoTIFF (RI-2-9) to H3 hexagons, saved as Parquet.

Adapted from radd-main/radd-to-h3.py, which does the same core raster->H3
conversion for RADD deforestation alerts. The conversion logic itself
(lib/rastertoh3.py) is generic and reused as-is; only the CLI, output
schema, and DuckDB aggregation are specific to this project (no month/region
concept here - this is a static layer, not a monthly alert feed).
"""

import argparse
import os
import sys
import time
from pathlib import Path

os.environ['ARROW_PRE_1_0_METADATA_VERSION'] = '1'
os.environ['PYARROW_IGNORE_TIMEZONE'] = '1'

import duckdb
from lib.rastertoh3 import extract_nonzero_with_exact_boundary_overlap_to_parquet


def main():
    parser = argparse.ArgumentParser(
        description="Convert a suitability GeoTIFF to H3 hexagons and save as Parquet",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("input_tif", help="Path to the suitability COG (e.g. suitable_cog.tif)")
    parser.add_argument("--output", "-o", default=None,
                         help="Output Parquet path (default: <input_tif stem>_h3.parquet next to this script)")
    parser.add_argument("--h3-resolution", "-r", type=int, default=11, help="H3 resolution level")
    parser.add_argument("--band", type=int, default=1, help="Raster band to process")
    parser.add_argument("--nonzero-value", type=int, default=1, help="Value to treat as suitable")
    parser.add_argument("--compression", default="zstd",
                         choices=["zstd", "snappy", "gzip", "lz4", "brotli"],
                         help="Parquet compression algorithm")
    parser.add_argument("--no-source", action="store_true", help="Don't add source path column")
    parser.add_argument("--no-progress", action="store_true", help="Don't show progress bar")
    args = parser.parse_args()

    input_path = Path(args.input_tif)
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output) if args.output else Path(f"./output/{input_path.stem}_h3.parquet")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    intermediate_path = output_path.with_suffix("")
    intermediate_path = Path(str(intermediate_path) + "-intermediate.parquet")

    print(f"Input: {input_path}")
    print(f"Intermediate: {intermediate_path}")
    print(f"Output: {output_path}")
    print(f"H3 resolution: {args.h3_resolution}")

    try:
        import psutil
        mem = psutil.virtual_memory()
        print(f"Available memory: {mem.available / (1024**3):.1f} GB ({mem.percent:.1f}% used)")
    except ImportError:
        pass

    t0 = time.time()
    rows, windows = extract_nonzero_with_exact_boundary_overlap_to_parquet(
        str(input_path),
        str(intermediate_path),
        res_h3=args.h3_resolution,
        band=args.band,
        nonzero_value=args.nonzero_value,
        compress=args.compression,
        add_source=not args.no_source,
        show_progress=not args.no_progress,
    )
    t1 = time.time()
    print(f"Extracted {rows} pixel-rows from {windows} windows in {t1 - t0:.1f}s "
          f"({rows / max(t1 - t0, 1e-9):.0f} rows/sec)")

    con = duckdb.connect()
    sql = f"""
    INSTALL h3 from community; LOAD h3;

    CREATE TABLE suitability_h3 AS
    SELECT
      h3,
      SUM(area_m2) AS suitable_area_m2,
      COUNT(*)     AS pixel_count
    FROM read_parquet('{intermediate_path}')
    GROUP BY 1;

    COPY suitability_h3 TO '{output_path}' (FORMAT 'parquet');
    """
    con.execute(sql)
    n_hexes = con.execute("SELECT COUNT(*) FROM suitability_h3").fetchone()[0]
    con.close()
    t2 = time.time()
    print(f"Aggregated to {n_hexes} H3 cells in {t2 - t1:.1f}s")
    print(f"Total wall time: {t2 - t0:.1f}s")

    intermediate_path.unlink()
    print(f"Done. Final output: {output_path}")


if __name__ == "__main__":
    main()
