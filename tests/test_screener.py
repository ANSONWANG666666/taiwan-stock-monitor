"""階段 2：穿山二龍、黑飛舞、三角收斂、族群強弱、領頭羊"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from pattern_data import (chuaner_bars, flat, frame, heifeiwu_bars, ramp, series,  # noqa: E402
                          triangle_bars)
import config     # noqa: E402
import screener   # noqa: E402


def signals(S, det):
    return [(i, s) for i in range(S.n) if (s := det(S, i))]


# ── 穿山二龍三階段 ───────────────────────────────────────────────
def test_chuaner_three_stages():
    S = series(chuaner_bars())
    hits = signals(S, screener.detect_chuaner)
    assert len(hits) == 1 and hits[0][0] == S.n - 1
    s = hits[0][1]
    assert s["entry_type"] == "收盤進場"
    assert s["a_rally"] == pytest.approx(140 * 1.01 / (100 * 0.99) - 1, rel=1e-3)   # 第1階段 低→高
    assert s["swing_high"] > s["base"] > s["pattern_low"]


def test_chuaner_limit_up_enters_next_open():
    s = screener.detect_chuaner(series(chuaner_bars(limit=True)), 85)
    assert s and s["entry_type"] == "次日開盤進場"


def test_chuaner_needs_30pct_rally():
    assert not signals(series(chuaner_bars(rally_to=125)), screener.detect_chuaner)


def test_chuaner_needs_strong_candle():
    assert not signals(series(chuaner_bars(final_close=120.5)), screener.detect_chuaner)


def test_chuaner_stage3_pct_configurable():
    cfg = config.Config()
    cfg.CHUANER_STAGE3_PCT = 0.08                       # 6.8% 不夠
    assert screener.detect_chuaner(series(chuaner_bars()), 85, cfg) is None


# ── 黑飛舞 ───────────────────────────────────────────────────────
def test_heifeiwu_day0_to_day1():
    S = series(heifeiwu_bars())
    hits = signals(S, screener.detect_heifeiwu)
    assert len(hits) == 1 and hits[0][0] == S.n - 1
    assert hits[0][1]["day0"] == S.date[S.n - 2] and hits[0][1]["entry_type"] == "收盤進場"


def test_heifeiwu_no_volume_shrink_no_signal():
    assert not signals(series(heifeiwu_bars(day1_vol_ratio=0.8)), screener.detect_heifeiwu)


def test_heifeiwu_break_ma5_invalidates():
    assert not signals(series(heifeiwu_bars(break_ma5=True)), screener.detect_heifeiwu)


def test_heifeiwu_needs_burst_volume():
    bars = heifeiwu_bars()
    o, h, l, c, v = bars[-2]
    bars[-2] = (o, h, l, c, 1500)                       # Day0 只有 1.5 倍均量
    assert not signals(series(bars), screener.detect_heifeiwu)


# ── 三角收斂 ─────────────────────────────────────────────────────
@pytest.mark.parametrize("kind,label", [("sym", "對稱三角"), ("asc", "上升三角")])
def test_triangle_breakout(kind, label):
    S = series(triangle_bars(kind))
    s = screener.detect_triangle(S, S.n - 1)
    assert s and s["tri_kind"] == label and s["bars"] >= 15
    assert s["a_amp"] > 0 and s["base"] == S.c[-1]


def test_triangle_needs_volume():
    S = series(triangle_bars("sym", breakout_vol=1000))
    assert screener.detect_triangle(S, S.n - 1) is None


def test_triangle_not_in_trend():
    S = series(flat(40) + ramp(60, 100, 160) + [(160, 166, 159, 165, 4000)])
    assert screener.find_triangle(S, S.n - 1) is None


def test_longer_triangle_scores_higher():
    S1, S2 = series(triangle_bars("sym", n=50)), series(triangle_bars("sym", n=30))
    long_, short = screener.detect_triangle(S1, S1.n - 1), screener.detect_triangle(S2, S2.n - 1)
    assert long_ and short and long_["bars"] > short["bars"] and long_["score"] > short["score"]


# ── 目標價（兩種 A）────────────────────────────────────────────────
def test_targets_both_modes():
    s = {"base": 100.0, "a_rally": 0.4, "a_amp": 0.2}
    assert screener.targets(s) == {"rally": pytest.approx(120.0), "amp": pytest.approx(110.0)}


# ── 族群強弱、領頭羊 ─────────────────────────────────────────────
def test_group_strength_threshold():
    groups = {"大族群": [f"A{i}" for i in range(20)], "小族群": ["B1", "B2", "B3", "B4", "B5"],
              "太小": ["C1", "C2"]}
    snap = {f"A{i}": {"above20": i < 5, "rs5": -1} for i in range(20)}        # 5/20 = 25%
    snap |= {f"B{i}": {"above20": i <= 4, "rs5": -1} for i in range(1, 6)}   # 4/5
    snap |= {"C1": {"above20": True, "rs5": 1}, "C2": {"above20": True, "rs5": 1}}
    st = screener.group_strength(groups, snap)
    assert not st["大族群"]["strong"]           # ≥4 檔但不到 30%
    assert st["小族群"]["strong"]
    assert "太小" not in st


def _stock(code, bars):
    return screener.prepare(__import__("history").adjust(frame(bars, code=code, name=code)))


def test_leader_ranking_and_rotation():
    # A 最早創新高且漲最多 → 領頭羊；之後 A 停滯、B 創新高 → 換手
    a = _stock("A", flat(60) + ramp(20, 100, 150) + flat(10, 149))
    b = _stock("B", flat(60) + ramp(28, 100, 120) + ramp(2, 121, 160))
    c = _stock("C", flat(60) + ramp(30, 100, 110))
    tx = _stock("IX", flat(90, 20000))
    series = {"A": a, "B": b, "C": c}
    ranked = screener.rank_leaders(["A", "B", "C"], series, tx)
    assert ranked[-1] == "C"
    state = {"G": {"code": "A", "date": "x"}}
    msg = screener.check_rotation("G", ranked[0], series, state, ["A", "B", "C"])
    assert state["G"]["code"] == "B" and msg and "換成 B" in msg


def _e2e_frames(index_bars=None):
    # 4 檔同族群都站上 20MA（強勢），其中 1 檔出現黑飛舞
    frames = [frame(heifeiwu_bars(), code="1111", name="飛舞")]
    for k, code in enumerate(("2222", "3333", "4444")):
        frames.append(frame(flat(60) + ramp(22, 100, 115 + k), code=code, name=code))
    frames.append(frame(index_bars or flat(82, 20000), code="IX0001", name="加權指數"))
    df = pd.concat(frames)
    df["amount"] = df["amount"] * 100                    # 通過流動性門檻
    industry = {c: {"name": c, "market": "TSE", "industry": "測試族群"} for c in ("1111", "2222", "3333", "4444")}
    return df, industry


def test_screen_default_heifeiwu_is_observe_only(tmp_path, monkeypatch):
    df, industry = _e2e_frames()
    monkeypatch.setattr(screener, "CONCEPT_FILE", tmp_path / "none.yaml")
    res = screener.screen(df, industry, {}, held={"1111"})
    assert res["strong_groups"]["測試族群"]["strong_n"] == 4
    assert res["candidates"] == [] and [s["pattern"] for s in res["observe"]] == ["黑飛舞"]
    msg = screener.format_report(res)
    assert "觀察用" in msg and "飛舞 1111" in msg and "【加碼】" not in msg
    assert "黑飛舞" not in res["watch"].get("2222", {}).get("tags", {})


def test_screen_active_pattern_with_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(screener, "CONCEPT_FILE", tmp_path / "none.yaml")
    cfg = config.Config()
    cfg.ACTIVE_PATTERNS = "黑飛舞"
    # 大盤平盤（收盤等於 20MA）→ 大盤濾網擋下
    df, industry = _e2e_frames()
    res = screener.screen(df, industry, {}, held={"1111"}, cfg=cfg)
    assert res["candidates"] == [] and res["held_back"]["market"] == 1 and not res["market_ok"]
    assert "大盤在 20MA 之下" in screener.format_report(res, cfg)
    # 大盤緩漲 → 站上 20MA，且個股跑贏大盤 → 列進場（持股標「加碼」）
    df, industry = _e2e_frames(ramp(82, 20000, 20400))
    res = screener.screen(df, industry, {}, held={"1111"}, cfg=cfg)
    c = res["candidates"]
    assert res["market_ok"] and len(c) == 1 and c[0]["pattern"] == "黑飛舞" and c[0]["action"] == "加碼"
    msg = screener.format_report(res, cfg)
    assert "型態選股" in msg and "【加碼】" in msg and "測試族群" in msg



# ── 分流：進場／觀察、每日前 N 檔、族群從嚴 ─────────────────────
def _sig(code, pattern, score, groups=("A",)):
    return {"code": code, "pattern": pattern, "score": score, "groups": list(groups)}


def test_split_signals():
    cfg = config.Config()
    cfg.SCREEN_TOP_N = 2
    kept = [_sig("1", "三角收斂", 90), _sig("2", "三角收斂", 80, ("B",)), _sig("3", "三角收斂", 70),
            _sig("4", "三角收斂", 60), _sig("5", "穿山二龍", 99), _sig("6", "黑飛舞", 50)]
    e, o, hb = screener.split_signals(kept, {"A"}, True, cfg)
    assert [s["code"] for s in e] == ["1", "3"] and [s["code"] for s in o] == ["5", "6"]
    assert hb == {"market": 0, "group": 1, "top_n": 1}
    e, o, hb = screener.split_signals(kept, {"A"}, False, cfg)
    assert e == [] and hb["market"] == 3
    cfg.SCREEN_MARKET_FILTER = cfg.SCREEN_STRICT_GROUP = False
    e, _, _ = screener.split_signals(kept, set(), False, cfg)
    assert [s["code"] for s in e] == ["1", "2"]


def test_group_strength_strict():
    groups = {"G": list("ABCD")}
    snap = {c: {"above20": True, "rs5": -0.01} for c in "ABCD"}
    assert screener.group_strength(groups, snap)["G"]["strong"]
    assert not screener.group_strength(groups, snap, strict=True)["G"]["strong"]
    snap = {c: {"above20": True, "rs5": 0.02} for c in "ABCD"}
    assert screener.group_strength(groups, snap, strict=True)["G"]["strong"]


def test_watch_tags_limited_to_active():
    S = series(heifeiwu_bars()[:-1])
    assert "黑飛舞" in screener.watch_tags(S)
    assert screener.watch_tags(S, patterns=("三角收斂",)) == {}


def test_record_journal_and_history_cache(tmp_path, monkeypatch):
    import csv
    import journal
    monkeypatch.setenv("JOURNAL_DIR", str(tmp_path))
    res = {"date": "2025-05-01",
           "candidates": [{"pattern": "三角收斂", "code": "1111", "name": "甲", "price": 50.0, "reason": "突破",
                           "groups": ["半導體業"]}],
           "observe": [{"pattern": "黑飛舞", "code": "2222", "name": "乙", "price": 30.0, "reason": "量縮",
                        "groups": []}]}
    screener.record_journal(res)
    screener.record_journal(res)                       # 重跑不重複
    rows = list(csv.DictReader((tmp_path / "signals_pattern_tri.csv").open(encoding="utf-8-sig")))
    assert len(rows) == 1 and rows[0]["slot"] == "進場" and rows[0]["code"] == "1111"
    rows = list(csv.DictReader((tmp_path / "signals_pattern_hfw.csv").open(encoding="utf-8-sig")))
    assert len(rows) == 1 and rows[0]["slot"] == "觀察"
    df = frame(flat(30, 50.0), code="1111")
    df = pd.concat([df, frame(flat(30, 20000), code="IX0001")])
    cache = screener.history_cache(df, days=10)
    assert len(cache["days"]) == 10 and journal.INDEX_CODE in cache["stocks"] and "1111" in cache["stocks"]
