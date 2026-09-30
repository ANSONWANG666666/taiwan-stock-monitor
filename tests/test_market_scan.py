"""全市場掃描離線測試：模擬 TWSE MI_INDEX、Google News、Jev、Telegram"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import market_scan                      # noqa: E402
import news_check                       # noqa: E402
from test_offline import FakeJevTransport, Resp, rss, NEWS   # noqa: E402

TZ = market_scan.TZ
SCAN = datetime(2026, 9, 30, tzinfo=TZ)            # 週三
FIELDS = ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價",
          "收盤價", "漲跌(+/-)", "漲跌價差"]


def row(code, name, close, lots):
    shares = lots * 1000
    return [code, name, f"{shares:,}", "1", f"{shares * (close or 0):,.0f}", "0", "0", "0",
            f"{close:,.2f}" if close else "--", "", "0"]


def market_day(date_str):
    d = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=TZ)
    if d.weekday() >= 5 or d > SCAN or d.date() == datetime(2026, 9, 25).date():   # 9/25 假日
        return {"stat": "很抱歉，沒有符合條件的資料!"}
    today = d.date() == SCAN.date()
    rows = [
        # 箱型整理後帶量突破：+6%、量比 4 → 3 分
        row("3481", "群創", 106 if today else 100, 40000 if today else 10000),
        # 大量上漲但前面已經漲過一段又回檔、沒突破 → 1 分
        row("2330", "台積電", 1030 if today else (1100 if d.day % 2 else 1000), 60000 if today else 20000),
        # 成交值太小 → 不看
        row("1234", "小型股", 20.5 if today else 19, 100 if today else 20),
        # 平盤
        row("2317", "鴻海", 200, 30000),
        # ETF、權證等非普通股 → 排除
        row("0050", "元大台灣50", 190, 50000),
        row("03001P", "某權證", 1.2, 5000),
        # 今天沒成交
        row("9999", "停牌股", None, 0),
    ]
    # 另外 12 檔都符合，用來測試「前 10 檔逐檔、其餘摘要」
    for i in range(12):
        code = f"41{i:02d}"
        rows.append(row(code, f"起漲{i}", 52 if today else 50, 9000 + i * 100 if today else 3000))
    return {"stat": "OK", "tables": [{"title": "大盤統計", "fields": ["指數"], "data": []},
                                     {"title": "每日收盤行情", "fields": FIELDS, "data": rows}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(market_scan, "CACHE_FILE", tmp_path / "market_cache.json")
    monkeypatch.setattr(market_scan, "STATE_FILE", tmp_path / "market_scan_state.json")
    monkeypatch.setattr(news_check, "CACHE_FILE", tmp_path / "jev_news_cache.json")
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("SCAN_DATE", "20260930")
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    sent, mi_calls, jev = [], [], FakeJevTransport()

    def fake_get(url, params=None, headers=None, timeout=None):
        if "MI_INDEX" in url:
            mi_calls.append(params["date"])
            return Resp(js=market_day(params["date"]))
        if "news.google.com" in url:
            return Resp(rss(NEWS.get("台積電") if "3481" in url else []))
        raise AssertionError(url)

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr(market_scan, "send_telegram", lambda t: sent.append(t) or True)
    real = news_check.check_news
    monkeypatch.setattr(market_scan, "check_news", lambda s, n, **kw: real(s, n, transport=jev, **kw))
    return sent, mi_calls, jev


def test_filters_and_parsing():
    assert market_scan.is_common_stock("3481")
    assert not market_scan.is_common_stock("0050")
    assert not market_scan.is_common_stock("03001P")
    f, rows = market_scan._find_stock_table(market_day("20260930"))
    assert "收盤價" in f and len(rows) == 19
    old = {"stat": "OK", "fields9": FIELDS, "data9": [row("3481", "群創", 10, 1)]}   # 舊版格式
    assert market_scan._find_stock_table(old)[1]


def test_full_scan(env):
    sent, mi_calls, jev = env
    market_scan.main()
    # 今天 1 次 + 回補 25 個交易日（跳過週末與 9/25 假日）
    assert mi_calls[0] == "20260930" and len(mi_calls) >= 26
    cache = json.loads(Path("market_cache.json").read_text(encoding="utf-8"))
    assert len(cache["days"]) == 26
    assert "0050" not in cache["stocks"] and "03001P" not in cache["stocks"] and "9999" not in cache["stocks"]

    summary = sent[0]
    assert "符合剛起漲 13 檔" in summary           # 群創 + 12 檔
    assert "台積電" not in summary and "鴻海" not in summary and "小型股" not in summary
    assert summary.count("•") == 3                 # 13 − 前 10 檔 = 3 檔只列摘要
    detail = sent[1:]
    assert len(detail) == 10
    assert detail[0].startswith("📡") and "群創（3481）" in detail[0]   # 3 分、量比最高排第一
    assert "新聞查證（Jev）" in detail[0]
    assert "查無相關新聞" in detail[1]


def test_no_double_push_and_incremental(env, monkeypatch):
    sent, mi_calls, _ = env
    market_scan.main()
    n_sent, n_calls = len(sent), len(mi_calls)
    market_scan.main()                               # 同一天再跑：略過
    assert len(sent) == n_sent and len(mi_calls) == n_calls
    monkeypatch.setenv("FORCE_SCAN", "1")
    market_scan.main()                               # 強制重跑：只抓今天，不再回補
    assert len(sent) == 2 * n_sent and len(mi_calls) == n_calls + 1


def test_holiday_or_not_published(env, monkeypatch):
    sent, _, _ = env
    monkeypatch.setenv("SCAN_DATE", "20260925")
    market_scan.main()
    assert sent == []
