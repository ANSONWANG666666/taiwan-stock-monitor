"""Jev 每日上限與訊號紀錄簿的離線測試"""

import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import journal       # noqa: E402
import market_scan   # noqa: E402
import news_check    # noqa: E402
import test_market_scan as tms   # noqa: E402
from test_offline import FakeJevTransport, Resp, rss   # noqa: E402
from test_market_scan import env as scan_env            # noqa: E402,F401  全市場掃描的假環境

TZ = news_check.TZ


# ── Jev 每日上限 ─────────────────────────────────────────────────────
@pytest.fixture
def news_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(news_check, "CACHE_FILE", tmp_path / "jev_news_cache.json")
    monkeypatch.setattr(news_check, "BUDGET_FILE", tmp_path / "jev_budget.json")
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    items = [(f"台積電新聞{i} 營收年增", "經濟日報", 1) for i in range(5)]
    monkeypatch.setattr("requests.get", lambda *a, **k: Resp(rss(items)))
    return FakeJevTransport()


def test_budget_limits_calls_and_tracks_tokens(news_env, monkeypatch):
    monkeypatch.setattr(news_check, "DAILY_LIMIT", 3)
    r = news_check.check_news("2330", "台積電", transport=news_env)
    assert news_env.calls == 3 and r["jev_calls"] == 3 and r["budget_skipped"] == 2
    b = news_check.budget_status()
    assert b["calls"] == 3 and b["tokens"] == 2700            # 假 Jev 每次回報 900 tokens
    assert "另有 2 則新聞未查證" in news_check.format_news_block(r)

    # 額度用完：不再呼叫 Jev；已查過的 3 則走快取，所以仍有結論
    r2 = news_check.check_news("2330", "台積電", transport=news_env, use_symbol_cache=False)
    assert news_env.calls == 3 and r2["verdict"] == "confirmed_positive"


def test_budget_exhausted_with_nothing_cached(news_env, monkeypatch):
    monkeypatch.setattr(news_check, "DAILY_LIMIT", 1)
    news_check._budget_add(1, 100)
    r = news_check.check_news("2330", "台積電", transport=news_env)
    assert news_env.calls == 0 and r["verdict"] == "budget"
    assert "額度已用完" in news_check.format_news_block(r, pct=3.0)


def test_budget_resets_next_day(news_env, monkeypatch):
    monkeypatch.setattr(news_check, "DAILY_LIMIT", 2)
    news_check.BUDGET_FILE.write_text(json.dumps({"date": "2000-01-01", "calls": 999, "tokens": 1}))
    assert news_check.budget_remaining() == 2


def test_no_limit_by_default(news_env):
    assert news_check.budget_remaining() is None
    news_check.check_news("2330", "台積電", transport=news_env)
    assert news_env.calls == 5


# ── 訊號紀錄簿 ───────────────────────────────────────────────────────
def test_record_skipped_without_journal_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNAL_DIR", str(tmp_path / "nope"))
    journal.record("monitor", "2330", "台積電", price=1000)
    assert not (tmp_path / "nope").exists()


def test_record_and_backfill(tmp_path, monkeypatch):
    jd = tmp_path / "journal-data"
    jd.mkdir()
    monkeypatch.setenv("JOURNAL_DIR", str(jd))
    days = [f"2026-09-{d:02d}" for d in (1, 2, 3, 4, 5, 8, 9, 10)]
    journal.record("market", "3481", "群創", date="2026-09-01", price=20, pct=9.9,
                   ev={"score": 2, "vol_ratio": 2.5, "breakout": True},
                   news={"verdict": "rumor_driven", "net": 0.1, "n_relevant": 8,
                         "top": [{"title": "輝達點名玻璃基板", "rumor": True}]})
    journal.record("monitor", "2330", "台積電", date="2026-09-08", price=1000, alerts="TRADE_AMT")
    cache = {"days": days, "stocks": {
        "3481": {"bars": [[d, 20 + i, 1] for i, d in enumerate(days)]},     # 每天 +1
        "2330": {"bars": [[d, 1000, 1] for d in days]},
        journal.INDEX_CODE: {"bars": [[d, 20000 + 100 * i, 0] for i, d in enumerate(days)]},
    }}
    assert journal.backfill(cache) == 1                  # 只有群創的 5 日可以填；20 日、台積電都還沒到
    rows = list(csv.DictReader((jd / "signals_market.csv").open(encoding="utf-8-sig")))
    r = rows[0]
    assert r["news_verdict"] == "rumor_driven" and r["news_top_rumor"] == "1"
    assert r["ret_5d"] == "25.00"                        # 20 → 25
    assert r["idx_5d"] == "2.50" and r["excess_5d"] == "22.50"
    assert r["ret_20d"] == ""
    assert journal.backfill(cache) == 0                  # 已填過不重填
    mon = list(csv.DictReader((jd / "signals_monitor.csv").open(encoding="utf-8-sig")))
    assert mon[0]["alerts"] == "TRADE_AMT" and mon[0]["news_verdict"] == "unchecked"


def test_market_scan_writes_journal(tmp_path, monkeypatch, scan_env):
    # 沿用全市場掃描的假資料，並在 MI_INDEX 加上加權指數表格
    real_day = tms.market_day

    def with_index(date_str):
        js = real_day(date_str)
        if js.get("stat") == "OK":
            js["tables"][0] = {"title": "價格指數", "fields": ["指數", "收盤指數", "漲跌(+/-)"],
                               "data": [["寶島股價指數", "25,000", ""], ["發行量加權股價指數", "22,500.50", ""]]}
        return js

    monkeypatch.setattr(tms, "market_day", with_index)
    jd = tmp_path / "journal-data"
    jd.mkdir()
    monkeypatch.setenv("JOURNAL_DIR", str(jd))
    sent, _, _ = scan_env
    market_scan.main()
    cache = json.loads(Path("market_cache.json").read_text(encoding="utf-8"))
    assert cache["stocks"][journal.INDEX_CODE]["bars"][-1][1] == 22500.5
    assert "上市普通股 16 檔" in sent[0]                  # 加權指數不算進股票數
    rows = list(csv.DictReader((jd / "signals_market.csv").open(encoding="utf-8-sig")))
    assert len(rows) == 13
    assert sum(r["news_verdict"] != "unchecked" for r in rows) == 10   # 前 10 檔有查新聞
    assert rows[0]["code"] == "3481"

    monkeypatch.setenv("FORCE_SCAN", "1")               # 強制重跑不重複記錄
    market_scan.main()
    rows = list(csv.DictReader((jd / "signals_market.csv").open(encoding="utf-8-sig")))
    assert len(rows) == 13
