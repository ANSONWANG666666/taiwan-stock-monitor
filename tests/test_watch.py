"""觀察名單：提醒規則、冷卻、市場溫度、收盤後／盤前流程、週覆盤、Jev 收錄過濾、門檻回測（全部離線）"""

import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx2
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import history          # noqa: E402
import journal          # noqa: E402
import watch_alert as wa        # noqa: E402
import watch_backtest as wb     # noqa: E402
import watch_intake as wi       # noqa: E402
import watch_review as wr       # noqa: E402
import weekly_report    # noqa: E402

TZ = wa.TZ
NO_EXT = {"vix": 15.0, "vix_pct": 30.0, "tnx": 4.5, "tnx_pct": 60.0, "hy": 3.0, "hy_pct": 50.0}


# ── 假資料 ───────────────────────────────────────────────────────
def path_series(n, start=100.0, drift=0.0005, vol=0.015, seed=0, tail=None):
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, n)
    if tail is not None:
        r[-len(tail):] = tail
    return start * np.exp(np.cumsum(r))


def market_df(n=320, special=None, end="2026-10-16"):
    """長表：IX0001 + 30 檔普通股；special = {代號: 收盤序列}"""
    days = pd.bdate_range(end=end, periods=n).strftime("%Y-%m-%d")
    rows = []
    idx = path_series(n, 20000, 0.0008, 0.01, seed=1)
    for d, c in zip(days, idx):
        rows.append({"date": d, "code": "IX0001", "name": "加權指數", "market": "IDX", "open": c, "high": c,
                     "low": c, "close": c, "volume": 0, "amount": 0.0, "ref": np.nan})
    codes = {f"{1100 + i}": path_series(n, 50, 0.0003 if i % 2 else -0.0003, 0.02, seed=10 + i) for i in range(30)}
    codes["2330"] = path_series(n, 500, 0.0006, 0.015, seed=99)
    codes.update(special or {})
    for code, cl in codes.items():
        vol = np.full(n, 1000.0)
        prev = np.r_[cl[0], cl[:-1]]
        for d, c, p, v in zip(days, cl, prev, vol):
            rows.append({"date": d, "code": code, "name": {"2330": "台積電", "3017": "奇鋐"}.get(code, f"股{code}"),
                         "market": "TSE", "open": c, "high": c * 1.005, "low": c * 0.995, "close": round(c, 2),
                         "volume": v, "amount": c * v * 1000, "ref": round(p, 2)})
    return history.adjust(pd.DataFrame(rows))


def ind(cl, volume=None):
    n = len(cl)
    s = pd.DataFrame({"Close": cl, "High": cl, "Low": cl, "Volume": volume if volume is not None else np.full(n, 1000.0),
                      "Raw": cl, "Ref": np.r_[cl[0], cl[:-1]], "name": "測試", "market": "TSE"},
                     index=pd.bdate_range(end="2026-10-16", periods=n))
    return wa.add_indicators(s)


SETTINGS = wa.DEFAULT_SETTINGS


# ── 規則 ─────────────────────────────────────────────────────────
def test_ladder_levels_and_vol_scaling():
    base = list(np.linspace(50, 100, 280))                      # 一路上漲，200MA 遠在下方
    s = ind(np.array(base + [100 * 0.89]))                       # 自 20 日高點回檔 11%
    a = wa.evaluate("9999", s, SETTINGS, 1.0)
    assert [(x.kind, x.level) for x in a if x.kind == "drawdown"] == [("drawdown", 2)]
    assert "技術修正" in a[0].reason
    a = wa.evaluate("9999", s, SETTINGS, 1.5)                    # 高波動股：門檻 7.5／15 → 第一級
    dd = [x for x in a if x.kind == "drawdown"][0]
    assert dd.level == 1 and "×1.50" in dd.reason


def test_level3_one_year_drawdown_beats_lower_levels():
    cl = np.r_[np.linspace(100, 150, 200), np.linspace(150, 110, 80)]     # 一年高點回檔 26%
    a = [x for x in wa.evaluate("9999", ind(cl), SETTINGS, 1.0) if x.kind == "drawdown"]
    assert len(a) == 1 and a[0].level == 3 and "一年高點" in a[0].reason


def test_break_ma200_only_when_just_crossed():
    cl = np.r_[np.full(250, 100.0), [101, 102, 103, 96]]
    a = [x for x in wa.evaluate("9999", ind(cl), SETTINGS, 1.0) if x.kind == "drawdown"]
    assert a and a[0].level == 3 and "200 日均線" in a[0].reason
    below_long = np.r_[np.full(250, 100.0), np.full(10, 95.0)]          # 一直在下面，不是「剛跌破」
    a = [x for x in wa.evaluate("9999", ind(below_long), SETTINGS, 1.0) if "200 日均線" in x.reason]
    assert not a


