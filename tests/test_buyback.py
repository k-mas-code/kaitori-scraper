from datetime import date

from arbitrage import buyback as bb


def _rows():
    def row(jan, source, day, price, condition="new"):
        return {"jan_code": jan, "source": source, "condition": condition, "scraped_date": day, "price": price}
    return [
        # JAN 1: rudeya 7 日前 9,000 → 今 10,000。kaitorishouten は今だけ 9,500 (7 日前以前の行なし)
        row("1", "rudeya", "2026-10-31", 9000), row("1", "rudeya", "2026-11-06", 10000),
        row("1", "kaitorishouten", "2026-11-05", 9500),
        # JAN 2: 最新は 8 日前が最後 (直近 7 日に行が無い) → 対象外
        row("2", "rudeya", "2026-10-30", 5000),
        # JAN 3: rudeya の中古の行は無視。kaitoriwiki は used でも新品扱い。7 日前以前の最新は 10/30 の 3,000
        row("3", "rudeya", "2026-10-30", 3000), row("3", "rudeya", "2026-11-07", 3000, "used"),
        row("3", "kaitoriwiki", "2026-11-07", 2800, "used"),
        # JAN 4: 7 日前以前の行が無い → prev_price None
        row("4", "rudeya", "2026-11-03", 7000),
    ]


def test_load_buyback_prices_with_prev(monkeypatch):
    calls = []

    def fetch_all(url, key, table, params):
        calls.append((table, params))
        return iter(_rows()) if table == "price_history" else iter([])

    monkeypatch.setattr(bb, "read_credentials", lambda: ("https://x", "k"))
    monkeypatch.setattr(bb, "_fetch_all", fetch_all)
    today = date(2026, 11, 7)
    best = bb.load_buyback_prices(today, with_prev=True)
    assert calls[0][1]["scraped_date"] == "gte.2026-10-24"      # 7 日前からさらに 7 日さかのぼる
    assert set(best) == {"1", "3", "4"}
    assert (best["1"].price, best["1"].prev_price, best["1"].rise) == (10000, 9000, 1000)
    assert (best["3"].price, best["3"].prev_price, best["3"].rise) == (2800, 3000, -200)
    assert best["4"].prev_price is None and best["4"].rise is None

    plain = bb.load_buyback_prices(today)
    assert calls[-2][1]["scraped_date"] == "gte.2026-10-31"
    assert set(plain) == {"1", "3", "4"} and all(b.prev_price is None for b in plain.values())
    assert plain["1"].price == 10000
