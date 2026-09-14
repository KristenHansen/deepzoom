# US Deep Zoom Map

An interactive web map of the US that swaps spatial layers as you zoom in —
**county → hex3 → hex4 → hex5 → hex6 → hex7 → hex8** — with **census tract**
and **ZIP5 (ZCTA)** available as pinned layers, all drawn over a basemap
that switches from OpenStreetMap to satellite imagery as you zoom in,
colored as a choropleth by exact **2020 Census population**.

## Quick start

```bash
git clone <this-repo-url>
cd usmap
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python data/build_data.py    # one-time: builds data/usmap.duckdb
                              # ~20-30 min, downloads ~2GB of public Census
                              # files (cached in data/raw/ so re-runs are fast)

uvicorn main:app --reload --port 8420 --app-dir app
```

Open http://127.0.0.1:8420. Everything downloaded is public and free — no
API key, account, or credit card needed anywhere in this pipeline.

## How it works

- **Backend**: FastAPI (`app/main.py`) + DuckDB with the `spatial` extension.
  County/tract/ZCTA geometries are precomputed once from real Census TIGER
  boundary files and queried by viewport bounding box. Each worker thread
  gets its own read-only DuckDB connection (a single shared connection
  isn't safe under FastAPI's threaded request handling).
- **Hex layers are generated on the fly**, not precomputed. Nationwide hex8
  alone would be ~13 million polygons — instead, the server uses H3's
  `polygon_to_cells` to generate only the cells inside your current map
  view, then colors them from a small precomputed lookup table
  (`hex_values`), built once from ~5.8 million populated census blocks.
  This is what makes zooming to hex7/hex8 anywhere in the country
  practical.
- **Frontend**: a single Leaflet page (`static/index.html`). On every
  pan/zoom it refetches the active layer for the new viewport. A dropdown
  lets you pin any layer manually instead of the automatic zoom cascade.
  The basemap switches automatically too: OpenStreetMap street tiles until
  zoom 11, then Esri World Imagery satellite tiles from zoom 11 on.

## Data sources (all public, no API key anywhere)

- **Boundaries** (county, tract, ZIP5): [Census TIGER/Line cartographic
  boundary files](https://www.census.gov/geographies/mapping-files/time-series/geo/carto-boundary-file.html)
  (2023 vintage; ZCTA is 2020, the most recent decennial vintage available).
  ZCTA is the standard proxy for ZIP5 — the USPS does not publish official
  ZIP code polygons.
- **Population**: the public [2020 Census Redistricting Data (P.L. 94-171)
  geographic header files](https://www.census.gov/programs-surveys/decennial-census/about/rdo/summary-files.html),
  downloaded directly per state (`data/pl94171.py`) — plain file downloads,
  not the api.census.gov gateway (which now requires a free key for
  everything, even requests that used to work anonymously). These files
  carry exact total population (`POP100`) for every 2020 Census geography,
  including individual blocks, so:
  - county/tract population comes straight from the matching summary-level
    records, with a point-in-polygon spatial fallback (`data/build_data.py:
    fix_zero_population_states`) for any state where the boundary file's
    FIPS codes don't line up with the 2020 population codes — this actually
    happens for Connecticut, which the 2023 TIGER files split into 9
    "planning regions" with new codes, while the 2020 population data still
    uses the old 8-county codes.
  - ZCTA population is built by summing block population using the
    official [block→ZCTA relationship
    file](https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_tabblock20_natl.txt)
    (blocks split ZCTAs cleanly — no fractional overlap, since ZCTAs are
    built by aggregating whole blocks).
  - hex population is built the way H3 is meant to be used: no geometry
    needed, just bucket each census block's population by the H3 cell its
    internal point falls in, at every resolution.
- Hex3–hex8: generated with the [`h3`](https://h3geo.org/) library, not
  downloaded.

## Project structure

```
usmap/
├── app/
│   └── main.py           FastAPI server (serves /api/layer/{layer} and the static frontend)
├── static/
│   └── index.html        Leaflet frontend — single self-contained page
├── data/
│   ├── build_data.py     Run this once to build usmap.duckdb
│   ├── pl94171.py        2020 Census population fetch/parse (no API key)
│   ├── raw/              Cached downloads (gitignored, rebuildable)
│   └── usmap.duckdb      Built database (gitignored, ~600MB, rebuildable)
└── requirements.txt
```

## Known limitations / things to improve for production use

- **Feature caps.** Each request caps at 6,000 table features / 8,000 hex
  cells to keep payloads fast; the UI shows a warning and asks you to zoom
  in further if a view is truncated. This should never trigger in normal
  use since finer layers only appear once you've zoomed into a smaller
  area.
- **Territories outside the redistricting program** (American Samoa, Guam,
  Northern Mariana Islands, US Virgin Islands) don't have P.L. 94-171 files
  and will show 0 population; Puerto Rico and the 50 states + DC are fully
  covered.
- **Satellite threshold** (`SATELLITE_MIN_ZOOM` in `static/index.html`,
  currently zoom 11) is a simple fixed cutover — tune to taste.
- **Auto zoom→layer mapping** is a simple fixed table in `index.html`
  (`autoLayerForZoom`) — tune the zoom breakpoints to taste.
- Population is exactly as measured on **April 1, 2020** (the last
  decennial census) — it does not reflect growth/decline since then. For
  current-year estimates you'd need the ACS API (requires a free key from
  https://api.census.gov/data/key_signup.html) instead.
- The built database isn't checked into git (it's ~600MB and fully
  reproducible) — everyone who clones this runs `python data/build_data.py`
  once. If your team would rather share the built file directly instead of
  everyone re-running the build, upload `data/usmap.duckdb` +
  `data/metadata.json` somewhere with file storage (they're gitignored on
  purpose, not because they're sensitive).
