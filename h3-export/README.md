# Suitability → H3 export (technical reference)

For the step-by-step setup and run instructions, see the main
[`README.md`](../README.md#part-2-convert-to-h3-resolution-11) one level up. This file covers
what's in this folder and why.

## Scripts: use `polygon_to_h3.py`, not `suitability_to_h3.py`

Two scripts here solve the same problem two different ways:

- **`suitability_to_h3.py`** — the original approach, adapted directly from
  `radd-main/radd-to-h3.py`. Walks every individual raster pixel (H3 lookup + boundary
  polygon intersection). RADD's alerts are sparse (a small % of any region), so this is fine
  there. Our suitability layer is ~30% "suitable" across the whole raster — benchmarked at
  ~1,000 rows/sec on Aceh, which extrapolates to 12+ hours. **Don't use this one.** Kept only
  so the before/after is visible.
- **`polygon_to_h3.py`** — vectorizes the raster's suitable area into polygons first
  (`gdal_polygonize`, ~5s), then fills each with H3 cells (`h3.polygon_to_cells`) instead of
  walking pixels. Confirmed working, 46,699 polygons → 21,133,020 resolution-11 cells in
  4h23m. **Use this one** — but see the caveat below before assuming it scales further.

## Why `polygon_to_h3.py` is still slow, and what actually fixes it

H3's `polygon_to_cells` tests every candidate hexagon individually against the polygon
boundary (point-in-polygon) — confirmed via live process sampling (`sample <pid>`, showing it
parked inside `polygonToCells → pointInsidePolygon → pointInsideGeoLoop`). There's no
shortcut for hexagons deep in a polygon's solid interior — a huge, simply-connected blob
(a mountain range, a coastal plain) costs the same per-cell as one near a complex edge. Two
separate polygons in the Aceh run each took over an hour by themselves this way.

The real fix (not yet built): adaptive refinement using H3's cell hierarchy — start coarse
(e.g. resolution 6), test each cell as fully-inside/fully-outside/straddles-a-boundary against
the polygons, and only recurse into children for cells that straddle. Fully-inside cells get
their resolution-11 descendants generated via `h3.cell_to_children` / `h3.uncompact_cells` —
pure index arithmetic, no geometry, effectively free even for millions of cells. This still
produces a uniform resolution-11 list (Kris's requirement unchanged), it just never runs an
expensive per-cell test on solid interior area regardless of polygon size or complexity.

## `lib/rastertoh3.py`

Vendored verbatim from `radd-main/lib/rastertoh3.py` — fully generic, no RADD-specific logic,
used as-is by `suitability_to_h3.py`. `polygon_to_h3.py` doesn't use this module at all (it
doesn't need per-pixel boundary-overlap math).

## Visualizing a sample locally (QGIS)

The full 21M-row output is better viewed in kepler.gl (native H3 rendering at scale — see main
README). For a smaller local/offline sample in QGIS, convert cells to boundary polygons and
export GeoJSON:

```bash
source .venv/bin/activate
python3 -c "
import h3, json
import pyarrow.parquet as pq

t = pq.read_table('output/suitable_cog_h3.parquet')
ids = t['h3'].to_pylist()[:50000]  # sample - full 21M would be heavy in QGIS

features = []
for h in ids:
    boundary = h3.cell_to_boundary(h)  # [(lat,lng), ...]
    coords = [[lng, lat] for lat, lng in boundary] + [[boundary[0][1], boundary[0][0]]]
    features.append({'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': [coords]}, 'properties': {'h3': h}})

with open('output/suitable_sample.geojson', 'w') as f:
    json.dump({'type': 'FeatureCollection', 'features': features}, f)
print('wrote', len(features), 'polygons')
"
```

Then drag `output/suitable_sample.geojson` into QGIS like any other vector layer.
