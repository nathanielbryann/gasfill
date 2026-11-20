"""Offline checks for scripts/fetch_gie.py: parsing (flat and nested), year chunks, paging, merging, round trip.

The rows below are hand-made fixtures shaped like the GIE API documentation, not real data.
    python tests/test_fetch.py
"""
import datetime as dt
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import fetch_gie as fg  # noqa: E402

AGSI, ALSI = fg.DATASETS["agsi"], fg.DATASETS["alsi"]


def agsi_row(day, full, status="C"):
    return {"name": "Fixture", "code": "XX", "url": "XX", "gasDayStart": day, "gasInStorage": f"{full * 2.5:.4f}",
            "injection": "120.50", "withdrawal": "-", "workingGasVolume": "250.0000", "injectionCapacity": "1200.00",
            "withdrawalCapacity": "2000.00", "status": status, "trend": "0.05", "full": f"{full:.2f}",
            "consumption": "850.0000", "consumptionFull": f"{full * 0.3:.2f}", "info": []}


def alsi_row(day, nested=True):
    if nested:
        return {"gasDayStart": day, "inventory": {"lng": "350.5", "gwh": "2400.2"}, "sendOut": "310.4",
                "dtmi": {"lng": "700", "gwh": "4800"}, "dtrs": "500", "status": "C"}
    return {"gasDayStart": day, "inventory": "2400.2", "sendOut": "310.4", "dtmi": "4800", "dtrs": "500", "status": "E"}


def main():
    rows = fg.parse_rows([agsi_row("2026-11-17", 80.12), agsi_row("2026-11-18", 79.5, "E"), {"gasDayStart": "nope"}], AGSI["fields"])
    assert list(rows) == ["2026-11-17", "2026-11-18"], rows
    r = rows["2026-11-18"]
    assert r["full"] == 79.5 and r["wdr"] is None and r["status"] == "E" and r["wgv"] == 250.0, r

    a = fg.parse_rows([alsi_row("2026-11-18")], ALSI["fields"])["2026-11-18"]
    assert a["inv"] == 2400.2 and a["invLng"] == 350.5 and a["sendOut"] == 310.4 and a["dtmi"] == 4800 and a["dtrs"] == 500, a
    b = fg.parse_rows([alsi_row("2026-11-18", nested=False)], ALSI["fields"])["2026-11-18"]
    assert b["inv"] == 2400.2 and b["invLng"] is None and b["dtmi"] == 4800 and b["status"] == "E", b

    assert list(fg.year_chunks("2024-11-01", "2026-02-03")) == [("2024-11-01", "2024-12-31"), ("2025-01-01", "2025-12-31"), ("2026-01-01", "2026-02-03")]

    # paging inside one chunk, and one call series per year chunk
    calls = []
    def fake_get(api, params, key):
        calls.append((params["from"][:4], params["page"]))
        day = dt.date.fromisoformat(params["from"]) + dt.timedelta(days=params["page"] - 1)
        return {"last_page": 2, "data": [agsi_row(day.isoformat(), 50 + params["page"])]}
    real = fg.api_get
    fg.api_get = fake_get
    try:
        got = fg.fetch_range(AGSI, {"country": "XX"}, "2025-12-30", "2026-01-02", "key")
    finally:
        fg.api_get = real
    assert calls == [("2025", 1), ("2025", 2), ("2026", 1), ("2026", 2)], calls
    assert sorted(got) == ["2025-12-30", "2025-12-31", "2026-01-01", "2026-01-02"], got

    # round trip: write columnar JSON, read it back unchanged; first/last mark reported days only
    with tempfile.TemporaryDirectory() as tmp:
        ds = dict(AGSI, out=os.path.join(tmp, "storage.json"))
        cfg = {"fetch_from": "2025-12-29", "display_from": "2025-12-29",
               "countries": [{"code": "XX", "name": "Fixture", "agsi": {"country": "XX"}, "eu_target": True}]}
        data = {"XX": dict(got)}
        data["XX"]["2025-12-29"] = {k: None for k in AGSI["fields"]} | {"status": "N"}
        fg.write(ds, cfg, data, "agsi")
        back = fg.load_existing(ds)
        assert back["XX"] == data["XX"], (back, data)
        js = json.load(open(ds["out"]))
        s = js["series"]["XX"]
        assert s["status"] == "NCCCC" and s["first"] == "2025-12-30" and s["last"] == "2026-01-02" and js["end"] == "2026-01-02", s
    print("fetch_gie tests passed")


if __name__ == "__main__":
    main()


def test_listing():
    # two plausible shapes of ALSI's listing: nested by region and country, and a flat list
    nested = {"LSO": {"Europe": {"DE": [{"name": "Deutsche Energy Terminal GmbH", "short_name": "DET", "eic": "37X-DET", "country": {"code": "DE"},
                                         "facilities": [{"name": "Wilhelmshaven 1", "eic": "W1"}, {"name": "Brunsbuettel", "eic": "BR"}]}],
                                  "NL": [{"name": "Gate terminal", "eic": "21X-GATE", "facilities": [{"name": "Rotterdam Gate", "eic": "GATE", "country": {"code": "NL"}}]}]}}}
    t = fg.walk_listing(nested)
    assert [(x["facility"], x["country"], x["company"]) for x in t] == [("W1", "DE", "37X-DET"), ("BR", "DE", "37X-DET"), ("GATE", "NL", "21X-GATE")], t
    # the same terminal under two operators is kept twice
    two = {"IT": [{"eic": "OLD", "name": "GNL Italia", "facilities": [{"eic": "PAN", "name": "Panigaglia (historical)"}]},
                  {"eic": "NEW", "name": "SNAM LNG", "facilities": [{"eic": "PAN", "name": "Panigaglia"}]}]}
    assert [x["id"] for x in fg.walk_listing(two)] == ["OLD:PAN", "NEW:PAN"]
    flat = [{"eic": "C1", "name": "Op", "country": "FR", "facilities": [{"eic": "F1", "name": "Dunkerque"}]}]
    assert fg.walk_listing(flat) == [{"id": "C1:F1", "facility": "F1", "company": "C1", "name": "Dunkerque", "company_name": "Op", "country": "FR"}]
    print("listing tests passed")


def test_terminal_countries():
    codes, scope = fg.terminal_countries({"countries": ["EU"]})
    assert scope == "EU" and {"DE", "ES", "PL", "GR", "EL"} <= codes and "GB" not in codes and "NO" not in codes
    codes, scope = fg.terminal_countries({"countries": ["de", "NL"]})
    assert codes == {"DE", "NL"} and scope is None
    print("terminal countries ok")


if __name__ == "__main__":
    test_listing()
    test_terminal_countries()
