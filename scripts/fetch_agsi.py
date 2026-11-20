"""Fetch daily gas storage from GIE AGSI+ into site/data/storage.json.

Countries and dates come from config.json. A country that isn't in the file yet is fetched from
`fetch_from`; the others re-download their last `refresh_days` days, because storage operators
correct recent days and AGSI marks each day as estimated (E) or confirmed (C).

    AGSI_API_KEY=... python scripts/fetch_agsi.py            # daily update
    AGSI_API_KEY=... python scripts/fetch_agsi.py --full     # everything again from fetch_from
    AGSI_API_KEY=... python scripts/fetch_agsi.py --only DE  # one country

API: https://agsi.gie.eu/api with the key in an `x-key` header, `type=eu` for the EU total,
`country=XX` for a country, `from`/`to` gas-day dates, `size` up to 300 rows per page. More than
60 calls a minute from one IP is blocked for 60 seconds, so calls are spaced 1.1 s apart.
Data use requires naming GIE / AGSI+ as the source (done on the page).
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONFIG = os.path.join(ROOT, "config.json")
OUT = os.path.join(ROOT, "site", "data", "storage.json")
API = "https://agsi.gie.eu/api"
UA = "gas-storage-view/1.0 (GitHub Actions; personal analysis)"
PAGE_SIZE = 300
MIN_GAP_S = 1.1            # 60 calls a minute is AGSI's limit; stay under it

# file key -> (AGSI field, decimals, unit)
FIELDS = {
    "gas": ("gasInStorage", 3, "TWh"),             # gas in storage at the end of the gas day
    "full": ("full", 2, "%"),                      # share of working gas volume
    "inj": ("injection", 1, "GWh/d"),
    "wdr": ("withdrawal", 1, "GWh/d"),
    "wgv": ("workingGasVolume", 3, "TWh"),         # technical capacity
    "injCap": ("injectionCapacity", 0, "GWh/d"),
    "wdrCap": ("withdrawalCapacity", 0, "GWh/d"),
    "cons": ("consumption", 2, "TWh/yr"),          # countries only: reference annual consumption
    "consFull": ("consumptionFull", 2, "%"),       # countries only: gas in storage as % of it
}

_last_call = [0.0]


def num(v):
    if v is None:
        return None
    try:
        f = float(str(v).replace(",", "").strip())
    except ValueError:
        return None
    return f if f == f else None   # drop NaN


def api_get(params, key):
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(5):
        wait = MIN_GAP_S - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        req = urllib.request.Request(url, headers={"x-key": key, "User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                body = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("  too many requests; waiting 65 s", flush=True)
                time.sleep(65)
                continue
            if e.code >= 500 and attempt < 4:
                time.sleep(10 * (attempt + 1))
                continue
            raise RuntimeError(f"AGSI answered HTTP {e.code} for {url}: {e.read()[:300]!r}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 4:
                time.sleep(10 * (attempt + 1))
                continue
            raise RuntimeError(f"AGSI unreachable ({e}) for {url}") from None
        try:
            js = json.loads(body)
        except ValueError:
            raise RuntimeError(f"AGSI sent something that isn't JSON for {url}: {body[:300]!r}") from None
        if not isinstance(js, dict) or "data" not in js:
            raise RuntimeError(f"AGSI response has no 'data' for {url}: {body[:300]!r}")
        return js
    raise RuntimeError(f"AGSI kept refusing {url}")


def parse_rows(items):
    """AGSI rows -> {date: {key: value, 'status': 'C'|'E'|'N'}} (one aggregate row per gas day)."""
    out = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        d = str(it.get("gasDayStart") or "")[:10]
        try:
            dt.date.fromisoformat(d)
        except ValueError:
            continue
        rec = {}
        for k, (field, dec, _) in FIELDS.items():
            v = num(it.get(field))
            rec[k] = None if v is None else round(v, dec)
        st = str(it.get("status") or "").strip().upper()[:1]
        rec["status"] = st if st in ("C", "E", "N") else "-"
        out[d] = rec
    return out


def fetch_range(entity, start, end, key):
    rows, page, last = {}, 1, 1
    while page <= last:
        params = dict(entity, **{"from": start, "to": end, "page": page, "size": PAGE_SIZE})
        js = api_get(params, key)
        last = int(num(js.get("last_page")) or 1)
        rows.update(parse_rows(js.get("data")))
        page += 1
    return rows


def daterange(a, b):
    d = dt.date.fromisoformat(a)
    e = dt.date.fromisoformat(b)
    while d <= e:
        yield d.isoformat()
        d += dt.timedelta(days=1)


def load_existing():
    """storage.json (columnar) -> {code: {date: record}}"""
    if not os.path.exists(OUT):
        return {}
    with open(OUT, encoding="utf-8") as f:
        js = json.load(f)
    out = {}
    start = dt.date.fromisoformat(js["start"])
    for code, s in js.get("series", {}).items():
        recs = {}
        n = len(s.get("full", []))
        status = s.get("status", "")
        for i in range(n):
            d = (start + dt.timedelta(days=i)).isoformat()
            rec = {k: (s[k][i] if k in s and i < len(s[k]) else None) for k in FIELDS}
            rec["status"] = status[i] if i < len(status) else "-"
            if any(rec[k] is not None for k in FIELDS):
                recs[d] = rec
        out[code] = recs
    return out


def write(cfg, data):
    start = cfg["fetch_from"]
    ends = [max(r) for r in data.values() if r]
    end = max(ends) if ends else start
    days = list(daterange(start, end))
    series = {}
    for c in cfg["countries"]:
        recs = data.get(c["code"], {})
        if not recs:
            continue
        s = {k: [recs.get(d, {}).get(k) for d in days] for k in FIELDS}
        s["status"] = "".join(recs.get(d, {}).get("status", "-") for d in days)
        s["last"] = max(recs)
        series[c["code"]] = s
    js = {
        "meta": {
            "source": "GIE AGSI+ (https://agsi.gie.eu)",
            "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fetch_from": cfg["fetch_from"],
            "display_from": cfg["display_from"],
            "units": {k: u for k, (_, _, u) in FIELDS.items()},
            "countries": [{k: c[k] for k in ("code", "name", "eu_target") if k in c} | {"aggregate": bool(c.get("aggregate"))}
                          for c in cfg["countries"] if c["code"] in series],
        },
        "start": start,
        "end": end,
        "series": series,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(js, f, separators=(",", ":"))
    os.replace(tmp, OUT)
    print(f"wrote {OUT}: {os.path.getsize(OUT) / 1024:.0f} KB, {start} to {end}, {len(series)} series")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="fetch every country again from fetch_from")
    ap.add_argument("--only", nargs="*", help="country codes to fetch (default: all in config.json)")
    args = ap.parse_args()

    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    key = os.environ.get("AGSI_API_KEY", "").strip()
    if not key:
        print("::warning::AGSI_API_KEY is not set; nothing fetched.")
        return 0

    data = load_existing()
    today = dt.date.today().isoformat()
    wanted = [c for c in cfg["countries"] if not args.only or c["code"] in args.only]
    problems = []
    for c in wanted:
        code, have = c["code"], data.get(c["code"], {})
        if args.full or not have:
            start = cfg["fetch_from"]
        else:
            start = (dt.date.fromisoformat(max(have)) - dt.timedelta(days=int(cfg["refresh_days"]))).isoformat()
            start = max(start, cfg["fetch_from"])
        t0 = time.time()
        try:
            rows = fetch_range(c["agsi"], start, today, key)
        except RuntimeError as e:
            print(f"::warning::{code}: {e}")
            problems.append(code)
            continue
        if not rows:
            print(f"::warning::{code}: AGSI returned no rows for {start} to {today} ({c['agsi']})")
            problems.append(code)
            continue
        changed = sum(1 for d, r in rows.items() if have.get(d) != r)
        have.update(rows)
        data[code] = have
        last = max(rows)
        print(f"{code}: {len(rows)} days {min(rows)}..{last} ({changed} new or revised), "
              f"{last} full {rows[last]['full']}% status {rows[last]['status']}  [{time.time() - t0:.0f} s]", flush=True)

    if any(data.get(c["code"]) for c in cfg["countries"]):
        write(cfg, data)
    if problems and len(problems) == len(wanted):
        print("::error::No country could be fetched; check the AGSI_API_KEY secret and the log above.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