def test_volume_spike_squeeze_and_hot():
    n = 300
    cl = np.r_[np.linspace(10, 100, n - 20), np.full(20, 100.0) + np.linspace(0, 0.2, 20)]   # 正乖離 > 30%
    vol = np.full(n, 1000.0)
    vol[-1] = 3500
    kinds = {x.kind for x in wa.evaluate("9999", ind(cl, vol), SETTINGS, 1.0)}
    assert {"volume_spike", "squeeze", "ma200_hot"} <= kinds


def test_invalidation_rules():
    cl = np.r_[np.linspace(100, 150, 200), np.linspace(150, 100, 60)]
    s = ind(cl)
    item = {"code": "9999", "invalidation": {"rules": [
        {"type": "close_below", "value": 105}, {"type": "close_below", "value": None},
        {"type": "break_ma", "days": 200}, {"type": "drawdown_from_high", "pct": 30},
        {"type": "foreign_net_sell_streak", "days": 3}]}}
    sell = lambda code, days: pd.Series([5, -1, -2, -3])
    hits = wa.check_invalidation(item, s, sell)
    assert any("跌破關鍵支撐 105" in h for h in hits)
    assert any("200 日均線" in h for h in hits)
    assert any("回檔 33" in h for h in hits)
    assert any("外資連續 3" in h for h in hits)
    assert not wa.check_invalidation(item, s, lambda c, d: pd.Series([1, -1, -2]))[3:]


def test_cooldown_and_escalation():
    now = datetime(2026, 10, 16, 15, 20, tzinfo=TZ)
    a1 = wa.Alert("2330", "台積電", "drawdown", 1, "x", 1000, -1, "2026-10-16")
    a2 = wa.Alert("2330", "台積電", "drawdown", 2, "x", 950, -5, "2026-10-17")
    cd = {}
    assert wa.should_send(cd, a1, 72, now)
    wa.mark_sent(cd, a1, now)
    assert not wa.should_send(cd, a1, 72, now + timedelta(hours=24))     # 同級 72 小時內不重發
    assert wa.should_send(cd, a2, 72, now + timedelta(hours=24))         # 升級立即通知
    assert wa.should_send(cd, a1, 72, now + timedelta(hours=73))


# ── 市場溫度 ─────────────────────────────────────────────────────
def test_gate_scores_and_labels():
    t = {"tw_dev": 25.0, "breadth": 49.0, "otc_dev": 0.0, "vix_pct": 14.0, "hy_pct": 86.0, "tnx_pct": 98.0,
         "tw_px": 49000.0, "tw_dd": -1.0}
    g = wa.gate(t)
    assert g["scores"]["加權指數200MA乖離"] == 0 and g["scores"]["櫃買指數200MA乖離"] == 50
    assert g["gate_score"] == pytest.approx((51 + 0 + 50 + 14 + 86 + 2) / 6)
    assert g["gate"] == "暫停加碼、保留現金" and g["heat"] == "偏熱"
    g2 = wa.gate(t, "risk_off")
    assert g2["scores"]["美國高收益債利差分位"] == pytest.approx(14)
    g3 = wa.gate({"tw_dev": float("nan"), "breadth": float("nan")})          # 全部缺資料
    assert g3["gate"] == "資料不足"
    g4 = wa.gate({"tw_dev": 0.0, "breadth": 50.0, "tw_px": 1.0, "tw_dd": 0.0})   # 外部資料抓不到
    assert g4["n_valid"] == 2 and "只有 2/6 項有資料" in wa.temp_line(g4)
    assert "項有資料" not in wa.temp_line(g)


def test_breadth_counts_only_stocks_with_200_bars():
    df = market_df(n=260)
    b = wa.breadth(df)
    assert 0 <= b <= 100
    assert np.isnan(wa.breadth(market_df(n=150)))


