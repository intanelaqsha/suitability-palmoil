# Suitability → H3 export (technical reference)

For the step-by-step setup and run instructions, see the main
[`README.md`](../README.md#part-2-convert-to-h3-resolution-11) one level up. This file covers
what's in this folder and why.

## Scripts: use `polygon_to_h3.py`

Two scripts here solve the same problem two different ways:

- **`suitability_to_h3.py`** — is the original approach. Walks every individual raster pixel (H3 lookup + boundary
  polygon intersection). 
- **`polygon_to_h3.py`** — vectorizes the raster's suitable area into polygons first
  (`gdal_polygonize`), then fills each with H3 cells (`h3.polygon_to_cells`) instead of
  walking pixels.

## `lib/rastertoh3.py`

Vendored verbatim from `radd-main/lib/rastertoh3.py` — fully generic, no RADD-specific logic,
used as-is by `suitability_to_h3.py`. `polygon_to_h3.py` doesn't use this module at all (it
doesn't need per-pixel boundary-overlap math).
