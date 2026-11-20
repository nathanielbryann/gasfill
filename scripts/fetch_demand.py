"""Monthly gas demand for the page: site/data/demand.json.

Source: Eurostat dataset nrg_cb_gasm ("Supply, transformation and consumption of gas - monthly data"),
inland consumption as calculated under the monthly gas questionnaire (nrg_bal=IC_CAL_MG), natural
gas (siec=G3000), terajoules on a gross calorific value basis (unit=TJ_GCV), converted to TWh
(1 TWh = 3 600 TJ). Eurostat publishes each month about two to three months later, and recent
months are often provisional.

Countries come from config.json: each entry's "eurostat" code if it has one (EU -> EU27_2020),
otherwise its own code; "eurostat": null leaves a country out. If Eurostat can't be reached, the
existing site/data/demand.json is kept unchanged.

    python scripts/fetch_demand.py
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "site", "data", "demand.json")
API = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
TJ_PER_TWH = 3600.0


def month_code(t):
    """'2024-01', '2024M01' or '2024-1' -> '2024-01'."""
    t = str(t).strip().replace("M", "-")
    y, m = t.split("-")[:2]
    return f"{int(y):04d}-{int(m):02d}"


def parse_jsonstat(js, geo_dim="geo", time_dim="time"):
    """Eurostat JSON-stat 2.0 -> {geo: {month: (value, flag)}}. Every other dimension must have size 1."""
    ids, sizes, dims = js["id"], js["size"], js["dimension"]
    strides, s = [0] * len(ids), 1
    for k in range(len(ids) - 1, -1, -1):
        strides[k] = s
        s *= sizes[k]

    def index_of(dim):
        idx = dims[dim]["category"]["index"]
        return {c: i for i, c in enumerate(idx)} if isinstance(idx, list) else idx

    for k, d in enumerate(ids):
        if d not in (geo_dim, time_dim) and sizes[k] != 1:
            raise ValueError(f"dimension {d} has {sizes[k]} values; narrow the query")
    gi, ti = index_of(geo_dim), index_of(time_dim)
    gs, ts = strides[ids.index(geo_dim)], strides[ids.index(time_dim)]
    values, status = js.get("value", {}), js.get("status", {}) or {}

    def get(store, i):
        if isinstance(store, list):
            return store[i] if i < len(store) else None
        return store.get(str(i))

    out = {}
    for g, a in gi.items():
        for t, b in ti.items():
            i = a * gs + b * ts
            v = get(values, i)
            if v is None:
                continue
            out.setdefault(g, {})[month_code(t)] = (float(v), get(status, i) or "")
    return out


def fetch(dataset, geos, since, tries=4):
    params = [("format", "JSON"), ("lang", "EN"), ("freq", "M"), ("nrg_bal", "IC_CAL_MG"), ("siec", "G3000"),
              ("unit", "TJ_GCV"), ("sinceTimePeriod", since)] + [("geo", g) for g in geos]
    url = API + dataset + "?" + urllib.parse.urlencode(params)
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "gas-storage-view/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                js = json.loads(r.read().decode("utf-8"))
            if js.get("class") == "error" or "error" in js:
                raise RuntimeError(str(js.get("error"))[:300])
            return js
        except Exception as e:   # Eurostat sometimes answers 413/503 while it prepares a large extract
            last = e
            print(f"  try {k + 1}: {e}", flush=True)
            time.sleep(20 * (k + 1))
    raise RuntimeError(f"Eurostat not reachable: {last}")


def main():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    dc = cfg.get("demand") or {}
    dataset, since = dc.get("dataset", "nrg_cb_gasm"), dc.get("from", "2014-01")
    pairs = []   # (page code, eurostat code)
    for c in cfg["countries"]:
        es = c.get("eurostat", "EU27_2020" if c["code"] == "EU" else c["code"])
        if es:
            pairs.append((c["code"], es))
    print("--- EUROSTAT DEMAND ---", flush=True)
    try:
        js = fetch(dataset, [es for _, es in pairs], since)
        got = parse_jsonstat(js)
    except Exception as e:   # never block the storage update
        print(f"::warning::Demand not updated ({e}); keeping the existing file.")
        return 0
    months = sorted({m for v in got.values() for m in v})
    if not months:
        print("::warning::Eurostat returned no values; keeping the existing file.")
        return 0
    series, flags, last = {}, {}, {}
    for code, es in pairs:
        recs = got.get(es, {})
        if not recs:
            print(f"::warning::no Eurostat demand for {code} ({es})")
            continue
        series[code] = [round(recs[m][0] / TJ_PER_TWH, 3) if m in recs else None for m in months]
        flags[code] = {m: recs[m][1] for m in months if m in recs and recs[m][1]}
        last[code] = max(recs)
        m = last[code]
        print(f"{code} ({es}): {len(recs)} months, {min(recs)} to {m}, last {series[code][months.index(m)]} TWh" + (f" [{recs[m][1]}]" if recs[m][1] else ""), flush=True)
    if not series:
        print("::warning::no country had demand data; keeping the existing file.")
        return 0
    out = {"meta": {"source": "Eurostat " + dataset + ", inland consumption (IC_CAL_MG), natural gas, TJ GCV",
                    "unit": "TWh per month (GCV)", "updated": js.get("updated"),
                    "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "last": last, "flags_legend": js.get("extension", {}).get("status", {}).get("label") or {"p": "provisional", "e": "estimated"}},
           "months": months, "series": series, "flags": flags}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(tmp, OUT)
    print(f"wrote {OUT}: {len(series)} countries, {months[0]} to {months[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