# ── 收盤後流程 ───────────────────────────────────────────────────
WATCH_YAML = """
settings:
  jev_news_for: [drawdown2, drawdown3, invalidation, volume_spike]
watchlist:
  - code: "3017"
    name: 奇鋐
    added: 2026-10-01
    source: 自行研究
    thesis: AI 伺服器水冷板出貨占營收由 2 成升至 4 成
    invalidation:
      text: [水冷滲透率不如預期]
      rules:
        - {type: close_below, value: 1}
  - code: "8888"
    name: 不存在
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "watchlist.yaml").write_text(WATCH_YAML, encoding="utf-8")
    (tmp_path / "journal").mkdir()
    monkeypatch.setattr(wa, "WATCHLIST_FILE", tmp_path / "watchlist.yaml")
    monkeypatch.setattr(wa, "COOLDOWN_FILE", tmp_path / "state" / "watch_cooldown.json")
    monkeypatch.setattr(wa, "TEMP_FILE", tmp_path / "state" / "market_temp.json")
    monkeypatch.setattr(wa, "CARD_STATE_FILE", tmp_path / "card.json")
    monkeypatch.setattr(wa, "yf_close", lambda *a, **k: pd.Series(dtype=float))
    monkeypatch.setenv("JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    return tmp_path


def test_run_post_pushes_card_with_news_and_records_journal(env):
    n = 300
    kiq = np.r_[path_series(n - 1, 300, 0.001, 0.01, seed=5), [0]]
    kiq[-1] = kiq[-21:-1].max() * 0.88                                      # 回檔 12% → 第二級
    df = market_df(n, special={"3017": kiq})
    sent, asked = [], []
    news = lambda a, s: (asked.append(a.jev_key) or ({"verdict": "confirmed_negative", "net": -0.3, "top": []},
                                                     "\n📰 <b>新聞查證（Jev）</b>：🔻 有證實的利空消息"))
    now = datetime(2026, 10, 16, 15, 20, tzinfo=TZ)
    res = wa.run_post(datetime(2026, 10, 16).date(), now=now, df=df, ext=NO_EXT, news=news,
                      foreign=lambda c, d: pd.Series(dtype=float), send=lambda m: sent.append(m) or True)
    assert res["sent"] >= 1
    card = next(m for m in sent if "第二級" in m)
    for text in ("奇鋐（3017）", "核心投資理由", "水冷板", "推翻條件", "水冷滲透率不如預期",
                 "收盤跌破 1.00", "新聞查證（Jev）", "市場溫度", "不是買進或賣出訊號"):
        assert text in card, text
    assert "drawdown2" in asked
    rows = list(csv.DictReader(open(env / "journal" / "signals_watch.csv", encoding="utf-8-sig")))
    assert rows[0]["source"] == "watch" and rows[0]["slot"] == "L2"
    assert rows[0]["news_verdict"] == "confirmed_negative"
    temp = json.loads((env / "state" / "market_temp.json").read_text(encoding="utf-8"))
    assert temp["asof"] == "2026-10-16" and "gate_score" in temp

    # 同一個資料日再跑（備援排程）→ 略過；強制重跑 → 冷卻擋下
    again = wa.run_post(datetime(2026, 10, 16).date(), now=now, df=df, ext=NO_EXT, news=news,
                        send=lambda m: sent.append(m) or True)
    assert again["skipped"] == "done"
    forced = wa.run_post(datetime(2026, 10, 16).date(), now=now + timedelta(hours=1), df=df, ext=NO_EXT,
                         news=news, force=True, send=lambda m: sent.append(m) or True)
    assert forced["sent"] == 0


def test_run_post_without_watchlist_still_saves_temperature(env):
    (env / "watchlist.yaml").write_text("watchlist:\n", encoding="utf-8")
    res = wa.run_post(datetime(2026, 10, 16).date(), df=market_df(300), ext=NO_EXT, send=lambda m: True)
    assert res["sent"] == 0 and (env / "state" / "market_temp.json").exists()


def test_news_only_for_configured_kinds(monkeypatch):
    a = wa.Alert("2330", "台積電", "drawdown", 1, "x", 1, 0, "d")
    assert wa._news(a, {"jev_news_for": ["drawdown2"]}) == (None, "")


# ── 盤前溫度卡 ───────────────────────────────────────────────────
def test_run_pre_once_per_day_and_skips(env):
    wa._write_json(wa.TEMP_FILE, {"asof": "2026-10-15", "tw_px": 49000.0, "tw_dev": 23.0, "tw_dd": -1.3,
                                  "breadth": 49.0, "otc_px": float("nan"), "otc_dev": float("nan"), "otc_dd": float("nan")})
    sent = []
    send = lambda m: sent.append(m) or True
    t = datetime(2026, 10, 16, 8, 52, tzinfo=TZ)                      # 週五 08:52
    assert wa.run_pre(now=t, ext=NO_EXT, holiday=lambda d: False, send=send)
    assert "開盤前市場溫度" in sent[0] and "閘門分項" in sent[0] and "櫃買指數200MA乖離：—" in sent[0]
    assert not wa.run_pre(now=t, ext=NO_EXT, holiday=lambda d: False, send=send)          # 同一天不重推
    assert not wa.run_pre(now=t.replace(day=17), ext=NO_EXT, holiday=lambda d: False, send=send)   # 週六
    assert not wa.run_pre(now=t.replace(day=19, hour=10), ext=NO_EXT, holiday=lambda d: False, send=send)
    assert not wa.run_pre(now=t.replace(day=19), ext=NO_EXT, holiday=lambda d: True, send=send)    # 休市
    assert len(sent) == 1


# ── 週覆盤 ───────────────────────────────────────────────────────
def test_weekly_review_honest_vs_benchmark(env):
    n = 300
    df = market_df(n, special={"3017": path_series(n, 300, -0.002, 0.01, seed=7)})
    items = wa.load_watchlist()["watchlist"]
    bench = pd.Series(np.linspace(100, 130, 400), index=pd.bdate_range(end="2026-10-16", periods=400))
    journal.record("watch", "3017", "奇鋐", slot="L2", price=float(df[df.code == "3017"].close.iloc[-3]),
                   date=str(df.date.unique()[-3]), alerts="回檔")
    msg = wr.build(items, df, bench, "0050", datetime(2026, 10, 16).date(), {},
                   foreign=lambda c, d: pd.Series(dtype=float))
    assert "選股目前沒有帶來超額報酬" in msg
    assert "奇鋐 3017【L2】" in msg
    assert "「水冷滲透率不如預期」本週是否發生" in msg
    assert "不存在" in msg and "沒有填推翻條件" in msg


def test_weekly_report_has_watch_section(tmp_path):
    rows = [{"source": "watch", "date": "2026-09-01", "code": "3017", "name": "奇鋐", "slot": "L2",
             "excess_5d": "2.0", "excess_20d": "-1.0"}]
    msg = weekly_report.build_report(rows, datetime(2026, 10, 16, tzinfo=TZ))
    assert "觀察名單提醒" in msg and "第二級 回檔" in msg


# ── Jev 收錄過濾 ─────────────────────────────────────────────────
TABLE = {"3017": "奇鋐", "2603": "長榮", "2330": "台積電"}
TRANSCRIPT = ("主持人：今天先聊航運，長榮最近很熱。\n"
              "來賓：我比較看好奇鋐，水冷板出貨占營收從兩成升到四成，毛利率比氣冷高 5 個百分點。\n"
              "來賓：順便講一下台積電很穩啦。")


def test_find_mentions_and_segments():
    m = wi.find_mentions(TRANSCRIPT, TABLE)
    assert set(m) == {"3017", "2603", "2330"}
    seg = wi.segment_for(wi.sentences(TRANSCRIPT), m["3017"])
    assert "水冷板" in seg


def fake_intake_answers(state):
    seg = state["transcript"]
    good = "水冷板" in seg and state["company"]["ticker"] == "3017"
    return {"substantive": {"type": "noul", "noul": 0.9 if good else 0.1},
            "checkable": {"type": "noul", "noul": 0.85 if good else 0.2},
            "stance": {"type": "choice", "choice": "bullish" if good else "neutral",
                       "probabilities": {"bullish": 0.8, "bearish": 0.05, "neutral": 0.15}, "confidence": 0.8}}


def test_intake_with_jev(monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr("news_check.BUDGET_FILE", tmp_path / "b.json")
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body)
        return httpx2.Response(200, json={"model": "jev-mock", "answers": fake_intake_answers(body["state"]),
                                          "usage": {"input_tokens": 100}})

    res = wi.screen_text(TRANSCRIPT, TABLE, transport=httpx2.MockTransport(handler))
    assert res["mode"] == "jev" and len(calls) == 3
    acc = [r["code"] for r in res["results"] if r["accepted"]]
    assert acc == ["3017"]
    msg = wi.format_message(res, "測試節目")
    assert "奇鋐 3017" in msg and "看多" in msg and "長榮（沒有實質論點" in msg and "<pre>" in msg


def test_intake_without_key_uses_keywords(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    res = wi.screen_text(TRANSCRIPT, TABLE)
    assert res["mode"] == "heuristic"
    assert [r["code"] for r in res["results"] if r["accepted"]] == ["3017"]
    assert "關鍵字規則" in wi.format_message(res, "x")


# ── 門檻回測 ─────────────────────────────────────────────────────
def test_backtest_vol_scaled_suggestions():
    n = 2500
    idx = pd.bdate_range(end="2026-10-16", periods=n)

    def loader(symbol, years):
        spec = {"2330.TW": (0.015, 1), "3017.TW": (0.03, 2), "^TWII": (0.01, 3)}[symbol]
        cl = path_series(n, 100, 0.0004, spec[0], seed=spec[1])
        return pd.DataFrame({"Close": cl, "Volume": np.full(n, 1000.0)}, index=idx)

    res = wb.run(["2330", "3017"], "2330", 10, (5, 10, 20), loader=loader, markets={})
    s = {x["股票"]: x for x in res["suggestions"]}
    assert s["2330"]["L2 20日回檔"] == 10.0
    assert 1.6 < s["3017"]["波動倍數"] < 2.4 and s["3017"]["L2 20日回檔"] > 15
    assert "基準：任意一天" in res["markdown"] and "不是買進訊號" in res["markdown"]
    assert wb.yahoo_symbol("6488", {"6488": "OTC"}) == "6488.TWO"
