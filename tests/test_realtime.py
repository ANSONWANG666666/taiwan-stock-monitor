"""模組二：盤中即時監控（證交所即時快照，假資料）"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from pattern_data import chuaner_bars, heifeiwu_bars, series, triangle_bars  # noqa: E402
import realtime_monitor as rm  # noqa: E402
import screener               # noqa: E402

TZ = rm.TZ
DAY = "20261005"


def T(h, m, s=0):
    return datetime(2026, 10, 5, h, m, s, tzinfo=TZ)


def ctx(**kw):
    base = {"prev_close": 100.0, "sum4": 400.0, "sum9": 900.0, "sum19": 1900.0, "ma5_prev": 99.0,
            "ma20_prev": 100.0, "above20": True, "vol20": 5000.0, "amount20": 5e8}
    return base | kw


def stock(tags=None, **kw):
    return {"name": "測試", "market": "TSE", "groups": ["半導體業"], "tags": tags or {"候選": {}}, "ctx": ctx(**kw)}


def q(z, tv, v, code="9999", y=100.0, h=None, l=None, a=None, b=None, d=DAY):
    return {"c": code, "n": "測試", "z": str(z), "tv": str(tv), "v": str(v), "y": str(y),
            "h": str(h or z), "l": str(l or z), "a": f"{a or z + 0.5}_", "b": f"{b or z - 0.5}_", "d": d}


def feed(mon, seq):
    out = []
    for t, item in seq:
        out += mon.on_quotes([item], t)
    return out


# ── 大單 ─────────────────────────────────────────────────────────
def test_large_buy_on_ask_alerts_with_net():
    mon = rm.Monitor({"9999": stock()})
    out = feed(mon, [(T(10, 0), q(100, 5, 1000)),
                     (T(10, 0, 20), q(100.5, 600, 1600, a=100.5, b=100))])      # 600 張外盤
    assert [a["signal"] for a in out] == ["大單敲進"]
    assert out[0]["net_large"] == pytest.approx(100.5 * 600 * 1000)


def test_large_buy_cooldown_30min():
    mon = rm.Monitor({"9999": stock()})
    seq = [(T(10, 0), q(100, 5, 1000)), (T(10, 0, 20), q(100.5, 600, 1600, a=100.5))]
    seq += [(T(10, 10), q(100.5, 600, 2200, a=100.5)), (T(10, 31), q(100.5, 600, 2800, a=100.5))]
    assert [a["signal"] for a in feed(mon, seq)] == ["大單敲進", "大單敲進"]


def test_first_snapshot_and_small_trades_ignored():
    mon = rm.Monitor({"9999": stock()})
    out = feed(mon, [(T(10, 0), q(100.5, 900, 1000, a=100.5)),                # 第一次輪詢不算
                     (T(10, 0, 20), q(100.5, 20, 1020, a=100.5)),               # 20 張 201 萬 < 500 萬
                     (T(10, 0, 40), q(100.5, 600, 1020, a=100.5))])             # 累計量沒變 = 同一筆
    assert out == []


def test_sell_side_only_reported_for_holdings():
    seq = [(T(10, 0), q(100, 5, 1000)), (T(10, 0, 20), q(99.5, 600, 1600, a=100, b=99.5))]
    assert feed(rm.Monitor({"9999": stock()}), seq) == []
    out = feed(rm.Monitor({"9999": stock({"持股": {}})}), seq)
    assert [a["signal"] for a in out] == ["大單賣出"] and out[0]["net_large"] < 0


@pytest.mark.parametrize("amount20, lots, expect", [
    (5e7, 40, True),        # 小型股門檻 300 萬：40 張 × 100 = 400 萬
    (5e8, 40, False),       # 中型 500 萬
    (2e9, 80, False),       # 大型 1000 萬：800 萬不算
    (2e9, 500, True),       # 但 ≥500 張一律算
])
def test_large_threshold_tiers(amount20, lots, expect):
    mon = rm.Monitor({"9999": stock(amount20=amount20)})
    out = feed(mon, [(T(10, 0), q(100, 5, 1000)), (T(10, 0, 20), q(100, lots, 1000 + lots, a=100))])
    assert bool(out) is expect


# ── 突破 ─────────────────────────────────────────────────────────
def test_limit_up_touch_once_per_day():
    mon = rm.Monitor({"9999": stock()})
    out = feed(mon, [(T(10, 0), q(110, 1, 100, h=110)), (T(11, 0), q(109, 1, 101, h=110)),
                     (T(12, 0), q(110, 1, 102, h=110))])
    assert [a["signal"] for a in out] == ["觸及漲停"]


def steady(mon, until, per5=100, price=99.0, start_v=0):
    """09:00 起每 5 分鐘 per5 張"""
    out, t, v = [], T(9, 0), start_v
    while t < until:
        out += mon.on_quotes([q(price, 1, v)], t)
        t += timedelta(minutes=5)
        v += per5
    return out, v


def test_cross_20ma_needs_volume_burst():
    # 昨收在 20MA 之下；今天 103 → 即時 20MA = (1900+103)/20 = 100.15
    mon = rm.Monitor({"9999": stock(above20=False)})
    _, v = steady(mon, T(10, 0))
    assert mon.on_quotes([q(103, 1, v + 120)], T(10, 5)) == []                # 量沒放大
    mon = rm.Monitor({"9999": stock({"穿山二龍": {"rally": 0.4}}, above20=False)})
    _, v = steady(mon, T(10, 0))
    out = mon.on_quotes([q(103, 1, v + 1000)], T(10, 5))                       # 5 分鐘 1000 張 ≈ 10 倍
    assert [a["signal"] for a in out] == ["穿二站回 20MA"] and "3 階段" in out[0]["reason"]


def test_triangle_breakout():
    tags = {"三角收斂": {"upper": 105.0, "kind": "對稱三角", "bars": 30}}
    mon = rm.Monitor({"9999": stock(tags)})
    _, v = steady(mon, T(10, 0), price=104)
    assert mon.on_quotes([q(104.5, 1, v + 1000)], T(10, 5)) == []             # 還沒過上緣
    out = mon.on_quotes([q(105.5, 1, v + 2500)], T(10, 10))
    assert [a["signal"] for a in out] == ["三角突破"]


def test_burst_falls_back_to_vol20_early():
    mon = rm.Monitor({"9999": stock(above20=False, vol20=2700)})              # 平均每 5 分鐘 50 張
    mon.on_quotes([q(99, 1, 0)], T(9, 0, 10))
    out = mon.on_quotes([q(103, 1, 400)], T(9, 6))                             # 開盤 6 分鐘 400 張 = 8 倍
    assert [a["signal"] for a in out] == ["站上 20MA"]


# ── 量縮拉回加碼 ─────────────────────────────────────────────────
def test_pullback_addon_for_holding():
    mon = rm.Monitor({"9999": stock({"持股": {}}, sum4=404.0)})               # 價 101 → 5MA = 101
    _, v = steady(mon, T(10, 30), per5=50, price=101)                          # 預估全日量 ≈ 2,700 < 5,000
    out = mon.on_quotes([q(101, 1, v, y=103)], T(10, 30))
    assert [a["signal"] for a in out] == ["量縮拉回加碼"] and "5MA" in out[0]["reason"]


def test_pullback_needs_low_volume_and_holding():
    mon = rm.Monitor({"9999": stock({"持股": {}}, sum4=404.0)})
    _, v = steady(mon, T(10, 30), per5=400, price=101)                         # 預估 ≈ 21,600 張
    assert mon.on_quotes([q(101, 1, v, y=103)], T(10, 30)) == []
    mon = rm.Monitor({"9999": stock(sum4=404.0)})                              # 不是持股
    _, v = steady(mon, T(10, 30), per5=50, price=101)
    assert mon.on_quotes([q(101, 1, v, y=103)], T(10, 30)) == []


# ── 黑飛舞觀察中 ─────────────────────────────────────────────────
def test_heifeiwu_watch():
    tags = {"黑飛舞": {"day0": "2026-10-02", "day0_volume": 10000.0}}
    mon = rm.Monitor({"9999": stock(tags, sum4=404.0)})                        # 101.5 → 5MA 101.1
    out, v = steady(mon, T(11, 0), per5=60, price=101.5)                       # 預估 ≈ 3,240 < 6,000
    out += mon.on_quotes([q(101.5, 1, v, l=101.0)], T(11, 0))
    assert [a["signal"] for a in out] == ["黑飛舞觀察中"] and "收盤後確認" in out[0]["reason"]   # 一天只報一次


def test_heifeiwu_watch_volume_too_big():
    tags = {"黑飛舞": {"day0": "2026-10-02", "day0_volume": 10000.0}}
    mon = rm.Monitor({"9999": stock(tags, sum4=404.0)})
    out, v = steady(mon, T(11, 0), per5=200, price=101.5)                      # 預估 ≈ 10,800
    assert out + mon.on_quotes([q(101.5, 1, v)], T(11, 0)) == []


def test_stale_holiday_data_ignored():
    mon = rm.Monitor({"9999": stock()})
    assert feed(mon, [(T(9, 1), q(110, 1, 100, h=110, d="20261002"))]) == []


# ── tick：讀觀察名單、推播 ─────────────────────────────────────
def test_tick_reads_watch_and_sends(tmp_path, monkeypatch):
    f = tmp_path / "candidates.json"
    f.write_text(json.dumps({"date": "2026-10-02", "watch": {"9999": stock()}}), encoding="utf-8")
    monkeypatch.setattr(rm, "WATCH_FILE", f)
    monkeypatch.setattr(rm, "_monitor", None)
    sent = []
    rm.tick(T(10, 0), fetch=lambda: [q(100, 5, 1000)], send=sent.append)
    out = rm.tick(T(10, 0, 20), fetch=lambda: [q(100.5, 600, 1600, a=100.5)], send=sent.append)
    assert len(out) == 1 and len(sent) == 1 and "盤中即時" in sent[0] and "大單敲進" in sent[0]


def test_load_watch_expired_or_missing(tmp_path):
    assert rm.load_watch(tmp_path / "none.json") == {}
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"date": "2026-09-01", "watch": {"9999": {}}}), encoding="utf-8")
    assert rm.load_watch(f, T(10, 0)) == {}


# ── screener 產生的觀察名單 ─────────────────────────────────────
def test_watch_tags_heifeiwu_after_day0():
    S = series(heifeiwu_bars()[:-1])                    # 只到 Day 0
    tags = screener.watch_tags(S)
    assert tags["黑飛舞"]["day0"] == S.date[-1] and tags["黑飛舞"]["day0_volume"] == 5000


def test_watch_tags_heifeiwu_gone_after_day1():
    assert "黑飛舞" not in screener.watch_tags(series(heifeiwu_bars()))


def test_watch_tags_chuaner_stage2():
    S = series(chuaner_bars()[:-1])                     # 第 2 階段：還在 20MA 之下
    assert "穿山二龍" in screener.watch_tags(S)


def test_watch_tags_triangle_upper():
    bars = triangle_bars()
    S = series(bars[:-1])                               # 收斂中、尚未突破
    t = screener.watch_tags(S)["三角收斂"]
    assert bars[-1][3] > t["upper"] > S.c[-1]           # 原本的突破日收盤確實在上緣之上


def test_live_context_matches_moving_averages():
    bars = chuaner_bars()
    S0, S1 = series(bars[:-1]), series(bars)
    c = screener.live_context(S0)
    nxt = S1.c[-1]
    assert (c["sum19"] + nxt) / 20 == pytest.approx(S1.ma20[-1])
    assert (c["sum4"] + nxt) / 5 == pytest.approx(S1.ma5[-1])
    assert (c["sum9"] + nxt) / 10 == pytest.approx(S1.ma10[-1])
    assert c["above20"] is False


# ── intraday_loop 排程 ───────────────────────────────────────────
def test_intraday_loop_calls_realtime_every_20s(monkeypatch):
    import intraday_loop as L
    import stock_check_once
    t = [T(13, 25)]
    monkeypatch.setattr(L, "now", lambda: t[0])
    monkeypatch.setattr(L, "sleep", lambda s: t.__setitem__(0, t[0] + timedelta(seconds=s)))
    monkeypatch.setattr(stock_check_once, "main", lambda: None)
    monkeypatch.setattr(stock_check_once, "fetch_stocks", lambda a, b: [{"d": DAY}])
    monkeypatch.setattr(L, "run_screener", lambda s: None)
    monkeypatch.setattr(L.stock_screener, "current_slot", lambda x: "1400" if x >= T(13, 50) else None)
    calls = []
    monkeypatch.setattr(L, "run_realtime", lambda: calls.append(t[0]))
    L.main()
    assert calls[0] == T(13, 25) and calls[-1] <= T(13, 30) and len(calls) == 16


# ── 精簡推播：預設只盯持股＋候選；大單監控、盤中選股可關閉 ───────
def test_build_watch_default_only_held_and_candidates():
    tri = series(triangle_bars()[:-1], code="1111")              # 收斂中、尚未突破
    held = series(chuaner_bars(), code="2222")
    cand = {"code": "3333", "pattern": "三角收斂", "score": 70, "groups": ["G"]}
    ser = {"1111": tri, "3333": series(triangle_bars(), code="3333")}
    w = screener.build_watch(ser, {"1111": ["G"], "3333": ["G"]}, [cand], {"2222": held})
    assert set(w) == {"2222", "3333"} and w["2222"]["tags"] == {"持股": {}}
    import config
    c = config.Config()
    c.RT_WATCH_FORMING = True
    w = screener.build_watch(ser, {"1111": ["G"], "3333": ["G"]}, [cand], {"2222": held}, c)
    assert "1111" in w and "三角收斂" in w["1111"]["tags"]


def test_intraday_loop_switches_off_big_order_and_screener(monkeypatch):
    import intraday_loop as L
    import stock_check_once
    t = [T(9, 0)]
    monkeypatch.setattr(L, "now", lambda: t[0])
    monkeypatch.setattr(L, "sleep", lambda s: t.__setitem__(0, t[0] + timedelta(seconds=s)))
    monkeypatch.setattr(L, "ENABLE_BIG_ORDER", False)
    monkeypatch.setattr(L, "ENABLE_SCREENER", False)
    calls = {"big": 0, "scr": 0, "rt": 0, "exit": 0}
    monkeypatch.setattr(stock_check_once, "main", lambda: calls.__setitem__("big", calls["big"] + 1))
    monkeypatch.setattr(stock_check_once, "fetch_stocks", lambda a, b: [{"d": DAY}])
    monkeypatch.setattr(L, "run_screener", lambda s: calls.__setitem__("scr", calls["scr"] + 1))
    monkeypatch.setattr(L.stock_screener, "current_slot",
                        lambda x: "0930" if x < T(12, 0) else ("1400" if x >= T(13, 50) else "1300"))
    monkeypatch.setattr(L, "run_realtime", lambda: calls.__setitem__("rt", calls["rt"] + 1))
    monkeypatch.setattr(L, "run_exit_check", lambda: calls.__setitem__("exit", calls["exit"] + 1))
    L.main()
    assert calls["big"] == 0 and calls["scr"] == 0 and calls["rt"] > 100 and calls["exit"] == 1
    assert t[0] <= T(13, 51)                                      # 13:50 後照常結束
