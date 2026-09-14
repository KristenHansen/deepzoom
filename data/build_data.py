"""
Builds usmap/data/usmap.duckdb from public US Census boundary + population files.

Layers stored:
  - county          (~3.2k features, national cartographic boundary file)
  - tract           (~85k features, per-state cartographic boundary files)
  - zcta            (~33k features, national ZCTA cartographic boundary file, proxy for ZIP5)
  - hex_values      (lookup table: resolution, h3_index -> value; hex polygons
                      themselves are generated on the fly by the API server so
                      we never have to materialize millions of hex8 rows)

Metric ("value" column): exact 2020 Census total population (POP100), pulled
directly from the public 2020 Census Redistricting Data (P.L. 94-171)
geographic header files -- no API key needed. See data/pl94171.py.

  - county/tract population comes straight from the 050/140 summary-level
    records.
  - ZCTA population is built by summing block-level (750) population using
    the official block->ZCTA relationship file (blocks split ZCTAs cleanly;
    no fractional overlap since ZCTAs are built by aggregating whole blocks).
  - hex population is built the same way H3 is meant to be used: no
    geometry needed, just bucket each block's population by the H3 cell its
    internal point falls in, at each resolution. Using individual blocks
    (~8.1M nationally) instead of census tract centroids makes hex7/hex8
    dramatically more accurate.

Usage:
    source .venv/bin/activate
    python data/build_data.py
"""
import json
import sys
from pathlib import Path

import duckdb
import geopandas as gpd
import h3
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
import pl94171  # noqa: E402

HERE = Path(__file__).parent
CACHE = HERE / "raw"
CACHE.mkdir(exist_ok=True)
DB_PATH = HERE / "usmap.duckdb"
META_PATH = HERE / "metadata.json"

GENZ_YEAR = 2023
ZCTA_YEAR = 2020  # ZCTA cartographic boundaries are only refreshed each decennial census

TIGER_BASE = f"https://www2.census.gov/geo/tiger/GENZ{GENZ_YEAR}/shp"
ZCTA_URL = f"https://www2.census.gov/geo/tiger/GENZ{ZCTA_YEAR}/shp/cb_{ZCTA_YEAR}_us_zcta520_500k.zip"

HEX_RESOLUTIONS = [3, 4, 5, 6, 7, 8]


def download(url: str) -> Path:
    fname = CACHE / url.split("/")[-1]
    if fname.exists() and fname.stat().st_size > 0:
        return fname
    print(f"  downloading {url}")
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    fname.write_bytes(r.content)
    return fname


def read_shapefile_zip(path: Path) -> gpd.GeoDataFrame:
    return gpd.read_file(f"zip://{path}")


