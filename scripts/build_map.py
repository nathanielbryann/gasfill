"""Build site/data/countries-map.json: simplified SVG country shapes for the storage map.

Country borders are the unions of the ENTSO-E bidding-zone shapes shipped with entsoe-py (MIT),
the same source as Spotrank's map. Countries outside ENTSO-E's zones (GB, IE, UA, the western
Balkans, ...) come from Natural Earth's 1:110m countries (public domain), as bundled with
geopandas 0.14. Every European country gets a shape, so adding a country to config.json never
needs a new map; countries without data are drawn grey.

Run once; the output is static:
    pip install "geopandas==0.14.4" pyogrio shapely pyproj entsoe-py
    python scripts/build_map.py
"""
import json
import os

import entsoe
import geopandas as gpd
from shapely.geometry import box, Polygon
from shapely.ops import polylabel, unary_union

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "site", "data", "countries-map.json")
GEO = os.path.join(os.path.dirname(entsoe.__file__), "geo", "geojson")

# ISO code -> entsoe-py zone files whose union is the country (DE_LU includes Luxembourg)
ENTSOE = {
    "AT": ["AT"], "BE": ["BE"], "BG": ["BG"], "CH": ["CH"], "CZ": ["CZ"], "DE": ["DE_LU"],
    "DK": ["DK_1", "DK_2"], "EE": ["EE"], "ES": ["ES"], "FI": ["FI"], "FR": ["FR"], "GR": ["GR"],
    "HR": ["HR"], "HU": ["HU"], "LT": ["LT"], "LV": ["LV"], "NL": ["NL"], "PL": ["PL"], "PT": ["PT"],
    "RO": ["RO"], "RS": ["RS"], "SI": ["SI"], "SK": ["SK"],
    "IT": ["IT_NORD", "IT_CNOR", "IT_CSUD", "IT_SUD", "IT_CALA", "IT_SICI", "IT_SARD"],
    "NO": ["NO_1", "NO_2", "NO_3", "NO_4", "NO_5"],
    "SE": ["SE_1", "SE_2", "SE_3", "SE_4"],
}
# ISO code -> Natural Earth name, for European countries that have no ENTSO-E zone file
NE_COUNTRIES = {
    "GB": "United Kingdom", "IE": "Ireland", "UA": "Ukraine", "BA": "Bosnia and Herz.", "ME": "Montenegro",
    "AL": "Albania", "MK": "North Macedonia", "XK": "Kosovo", "MD": "Moldova", "BY": "Belarus",
}
NE_CONTEXT = ["Russia", "Turkey", "Morocco", "Algeria", "Tunisia", "Iceland", "Georgia", "Syria", "Cyprus"]

CRS = "EPSG:3035"                          # ETRS89 Lambert Azimuthal Equal Area, Europe
BBOX_LL = (-11.5, 34.5, 40.5, 71.5)        # Iberia to North Cape, wide enough for all of Ukraine
W = 1000                                   # SVG viewBox width


def load_union(names):
    parts = []
    for n in names:
        g = gpd.read_file(os.path.join(GEO, n + ".geojson")).to_crs(CRS)
        parts.append(g.union_all() if hasattr(g, "union_all") else g.unary_union)
    # a small buffer out and back in closes slivers along internal zone borders
    return unary_union(parts).buffer(300).buffer(-300)


def main():
    clip = gpd.GeoSeries([box(*BBOX_LL)], crs="EPSG:4326").to_crs(CRS).iloc[0].envelope
    minx, miny, maxx, maxy = clip.bounds
    scale = W / (maxx - minx)
    H = round((maxy - miny) * scale)

    def to_path(geom, tol_m):
        geom = geom.intersection(clip).simplify(tol_m, preserve_topology=True)
        polys = [geom] if isinstance(geom, Polygon) else list(getattr(geom, "geoms", []))
        parts = []
        for poly in polys:
            if not isinstance(poly, Polygon) or poly.area < (tol_m * 3) ** 2:
                continue
            for ring in [poly.exterior] + list(poly.interiors):
                pts = [((x - minx) * scale, (maxy - y) * scale) for x, y in ring.coords]
                parts.append("M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in pts[:-1]) + "Z")
        return "".join(parts)

    def anchor(geom):
        geom = geom.intersection(clip)
        polys = [geom] if isinstance(geom, Polygon) else list(getattr(geom, "geoms", []))
        big = max(polys, key=lambda p: p.area)
        pt = polylabel(big, tolerance=2000)
        return round((pt.x - minx) * scale, 1), round((maxy - pt.y) * scale, 1)

    countries = {}
    for code, files in ENTSOE.items():
        g = load_union(files)
        x, y = anchor(g)
        countries[code] = {"d": to_path(g, 2500), "x": x, "y": y}
    ne = gpd.read_file(gpd.datasets.get_path("naturalearth_lowres")).to_crs(CRS)
    for code, name in NE_COUNTRIES.items():
        row = ne[ne["name"] == name]
        if len(row):
            g = row.geometry.iloc[0]
            x, y = anchor(g)
            countries[code] = {"d": to_path(g, 4000), "x": x, "y": y}
    context = []
    for name in NE_CONTEXT:
        row = ne[ne["name"] == name]
        if len(row):
            d = to_path(row.geometry.iloc[0], 4000)
            if d:
                context.append({"id": name, "d": d})
    out = {
        "note": "Borders: unions of entsoe-py bidding zones (MIT); other countries Natural Earth 1:110m (public domain). Projection EPSG:3035.",
        "viewBox": [0, 0, W, H],
        "countries": {k: v for k, v in countries.items() if v["d"]},
        "context": context,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"wrote {OUT}: {os.path.getsize(OUT) / 1024:.0f} KB, viewBox {W}x{H}, "
          f"{len(out['countries'])} countries, {len(context)} context shapes")


if __name__ == "__main__":
    main()
