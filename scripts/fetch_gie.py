"""Fetch daily data from GIE's transparency platforms into the site's data files.

- AGSI+ (underground gas storage) -> site/data/storage.json
- ALSI (LNG terminals), by country -> site/data/lng.json
- ALSI, terminal by terminal       -> site/data/lng_terminals.json (countries and start in config.json "terminals")

Countries and dates come from config.json: each country lists its AGSI+ and ALSI query
(`"agsi": {"country": "DE"}`, `"alsi": {"type": "eu"}`, ...). A country that isn't in the file yet
is fetched from `fetch_from`; the others re-download their last `refresh_days` days, because
operators correct recent days (each day is marked E = estimated or C = confirmed).

    AGSI_API_KEY=... python scripts/fetch_gie.py                  # daily update, both datasets
    AGSI_API_KEY=... python scripts/fetch_gie.py --full           # everything again from fetch_from
    AGSI_API_KEY=... python scripts/fetch_gie.py --dataset alsi   # only one dataset (agsi, alsi, terminals)
    AGSI_API_KEY=... python scripts/fetch_gie.py --only DE NL     # only some countries

API (both platforms): key in an `x-key` header, `type=eu` for the EU total, `country=XX` for a
country, `from`/`to` by gas day, `size` up to 300 rows a page. Requests go one calendar year at a
time, so a long history never depends on paging through it in one go. More than 60 calls a minute
from one IP is blocked for 60 seconds, so calls are spaced 1.1 s apart. Data use requires naming
GIE / AGSI+ / ALSI as the source (done on the page).
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
UA = "gas-storage-view/1.1 (GitHub Actions; personal analysis)"
PAGE_SIZE = 300
MIN_GAP_S = 1.1            # 60 calls a minute is GIE's limit; stay under it

# file key -> (API field, sub-key for nested values or None, decimals, unit)
DATASETS = {
    "agsi": {
        "api": "https://agsi.gie.eu/api",
        "out": os.path.join(ROOT, "site", "data", "storage.json"),
        "source": "GIE AGSI+ (https://agsi.gie.eu)",
        "main": "full",                                          # a day counts as reported when this is set
        "fields": {
            "gas": ("gasInStorage", None, 3, "TWh"),             # gas in storage at the end of the gas day
            "full": ("full", None, 2, "%"),                      # share of working gas volume
            "inj": ("injection", None, 1, "GWh/d"),
            "wdr": ("withdrawal", None, 1, "GWh/d"),
            "wgv": ("workingGasVolume", None, 3, "TWh"),         # technical capacity
            "injCap": ("injectionCapacity", None, 0, "GWh/d"),
            "wdrCap": ("withdrawalCapacity", None, 0, "GWh/d"),
            "cons": ("consumption", None, 2, "TWh/yr"),          # countries only: reference annual consumption
            "consFull": ("consumptionFull", None, 2, "%"),       # countries only: gas in storage as % of it
        },
    },
    "alsi": {
        "api": "https://alsi.gie.eu/api",
        "out": os.path.join(ROOT, "site", "data", "lng.json"),
        "source": "GIE ALSI (https://alsi.gie.eu)",
        "main": "sendOut",
        "fields": {
            "inv": ("inventory", "gwh", 1, "GWh"),               # LNG in tanks at the end of the gas day
            "invLng": ("inventory", "lng", 1, "1000 m3 LNG"),
            "sendOut": ("sendOut", None, 1, "GWh/d"),            # regasified gas sent into the grid
            "dtmi": ("dtmi", "gwh", 1, "GWh"),                   # declared total maximum inventory
            "dtrs": ("dtrs", None, 1, "GWh/d"),                  # declared total reference send-out
        },
    },
}

_last_call = [0.0]


def num(v):
    if v is None or isinstance(v, (dict, list)):
        return None
    try:
        f = float(str(v).replace(",", "").strip())
    except ValueError:
        return None
    return f if f == f else None   # drop NaN


def api_get(api, params, key, need_data=True):
    url = api + "?" + urllib.parse.urlencode(params)
    for attempt in range(5):
        wait = MIN_GAP_S - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        req = urllib.request.Request(url, headers={"x-key": key, "User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("  too many requests; waiting 65 s", flush=True)
                time.sleep(65)
                continue
            if e.code >= 500 and attempt < 4:
                time.sleep(10 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code} for {url}: {e.read()[:300]!r}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 4:
                time.sleep(10 * (attempt + 1))
                continue
            raise RuntimeError(f"unreachable ({e}) for {url}") from None
        try:
            js = json.loads(body)
        except ValueError:
            raise RuntimeError(f"not JSON for {url}: {body[:300]!r}") from None
        if need_data and (not isinstance(js, dict) or "data" not in js):
            raise RuntimeError(f"no 'data' for {url}: {body[:300]!r}")
        return js
    raise RuntimeError(f"kept refusing {url}")


def pick(it, field, sub):
    v = it.get(field)
    if sub is None:
        return num(v)
    if isinstance(v, dict):
        return num(v.get(sub))
    return num(v) if sub == "gwh" else None   # a flat value is taken as energy


def parse_rows(items, fields):
    """API rows -> {date: {key: value, 'status': 'C'|'E'|'N'|'-'}} (one aggregate row per gas day)."""
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
        for k, (field, sub, dec, _) in fields.items():
            v = pick(it, field, sub)
            rec[k] = None if v is None else round(v, dec)
        st = str(it.get("status") or "").strip().upper()[:1]
        rec["status"] = st if st in ("C", "E", "N") else "-"
        out[d] = rec
    return out


def year_chunks(start, end):
    a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    for y in range(a.year, b.year + 1):
        yield max(a, dt.date(y, 1, 1)).isoformat(), min(b, dt.date(y, 12, 31)).isoformat()


def fetch_range(ds, entity, start, end, key, show_sample=False):
    rows = {}
    for a, b in year_chunks(start, end):
        page, last = 1, 1
        while page <= last:
            js = api_get(ds["api"], dict(entity, **{"from": a, "to": b, "page": page, "size": PAGE_SIZE}), key)
            last = int(num(js.get("last_page")) or 1)
            got = parse_rows(js.get("data"), ds["fields"])
            if show_sample and js.get("data"):
                sample = json.dumps(js["data"][0])[:600]
                print(f"  sample row: {sample}", flush=True)
                show_sample = False
            rows.update(got)
            page += 1
    return rows


def daterange(a, b):
    d, e = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    while d <= e:
        yield d.isoformat()
        d += dt.timedelta(days=1)


def load_existing(ds):
    """columnar JSON -> {code: {date: record}}"""
    if not os.path.exists(ds["out"]):
        return {}
    with open(ds["out"], encoding="utf-8") as f:
        js = json.load(f)
    out = {}
    start = dt.date.fromisoformat(js["start"])
    for code, s in js.get("series", {}).items():
        recs = {}
        n = max((len(s[k]) for k in ds["fields"] if k in s), default=0)
        status = s.get("status", "")
        for i in range(n):
            d = (start + dt.timedelta(days=i)).isoformat()
            rec = {k: (s[k][i] if k in s and i < len(s[k]) else None) for k in ds["fields"]}
            rec["status"] = status[i] if i < len(status) else "-"
            if any(rec[k] is not None for k in ds["fields"]) or rec["status"] != "-":
                recs[d] = rec
        out[code] = recs
    return out


def write(ds, cfg, data, name):
    start = cfg["fetch_from"]
    main = ds["main"]
    reported = {c: [d for d, r in recs.items() if r.get(main) is not None] for c, recs in data.items()}
    ends = [max(v) for v in reported.values() if v]
    end = max(ends) if ends else start
    days = list(daterange(start, end))
    series, countries = {}, []
    for c in cfg["countries"]:
        code = c["code"]
        recs = data.get(code, {})
        if not reported.get(code):
            continue
        s = {k: [recs.get(d, {}).get(k) for d in days] for k in ds["fields"]}
        s["status"] = "".join(recs.get(d, {}).get("status", "-") for d in days)
        s["first"], s["last"] = min(reported[code]), max(reported[code])
        series[code] = s
        countries.append({k: c[k] for k in ("code", "name", "eu_target") if k in c} | {"aggregate": bool(c.get("aggregate"))})
    js = {
        "meta": {
            "dataset": name,
            "source": ds["source"],
            "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fetch_from": cfg["fetch_from"],
            "display_from": cfg["display_from"],
            "units": {k: u for k, (_, _, _, u) in ds["fields"].items()},
            "countries": countries,
        },
        "start": start,
        "end": end,
        "series": series,
    }
    os.makedirs(os.path.dirname(ds["out"]), exist_ok=True)
    tmp = ds["out"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(js, f, separators=(",", ":"))
    os.replace(tmp, ds["out"])
    print(f"wrote {ds['out']}: {os.path.getsize(ds['out']) / 1024:.0f} KB, {start} to {end}, {len(series)} series")


def run(name, cfg, key, full, only):
    ds = DATASETS[name]
    data = load_existing(ds)
    today = dt.date.today().isoformat()
    wanted = [c for c in cfg["countries"] if name in c and (not only or c["code"] in only)]
    problems, sampled = [], False
    print(f"--- {name.upper()} ---", flush=True)
    for c in wanted:
        code, have = c["code"], data.get(c["code"], {})
        reported = [d for d, r in have.items() if r.get(ds["main"]) is not None]
        if full or not reported:
            start = cfg["fetch_from"]
        else:
            start = (dt.date.fromisoformat(max(reported)) - dt.timedelta(days=int(cfg["refresh_days"]))).isoformat()
            start = max(start, cfg["fetch_from"])
        t0 = time.time()
        try:
            rows = fetch_range(ds, c[name], start, today, key, show_sample=(name == "alsi" and not sampled))
            sampled = True
        except RuntimeError as e:
            print(f"::warning::{name.upper()} {code}: {e}")
            problems.append(code)
            continue
        got = [d for d, r in rows.items() if r.get(ds["main"]) is not None]
        if not got:
            print(f"::warning::{name.upper()} {code}: no values for {start} to {today} ({c[name]})")
            problems.append(code)
            continue
        changed = sum(1 for d, r in rows.items() if have.get(d) != r)
        have.update(rows)
        data[code] = have
        last = max(got)
        print(f"{code}: {len(got)} days with values {min(got)}..{last} ({changed} new or revised), "
              f"{last} {ds['main']} {rows[last][ds['main']]} status {rows[last]['status']}  [{time.time() - t0:.0f} s]", flush=True)
    if any(data.get(c["code"]) for c in cfg["countries"]):
        write(ds, cfg, data, name)
    return problems, wanted


# ---------- terminal by terminal (ALSI) ----------
TERMINALS_OUT = os.path.join(ROOT, "site", "data", "lng_terminals.json")
LISTING_URL = "https://alsi.gie.eu/api/about"


def walk_listing(js):
    """ALSI 'about?show=listing' -> [{id, company, name, company_name, country}] for every terminal.

    The listing nests operators (with an EIC code and a list of facilities) under region and country
    keys. It is read defensively: any dict with an 'eic' and a 'facilities' list is an operator, any
    dict with an 'eic' inside such a list is a terminal, and the country comes from the terminal, the
    operator, or the nearest two-letter key above them.
    """
    out, seen = [], set()

    def code_of(x):
        c = x.get("country") if isinstance(x, dict) else None
        if isinstance(c, dict):
            c = c.get("code")
        return str(c).upper()[:2] if c else None

    def go(node, cc):
        if isinstance(node, dict):
            facs = node.get("facilities")
            if node.get("eic") and isinstance(facs, list):
                ccomp = code_of(node) or cc
                for f in facs:
                    if isinstance(f, dict) and f.get("eic") and f["eic"] not in seen:
                        seen.add(f["eic"])
                        out.append({"id": f["eic"], "company": node["eic"], "name": str(f.get("name") or f["eic"]).strip(),
                                    "company_name": str(node.get("short_name") or node.get("name") or "").strip(),
                                    "country": code_of(f) or ccomp})
                return
            for k, v in node.items():
                go(v, k.upper() if isinstance(k, str) and len(k) == 2 and k.isalpha() else cc)
        elif isinstance(node, list):
            for v in node:
                go(v, cc)

    go(js, None)
    return out


def run_terminals(cfg, key, full):
    tc = cfg.get("terminals") or {}
    countries, start0 = set(tc.get("countries") or []), tc.get("from", cfg["fetch_from"])
    ds = DATASETS["alsi"]
    print("--- ALSI TERMINALS ---", flush=True)
    try:
        listing = api_get(LISTING_URL, {"show": "listing"}, key, need_data=False)
    except RuntimeError as e:
        print(f"::warning::ALSI listing unavailable ({e}); terminals not updated.")
        return
    terms = [t for t in walk_listing(listing) if t["country"] in countries]
    if not terms:
        shape = json.dumps(listing)[:800]
        print(f"::warning::No terminals for {sorted(countries)} found in the ALSI listing. Start of the listing: {shape}")
        return
    print("terminals: " + "; ".join(f"{t['country']} {t['name']} ({t['company_name']})" for t in terms), flush=True)

    # existing file -> {id: {date: record}}
    have_all = {}
    if os.path.exists(TERMINALS_OUT):
        with open(TERMINALS_OUT, encoding="utf-8") as f:
            old = json.load(f)
        st = dt.date.fromisoformat(old["start"])
        for tid, s in old.get("series", {}).items():
            recs, status = {}, s.get("status", "")
            for i in range(max((len(s[k]) for k in ds["fields"] if k in s), default=0)):
                rec = {k: (s[k][i] if k in s and i < len(s[k]) else None) for k in ds["fields"]}
                rec["status"] = status[i] if i < len(status) else "-"
                if any(rec[k] is not None for k in ds["fields"]):
                    recs[(st + dt.timedelta(days=i)).isoformat()] = rec
            have_all[tid] = recs
    today, sampled = dt.date.today().isoformat(), False
    for t in terms:
        have = have_all.get(t["id"], {})
        rep_days = [d for d, r in have.items() if r.get("sendOut") is not None or r.get("inv") is not None]
        start = start0 if full or not rep_days else max(start0, (dt.date.fromisoformat(max(rep_days)) - dt.timedelta(days=int(cfg["refresh_days"]))).isoformat())
        try:
            rows = fetch_range(ds, {"country": t["country"], "company": t["company"], "facility": t["id"]}, start, today, key, show_sample=not sampled)
            sampled = True
        except RuntimeError as e:
            print(f"::warning::terminal {t['name']}: {e}")
            continue
        got = [d for d, r in rows.items() if r.get("sendOut") is not None or r.get("inv") is not None]
        have.update(rows)
        have_all[t["id"]] = have
        last = max(got) if got else None
        print(f"{t['country']} {t['name']}: {len(got)} days with values" + (f", last {last} send-out {rows[last]['sendOut']} GWh/d, capacity {rows[last]['dtrs']}" if last else ""), flush=True)

    days_all = [d for recs in have_all.values() for d, r in recs.items() if r.get("sendOut") is not None or r.get("inv") is not None]
    if not days_all:
        print("::warning::No terminal data at all; file not written.")
        return
    end = max(days_all)
    days = list(daterange(start0, end))
    series, meta = {}, []
    for t in terms:
        recs = have_all.get(t["id"], {})
        rep_days = [d for d, r in recs.items() if r.get("sendOut") is not None or r.get("inv") is not None]
        if not rep_days:
            continue
        s = {k: [recs.get(d, {}).get(k) for d in days] for k in ds["fields"]}
        s["status"] = "".join(recs.get(d, {}).get("status", "-") for d in days)
        s["first"], s["last"] = min(rep_days), max(rep_days)
        series[t["id"]] = s
        meta.append({k: t[k] for k in ("id", "name", "company_name", "country")})
    js = {"meta": {"dataset": "alsi-terminals", "source": ds["source"], "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "units": {k: u for k, (_, _, _, u) in ds["fields"].items()}, "terminals": meta},
          "start": start0, "end": end, "series": series}
    tmp = TERMINALS_OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(js, f, separators=(",", ":"))
    os.replace(tmp, TERMINALS_OUT)
    print(f"wrote {TERMINALS_OUT}: {os.path.getsize(TERMINALS_OUT) / 1024:.0f} KB, {len(series)} terminals, {start0} to {end}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="fetch every country again from fetch_from")
    ap.add_argument("--dataset", choices=["agsi", "alsi", "terminals", "all"], default="all")
    ap.add_argument("--only", nargs="*", help="country codes to fetch (default: all in config.json)")
    args = ap.parse_args()

    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    key = os.environ.get("AGSI_API_KEY", "").strip()
    if not key:
        print("::warning::AGSI_API_KEY is not set; nothing fetched.")
        return 0
    names = ["agsi", "alsi", "terminals"] if args.dataset == "all" else [args.dataset]
    failed_all = False
    for name in names:
        if name == "terminals":
            run_terminals(cfg, key, args.full)
            continue
        problems, wanted = run(name, cfg, key, args.full, args.only)
        if name == "agsi" and wanted and len(problems) == len(wanted):
            failed_all = True
    if failed_all:
        print("::error::No AGSI+ country could be fetched; check the AGSI_API_KEY secret and the log above.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