def add_bbox(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    b = gdf.geometry.bounds
    gdf["xmin"], gdf["ymin"], gdf["xmax"], gdf["ymax"] = b["minx"], b["miny"], b["maxx"], b["maxy"]
    return gdf


def to_duckdb(con, table: str, gdf: gpd.GeoDataFrame):
    df = pd.DataFrame(gdf.drop(columns="geometry"))
    df["wkt"] = gdf.geometry.to_wkt()
    con.register("tmp_df", df)
    con.execute(f"""
        CREATE OR REPLACE TABLE {table} AS
        SELECT * EXCLUDE (wkt), ST_GeomFromText(wkt) AS geom
        FROM tmp_df
    """)
    con.unregister("tmp_df")
    print(f"  wrote table '{table}' ({len(df):,} rows)")


def build_pl94171_population(con, state_fips_list):
    """Streams every state's PL94-171 geo header (no API key needed) into
    three DuckDB tables: pl_county_pop, pl_tract_pop, pl_block. Also builds
    block_zcta from the official relationship file."""
    print("== 2020 Census population (P.L. 94-171 redistricting files) ==")
    pl94171.build_block_zcta_table(con, CACHE)

    con.execute("CREATE OR REPLACE TABLE pl_county_pop (geoid VARCHAR, pop BIGINT)")
    con.execute("CREATE OR REPLACE TABLE pl_tract_pop (geoid VARCHAR, pop BIGINT)")
    con.execute("CREATE OR REPLACE TABLE pl_block (block_geoid VARCHAR, pop BIGINT, lat DOUBLE, lon DOUBLE)")

    usable_fips = [f for f in state_fips_list if f in pl94171.STATE_FOLDERS]
    skipped = sorted(set(state_fips_list) - set(usable_fips))
    if skipped:
        print(f"  no PL94-171 data for territories {skipped} (not part of redistricting program); "
              f"those areas will show 0 population")

    for i, fips in enumerate(usable_fips):
        folder, _ = pl94171.STATE_FOLDERS[fips]
        print(f"  [{i+1}/{len(usable_fips)}] {folder} ({fips})")
        county_rows, tract_rows, block_rows = [], [], []
        for sumlev, geoid, state, county, tract, block, name, pop, lat, lon in pl94171.iter_state_geo_rows(fips, CACHE):
            if sumlev == "050":
                county_rows.append((state + county, pop))
            elif sumlev == "140":
                tract_rows.append((state + county + tract, pop))
            elif sumlev == "750" and lat is not None and pop > 0:
                block_rows.append((state + county + tract + block, pop, lat, lon))

        if county_rows:
            df = pd.DataFrame(county_rows, columns=["geoid", "pop"])
            con.register("tmp", df)
            con.execute("INSERT INTO pl_county_pop SELECT * FROM tmp")
            con.unregister("tmp")
        if tract_rows:
            df = pd.DataFrame(tract_rows, columns=["geoid", "pop"])
            con.register("tmp", df)
            con.execute("INSERT INTO pl_tract_pop SELECT * FROM tmp")
            con.unregister("tmp")
        if block_rows:
            df = pd.DataFrame(block_rows, columns=["block_geoid", "pop", "lat", "lon"])
            con.register("tmp", df)
            con.execute("INSERT INTO pl_block SELECT * FROM tmp")
            con.unregister("tmp")

    n_county, n_tract, n_block = (con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                                   for t in ("pl_county_pop", "pl_tract_pop", "pl_block"))
    print(f"  population rows: {n_county:,} counties, {n_tract:,} tracts, {n_block:,} blocks")

    con.execute("""
        CREATE OR REPLACE TABLE pl_zcta_pop AS
        SELECT bz.zcta AS geoid, SUM(b.pop) AS pop
        FROM pl_block b JOIN block_zcta bz ON b.block_geoid = bz.block_geoid
        GROUP BY bz.zcta
    """)
    n_zcta = con.execute("SELECT count(*) FROM pl_zcta_pop").fetchone()[0]
    print(f"  population rows: {n_zcta:,} ZCTAs")


def fix_zero_population_states(con, gdf):
    """Falls back to a spatial join (block internal points -> polygon) for
    any state where every county/tract came back with 0 population from the
    GEOID-based lookup.

    This happens for Connecticut: the 2023 TIGER cartographic boundary
    files switched CT from its 8 traditional counties to 9 "planning
    regions" with new FIPS-style codes, but the 2020 PL94-171 population
    data still uses the old county codes, so no GEOIDs match. Doing a
    point-in-polygon join instead of a GEOID match sidesteps the whole
    class of boundary-vintage mismatches (not just this one).
    """
    state_fips = gdf["geoid"].str[:2]
    zero_states = sorted(s for s, v in gdf.groupby(state_fips)["value"].sum().items() if v == 0)
    if not zero_states:
        return gdf

    print(f"  {len(zero_states)} state(s) had 0 population via GEOID match "
          f"(likely a boundary/vintage mismatch) -- falling back to a spatial join: {zero_states}")
    for sf in zero_states:
        mask = state_fips == sf
        subset = gdf.loc[mask, ["geoid", "geometry"]]
        if subset.empty:
            continue
        poly_df = pd.DataFrame({"geoid": subset["geoid"].values, "wkt": subset.geometry.to_wkt().values})
        con.register("tmp_poly", poly_df)
        con.execute("CREATE OR REPLACE TEMP TABLE tmp_poly_geom AS SELECT geoid, ST_GeomFromText(wkt) AS geom FROM tmp_poly")
        con.unregister("tmp_poly")
        result = con.execute(f"""
            SELECT p.geoid, SUM(b.pop) AS pop
            FROM tmp_poly_geom p
            JOIN pl_block b ON substr(b.block_geoid, 1, 2) = '{sf}'
            WHERE ST_Contains(p.geom, ST_Point(b.lon, b.lat))
            GROUP BY p.geoid
        """).fetchall()
        popmap = dict(result)
        fixed = gdf.loc[mask, "geoid"].map(popmap).fillna(0.0)
        gdf.loc[mask, "value"] = fixed.values
        print(f"    state {sf}: recovered {fixed.sum():,.0f} population across {len(fixed)} rows")
    return gdf


def build_county(con):
    print("== county ==")
    zpath = download(f"{TIGER_BASE}/cb_{GENZ_YEAR}_us_county_500k.zip")
    gdf = read_shapefile_zip(zpath)[["GEOID", "NAME", "STATEFP", "ALAND", "geometry"]]
    gdf = gdf.to_crs(4326)
    gdf["area_km2"] = gdf["ALAND"] / 1_000_000.0

    pop = dict(con.execute("SELECT geoid, pop FROM pl_county_pop").fetchall())
    gdf["value"] = gdf["GEOID"].map(pop).fillna(0.0)

    gdf = gdf.rename(columns={"GEOID": "geoid", "NAME": "name"})
    gdf = add_bbox(gdf[["geoid", "name", "area_km2", "value", "geometry"]])
    gdf = fix_zero_population_states(con, gdf)
    to_duckdb(con, "county", gdf)
    return sorted(read_shapefile_zip(zpath)["STATEFP"].unique())


def build_zcta(con):
    print("== zcta (ZIP5 proxy) ==")
    zpath = download(ZCTA_URL)
    gdf = read_shapefile_zip(zpath)
    geoid_col = "ZCTA5CE20" if "ZCTA5CE20" in gdf.columns else "ZCTA5CE10"
    aland_col = "ALAND20" if "ALAND20" in gdf.columns else "ALAND10"
    gdf = gdf[[geoid_col, aland_col, "geometry"]].rename(columns={geoid_col: "geoid", aland_col: "ALAND"})
    gdf = gdf.to_crs(4326)
    gdf["area_km2"] = gdf["ALAND"] / 1_000_000.0

    pop = dict(con.execute("SELECT geoid, pop FROM pl_zcta_pop").fetchall())
    gdf["value"] = gdf["geoid"].map(pop).fillna(0.0)

    gdf["name"] = "ZIP " + gdf["geoid"]
    gdf = add_bbox(gdf[["geoid", "name", "area_km2", "value", "geometry"]])
    to_duckdb(con, "zcta", gdf)


def build_tract(con, state_fips_list):
    print("== tract (per state) ==")
    frames = []
    for i, fips in enumerate(state_fips_list):
        print(f"  [{i+1}/{len(state_fips_list)}] state {fips}")
        try:
            zpath = download(f"{TIGER_BASE}/cb_{GENZ_YEAR}_{fips}_tract_500k.zip")
        except requests.HTTPError:
            print(f"    (no tract file for state {fips}, skipping)")
            continue
        gdf = read_shapefile_zip(zpath)[["GEOID", "NAME", "STATEFP", "ALAND", "geometry"]]
        frames.append(gdf)
    gdf = pd.concat(frames, ignore_index=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=frames[0].crs).to_crs(4326)
    gdf["area_km2"] = gdf["ALAND"] / 1_000_000.0

    pop = dict(con.execute("SELECT geoid, pop FROM pl_tract_pop").fetchall())
    gdf["value"] = gdf["GEOID"].map(pop).fillna(0.0)

    gdf = gdf.rename(columns={"GEOID": "geoid", "NAME": "name"})
    gdf = add_bbox(gdf[["geoid", "name", "area_km2", "value", "geometry"]])
    gdf = fix_zero_population_states(con, gdf)
    to_duckdb(con, "tract", gdf)


def build_hex_values(con):
    """Aggregate block-level population onto H3 cells at each resolution.

    We only store (resolution, h3_index) -> value for cells that actually
    contain a populated block. Hex polygon geometry is generated on demand
    by the API for whatever's on screen, so this table stays small even
    though hex8 alone would be ~13M cells if fully enumerated nationwide.
    Using block internal points (not tract centroids) gives hex7/hex8
    genuinely block-level accuracy instead of lumping a whole tract's
    population into one cell.
    """
    print("== hex_values (aggregated from census block population) ==")
    df = con.execute("SELECT lat, lon, pop FROM pl_block").fetchdf()
    lats, lons, vals = df["lat"].to_numpy(), df["lon"].to_numpy(), df["pop"].to_numpy()
    print(f"  aggregating {len(df):,} populated blocks across {len(HEX_RESOLUTIONS)} resolutions...")

    rows = []
    for res in HEX_RESOLUTIONS:
        agg = {}
        for lat, lon, v in zip(lats, lons, vals):
            h = h3.latlng_to_cell(lat, lon, res)
            agg[h] = agg.get(h, 0) + v
        for h, v in agg.items():
            rows.append((res, h, float(v)))
        print(f"  res {res}: {len(agg):,} non-empty cells")

    out = pd.DataFrame(rows, columns=["resolution", "h3_index", "value"])
    con.register("tmp_hex", out)
    con.execute("CREATE OR REPLACE TABLE hex_values AS SELECT * FROM tmp_hex")
    con.unregister("tmp_hex")


def main():
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = duckdb.connect(str(DB_PATH))
    con.install_extension("spatial")
    con.load_extension("spatial")

    # discover state fips from the county file first (cheap, no pop needed)
    county_zip = download(f"{TIGER_BASE}/cb_{GENZ_YEAR}_us_county_500k.zip")
    state_fips = sorted(read_shapefile_zip(county_zip)["STATEFP"].unique())

    build_pl94171_population(con, state_fips)
    build_county(con)
    build_zcta(con)
    build_tract(con, state_fips)
    build_hex_values(con)

    for table in ["county", "tract", "zcta"]:
        con.execute(f"CREATE INDEX IF NOT EXISTS {table}_bbox ON {table} (xmin, ymin, xmax, ymax)")
    con.execute("CREATE INDEX IF NOT EXISTS hex_values_idx ON hex_values (resolution, h3_index)")

    # drop staging tables, keep only what the API needs
    for t in ["pl_county_pop", "pl_tract_pop", "pl_block", "pl_zcta_pop", "block_zcta"]:
        con.execute(f"DROP TABLE IF EXISTS {t}")

    META_PATH.write_text(json.dumps({
        "metric": "population",
        "metric_label": "Population (2020 Census)",
        "genz_year": GENZ_YEAR,
        "zcta_year": ZCTA_YEAR,
    }, indent=2))

    con.close()
    print(f"\nDone. Database at {DB_PATH}")


if __name__ == "__main__":
    main()
