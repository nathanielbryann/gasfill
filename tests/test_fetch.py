"""Offline checks for scripts/fetch_agsi.py: parsing, paging, merging and the file round trip.

The rows below are a hand-made fixture shaped like the AGSI+ API documentation (v006), not real data.
    python tests/test_fetch.py
"""
import datetime as dt
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import fetch_agsi as fa  # noqa: E402


def fake_row(day, full, status="C"):
    return {"name": "Fixture", "code": "XX", "url": "XX", "gasDayStart": day, "gasInStorage": f"{full * 2.5:.4f}",
            "injection": "120.50", "withdrawal": "-", "workingGasVolume": "250.0000", "injectionCapacity": "1200.00",
            "withdrawalCapacity": "2000.00", "status": status, "trend": "0.05", "full": f"{full:.2f}",
            "consumption": "850.0000", "consumptionFull": f"{full * 0.3:.2f}", "info": []}


def main():
    # parsing: strings to numbers, "-" to None, status kept, bad dates dropped
    rows = fa.parse_rows([fake_row("2026-11-17", 80.12), fake_row("2026-11-18", 79.5, "E"), {"gasDayStart": "nope"}])
    assert list(rows) == ["2026-11-17", "2026-11-18"], rows
    r = rows["2026-11-18"]
    assert r["full"] == 79.5 and r["wdr"] is None and r["status"] == "E" and r["wgv"] == 250.0, r

    # paging: three pages, every page fetched once, rows merged
    calls = []
    def fake_get(params, key):
        calls.append(params["page"])
        day = dt.date(2026, 1, 1) + dt.timedelta(days=params["page"] - 1)
        return {"last_page": 3, "total": 3, "data": [fake_row(day.isoformat(), 50 + params["page"])]}
    real = fa.api_get
    fa.api_get = fake_get
    try:
        got = fa.fetch_range({"country": "XX"}, "2026-01-01", "2026-01-03", "key")
    finally:
        fa.api_get = real
    assert calls == [1, 2, 3] and len(got) == 3 and got["2026-01-03"]["full"] == 53, (calls, got)

    # file round trip: write columnar JSON, read it back unchanged
    with tempfile.TemporaryDirectory() as tmp:
        fa.OUT = os.path.join(tmp, "storage.json")
        cfg = {"fetch_from": "2026-01-01", "display_from": "2026-01-01",
               "countries": [{"code": "XX", "name": "Fixture", "agsi": {"country": "XX"}, "eu_target": True}]}
        data = {"XX": dict(got)}
        fa.write(cfg, data)
        back = fa.load_existing()
        assert back["XX"] == data["XX"], (back, data)
        js = json.load(open(fa.OUT))
        assert js["series"]["XX"]["status"] == "CCC" and js["end"] == "2026-01-03", js
    print("fetch_agsi tests passed")


if __name__ == "__main__":
    main()
