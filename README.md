# Gasfill

A one-page view of European gas storage, day by day since 2021: a map and ranking for any gas day,
automatic notes on what stands out, a country table, a winter (or refill) outlook built from past
years, seasonal curves against the 5-year range, and history of fill level, net
injection/withdrawal, LNG send-out and TTF on one time axis. Every chart can be saved as a PNG with
its legend and source line. Data: [GIE AGSI+](https://agsi.gie.eu) and [GIE ALSI](https://alsi.gie.eu).
Same look as Spotrank, but no game.

- `site/` is the website (GitHub Pages). One file, `site/index.html`, no libraries.
- `site/data/storage.json` holds the daily AGSI+ data, `site/data/lng.json` the ALSI LNG data by
  country and `site/data/lng_terminals.json` the ALSI data for each terminal (countries and start
  year under `terminals` in `config.json`; `"EU"` there means every EU member state), all written by `scripts/fetch_gie.py` (one script, one API
  key for both platforms). Terminals come from ALSI's own listing (`about?show=listing`), so a new
  terminal appears without code changes.
- `site/data/ttf.json` holds TTF, written by `scripts/fetch_ttf.py`.
- `site/data/countries-map.json` holds the map shapes for every European country (`scripts/build_map.py`, run once).
- `config.json` lists the countries and dates.
- `.github/workflows/update.yml` fetches new data, commits it and republishes the site.

## One-time setup

1. **Repo:** create an empty repository on GitHub and push these files to `main`.
2. **API key:** Settings → Secrets and variables → Actions → *New repository secret*, name
   `AGSI_API_KEY`, value: your AGSI+ key (from your account on agsi.gie.eu).
3. **Pages:** Settings → Pages → *Source: GitHub Actions*.
4. **First fetch:** Actions → *Update storage data and deploy* → *Run workflow*, tick
   *Fetch everything again*. It downloads 2016 to today for every country, about 14 pages per
   country at 1.1 s per call (AGSI+ blocks an IP for 60 s above 60 calls a minute), so 2–3 minutes.
5. **Daily update with cron-job.org** (as for Spotrank; GitHub's own schedule only runs for the
   person who last edited the cron line):
   - URL: `https://api.github.com/repos/<owner>/<repo>/actions/workflows/update.yml/dispatches`
   - Method: `POST`, body: `{"ref":"main"}`
   - Headers: `Accept: application/vnd.github+json`, `Authorization: Bearer <token>`,
     `X-GitHub-Api-Version: 2022-11-28`
   - Token: a fine-grained personal access token for this repository only, permission
     *Actions: Read and write*. You can also add this repository to the Spotrank token.
   - When: once a day after AGSI+ has published, e.g. 07:00 Europe/Helsinki. AGSI+ publishes at
     19:30 CET and runs again at 23:00 CET for late reporters.

To fetch right away without cron-job.org: *Run workflow*, or change `.github/update-now.txt` and commit.
To fetch everything again from `fetch_from` (both datasets), change `.github/refetch-all.txt` and commit.

**Who can see it:** a GitHub Pages site is public to anyone who has the link, also from a private
repository (Pages from private repos needs a paid plan). The page has `noindex`, so search engines
skip it, and it only shows public AGSI+ data and your TTF series. To keep it fully private, run it
locally instead (below).

## Run it locally

```
AGSI_API_KEY=your-key python scripts/fetch_gie.py --full
python scripts/fetch_ttf.py
python -m http.server -d site 8000      # then open http://localhost:8000
```

On Windows PowerShell: `$env:AGSI_API_KEY="your-key"; python scripts/fetch_gie.py --full`.
Only the Python standard library is used. `python tests/test_fetch.py` checks the fetcher offline.

## Adding a country

Add a line to `countries` in `config.json`, for example

```json
{"code": "AT", "name": "Austria", "agsi": {"country": "AT"}, "eu_target": true}
```

and push. A push that changes `config.json` fetches data, and a country that isn't in
`storage.json` yet is fetched from `fetch_from`. The map already has every European country, so
nothing else changes. `eu_target` draws the EU 90% target and window for that country (false for
the UK, Ukraine, Serbia).

## How the data is handled

- **What is fetched:** `https://agsi.gie.eu/api` and `https://alsi.gie.eu/api` with the key in an
  `x-key` header, `type=eu` for the EU total and `country=XX` for a country, `from`/`to` by gas day,
  300 rows a page, one calendar year per request (a single long request stopped paging early for
  Germany in the first run).
  Fields kept: gas in storage (TWh), fill % of working gas volume, injection and withdrawal
  (GWh/d), working gas volume (TWh), injection and withdrawal capacity (GWh/d), the country's
  reference annual consumption (TWh) and storage as % of it, and the E/C status per day. From
  ALSI: LNG in tanks (GWh and 1000 m³ LNG), send-out (GWh/d), declared maximum inventory and
  reference send-out.
- **UK:** AGSI+ has no UK data after 30 Dec 2020, so the page hides GB (any country without data
  in the last 30 days is hidden and listed at the top).
- **Revisions:** each daily run re-downloads the last 60 days (`refresh_days`), because operators
  correct recent days. AGSI+ marks a day E (estimated) until it is C (confirmed); the page marks
  estimated days.
- **History:** `fetch_from` is 2016 so that every day shown since 2021 (`display_from`) has the
  five earlier years its 5-year average and range need.
- **Late reporters:** when a country hasn't reported the chosen day yet, the map and table show its
  latest day (up to 3 days back), marked.
- **TTF:** read from the fuel file Spotrank already publishes (`ttf_url` in `config.json`), so a new
  export only has to be uploaded once, to Spotrank. To keep a separate copy instead, put a
  `data/ttf.csv` (columns `date,ttf`) in this repository: it wins over the URL.
- **Attribution:** AGSI+ data may be reused with GIE / AGSI+ named as the source; the page does.

`storage.json` layout: `start`/`end` dates, and per country (`series.DE`, ...) one array per field
with one value per day from `start` (null where missing), plus `status` as one character per day.

Method notes (5-year average, net flow, days of consumption, the filling target and its legal
basis) are on the page under *Method and sources*.
