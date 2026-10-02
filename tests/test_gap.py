"""漏掉一天沒掃描時的回歸測試（晟銘電 3013，2026-10-01 漏掃）"""

import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import journal       # noqa: E402
import market_scan   # noqa: E402

TZ = market_scan.TZ

# 9/1–9/29 平盤 80 元、每天 1000 張；9/30 帶量漲到 86.8（3000 張）；10/01 漲到 94（+8.3%、5000 張）；10/02 回到 92.7（−1.4%、4500 張）
def day_data(iso):
    d = datetime.fromisoformat(iso).date()
    if d.weekday() >= 5:
        return None
    if iso == "2026-10-01":
        close, vol = 94.0, 5000
    elif iso == "2026-10-02":
        close, vol = 92.7, 4500
    elif iso == "2026-09-30":
        close, vol = 86.8, 3000
    else:
        close, vol = 80.0, 1000
    return {"3013": {"name": "晟銘電", "close": close, "volume": vol, "amount": close * vol * 1000}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(market_scan, "CACHE_FILE", tmp_path / "market_cache.json")
    monkeypatch.setattr(market_scan, "STATE_FILE", tmp_path / "market_scan_state.json")
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []

    def fetch(date_str):
        iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        calls.append(iso)
        return day_data(iso)

    monkeypatch.setattr(market_scan, "fetch_market_day", fetch)
    monkeypatch.setattr(market_scan, "fetch_industry_map", lambda: {})
    monkeypatch.setattr(market_scan, "check_news", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(market_scan, "send_telegram", lambda t: sent.append(t) or True)
    jd = tmp_path / "journal-data"
    jd.mkdir()
    monkeypatch.setenv("JOURNAL_DIR", str(jd))
    return calls, sent, jd


def test_missed_day_is_filled(env, monkeypatch):
    calls, sent, jd = env
    monkeypatch.setenv("SCAN_DATE", "20260930")
    market_scan.main()                       # 9/30 正常掃描（含回補）
    calls.clear()
    monkeypatch.setenv("SCAN_DATE", "20261002")
    market_scan.main()                       # 10/01 沒跑，直接掃 10/02
    assert "2026-10-01" in calls             # 自動補抓漏掉的 10/01
    cache = json.loads(Path("market_cache.json").read_text(encoding="utf-8"))
    assert "2026-10-01" in cache["days"]
    bars = cache["stocks"]["3013"]["bars"]
    assert [b[0] for b in bars[-3:]] == ["2026-09-30", "2026-10-01", "2026-10-02"]
    # 10/02 實際是下跌 −1.4%，不應該被當成 +6.8% 起漲
    assert "符合剛起漲 0 檔" in sent[-1]
    ev = market_scan.evaluate_signal(
        [{"date": b[0], "close": b[1], "volume": b[2]} for b in bars],
        datetime(2026, 10, 2, 14, 30, tzinfo=TZ))
    assert ev["pct"] == pytest.approx(-1.38, abs=0.01)


def test_stock_missing_previous_day_is_skipped(env, monkeypatch):
    calls, sent, jd = env
    monkeypatch.setenv("SCAN_DATE", "20260930")
    market_scan.main()
    cache = json.loads(Path("market_cache.json").read_text(encoding="utf-8"))
    hits = market_scan.scan(cache, "2026-09-30", day_data("2026-09-30"))
    assert hits                               # 有前一天資料時正常評分
    # 拿掉 9/29 這根（模擬停牌或漏抓）→ 不評分
    cache["stocks"]["3013"]["bars"] = [b for b in cache["stocks"]["3013"]["bars"] if b[0] != "2026-09-29"]
    assert market_scan.scan(cache, "2026-09-30", day_data("2026-09-30")) == []


def test_force_rerun_replaces_journal_rows(env, monkeypatch):
    calls, sent, jd = env
    monkeypatch.setenv("SCAN_DATE", "20260930")
    market_scan.main()
    path = jd / "signals_market.csv"
    n = len(list(csv.DictReader(path.open(encoding="utf-8-sig"))))
    assert n == 1                              # 9/30 晟銘電 +8.5% 帶量突破
    monkeypatch.setenv("FORCE_SCAN", "1")
    market_scan.main()
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    assert len(rows) == 1                      # 重跑後覆蓋，不重複


def test_journal_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNAL_DIR", str(tmp_path))
    journal.record("market", "3013", "晟銘電", date="2026-10-02", price=92.7)
    journal.record("market", "2330", "台積電", date="2026-10-01", price=1000)
    assert journal.remove("market", "2026-10-02") == 1
    rows = list(csv.DictReader((tmp_path / "signals_market.csv").open(encoding="utf-8-sig")))
    assert [r["code"] for r in rows] == ["2330"]
