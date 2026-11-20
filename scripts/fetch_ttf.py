"""TTF day-ahead/spot prices for the page: site/data/ttf.json ({"start", "ttf": [...]}, EUR/MWh).

Source, in order:
1. data/ttf.csv in this repo (columns date,ttf), if you put one there: a manual copy that wins.
2. ttf_url in config.json: the fuel file Spotrank already publishes, so a new export only has
   to be uploaded once (to Spotrank).
If neither works, the existing site/data/ttf.json is kept unchanged.

    python scripts/fetch_ttf.py
"""
import csv
import datetime as dt
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "site", "data", "ttf.json")
MANUAL = os.path.join(ROOT, "data", "ttf.csv")


def from_csv(path):
    vals = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            d, v = (row.get("date") or "").strip()[:10], (row.get("ttf") or "").strip().replace(",", ".")
            if d and v:
                vals[d] = float(v)
    return vals, "data/ttf.csv"


def from_url(url):
    req = urllib.request.Request(url + "?t=" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H"), headers={"User-Agent": "gas-storage-view/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        js = json.loads(r.read().decode("utf-8"))
    start = dt.date.fromisoformat(js["start"])
    vals = {(start + dt.timedelta(days=i)).isoformat(): v for i, v in enumerate(js["ttf"]) if v is not None}
    name = js.get("meta", {}).get("series", {}).get("ttf", {}).get("name", "TTF")
    return vals, f"{name} (via {url})"


def main():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    try:
        vals, src = from_csv(MANUAL) if os.path.exists(MANUAL) else from_url(cfg["ttf_url"])
    except Exception as e:   # never block the storage update
        print(f"::warning::TTF not updated ({e}); keeping the existing file.")
        return 0
    if not vals:
        print("::warning::TTF source had no values; keeping the existing file.")
        return 0
    days = sorted(vals)
    start, end = dt.date.fromisoformat(days[0]), dt.date.fromisoformat(days[-1])
    arr = [vals.get((start + dt.timedelta(days=i)).isoformat()) for i in range((end - start).days + 1)]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"source": src, "unit": "EUR/MWh", "start": days[0], "end": days[-1], "ttf": arr}, f, separators=(",", ":"))
    print(f"wrote {OUT}: {days[0]} to {days[-1]} from {src}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
