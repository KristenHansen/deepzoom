"""
Pulls exact 2020 Census total population (POP100) directly from the public
2020 Census Redistricting Data (P.L. 94-171) Summary File geographic header
files. No API key needed -- these are plain downloadable files, unlike the
api.census.gov gateway (which now requires a key for everything, including
requests that used to work anonymously).

Field layout below was reverse-engineered from the official technical doc
(2020Census_PL94_171Redistricting_StatesTechDoc_English.pdf, Figure 2-4) and
verified against known values (e.g. state total population) before trusting
it -- see the geo header is pipe-delimited, 97 fields, same order regardless
of summary level (unused fields for a given level are just blank).

We only need the geographic header file per state (not File01/02/03) because
POP100 -- total population -- already lives there for every summary level,
including individual census blocks.

Only summary levels we care about:
    050  county
    140  census tract
    750  block   (used to build ZCTA population via the official block->ZCTA
                   relationship file, and to feed hex aggregation at
                   block-level precision instead of coarser tract centroids)
"""
import io
import zipfile
from pathlib import Path

import duckdb
import requests

BASE = "https://www2.census.gov/programs-surveys/decennial/2020/data/01-Redistricting_File--PL_94-171"
BLOCK_ZCTA_REL_URL = "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_tabblock20_natl.txt"

# 0-indexed column positions in the 97-field geographic header record.
IDX = {
    "SUMLEV": 2, "GEOID": 8, "STATE": 12, "COUNTY": 14, "TRACT": 32,
    "BLOCK": 34, "NAME": 87, "POP100": 90, "INTPTLAT": 92, "INTPTLON": 93,
}

# FIPS -> (census.gov folder name, USPS abbreviation). Only the 50 states +
# DC + PR have PL 94-171 redistricting files (other territories don't
# participate in congressional/state redistricting).
STATE_FOLDERS = {
    "01": ("Alabama", "al"), "02": ("Alaska", "ak"), "04": ("Arizona", "az"),
    "05": ("Arkansas", "ar"), "06": ("California", "ca"), "08": ("Colorado", "co"),
    "09": ("Connecticut", "ct"), "10": ("Delaware", "de"),
    "11": ("District_of_Columbia", "dc"), "12": ("Florida", "fl"),
    "13": ("Georgia", "ga"), "15": ("Hawaii", "hi"), "16": ("Idaho", "id"),
    "17": ("Illinois", "il"), "18": ("Indiana", "in"), "19": ("Iowa", "ia"),
    "20": ("Kansas", "ks"), "21": ("Kentucky", "ky"), "22": ("Louisiana", "la"),
    "23": ("Maine", "me"), "24": ("Maryland", "md"), "25": ("Massachusetts", "ma"),
    "26": ("Michigan", "mi"), "27": ("Minnesota", "mn"), "28": ("Mississippi", "ms"),
    "29": ("Missouri", "mo"), "30": ("Montana", "mt"), "31": ("Nebraska", "ne"),
    "32": ("Nevada", "nv"), "33": ("New_Hampshire", "nh"), "34": ("New_Jersey", "nj"),
    "35": ("New_Mexico", "nm"), "36": ("New_York", "ny"), "37": ("North_Carolina", "nc"),
    "38": ("North_Dakota", "nd"), "39": ("Ohio", "oh"), "40": ("Oklahoma", "ok"),
    "41": ("Oregon", "or"), "42": ("Pennsylvania", "pa"), "44": ("Rhode_Island", "ri"),
    "45": ("South_Carolina", "sc"), "46": ("South_Dakota", "sd"), "47": ("Tennessee", "tn"),
    "48": ("Texas", "tx"), "49": ("Utah", "ut"), "50": ("Vermont", "vt"),
    "51": ("Virginia", "va"), "53": ("Washington", "wa"), "54": ("West_Virginia", "wv"),
    "55": ("Wisconsin", "wi"), "56": ("Wyoming", "wy"), "72": ("Puerto_Rico", "pr"),
}


def _download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    r = requests.get(url, timeout=180, stream=True)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 20):
            f.write(chunk)
    return dest


def build_block_zcta_table(con: duckdb.DuckDBPyConnection, cache_dir: Path):
    """Official block->ZCTA assignment (blocks split ZCTAs cleanly, no
    fractional overlap, since ZCTAs are built by aggregating whole blocks)."""
    print("  downloading national block->ZCTA relationship file (~1GB, one-time)...")
    path = _download(BLOCK_ZCTA_REL_URL, cache_dir / "tab20_zcta520_tabblock20_natl.txt")
    con.execute(f"""
        CREATE OR REPLACE TABLE block_zcta AS
        SELECT GEOID_TABBLOCK_20 AS block_geoid, GEOID_ZCTA5_20 AS zcta
        FROM read_csv('{path.as_posix()}', delim='|', header=true, quote='', all_varchar=true)
        WHERE GEOID_ZCTA5_20 IS NOT NULL AND GEOID_ZCTA5_20 != ''
    """)
    n = con.execute("SELECT count(*) FROM block_zcta").fetchone()[0]
    print(f"  block->ZCTA mapping: {n:,} blocks")


def iter_state_geo_rows(fips: str, cache_dir: Path):
    """Yields (sumlev, geoid, state, county, tract, block, name, pop, lat, lon)
    for county/tract/block summary-level rows in one state's geo header."""
    folder, abbr = STATE_FOLDERS[fips]
    url = f"{BASE}/{folder}/{abbr}2020.pl.zip"
    zpath = cache_dir / f"{abbr}2020.pl.zip"
    try:
        _download(url, zpath)
    except requests.HTTPError as e:
        print(f"    no PL94-171 file for state {fips} ({folder}): {e}")
        return

    with zipfile.ZipFile(zpath) as z:
        member = f"{abbr}geo2020.pl"
        with z.open(member) as f:
            for raw in io.TextIOWrapper(f, encoding="utf-8", errors="replace"):
                line = raw.rstrip("\n")
                if not line:
                    continue
                fields = line.split("|")
                sumlev = fields[IDX["SUMLEV"]]
                if sumlev not in ("050", "140", "750"):
                    continue
                pop_raw = fields[IDX["POP100"]]
                lat_raw = fields[IDX["INTPTLAT"]]
                lon_raw = fields[IDX["INTPTLON"]]
                yield (
                    sumlev,
                    fields[IDX["GEOID"]],
                    fields[IDX["STATE"]],
                    fields[IDX["COUNTY"]],
                    fields[IDX["TRACT"]],
                    fields[IDX["BLOCK"]],
                    fields[IDX["NAME"]],
                    int(pop_raw) if pop_raw else 0,
                    float(lat_raw) if lat_raw else None,
                    float(lon_raw) if lon_raw else None,
                )
