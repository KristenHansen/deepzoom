"""
FastAPI backend for the deep-zoom US map.

Serves boundary/hex features as GeoJSON, filtered to the current map
viewport, for whichever layer the frontend requests. County/tract/zcta
geometries come from a precomputed DuckDB table (bbox-indexed). Hex layers
(hex3..hex8) are generated on the fly for the requested viewport using H3's
polygon-to-cells coverage, then colored using a small precomputed
(resolution, h3_index) -> value lookup table built from tract centroids.
Generating hex cells on demand (instead of storing all ~13M hex8 cells
nationwide) is what makes deep zoom into hex8 practical.
"""
import json
import threading
from pathlib import Path

import duckdb
import h3
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).parent
DATA_DIR = HERE.parent / "data"
DB_PATH = DATA_DIR / "usmap.duckdb"
META_PATH = DATA_DIR / "metadata.json"
STATIC_DIR = HERE.parent / "static"

TABLE_LAYERS = {"county", "tract", "zcta"}
HEX_LAYERS = {f"hex{r}" for r in range(3, 9)}
ALL_LAYERS = TABLE_LAYERS | HEX_LAYERS

MAX_TABLE_FEATURES = 6000
MAX_HEX_CELLS = 8000

app = FastAPI(title="US Deep Zoom Map")

# FastAPI runs sync route handlers in a thread pool, and a single DuckDB
# connection isn't safe to share across concurrent threads (it can crash the
# process, not just raise). Each worker thread gets its own read-only
# connection to the same file instead.
_local = threading.local()
_metadata = {"metric": "population", "metric_label": "Population"}


def get_con():
    if not hasattr(_local, "con"):
        if not DB_PATH.exists():
            raise RuntimeError("Database not found. Run `python data/build_data.py` first.")
        _local.con = duckdb.connect(str(DB_PATH), read_only=True)
        _local.con.load_extension("spatial")
    return _local.con


if META_PATH.exists():
    _metadata = json.loads(META_PATH.read_text())


@app.get("/api/metadata")
def metadata():
    return _metadata


def parse_bbox(bbox: str):
    try:
        minx, miny, maxx, maxy = [float(x) for x in bbox.split(",")]
    except Exception:
        raise HTTPException(400, "bbox must be 'minLon,minLat,maxLon,maxLat'")
    return minx, miny, maxx, maxy


def query_table_layer(layer: str, bbox: tuple):
    minx, miny, maxx, maxy = bbox
    con = get_con()
    total = con.execute(
        f"""SELECT count(*) FROM {layer}
            WHERE xmax >= ? AND xmin <= ? AND ymax >= ? AND ymin <= ?""",
        [minx, maxx, miny, maxy],
    ).fetchone()[0]

    rows = con.execute(
        f"""SELECT geoid, name, value, ST_AsGeoJSON(geom) AS gj
            FROM {layer}
            WHERE xmax >= ? AND xmin <= ? AND ymax >= ? AND ymin <= ?
            LIMIT {MAX_TABLE_FEATURES}""",
        [minx, maxx, miny, maxy],
    ).fetchall()

    features = [
        {
            "type": "Feature",
            "geometry": json.loads(gj),
            "properties": {"id": geoid, "name": name, "value": value},
        }
        for geoid, name, value, gj in rows
    ]
    truncated = total > len(features)
    return features, truncated, total


def query_hex_layer(layer: str, bbox: tuple):
    res = int(layer.replace("hex", ""))
    minx, miny, maxx, maxy = bbox
    # h3 wants (lat, lng) pairs, counter-clockwise, for the boundary polygon
    ring = [(miny, minx), (miny, maxx), (maxy, maxx), (maxy, minx)]
    shape = h3.LatLngPoly(ring)
    cells = h3.polygon_to_cells(shape, res)

    truncated = len(cells) > MAX_HEX_CELLS
    if truncated:
        cells = cells[:MAX_HEX_CELLS]

    con = get_con()
    values = {}
    if cells:
        placeholders = ",".join("?" for _ in cells)
        rows = con.execute(
            f"""SELECT h3_index, value FROM hex_values
                WHERE resolution = ? AND h3_index IN ({placeholders})""",
            [res, *cells],
        ).fetchall()
        values = dict(rows)

    features = []
    for h in cells:
        boundary = h3.cell_to_boundary(h)  # list of (lat, lng)
        ring_coords = [[lng, lat] for lat, lng in boundary]
        ring_coords.append(ring_coords[0])
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring_coords]},
            "properties": {
                "id": h,
                "name": f"{layer} {h}",
                "value": values.get(h, 0.0),
            },
        })
    return features, truncated, len(cells)


@app.get("/api/layer/{layer}")
def get_layer(
    layer: str,
    bbox: str = Query(..., description="minLon,minLat,maxLon,maxLat"),
):
    if layer not in ALL_LAYERS:
        raise HTTPException(404, f"unknown layer '{layer}'")
    bb = parse_bbox(bbox)

    if layer in TABLE_LAYERS:
        features, truncated, total = query_table_layer(layer, bb)
    else:
        features, truncated, total = query_hex_layer(layer, bb)

    return JSONResponse({
        "type": "FeatureCollection",
        "features": features,
        "truncated": truncated,
        "total": total,
    })


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
